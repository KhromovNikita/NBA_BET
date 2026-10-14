"""Send real repeated SIGINTs to the modeling runner in a disposable child."""

import asyncio
import signal
import sys
import threading
import time
from pathlib import Path

from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    BuildTrainingDatasetResult,
    ModelSourceReference,
    TrainingDatasetSummary,
)
from nba_live_totals.infrastructure.files.training_dataset import AtomicTrainingDatasetExporter
from scripts.build_training_dataset import main
from tests.unit.application.modeling_fakes import MemoryModelSource


def probe(path: Path, phase: str) -> int:
    joined = threading.Event()
    original_handler = signal.getsignal(signal.SIGINT)

    class InterruptingExporter(AtomicTrainingDatasetExporter):
        async def interrupt_worker(self) -> None:
            loop = asyncio.get_running_loop()
            for delay in (0.03, 0.07):
                loop.call_later(delay, signal.raise_signal, signal.SIGINT)

            def worker() -> None:
                time.sleep(0.2)
                joined.set()

            await self._io(worker)

        async def prepare(
            self, sources: tuple[ModelSourceReference, ...]
        ) -> TrainingDatasetSummary:
            if phase == "before":
                await self.interrupt_worker()
            return await super().prepare(sources)

        async def abort(self) -> None:
            try:
                if phase == "after":
                    await self.interrupt_worker()
            finally:
                await super().abort()

    async def command(request: BuildTrainingDatasetRequest) -> BuildTrainingDatasetResult:
        return await BuildTrainingDatasetService(MemoryModelSource(), InterruptingExporter()).build(
            request
        )

    code = main(
        [
            "--season",
            "2025-26",
            "--validation-from",
            "2025-11-01",
            "--test-from",
            "2025-12-01",
            "--output",
            str(path),
        ],
        run_command=command,
    )
    print(f"exit={code} joined={joined.is_set()}")
    print(f"handler_restored={signal.getsignal(signal.SIGINT) is original_handler}")
    return code


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1]), sys.argv[2]))
