from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import update

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.application.baselines.service import BaselineBuildService
from nba_live_totals.domain.baselines import DeterministicBaselineCalculator
from nba_live_totals.infrastructure.files.baseline_export import AtomicJsonlBaselineExporter
from nba_live_totals.infrastructure.postgres.schema import team_box_scores
from tests.integration.baseline_harness import PRIOR, SEASON, TARGET, baseline_acceptance

pytestmark = pytest.mark.integration


async def test_baseline_source_reads_real_snapshot_without_writes() -> None:
    async with baseline_acceptance() as harness:
        sources = await harness.seed_pipeline()
        before = await harness.projection()
        for source in sources:
            dataset = await harness.source.read(source)
            assert len(dataset.states) == source.reference.state_row_count
            await harness.source.recheck(source)
        assert await harness.projection() == before
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(team_box_scores)
                .where(team_box_scores.c.game_id == PRIOR.value)
                .values(points=99)
            )
        with pytest.raises(BaselineExportError):
            await harness.source.resolve(TARGET)


async def test_real_dependency_race_before_publication_preserves_old_file(tmp_path: Path) -> None:
    async with baseline_acceptance() as harness:
        await harness.seed_pipeline()
        path = tmp_path / "old.jsonl"
        path.write_bytes(b"old artifact")
        original = harness.source.recheck
        calls = 0

        async def raced(source: BaselineSourceReference) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                async with harness.engine.begin() as connection:
                    await connection.execute(
                        update(team_box_scores)
                        .where(team_box_scores.c.game_id == PRIOR.value)
                        .values(points=99)
                    )
            await original(source)

        with patch.object(harness.source, "recheck", raced):
            with pytest.raises(BaselineExportError):
                await BaselineBuildService(
                    harness.source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
                ).build(BuildBaselinesRequest((SEASON,), path, game_id=TARGET))
        assert calls == 2 and path.read_bytes() == b"old artifact"
