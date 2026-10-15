"""One owned CPU call at a time; cancellation signals stop and joins its task."""

from threading import Event

from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.ports import SimulationEngine
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
)


class OwnedSimulationExecutor:
    def __init__(self, engine: SimulationEngine) -> None:
        self._engine = engine
        self._busy = False

    def _claim(self) -> None:
        if self._busy:
            raise SimulationError("simulation_executor_busy")
        self._busy = True

    async def runtime(self) -> SimulationRuntime:
        self._claim()
        try:
            return await run_owned_io(self._engine.runtime)
        finally:
            self._busy = False

    async def simulate(self, frame: SimulationFrame, config: SimulationConfig) -> SimulationOutcome:
        self._claim()
        stop = Event()
        try:
            return await run_owned_io(
                lambda: self._engine.simulate(frame, config, stop_requested=stop.is_set),
                on_cancel=stop.set,
            )
        finally:
            self._busy = False
