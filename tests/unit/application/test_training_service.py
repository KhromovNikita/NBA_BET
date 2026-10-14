import asyncio
import json
import sys
from importlib import util
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import TrainModelsRequest, ValidatedTrainingDataset
from nba_live_totals.application.modeling.training_service import TrainModelsService
from nba_live_totals.application.modeling.worker_models import (
    TrainingWorkerJob,
    TrainingWorkerResult,
)
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes
from nba_live_totals.domain.model_training import TrainingConfig
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from nba_live_totals.infrastructure.ml.process import SubprocessTrainingWorker
from tests.unit.infrastructure.ml.job_helpers import write_tiny_dataset
from tests.unit.infrastructure.ml.test_training_process import PROBE, wait_started

requires_ml = pytest.mark.skipif(
    util.find_spec("catboost") is None, reason="optional ml extra is not installed"
)


class TrackingWorker(SubprocessTrainingWorker):
    workspace: Path | None = None

    async def execute(self, job: TrainingWorkerJob) -> TrainingWorkerResult:
        self.workspace = job.work_dir
        return await super().execute(job)


async def test_cancel_during_copy_joins_io_before_workspace_cleanup(tmp_path: Path) -> None:
    started, release, completed = Event(), Event(), Event()

    class PausedCopy(TrainModelsService):
        def _stage_copy(self, request: TrainModelsRequest) -> ValidatedTrainingDataset:
            checked = super()._stage_copy(request)
            started.set()
            release.wait(5)
            (checked.path.parent / "copy-completed").write_bytes(b"done")
            completed.set()
            return checked

    dataset = await write_tiny_dataset(tmp_path / "training")
    path = tmp_path / "old.nbamodel"
    path.write_bytes(b"old")
    service = PausedCopy(
        CanonicalTrainingDatasetReader(), SubprocessTrainingWorker(), VerifiedModelBundleStore()
    )
    task = asyncio.create_task(
        service.train(TrainModelsRequest(dataset.path, path, TrainingConfig()))
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    assert service._workspace is not None and not service._workspace.exists()
    assert path.read_bytes() == b"old"


@requires_ml
async def test_cancel_during_bundle_save_waits_for_io_then_preserves_old_output(
    tmp_path: Path,
) -> None:
    started, release, completed = Event(), Event(), Event()

    class PausedSave(VerifiedModelBundleStore):
        def _prepare(self, dataset: ValidatedTrainingDataset, result: TrainingWorkerResult) -> None:
            started.set()
            release.wait(5)
            super()._prepare(dataset, result)
            completed.set()

    dataset = await write_tiny_dataset(tmp_path / "training")
    path = tmp_path / "old.nbamodel"
    path.write_bytes(b"old")
    worker = TrackingWorker()
    bundles = PausedSave()
    service = TrainModelsService(CanonicalTrainingDatasetReader(), worker, bundles)
    task = asyncio.create_task(
        service.train(
            TrainModelsRequest(
                dataset.path, path, TrainingConfig(iterations=12, early_stopping_rounds=3)
            )
        )
    )
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    assert worker.workspace is not None and not worker.workspace.exists()
    assert bundles._ready is not None and not bundles._ready.exists()
    assert path.read_bytes() == b"old"


async def test_cancel_reaps_worker_before_staging_cleanup(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    path = tmp_path / "old.nbamodel"
    path.write_bytes(b"old")
    worker = TrackingWorker(command=(sys.executable, str(PROBE), "--mode", "block"))
    service = TrainModelsService(
        CanonicalTrainingDatasetReader(), worker, VerifiedModelBundleStore()
    )
    task = asyncio.create_task(
        service.train(
            TrainModelsRequest(
                dataset.path, path, TrainingConfig(iterations=12, early_stopping_rounds=3)
            )
        )
    )
    for _ in range(100):
        if worker.workspace is not None:
            break
        await asyncio.sleep(0.02)
    assert worker.workspace is not None
    await wait_started(worker.workspace)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.last_returncode is not None
    assert path.read_bytes() == b"old"
    assert not worker.workspace.exists()


@requires_ml
async def test_original_input_change_after_copy_does_not_change_worker_input(
    tmp_path: Path,
) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")

    class ChangeOriginal(TrackingWorker):
        async def execute(self, job: TrainingWorkerJob) -> TrainingWorkerResult:
            dataset.path.write_bytes(b"original changed after private copy")
            assert (
                job.dataset.summary.dataset_artifact_sha256
                == dataset.summary.dataset_artifact_sha256
            )
            return await super().execute(job)

    worker = ChangeOriginal()
    path = tmp_path / "m.nbamodel"
    result = await TrainModelsService(
        CanonicalTrainingDatasetReader(), worker, VerifiedModelBundleStore()
    ).train(
        TrainModelsRequest(
            dataset.path, path, TrainingConfig(iterations=12, early_stopping_rounds=3)
        )
    )
    assert result.exit_code == 0 and path.is_file()
    assert (
        result.manifest.dataset.dataset_artifact_sha256 == dataset.summary.dataset_artifact_sha256
    )
    assert worker.workspace is not None and not worker.workspace.exists()


@requires_ml
async def test_cleanup_failure_after_commit_keeps_training_result(tmp_path: Path) -> None:
    class CleanupError(VerifiedModelBundleStore):
        async def abort(self) -> None:
            await super().abort()
            raise OSError("synthetic postcommit cleanup failure")

    dataset = await write_tiny_dataset(tmp_path / "training")
    path = tmp_path / "m.nbamodel"
    result = await TrainModelsService(
        CanonicalTrainingDatasetReader(), SubprocessTrainingWorker(), CleanupError()
    ).train(
        TrainModelsRequest(
            dataset.path, path, TrainingConfig(iterations=12, early_stopping_rounds=3)
        )
    )
    assert result.exit_code == 0
    assert VerifiedModelBundleStore().load(path).artifact_sha256 == result.artifact_sha256


@requires_ml
@pytest.mark.parametrize("mutation", ["hash", "path"])
async def test_tampered_child_result_preserves_previous_model(
    tmp_path: Path, mutation: str
) -> None:
    class TamperingWorker(TrackingWorker):
        def _read_result(self, job: TrainingWorkerJob) -> TrainingWorkerResult:
            path = job.work_dir / "result.json"
            record = json.loads(path.read_bytes())
            key, value = ("sha256", "0" * 64) if mutation == "hash" else ("filename", "../pace.cbm")
            record["models"][0][key] = value
            path.write_bytes(canonical_json_bytes(cast(CanonicalObject, record)) + b"\n")
            return super()._read_result(job)

    dataset = await write_tiny_dataset(tmp_path / "training")
    path = tmp_path / "old.nbamodel"
    path.write_bytes(b"old")
    worker = TamperingWorker()
    with pytest.raises(ModelingError, match="model_worker_protocol_invalid"):
        await TrainModelsService(
            CanonicalTrainingDatasetReader(), worker, VerifiedModelBundleStore()
        ).train(
            TrainModelsRequest(
                dataset.path, path, TrainingConfig(iterations=12, early_stopping_rounds=3)
            )
        )
    assert worker.last_returncode == 0
    assert worker.workspace is not None and not worker.workspace.exists()
    assert path.read_bytes() == b"old"
