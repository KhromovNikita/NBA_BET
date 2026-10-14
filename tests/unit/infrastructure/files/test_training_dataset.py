import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition, build_training_rows
from nba_live_totals.infrastructure.files.training_dataset import (
    AtomicTrainingDatasetExporter,
    CanonicalTrainingDatasetReader,
)
from tests.unit.application.modeling_fakes import (
    MemoryModelSource,
    make_dataset_request,
    remap_training_input,
)
from tests.unit.domain.modeling_helpers import make_training_input


async def export(path: Path, source: MemoryModelSource | None = None) -> None:
    await BuildTrainingDatasetService(
        source or MemoryModelSource(), AtomicTrainingDatasetExporter()
    ).build(make_dataset_request(path))


async def test_equal_bytes_at_different_paths_and_strict_reader_roundtrip(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    await export(first)
    await export(second)
    assert first.read_bytes() == second.read_bytes()
    reader = CanonicalTrainingDatasetReader()
    dataset = reader.validate(first)
    head = reader.head_rows(dataset, TemporalPartition.TRAIN, ModelHead.PACE)
    assert head.row_count == 7
    assert len(tuple(head.iter_rows())) == 7
    assert len(tuple(head.iter_rows())) == 7
    assert dataset.summary.states == 8


async def test_snapshot_uuid_changes_artifact_but_not_semantic_revision(tmp_path: Path) -> None:
    source = MemoryModelSource()
    first, second = tmp_path / "first", tmp_path / "second"
    await export(first, source)
    source.replace_snapshot_uuid(next(iter(source.references)))
    await export(second, source)
    reader = CanonicalTrainingDatasetReader()
    a, b = reader.validate(first).summary, reader.validate(second).summary
    assert a.dataset_artifact_sha256 != b.dataset_artifact_sha256
    assert a.dataset_semantic_revision == b.dataset_semantic_revision


@pytest.mark.parametrize("mutation", ["crlf", "count", "mask", "descriptor", "extra"])
async def test_reader_rejects_noncanonical_or_tampered_metadata(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "training"
    await export(path)
    content = path.read_bytes()
    if mutation == "crlf":
        content = content.replace(b"\n", b"\r\n")
    else:
        rows = [json.loads(line) for line in content.splitlines()]
        if mutation == "count":
            rows[0]["counts"][0]["states"] += 1
        elif mutation == "mask":
            rows[1]["eligibility"]["pace"]["eligible"] = False
        elif mutation == "descriptor":
            rows[0]["descriptor"]["columns"][0]["divisor"] = 1_000_000
        else:
            rows[1]["unexpected"] = 1
        content = (
            b"\n".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode() for row in rows
            )
            + b"\n"
        )
    path.write_bytes(content)
    with pytest.raises(ModelingError, match="training_dataset_invalid"):
        CanonicalTrainingDatasetReader().validate(path)


async def test_one_shot_rows_and_replace_failure_preserve_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = MemoryModelSource()
    path = tmp_path / "training"
    path.write_bytes(b"old")
    request = make_dataset_request(path)
    exporter = AtomicTrainingDatasetExporter()
    ref = next(iter(source.references.values()))
    inputs = next(iter(source.inputs.values()))
    await exporter.begin(request)
    await exporter.append_game(
        ref, build_training_rows(inputs, request.split, request.baseline_config)
    )
    await exporter.prepare((ref,))

    def deny(*args: object) -> None:
        raise PermissionError("synthetic sharing lock")

    monkeypatch.setattr("nba_live_totals.infrastructure.files.training_dataset.os.replace", deny)
    with pytest.raises(ModelingError, match="training_dataset_io_error"):
        exporter.publish()
    await exporter.abort()
    assert path.read_bytes() == b"old"
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == [path]


async def test_cancellation_joins_worker_before_removing_temporaries(tmp_path: Path) -> None:
    started, released = Event(), Event()

    class PausedExporter(AtomicTrainingDatasetExporter):
        def _prepare(self) -> None:
            started.set()
            released.wait(5)
            super()._prepare()

    exporter = PausedExporter()
    source = MemoryModelSource()
    request = make_dataset_request(tmp_path / "out")
    ref = next(iter(source.references.values()))
    await exporter.begin(request)
    await exporter.append_game(
        ref,
        build_training_rows(
            next(iter(source.inputs.values())), request.split, request.baseline_config
        ),
    )
    task = asyncio.create_task(exporter.prepare((ref,)))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await exporter.abort()
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


def golden_source() -> MemoryModelSource:
    first = make_training_input()
    second = remap_training_input(
        first, GameId("0022500002"), datetime(2025, 10, 22, 23, tzinfo=UTC)
    )
    state = second.features.states[2]
    invalid = replace(
        state,
        home_player_ids=(),
        home_lineup_status=LineupStatus.INVALID,
        home_invalid_reason="missing_substitution",
        home_invalid_since=state.boundary,
    )
    second = replace(
        second,
        features=FeatureDataset(
            second.game.game_id,
            (*second.features.states[:2], invalid, *second.features.states[3:]),
            (),
        ),
    )
    embargo = remap_training_input(
        first, GameId("0022500003"), datetime(2025, 10, 31, 23, tzinfo=UTC)
    )
    return MemoryModelSource((first, second, embargo))


async def test_literal_golden_retains_invalid_terminal_and_embargo_records(tmp_path: Path) -> None:
    path = tmp_path / "out"
    await export(path, golden_source())
    golden = Path(__file__).parents[3] / "fixtures/nba/modeling/training.expected.jsonl"
    assert path.read_bytes() == golden.read_bytes()
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(rows) == 25
    assert rows[3]["targets"]["home_ppp"] == {"value_scaled": 1_666_667, "unavailable_reason": None}
    assert rows[11]["targets"]["pace"]["unavailable_reason"] == "invalid_feature_state"
    assert rows[8]["targets"]["pace"]["unavailable_reason"] == "no_future_time"
    assert rows[19]["targets"]["home_ppp"]["unavailable_reason"] is None
    assert rows[19]["eligibility"]["home_ppp"] == {"eligible": False, "reason": "temporal_embargo"}


async def test_real_partition_minimums_make_dataset_trainable(tmp_path: Path) -> None:
    first = make_training_input()
    inputs = (
        first,
        *(
            remap_training_input(
                first, GameId(f"002250000{index}"), datetime(2025, month, day, tzinfo=UTC)
            )
            for index, month, day in ((2, 10, 22), (3, 11, 5), (4, 12, 5))
        ),
    )
    path = tmp_path / "out"
    await export(path, MemoryModelSource(inputs))
    checked = CanonicalTrainingDatasetReader().validate(path)
    assert checked.summary.trainable
    assert checked.summary.games == 4
