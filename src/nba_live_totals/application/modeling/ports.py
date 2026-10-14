"""Ports keep native learning and persistence outside application orchestration."""

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    HeadRows,
    LoadedModelBundle,
    ModelBundleManifest,
    ModelSourceReference,
    PredictModelsRequest,
    PredictModelsResult,
    TrainingDatasetSummary,
    TrainModelsRequest,
    ValidatedTrainingDataset,
)
from nba_live_totals.application.modeling.worker_models import (
    TrainingWorkerJob,
    TrainingWorkerResult,
)
from nba_live_totals.application.possessions.models import GamePbpSnapshot
from nba_live_totals.domain.canonical import CanonicalObject
from nba_live_totals.domain.features import FeatureDataset, GameStateFeature
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_features import FeatureProjectionDescriptor, ModelFeatureVector
from nba_live_totals.domain.model_predictions import StateModelPrediction
from nba_live_totals.domain.model_targets import ModelHead, TrainingGameInput
from nba_live_totals.domain.model_training import TemporalPartition, TrainingConfig, TrainingRow
from nba_live_totals.domain.possessions import NormalizedGameActions


@runtime_checkable
class ModelFeatureSource(Protocol):
    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]: ...
    async def resolve(self, game_id: GameId) -> ModelSourceReference: ...
    async def read_features(self, source: ModelSourceReference) -> FeatureDataset: ...
    async def read_training(self, source: ModelSourceReference) -> TrainingGameInput: ...
    async def recheck(self, source: ModelSourceReference) -> None: ...


@runtime_checkable
class HorizonNormalizer(Protocol):
    @property
    def version(self) -> str: ...
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions: ...


@runtime_checkable
class TrainingDatasetExporter(Protocol):
    async def begin(self, request: BuildTrainingDatasetRequest) -> None: ...
    async def append_game(
        self, source: ModelSourceReference, rows: Iterable[TrainingRow]
    ) -> None: ...
    async def prepare(
        self, sources: tuple[ModelSourceReference, ...]
    ) -> TrainingDatasetSummary: ...
    def publish(self) -> None: ...
    async def abort(self) -> None: ...


@runtime_checkable
class TrainingDatasetReader(Protocol):
    def validate(self, path: Path) -> ValidatedTrainingDataset: ...
    def head_rows(
        self, dataset: ValidatedTrainingDataset, partition: TemporalPartition, head: ModelHead
    ) -> HeadRows: ...


@runtime_checkable
class RateModel(Protocol):
    @property
    def feature_names(self) -> tuple[str, ...]: ...
    @property
    def best_iteration(self) -> int: ...
    @property
    def tree_count(self) -> int: ...
    @property
    def resolved_parameters(self) -> CanonicalObject: ...
    def predict(
        self, vectors: Sequence[ModelFeatureVector], descriptor: FeatureProjectionDescriptor
    ) -> tuple[float, ...]: ...
    def save(self, path: Path) -> None: ...


@runtime_checkable
class RateModelTrainer(Protocol):
    def fit_head(
        self,
        train: HeadRows,
        validation: HeadRows,
        descriptor: FeatureProjectionDescriptor,
        config: TrainingConfig,
    ) -> RateModel: ...


@runtime_checkable
class ModelPredictor(Protocol):
    def predict_state(self, state: GameStateFeature) -> StateModelPrediction: ...


@runtime_checkable
class TrainingWorker(Protocol):
    async def execute(self, job: TrainingWorkerJob) -> TrainingWorkerResult: ...


@runtime_checkable
class ModelBundleStore(Protocol):
    def load(self, path: Path) -> LoadedModelBundle: ...
    async def prepare(
        self,
        request: TrainModelsRequest,
        dataset: ValidatedTrainingDataset,
        worker_result: TrainingWorkerResult,
    ) -> ModelBundleManifest: ...
    def publish(self) -> str: ...
    async def abort(self) -> None: ...


@runtime_checkable
class PredictionExporter(Protocol):
    async def begin(self, request: PredictModelsRequest, bundle: LoadedModelBundle) -> None: ...
    async def append_game(
        self, source: ModelSourceReference, rows: Iterable[StateModelPrediction]
    ) -> None: ...
    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> PredictModelsResult: ...
    def publish(self) -> None: ...
    async def abort(self) -> None: ...
