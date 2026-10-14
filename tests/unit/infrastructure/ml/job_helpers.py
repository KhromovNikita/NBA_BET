"""Real canonical datasets used by CPU-job and bundle tests without database stores."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.models import ValidatedTrainingDataset
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId
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


async def write_tiny_dataset(path: Path, *, test_points_delta: int = 0) -> ValidatedTrainingDataset:
    first = make_training_input()
    second = remap_training_input(first, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC))
    validation = remap_training_input(
        first, GameId("0022500003"), datetime(2025, 11, 5, tzinfo=UTC)
    )
    test = remap_training_input(first, GameId("0022500004"), datetime(2025, 12, 5, tzinfo=UTC))
    if test_points_delta:
        last = test.possessions[-1]
        possession = replace(
            last,
            offense_points=last.offense_points + test_points_delta,
            home_score=last.home_score + test_points_delta,
            score_diff=last.score_diff + test_points_delta,
        )
        state = test.features.states[-1]
        changed_state = replace(
            state,
            home_score=state.home_score + test_points_delta,
            score_diff=state.score_diff + test_points_delta,
        )
        assert test.game.home_score is not None
        test = replace(
            test,
            game=replace(test.game, home_score=test.game.home_score + test_points_delta),
            features=FeatureDataset(
                test.game.game_id, (*test.features.states[:-1], changed_state), ()
            ),
            possessions=(*test.possessions[:-1], possession),
            period_ends=(
                replace(
                    test.period_ends[0],
                    home_score=test.period_ends[0].home_score + test_points_delta,
                ),
            ),
        )
    await BuildTrainingDatasetService(
        MemoryModelSource((first, second, validation, test)), AtomicTrainingDatasetExporter()
    ).build(make_dataset_request(path))
    return CanonicalTrainingDatasetReader().validate(path)
