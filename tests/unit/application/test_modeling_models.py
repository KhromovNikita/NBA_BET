from dataclasses import FrozenInstanceError, replace
from datetime import date
from pathlib import Path

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    BuildTrainingDatasetResult,
    HeadCounts,
    HeadRows,
    PartitionCounts,
    PredictModelsRequest,
    PredictModelsResult,
    TrainingDatasetSummary,
    TrainModelsRequest,
)
from nba_live_totals.application.modeling.worker_models import (
    HeadModelFile,
    RuntimeVersions,
    ValidationProbe,
)
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_features import model_feature_descriptor
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import (
    TemporalPartition,
    TemporalSplitConfig,
    TrainingConfig,
)
from tests.unit.application.modeling_fakes import make_dataset_request, make_model_source_reference
from tests.unit.domain.modeling_helpers import make_training_input


def test_trainer_never_overwrites_input_or_resolved_alias(tmp_path: Path) -> None:
    for output in (tmp_path / "same", tmp_path / "sub" / ".." / "same"):
        with pytest.raises(ValueError):
            TrainModelsRequest(tmp_path / "same", output, TrainingConfig())
    with pytest.raises(ValueError):
        PredictModelsRequest((SeasonId("2025-26"),), tmp_path / "same", tmp_path / "same")
    request = TrainModelsRequest(tmp_path / "input", tmp_path / "out", TrainingConfig())
    assert request.dataset_path.is_absolute()
    with pytest.raises(FrozenInstanceError):
        request.output_path = tmp_path / "new"  # type: ignore[misc]


def test_selectors_reuse_verify_rules(tmp_path: Path) -> None:
    split = TemporalSplitConfig(date(2025, 7, 1), date(2026, 7, 1))
    for seasons in ((), (SeasonId("2025-26"),) * 2, (SeasonId("2026-27"), SeasonId("2025-26"))):
        with pytest.raises(ValueError):
            BuildTrainingDatasetRequest(seasons, tmp_path / "out", split, BaselineConfig())
    with pytest.raises(ValueError):
        PredictModelsRequest(
            (SeasonId("2024-25"), SeasonId("2025-26")),
            tmp_path / "model",
            tmp_path / "out",
            GameId("0022500001"),
        )
    assert make_dataset_request(tmp_path / "out").output_path.is_absolute()


def test_source_matches_game_and_feature_lineage() -> None:
    inputs = make_training_input()
    source = make_model_source_reference(inputs.game, inputs.features)
    assert source.reference.state_row_count == 8
    with pytest.raises(ValueError):
        replace(source, game=replace(source.game, home_score=8))
    with pytest.raises(ValueError):
        replace(source, pbp_lineage=replace(source.pbp_lineage, canonical_event_sha256="bad"))


def test_head_rows_freeze_checked_per_game_counts() -> None:
    game_id = GameId("0022500001")
    counts = {game_id: 2}
    rows = HeadRows(TemporalPartition.TRAIN, ModelHead.PACE, 2, counts, lambda: iter(()))
    counts[game_id] = 3
    assert rows.game_counts[game_id] == 2
    with pytest.raises(ValueError):
        replace(rows, row_count=3)
    with pytest.raises(ValueError):
        replace(rows, partition=TemporalPartition.EMBARGO)


def test_worker_metadata_checks_hashes_limits_and_head_identity(tmp_path: Path) -> None:
    file = HeadModelFile(ModelHead.PACE, tmp_path / "pace.cbm", "a" * 64, 100, 0, 1)
    with pytest.raises(ValueError):
        replace(file, best_iteration=1)
    with pytest.raises(ValueError):
        replace(file, sha256="bad")
    with pytest.raises(ValueError):
        replace(file, size=128 * 1024**2 + 1)
    with pytest.raises(ValueError):
        ValidationProbe(ModelHead.PACE, (), ())
    assert RuntimeVersions("3.14.8", "1.2.10", "2.4.0", "Windows", "AMD64").catboost == "1.2.10"
    with pytest.raises(ValueError):
        RuntimeVersions("3.13.0", "1.2.10", "2.4.0", "Windows", "AMD64")


def test_prediction_result_counts_and_exit_codes(tmp_path: Path) -> None:
    result = PredictModelsResult(tmp_path / "out", 1, 2, 1, 1, "a" * 64)
    assert result.exit_code == 1
    assert replace(result, available=2, unavailable=0).exit_code == 0
    with pytest.raises(ValueError):
        replace(result, states=3)
    assert str(ModelingError("model_source_changed")) == "model_source_changed"
    with pytest.raises(ValueError):
        ModelingError("password: secret")


def test_dataset_summary_and_partition_counts_have_exact_exit_contract(tmp_path: Path) -> None:
    inputs = make_training_input()
    source = make_model_source_reference(inputs.game, inputs.features)
    counts = tuple(
        PartitionCounts(
            partition,
            1 if partition is TemporalPartition.TRAIN else 0,
            8 if partition is TemporalPartition.TRAIN else 0,
            tuple(
                HeadCounts(head, 7, 1, True, (("no_future_time", 1),))
                if partition is TemporalPartition.TRAIN
                else HeadCounts(head, 0, 0, False, ())
                for head in ModelHead
            ),
        )
        for partition in TemporalPartition
    )
    request = make_dataset_request(tmp_path / "out")
    summary = TrainingDatasetSummary(
        request.seasons,
        model_feature_descriptor(request.baseline_config),
        request.baseline_config,
        request.split,
        FeatureVersions(),
        source.possession_versions,
        source.lineup_versions,
        (source,),
        counts,
        *("a" * 64 for _ in range(5)),
    )
    assert summary.games == 1 and summary.states == 8 and not summary.trainable
    assert BuildTrainingDatasetResult(tmp_path / "out", summary).exit_code == 1
    with pytest.raises(ValueError):
        replace(summary, body_sha256="bad")
    with pytest.raises(ValueError):
        replace(counts[0], states=9)
    with pytest.raises(ValueError):
        replace(
            summary, descriptor=model_feature_descriptor(BaselineConfig(ppp_prior_possessions=40))
        )
