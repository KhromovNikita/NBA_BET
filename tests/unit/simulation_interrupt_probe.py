"""Real SIGINT probes use callback/Event barriers, never timed sleeps for ordering."""

import asyncio
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event

from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.simulation.errors import SimulationStopped
from nba_live_totals.application.simulation.models import (
    SimulateTotalsRequest,
    SimulateTotalsResult,
)
from nba_live_totals.application.simulation.service import SimulateTotalsService
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
)
from nba_live_totals.infrastructure.files.simulation_export import AtomicSimulationExporter
from nba_live_totals.infrastructure.simulation.executor import OwnedSimulationExecutor
from scripts.simulate_totals import main
from tests.unit.application.simulation_fakes import MemorySimulationSource
from tests.unit.application.test_simulation_service import FixedStore, fake_executor
from tests.unit.infrastructure.files.test_model_predictions import fixed_bundle


def probe(path: Path, phase: str) -> int:
    joined = Event()
    original = signal.getsignal(signal.SIGINT)
    bundle = asyncio.run(fixed_bundle(path.parent))

    async def command(request: SimulateTotalsRequest) -> SimulateTotalsResult:
        loop = asyncio.get_running_loop()
        gate = Event()

        def first_signal() -> None:
            signal.raise_signal(signal.SIGINT)
            loop.call_soon(second_signal)

        def second_signal() -> None:
            signal.raise_signal(signal.SIGINT)
            loop.call_soon(gate.set)

        def worker() -> None:
            loop.call_soon_threadsafe(first_signal)
            assert gate.wait(5), "second interrupt barrier not released"
            joined.set()

        class InterruptedExporter(AtomicSimulationExporter):
            async def prepare(
                self, sources: tuple[ModelSourceReference, ...]
            ) -> SimulateTotalsResult:
                if phase == "before":
                    await self._io(worker)
                return await super().prepare(sources)

            async def abort(self) -> None:
                try:
                    if phase == "after" and self._published:
                        await self._io(worker)
                finally:
                    await super().abort()

        class InterruptedEngine:
            def runtime(self) -> SimulationRuntime:
                return SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")

            def simulate(
                self,
                frame: SimulationFrame,
                config: SimulationConfig,
                *,
                stop_requested: Callable[[], bool],
            ) -> SimulationOutcome:
                loop.call_soon_threadsafe(first_signal)
                assert gate.wait(5), "second interrupt barrier not released"
                assert stop_requested(), "CPU cancellation did not set its stop event"
                joined.set()
                raise SimulationStopped()

        executor = (
            OwnedSimulationExecutor(InterruptedEngine()) if phase == "cpu" else fake_executor()
        )
        return await SimulateTotalsService(
            MemorySimulationSource(), FixedStore(bundle), executor, InterruptedExporter()
        ).simulate(request)

    code = main(
        [
            "--season",
            "2025-26",
            "--model",
            str(path.parent / "m.nbamodel"),
            "--output",
            str(path),
            "--simulations",
            "4",
        ],
        run_command=command,
    )
    print(
        f"exit={code} joined={joined.is_set()} "
        f"handler_restored={signal.getsignal(signal.SIGINT) is original}"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1]), sys.argv[2]))
