"""Pin validated input before fitting; publish and preserve one committed bundle."""

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    TrainModelsRequest,
    TrainModelsResult,
    ValidatedTrainingDataset,
)
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.ports import (
    ModelBundleStore,
    TrainingDatasetReader,
    TrainingWorker,
)
from nba_live_totals.application.modeling.worker_models import TrainingWorkerJob

logger = logging.getLogger(__name__)
_MARKER = b"nba-model-training-workspace-v1\n"


class TrainModelsService:
    def __init__(
        self, reader: TrainingDatasetReader, worker: TrainingWorker, bundles: ModelBundleStore
    ) -> None:
        self._reader, self._worker, self._bundles = reader, worker, bundles
        self._workspace: Path | None = None
        self._parent: Path | None = None
        self._running = False

    def _stage_copy(self, request: TrainModelsRequest) -> ValidatedTrainingDataset:
        original = self._reader.validate(request.dataset_path)
        if not original.summary.trainable:
            raise ModelingError("insufficient_training_data")
        self._parent = request.output_path.parent
        self._workspace = Path(tempfile.mkdtemp(prefix=".nba-train-", dir=self._parent)).resolve()
        (self._workspace / ".owned").write_bytes(_MARKER)
        private = self._workspace / "training.jsonl"
        digest = hashlib.sha256()
        with request.dataset_path.open("rb") as source, private.open("xb") as output:
            while block := source.read(1_048_576):
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != original.summary.dataset_artifact_sha256:
            raise ModelingError("training_dataset_changed")
        checked = self._reader.validate(private)
        if checked.summary != original.summary:
            raise ModelingError("training_dataset_changed")
        return checked

    def _cleanup_workspace(self) -> None:
        root = self._workspace
        if root is None:
            return
        if (
            root.is_symlink()
            or root.resolve() != root
            or root.parent != self._parent
            or not root.name.startswith(".nba-train-")
            or (root / ".owned").read_bytes() != _MARKER
        ):
            raise ValueError("refusing unowned training workspace cleanup")
        shutil.rmtree(root)

    async def train(self, request: TrainModelsRequest) -> TrainModelsResult:
        if self._running:
            raise ModelingError("model_training_busy")
        self._running = True
        self._workspace = None
        result: TrainModelsResult | None = None
        active_error: BaseException | None = None
        try:
            dataset = await run_owned_io(lambda: self._stage_copy(request))
            assert self._workspace is not None
            worker_result = await self._worker.execute(
                TrainingWorkerJob(dataset, self._workspace, request.config)
            )
            manifest = await self._bundles.prepare(request, dataset, worker_result)
            digest = self._bundles.publish()
            result = TrainModelsResult(request.output_path, manifest, digest)
            return result
        except OSError, ValueError, TypeError:
            active_error = ModelingError("model_training_io_error")
            raise active_error from None
        except BaseException as error:
            active_error = error
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                await self._bundles.abort()
            except BaseException as error:
                cleanup_error = error
            try:
                await run_owned_io(self._cleanup_workspace)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            self._running = False
            if cleanup_error is not None:
                if result is None and active_error is None:
                    raise cleanup_error
                logger.error(
                    "model_training_cleanup_failed",
                    extra={
                        "event": "model_training_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(cleanup_error).__name__[:64],
                    },
                )
