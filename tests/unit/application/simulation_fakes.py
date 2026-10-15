"""Small typed simulation fixtures; sampled distributions are supplied by tests."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from nba_live_totals.application.modeling.models import LoadedModelBundle, ModelSourceReference
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsRequest,
    SimulateTotalsResult,
    SimulationCounters,
    SimulationManifest,
    SimulationModelIdentity,
    SimulationSourceSelection,
)
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_features import model_feature_descriptor
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationQuery,
    SimulationRuntime,
)
from nba_live_totals.domain.simulation_summary import validate_simulation_outcome
from tests.unit.application.modeling_fakes import MemoryModelSource, make_model_source_reference
from tests.unit.domain.modeling_helpers import make_training_input


def make_simulation_request(path: Path) -> SimulateTotalsRequest:
    return SimulateTotalsRequest(
        (SeasonId("2025-26"),),
        path.parent / "m.nbamodel",
        path,
        SimulationConfig(simulations=4, batch_size=4),
        SimulationQuery(),
    )


def make_simulation_manifest() -> SimulationManifest:
    inputs = make_training_input()
    source = make_model_source_reference(inputs.game, inputs.features)
    identity = SimulationModelIdentity(
        "a" * 64,
        "b" * 64,
        model_feature_descriptor(BaselineConfig()),
        source.reference.attempt.input_token.versions,
        source.possession_versions,
        source.lineup_versions,
    )
    return SimulationManifest(
        (SeasonId("2025-26"),),
        None,
        None,
        SimulationConfig(simulations=4, batch_size=4),
        SimulationQuery(),
        identity,
        SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64"),
        (SimulationSourceSelection(source, SelectedSimulationStates(8)),),
        SimulationCounters(1, 8, 7, 0, 1, 0, ()),
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
    )


class MemorySimulationSource:
    def __init__(self, delegate: MemoryModelSource | None = None) -> None:
        self.delegate = delegate if delegate is not None else MemoryModelSource()

    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]:
        return await self.delegate.select_games(seasons, game_id)

    async def resolve(self, game_id: GameId) -> ModelSourceReference:
        return await self.delegate.resolve(game_id)

    async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
        return await self.delegate.read_features(source)

    async def recheck(self, source: ModelSourceReference) -> None:
        await self.delegate.recheck(source)

    async def read_training(self, source: ModelSourceReference) -> None:
        raise AssertionError("simulation must never read future training targets")


class MemorySimulationExecutor:
    def __init__(
        self, operation: Callable[[SimulationFrame, SimulationConfig], SimulationOutcome]
    ) -> None:
        self.operation = operation
        self.identity = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
        self.active = 0
        self.calls: list[tuple[GameId, int]] = []

    async def runtime(self) -> SimulationRuntime:
        return self.identity

    async def simulate(self, frame: SimulationFrame, config: SimulationConfig) -> SimulationOutcome:
        if self.active:
            raise SimulationError("simulation_executor_busy")
        self.active += 1
        self.calls.append((frame.game_id, frame.state_number))
        try:
            await asyncio.sleep(0)
            outcome = self.operation(frame, config)
            validate_simulation_outcome(outcome, config)
            return outcome
        finally:
            self.active -= 1


class MemorySimulationExporter:
    def __init__(self, result: SimulateTotalsResult) -> None:
        self.result = result
        self.events: list[str] = []
        self.outcomes: list[SimulationOutcome] = []
        self.published = False
        self.fail_abort = False

    async def begin(
        self, request: SimulateTotalsRequest, bundle: LoadedModelBundle, runtime: SimulationRuntime
    ) -> None:
        self.request = request
        self.events.append("begin")

    async def begin_game(
        self, source: ModelSourceReference, selection: SelectedSimulationStates
    ) -> None:
        self.events.append("begin_game")

    async def append_state(self, outcome: SimulationOutcome) -> None:
        validate_simulation_outcome(outcome, self.request.config)
        self.events.append("append_state")
        self.outcomes.append(outcome)

    async def end_game(self) -> None:
        self.events.append("end_game")

    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> SimulateTotalsResult:
        self.events.append("prepare")
        return self.result

    def publish(self) -> None:
        self.events.append("publish")
        self.published = True

    async def abort(self) -> None:
        self.events.append("abort")
        if self.fail_abort:
            raise OSError("controlled cleanup failure")
