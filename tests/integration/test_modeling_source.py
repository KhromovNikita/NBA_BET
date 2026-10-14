from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import update

from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.domain.features import scaled_ratio
from nba_live_totals.domain.model_targets import calculate_remaining_targets
from nba_live_totals.infrastructure.files.training_dataset import AtomicTrainingDatasetExporter
from nba_live_totals.infrastructure.postgres.schema import team_box_scores
from tests.integration.modeling_harness import GAMES, modeling_acceptance

pytestmark = pytest.mark.integration


async def test_modeling_source_verifies_full_regulation_and_two_known_overtimes() -> None:
    async with modeling_acceptance() as harness:
        sources = await harness.seed_pipeline()
        before = await harness.storage_projection()
        for source in sources:
            game = await harness.source.read_training(source)
            assert game.period_ends[-1].home_score == game.game.home_score
            assert game.period_ends[-1].away_score == game.game.away_score
            await harness.source.recheck(source)
        overtime = await harness.source.read_training(sources[1])
        assert tuple(e.period for e in overtime.period_ends) == (4, 5, 6)
        first_ot = next(s for s in overtime.features.states if s.period == 5)
        end = overtime.period_ends[1]
        label = calculate_remaining_targets(first_ot, end)
        count = (
            end.home_possessions
            + end.away_possessions
            - first_ot.home_possessions
            - first_ot.away_possessions
        )
        assert label.pace.value_scaled == scaled_ratio(
            2_880_000 * count, 2 * first_ot.seconds_remaining_game_ms
        )
        with pytest.raises(ValueError, match="horizon mismatch"):
            calculate_remaining_targets(first_ot, overtime.period_ends[2])
        assert await harness.storage_projection() == before


async def test_modeling_source_race_preserves_previous_dataset(tmp_path: Path) -> None:
    async with modeling_acceptance() as harness:
        await harness.seed_pipeline()
        path = tmp_path / "old.jsonl"
        path.write_bytes(b"old artifact")
        original = harness.source.recheck
        calls = 0

        async def raced(source: ModelSourceReference) -> None:
            nonlocal calls
            calls += 1
            if calls == 6:
                async with harness.engine.begin() as connection:
                    await connection.execute(
                        update(team_box_scores)
                        .where(team_box_scores.c.game_id == GAMES[0].value)
                        .values(points=99)
                    )
            await original(source)

        with patch.object(harness.source, "recheck", raced):
            with pytest.raises(ModelingError):
                await BuildTrainingDatasetService(
                    harness.source, AtomicTrainingDatasetExporter()
                ).build(harness.dataset_request(path))
        assert calls == 7 and path.read_bytes() == b"old artifact"
