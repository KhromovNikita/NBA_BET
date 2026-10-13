import asyncio
import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.domain.baselines import BaselineConfig, DeterministicBaselineCalculator
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import SeasonId
from nba_live_totals.infrastructure.files.baseline_export import AtomicJsonlBaselineExporter
from tests.unit.application.test_feature_models import make_reference
from tests.unit.domain.baseline_helpers import make_state


async def prepare(path: Path) -> AtomicJsonlBaselineExporter:
    ref = make_reference()
    source = BaselineSourceReference(ref.attempt.game, ref)
    request = BuildBaselinesRequest((SeasonId("2025-26"),), path)
    dataset = FeatureDataset(
        source.game.game_id,
        tuple(make_state(state_number=n, game_id=source.game.game_id) for n in (0, 1)),
        (),
    )
    writer = AtomicJsonlBaselineExporter()
    await writer.begin(request)
    await writer.append_game(
        source, DeterministicBaselineCalculator().calculate_dataset(dataset, BaselineConfig())
    )
    await writer.prepare((source,))
    return writer


async def test_streamed_export_is_canonical_and_reproducible(tmp_path: Path) -> None:
    first = await prepare(tmp_path / "one.jsonl")
    second = await prepare(tmp_path / "two.jsonl")
    first.publish()
    second.publish()
    await first.abort()
    await second.abort()
    data = (tmp_path / "one.jsonl").read_bytes()
    assert data == (tmp_path / "two.jsonl").read_bytes()
    manifest, *rows = data.splitlines(keepends=True)
    assert json.loads(manifest)["outcome_row_count"] == len(rows) == 6
    assert json.loads(manifest)["outcome_sha256"] == hashlib.sha256(b"".join(rows)).hexdigest()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".baseline-*.tmp")))
    assert b"\r" not in data


async def test_replace_failure_preserves_old_output(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_bytes(b"old")
    writer = await prepare(path)
    with patch(
        "nba_live_totals.infrastructure.files.baseline_export.os.replace",
        side_effect=PermissionError,
    ):
        with pytest.raises(BaselineExportError):
            writer.publish()
    await writer.abort()
    assert path.read_bytes() == b"old"
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".baseline-*.tmp")))


async def test_cancel_joins_worker_before_cleanup(tmp_path: Path) -> None:
    writer = AtomicJsonlBaselineExporter()
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = writer._begin

    def delayed() -> None:
        started.set()
        release.wait(5)
        original()
        stopped.set()

    with patch.object(writer, "_begin", delayed):
        task = asyncio.create_task(
            writer.begin(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
        )
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await writer.abort()
    assert stopped.is_set()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".baseline-*.tmp")))


async def test_late_abort_cannot_remove_published_destination(tmp_path: Path) -> None:
    path = tmp_path / "out"
    writer = await prepare(path)
    writer.publish()
    await writer.abort()
    await writer.abort()
    assert path.is_file()


async def test_body_corruption_is_detected_before_publication(tmp_path: Path) -> None:
    original = AtomicJsonlBaselineExporter._prepare

    def corrupt(writer: AtomicJsonlBaselineExporter) -> None:
        assert writer._body is not None
        writer._body.write_bytes(b"{}\n")
        original(writer)

    ref = make_reference()
    source = BaselineSourceReference(ref.attempt.game, ref)
    writer = AtomicJsonlBaselineExporter()
    await writer.begin(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    dataset = FeatureDataset(
        source.game.game_id,
        tuple(make_state(state_number=n, game_id=source.game.game_id) for n in (0, 1)),
        (),
    )
    await writer.append_game(
        source, DeterministicBaselineCalculator().calculate_dataset(dataset, BaselineConfig())
    )
    with patch.object(AtomicJsonlBaselineExporter, "_prepare", corrupt):
        with pytest.raises(BaselineExportError):
            await writer.prepare((source,))
    await writer.abort()
    assert not (tmp_path / "out").exists()


async def test_missing_parent_and_fsync_failure_cleanup(tmp_path: Path) -> None:
    writer = AtomicJsonlBaselineExporter()
    with pytest.raises(BaselineExportError):
        await writer.begin(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "missing/out"))
    await writer.abort()
    writer = AtomicJsonlBaselineExporter()
    await writer.begin(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    ref = make_reference()
    source = BaselineSourceReference(ref.attempt.game, ref)
    dataset = FeatureDataset(
        source.game.game_id,
        tuple(make_state(state_number=n, game_id=source.game.game_id) for n in (0, 1)),
        (),
    )
    with patch(
        "nba_live_totals.infrastructure.files.baseline_export.os.fsync", side_effect=OSError
    ):
        with pytest.raises(BaselineExportError):
            await writer.append_game(
                source,
                DeterministicBaselineCalculator().calculate_dataset(dataset, BaselineConfig()),
            )
    await writer.abort()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".baseline-*.tmp")))
