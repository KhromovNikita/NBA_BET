"""Validated selectors, source identities and artifact descriptions for offline ML."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    CurrentFeatureSnapshotReference,
    FeatureBuildMode,
)
from nba_live_totals.application.modeling.worker_models import (
    CATBOOST_VERSION,
    MAX_JSON_BYTES,
    MAX_MODEL_BYTES,
    RuntimeVersions,
)
from nba_live_totals.domain.analytical_lineage import PbpLineage
from nba_live_totals.domain.baseline_hashing import baseline_config_sha256
from nba_live_totals.domain.baselines import BaselineConfig, bounded_integer
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.games import Game
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.domain.model_features import (
    PROJECTION_VERSION,
    FeatureProjectionDescriptor,
    model_feature_descriptor,
    require_sha256,
)
from nba_live_totals.domain.model_predictions import PREDICTION_POLICY_VERSION
from nba_live_totals.domain.model_targets import LABEL_VERSION, ModelHead
from nba_live_totals.domain.model_training import (
    SPLIT_VERSION,
    TRAINING_VERSION,
    TemporalPartition,
    TemporalSplitConfig,
    TrainingConfig,
    TrainingRow,
    classify_game,
    training_config_projection,
)
from nba_live_totals.domain.possessions import PossessionVersions

if TYPE_CHECKING:
    from nba_live_totals.application.modeling.ports import ModelPredictor

DATASET_SCHEMA_VERSION = "training-dataset-jsonl-v1"
BUNDLE_VERSION = "model-bundle-v1"


def resolved_path(value: Path) -> Path:
    if not isinstance(value, Path) or not value.name:
        raise ValueError("artifact path requires a named Path")
    return value.resolve()


@dataclass(frozen=True, slots=True)
class BuildTrainingDatasetRequest:
    seasons: tuple[SeasonId, ...]
    output_path: Path
    split: TemporalSplitConfig
    baseline_config: BaselineConfig

    def __post_init__(self) -> None:
        selector = BuildDatasetRequest(self.seasons, mode=FeatureBuildMode.VERIFY)
        object.__setattr__(self, "seasons", selector.seasons)
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        if not isinstance(self.split, TemporalSplitConfig) or not isinstance(
            self.baseline_config, BaselineConfig
        ):
            raise ValueError("invalid dataset split or baseline configuration")


@dataclass(frozen=True, slots=True)
class TrainModelsRequest:
    dataset_path: Path
    output_path: Path
    config: TrainingConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_path", resolved_path(self.dataset_path))
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        if self.dataset_path == self.output_path or not isinstance(self.config, TrainingConfig):
            raise ValueError("training output must differ from its input")


@dataclass(frozen=True, slots=True)
class PredictModelsRequest:
    seasons: tuple[SeasonId, ...]
    model_path: Path
    output_path: Path
    game_id: GameId | None = None

    def __post_init__(self) -> None:
        selector = BuildDatasetRequest(
            self.seasons, game_id=self.game_id, mode=FeatureBuildMode.VERIFY
        )
        object.__setattr__(self, "seasons", selector.seasons)
        object.__setattr__(self, "model_path", resolved_path(self.model_path))
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        if self.model_path == self.output_path:
            raise ValueError("prediction output must differ from the model input")


@dataclass(frozen=True, slots=True)
class ModelSourceReference:
    game: Game
    reference: CurrentFeatureSnapshotReference
    pbp_lineage: PbpLineage
    possession_versions: PossessionVersions
    lineup_versions: LineupVersions

    def __post_init__(self) -> None:
        if (
            not isinstance(self.game, Game)
            or not isinstance(self.reference, CurrentFeatureSnapshotReference)
            or self.game != self.reference.attempt.game
        ):
            raise ValueError("model source game or feature reference mismatch")
        if (
            not isinstance(self.pbp_lineage, PbpLineage)
            or self.pbp_lineage.game_id != self.game.game_id
            or self.pbp_lineage.game_context_sha256
            != self.reference.attempt.input_token.game_context_sha256
        ):
            raise ValueError("model source PBP lineage mismatch")
        if not isinstance(self.possession_versions, PossessionVersions) or not isinstance(
            self.lineup_versions, LineupVersions
        ):
            raise ValueError("model source requires parser versions")


@dataclass(frozen=True, slots=True)
class HeadCounts:
    head: ModelHead
    eligible_rows: int
    eligible_games: int
    has_target_variation: bool
    excluded_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.head, ModelHead) or type(self.has_target_variation) is not bool:
            raise ValueError("invalid head counter identity")
        bounded_integer(self.eligible_rows)
        bounded_integer(self.eligible_games, maximum=self.eligible_rows)
        if bool(self.eligible_rows) != bool(self.eligible_games) or (
            self.has_target_variation and self.eligible_rows < 2
        ):
            raise ValueError("inconsistent head coverage or variation")
        if (
            type(self.excluded_counts) is not tuple
            or tuple(sorted(self.excluded_counts)) != self.excluded_counts
            or len({r for r, _ in self.excluded_counts}) != len(self.excluded_counts)
        ):
            raise ValueError("exclusions must be sorted unique counters")
        for reason, count in self.excluded_counts:
            if reason not in {
                "temporal_embargo",
                "invalid_feature_state",
                "no_future_time",
                "no_future_possessions",
            }:
                raise ValueError("unsupported exclusion reason")
            bounded_integer(count, minimum=1)


@dataclass(frozen=True, slots=True)
class PartitionCounts:
    partition: TemporalPartition
    games: int
    states: int
    heads: tuple[HeadCounts, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.partition, TemporalPartition):
            raise ValueError("invalid partition counter identity")
        bounded_integer(self.games)
        bounded_integer(self.states, minimum=self.games)
        if bool(self.games) != bool(self.states):
            raise ValueError("partition game/state counts mismatch")
        if (
            type(self.heads) is not tuple
            or any(not isinstance(h, HeadCounts) for h in self.heads)
            or tuple(h.head for h in self.heads) != tuple(ModelHead)
        ):
            raise ValueError("partition requires three ordered head counters")
        for head in self.heads:
            if (
                head.eligible_games > self.games
                or head.eligible_rows + sum(n for _, n in head.excluded_counts) != self.states
            ):
                raise ValueError("partition head coverage mismatch")
            if self.partition is TemporalPartition.EMBARGO and head.eligible_rows:
                raise ValueError("embargo cannot have eligible rows")


@dataclass(frozen=True, slots=True)
class TrainingDatasetSummary:
    seasons: tuple[SeasonId, ...]
    descriptor: FeatureProjectionDescriptor
    baseline_config: BaselineConfig
    split: TemporalSplitConfig
    feature_versions: FeatureVersions
    possession_versions: PossessionVersions
    lineup_versions: LineupVersions
    sources: tuple[ModelSourceReference, ...]
    counts: tuple[PartitionCounts, ...]
    source_sha256: str
    body_sha256: str
    semantic_body_sha256: str
    dataset_semantic_revision: str
    dataset_artifact_sha256: str
    schema_version: str = DATASET_SCHEMA_VERSION
    label_version: str = LABEL_VERSION
    split_version: str = SPLIT_VERSION

    def __post_init__(self) -> None:
        BuildDatasetRequest(self.seasons, mode=FeatureBuildMode.VERIFY)
        if not isinstance(
            self.baseline_config, BaselineConfig
        ) or self.descriptor != model_feature_descriptor(self.baseline_config):
            raise ValueError("dataset projection/config mismatch")
        if (
            not isinstance(self.split, TemporalSplitConfig)
            or not isinstance(self.feature_versions, FeatureVersions)
            or not isinstance(self.possession_versions, PossessionVersions)
            or not isinstance(self.lineup_versions, LineupVersions)
        ):
            raise ValueError("invalid dataset versions or split")
        if (self.schema_version, self.label_version, self.split_version) != (
            DATASET_SCHEMA_VERSION,
            LABEL_VERSION,
            SPLIT_VERSION,
        ):
            raise ValueError("unsupported dataset schema")
        if (
            type(self.sources) is not tuple
            or not self.sources
            or any(not isinstance(s, ModelSourceReference) for s in self.sources)
        ):
            raise ValueError("dataset requires immutable typed sources")
        keys = tuple((s.game.started_at, s.game.game_id.value) for s in self.sources)
        if keys != tuple(sorted(keys)) or len({s.game.game_id for s in self.sources}) != len(
            self.sources
        ):
            raise ValueError("dataset sources must be ordered and unique")
        if any(
            s.game.season not in self.seasons
            or s.reference.attempt.input_token.versions != self.feature_versions
            or s.possession_versions != self.possession_versions
            or s.lineup_versions != self.lineup_versions
            for s in self.sources
        ):
            raise ValueError("dataset source selectors or versions mismatch")
        if (
            type(self.counts) is not tuple
            or any(not isinstance(c, PartitionCounts) for c in self.counts)
            or tuple(c.partition for c in self.counts) != tuple(TemporalPartition)
        ):
            raise ValueError("dataset requires ordered partition counters")
        for count in self.counts:
            sources = tuple(
                s
                for s in self.sources
                if classify_game(s.game.started_at, self.split) is count.partition
            )
            if count.games != len(sources) or count.states != sum(
                s.reference.state_row_count for s in sources
            ):
                raise ValueError("dataset partition counts do not match sources")
        for value in (
            self.source_sha256,
            self.body_sha256,
            self.semantic_body_sha256,
            self.dataset_semantic_revision,
            self.dataset_artifact_sha256,
        ):
            require_sha256(value)

    @property
    def games(self) -> int:
        return len(self.sources)

    @property
    def states(self) -> int:
        return sum(c.states for c in self.counts)

    @property
    def trainable(self) -> bool:
        train, validation, test, _ = self.counts
        return all(
            train.heads[i].eligible_games >= 2
            and train.heads[i].has_target_variation
            and validation.heads[i].eligible_games >= 1
            and test.heads[i].eligible_games >= 1
            for i in range(3)
        )


@dataclass(frozen=True, slots=True)
class ValidatedTrainingDataset:
    path: Path
    summary: TrainingDatasetSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", resolved_path(self.path))
        if not isinstance(self.summary, TrainingDatasetSummary):
            raise ValueError("invalid validated dataset summary")


@dataclass(frozen=True, slots=True)
class HeadRows:
    partition: TemporalPartition
    head: ModelHead
    row_count: int
    game_counts: Mapping[GameId, int]
    iter_rows: Callable[[], Iterator[TrainingRow]]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.partition, TemporalPartition)
            or self.partition is TemporalPartition.EMBARGO
            or not isinstance(self.head, ModelHead)
        ):
            raise ValueError("invalid head row selector")
        bounded_integer(self.row_count)
        counts = dict(self.game_counts)
        if any(not isinstance(g, GameId) for g in counts):
            raise ValueError("invalid head row game IDs")
        for count in counts.values():
            bounded_integer(count, minimum=1)
        if sum(counts.values()) != self.row_count or not callable(self.iter_rows):
            raise ValueError("head row counts or iterator mismatch")
        object.__setattr__(self, "game_counts", MappingProxyType(counts))


@dataclass(frozen=True, slots=True)
class ModelHeadMetadata:
    head: ModelHead
    sha256: str
    size: int
    best_iteration: int
    tree_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.head, ModelHead):
            raise ValueError("invalid model head metadata")
        require_sha256(self.sha256)
        bounded_integer(self.size, minimum=1, maximum=MAX_MODEL_BYTES)
        bounded_integer(self.tree_count, minimum=1, maximum=100_000)
        bounded_integer(self.best_iteration, maximum=self.tree_count - 1)


def model_semantic_revision(
    dataset: TrainingDatasetSummary, config: TrainingConfig, engine_version: str
) -> str:
    if engine_version != CATBOOST_VERSION:
        raise ValueError("unsupported model recipe engine")
    return canonical_json_sha256(
        {
            "dataset_semantic_revision": dataset.dataset_semantic_revision,
            "training_config": training_config_projection(config),
            "label_version": LABEL_VERSION,
            "projection_version": PROJECTION_VERSION,
            "split_version": SPLIT_VERSION,
            "training_version": TRAINING_VERSION,
            "bundle_version": BUNDLE_VERSION,
            "prediction_policy_version": PREDICTION_POLICY_VERSION,
            "catboost_version": engine_version,
        }
    )


@dataclass(frozen=True, slots=True)
class ModelBundleManifest:
    dataset: TrainingDatasetSummary
    descriptor: FeatureProjectionDescriptor
    baseline_config: BaselineConfig
    training_config: TrainingConfig
    feature_versions: FeatureVersions
    runtime: RuntimeVersions
    heads: tuple[ModelHeadMetadata, ...]
    resolved_parameters: Mapping[ModelHead, CanonicalObject]
    metrics_sha256: str
    metrics_size: int
    model_revision_sha256: str
    label_version: str = LABEL_VERSION
    projection_version: str = PROJECTION_VERSION
    split_version: str = SPLIT_VERSION
    training_version: str = TRAINING_VERSION
    bundle_version: str = BUNDLE_VERSION
    prediction_policy_version: str = PREDICTION_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, TrainingDatasetSummary) or not self.dataset.trainable:
            raise ValueError("bundle requires a trainable dataset")
        if (
            self.descriptor != self.dataset.descriptor
            or self.baseline_config != self.dataset.baseline_config
            or self.feature_versions != self.dataset.feature_versions
        ):
            raise ValueError("bundle projection or source versions mismatch")
        if not isinstance(self.training_config, TrainingConfig) or not isinstance(
            self.runtime, RuntimeVersions
        ):
            raise ValueError("invalid model configuration or runtime")
        if (
            self.label_version,
            self.projection_version,
            self.split_version,
            self.training_version,
            self.bundle_version,
            self.prediction_policy_version,
        ) != (
            LABEL_VERSION,
            PROJECTION_VERSION,
            SPLIT_VERSION,
            TRAINING_VERSION,
            BUNDLE_VERSION,
            PREDICTION_POLICY_VERSION,
        ):
            raise ValueError("unsupported model bundle version")
        if (
            type(self.heads) is not tuple
            or any(not isinstance(h, ModelHeadMetadata) for h in self.heads)
            or tuple(h.head for h in self.heads) != tuple(ModelHead)
        ):
            raise ValueError("bundle requires all three ordered heads")
        if any(h.tree_count > self.training_config.iterations for h in self.heads):
            raise ValueError("tree count exceeds requested iterations")
        parameters = deepcopy(dict(self.resolved_parameters))
        if set(parameters) != set(ModelHead) or any(
            not isinstance(h, ModelHead) for h in parameters
        ):
            raise ValueError("bundle requires parameters for all heads")
        for value in parameters.values():
            if type(value) is not dict:
                raise ValueError("resolved parameters require canonical objects")
            canonical_json_bytes(value)
        object.__setattr__(self, "resolved_parameters", MappingProxyType(parameters))
        require_sha256(self.metrics_sha256)
        bounded_integer(self.metrics_size, minimum=1, maximum=MAX_JSON_BYTES)
        require_sha256(self.model_revision_sha256)
        if self.model_revision_sha256 != model_semantic_revision(
            self.dataset, self.training_config, self.runtime.catboost
        ):
            raise ValueError("model semantic revision mismatch")
        baseline_config_sha256(self.baseline_config)


@dataclass(frozen=True, slots=True)
class LoadedModelBundle:
    manifest: ModelBundleManifest
    artifact_sha256: str
    predictor: ModelPredictor

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ModelBundleManifest) or not callable(
            getattr(self.predictor, "predict_state", None)
        ):
            raise ValueError("invalid loaded model bundle")
        require_sha256(self.artifact_sha256)


@dataclass(frozen=True, slots=True)
class BuildTrainingDatasetResult:
    output_path: Path
    summary: TrainingDatasetSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        if not isinstance(self.summary, TrainingDatasetSummary):
            raise ValueError("invalid dataset result summary")

    @property
    def exit_code(self) -> int:
        return int(not self.summary.trainable)


@dataclass(frozen=True, slots=True)
class TrainModelsResult:
    output_path: Path
    manifest: ModelBundleManifest
    artifact_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        if not isinstance(self.manifest, ModelBundleManifest):
            raise ValueError("invalid training result manifest")
        require_sha256(self.artifact_sha256)

    @property
    def exit_code(self) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class PredictModelsResult:
    output_path: Path
    games: int
    states: int
    available: int
    unavailable: int
    artifact_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", resolved_path(self.output_path))
        bounded_integer(self.games, minimum=1)
        bounded_integer(self.states, minimum=self.games)
        bounded_integer(self.available)
        bounded_integer(self.unavailable)
        if self.available + self.unavailable != self.states:
            raise ValueError("prediction outcome counts mismatch")
        require_sha256(self.artifact_sha256)

    @property
    def exit_code(self) -> int:
        return int(bool(self.unavailable))
