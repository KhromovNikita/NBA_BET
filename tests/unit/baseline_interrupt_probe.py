"""Run real SIGINTs in a child process so pytest's signal handler stays untouched."""

import asyncio
import signal
import sys
import threading
import time
from pathlib import Path

from nba_live_totals.application.baselines.models import (
    BaselineArtifactStats,
    BaselineSourceReference,
    BuildBaselinesRequest,
    BuildBaselinesResult,
)
from nba_live_totals.application.baselines.service import BaselineBuildService
from nba_live_totals.domain.baselines import DeterministicBaselineCalculator
from nba_live_totals.infrastructure.files.baseline_export import AtomicJsonlBaselineExporter
from nba_live_totals.settings import Settings
from scripts.build_baselines import main
from tests.unit.application.baseline_fakes import MemoryBaselineSource


def probe(path: Path, phase: str, interrupt_count: int) -> int:
    joined = threading.Event()
    original_handler = signal.getsignal(signal.SIGINT)

    class InterruptingExporter(AtomicJsonlBaselineExporter):
        async def interrupt_worker(self) -> None:
            loop = asyncio.get_running_loop()
            for index in range(interrupt_count):
                loop.call_later(0.03 + index * 0.04, signal.raise_signal, signal.SIGINT)

            def worker() -> None:
                time.sleep(0.2)
                joined.set()

            await self._io(worker)

        async def prepare(
            self, sources: tuple[BaselineSourceReference, ...]
        ) -> BaselineArtifactStats:
            if phase == "before":
                await self.interrupt_worker()
            return await super().prepare(sources)

        async def abort(self) -> None:
            try:
                if phase == "after":
                    await self.interrupt_worker()
            finally:
                await super().abort()

    async def command(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult:
        return await BaselineBuildService(
            MemoryBaselineSource(), DeterministicBaselineCalculator(), InterruptingExporter()
        ).build(request)

    code = main(["--season", "2025-26", "--output", str(path)], run_command=command)
    print(f"exit={code} joined={joined.is_set()}")
    print(f"handler_restored={signal.getsignal(signal.SIGINT) is original_handler}")
    return code


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
