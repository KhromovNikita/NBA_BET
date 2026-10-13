import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import BuildBaselinesRequest
from nba_live_totals.application.baselines.service import BaselineBuildService
from nba_live_totals.domain.baselines import DeterministicBaselineCalculator
from nba_live_totals.domain.identifiers import SeasonId
from nba_live_totals.infrastructure.files.baseline_export import AtomicJsonlBaselineExporter
from tests.unit.application.baseline_fakes import MemoryBaselineSource


async def test_service_rechecks_all_sources_before_single_publication(tmp_path: Path) -> None:
    source, writer = MemoryBaselineSource(), AtomicJsonlBaselineExporter()
    result = await BaselineBuildService(source, DeterministicBaselineCalculator(), writer).build(
        BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out")
    )
    assert source.events == ["select", "resolve", "read", "recheck", "recheck"]
    assert result.stats.row_count == 6 and result.exit_code == 1
    assert (tmp_path / "out").exists()


async def test_source_changes_during_final_recheck_preserve_old_output(tmp_path: Path) -> None:
    path = tmp_path / "out"
    path.write_bytes(b"old")
    source = MemoryBaselineSource()
    source.fail_final = True
    with pytest.raises(BaselineExportError, match="baseline_source_changed"):
        await BaselineBuildService(
            source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
        ).build(BuildBaselinesRequest((SeasonId("2025-26"),), path))
    assert path.read_bytes() == b"old"


async def test_empty_selection_does_not_touch_output(tmp_path: Path) -> None:
    source = MemoryBaselineSource()
    source.empty = True
    with pytest.raises(BaselineExportError, match="no_eligible_games"):
        await BaselineBuildService(
            source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
        ).build(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    assert not (tmp_path / "out").exists()


async def test_cleanup_failure_after_commit_preserves_success_result(tmp_path: Path) -> None:
    writer = AtomicJsonlBaselineExporter()
    original = writer.abort

    async def bad_cleanup() -> None:
        await original()
        raise OSError("sensitive text")

    with patch.object(writer, "abort", AsyncMock(side_effect=bad_cleanup)):
        result = await BaselineBuildService(
            MemoryBaselineSource(), DeterministicBaselineCalculator(), writer
        ).build(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    assert result.exit_code == 1 and (tmp_path / "out").exists()


async def test_duplicate_selection_rejects_before_export(tmp_path: Path) -> None:
    source = MemoryBaselineSource()
    gid = source.reference.game.game_id
    with patch.object(source, "select_games", AsyncMock(return_value=(gid, gid))):
        with pytest.raises(BaselineExportError, match="baseline_duplicate_games"):
            await BaselineBuildService(
                source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
            ).build(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    assert not (tmp_path / "out").exists()


async def test_cancelled_read_preserves_old_output_and_cleans_temp(tmp_path: Path) -> None:
    path = tmp_path / "out"
    path.write_bytes(b"old")
    source = MemoryBaselineSource()
    with patch.object(source, "read", AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await BaselineBuildService(
                source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
            ).build(BuildBaselinesRequest((SeasonId("2025-26"),), path))
    assert path.read_bytes() == b"old"
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".baseline-*.tmp")))
