"""Simulation ports expose no native arrays, source writes or future training labels."""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from nba_live_totals.application.modeling.models import LoadedModelBundle, ModelSourceReference
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsRequest,
    SimulateTotalsResult,
)
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
)


@runtime_checkable
class SimulationFeatureSource(Protocol):
    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]: ...
    async def resolve(self, game_id: GameId) -> ModelSourceReference: ...
    async def read_features(self, source: ModelSourceReference) -> FeatureDataset: ...
    async def recheck(self, source: ModelSourceReference) -> None: ...


@runtime_checkable
class SimulationEngine(Protocol):
    def runtime(self) -> SimulationRuntime: ...
    def simulate(
        self,
        frame: SimulationFrame,
        config: SimulationConfig,
        *,
        stop_requested: Callable[[], bool],
    ) -> SimulationOutcome: ...


@runtime_checkable
class SimulationExecutor(Protocol):
    async def runtime(self) -> SimulationRuntime: ...
    async def simulate(
        self, frame: SimulationFrame, config: SimulationConfig
    ) -> SimulationOutcome: ...


@runtime_checkable
class SimulationExporter(Protocol):
    async def begin(
        self, request: SimulateTotalsRequest, bundle: LoadedModelBundle, runtime: SimulationRuntime
    ) -> None: ...
    async def begin_game(
        self, source: ModelSourceReference, selection: SelectedSimulationStates
    ) -> None: ...
    async def append_state(self, outcome: SimulationOutcome) -> None: ...
    async def end_game(self) -> None: ...
    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> SimulateTotalsResult: ...
    def publish(self) -> None: ...
    async def abort(self) -> None: ...
