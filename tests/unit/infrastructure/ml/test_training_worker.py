import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from importlib import util
from pathlib import Path
from typing import cast
from weakref import ref

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import HeadRows, ValidatedTrainingDataset
from nba_live_totals.application.modeling.worker_models import TrainingWorkerJob
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes
from nba_live_totals.domain.model_features import FeatureProjectionDescriptor
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition, TrainingConfig
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from nba_live_totals.infrastructure.ml.catboost_adapter import (
    CatBoostRateModel,
    CatBoostRateModelTrainer,
)
from nba_live_totals.infrastructure.ml.training_worker import run_training_job
from nba_live_totals.infrastructure.ml.worker_protocol import (
    parse_worker_job,
    parse_worker_result,
    worker_job_projection,
    worker_result_projection,
)
from tests.unit.infrastructure.ml.job_helpers import write_tiny_dataset

requires_ml = pytest.mark.skipif(
    util.find_spec("catboost") is None, reason="optional ml extra is not installed"
)


def make_job(dataset: ValidatedTrainingDataset, work_dir: Path) -> TrainingWorkerJob:
    work_dir.mkdir()
    private = work_dir / "training.jsonl"
    shutil.copyfile(dataset.path, private)
    checked = CanonicalTrainingDatasetReader().validate(private)
    return TrainingWorkerJob(
        checked, work_dir, TrainingConfig(iterations=12, early_stopping_rounds=3)
    )


@requires_ml
async def test_changing_only_test_targets_never_changes_fitted_values(tmp_path: Path) -> None:
    first = await write_tiny_dataset(tmp_path / "a.jsonl")
    second = await write_tiny_dataset(tmp_path / "b.jsonl", test_points_delta=3)
    left = run_training_job(
        make_job(first, tmp_path / "job-a"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    right = run_training_job(
        make_job(second, tmp_path / "job-b"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    assert left.probes == right.probes
    assert left.evaluation != right.evaluation
    assert tuple(model.head for model in left.models) == tuple(ModelHead)
    assert len(left.evaluation.cohorts) == 18
    assert all(model.path.is_file() for model in left.models)


@requires_ml
async def test_worker_fits_only_train_validation_and_releases_models_between_heads(
    tmp_path: Path,
) -> None:
    seen: list[tuple[TemporalPartition, TemporalPartition, ModelHead]] = []
    previous = None

    class TrackingTrainer(CatBoostRateModelTrainer):
        def fit_head(
            self,
            train: HeadRows,
            validation: HeadRows,
            descriptor: FeatureProjectionDescriptor,
            config: TrainingConfig,
        ) -> CatBoostRateModel:
            nonlocal previous
            assert previous is None or previous() is None
            seen.append((train.partition, validation.partition, train.head))
            model = super().fit_head(train, validation, descriptor, config)
            previous = ref(model)
            return model

    dataset = await write_tiny_dataset(tmp_path / "training")
    run_training_job(
        make_job(dataset, tmp_path / "job"), CanonicalTrainingDatasetReader(), TrackingTrainer()
    )
    assert seen == [
        (TemporalPartition.TRAIN, TemporalPartition.VALIDATION, head) for head in ModelHead
    ]


@requires_ml
async def test_third_head_failure_returns_no_complete_result(tmp_path: Path) -> None:
    class FailingTrainer(CatBoostRateModelTrainer):
        def fit_head(
            self,
            train: HeadRows,
            validation: HeadRows,
            descriptor: FeatureProjectionDescriptor,
            config: TrainingConfig,
        ) -> CatBoostRateModel:
            if train.head is ModelHead.AWAY_PPP:
                raise RuntimeError("private diagnostic must not escape")
            return super().fit_head(train, validation, descriptor, config)

    dataset = await write_tiny_dataset(tmp_path / "training")
    with pytest.raises(ModelingError, match="model_training_failed"):
        run_training_job(
            make_job(dataset, tmp_path / "job"), CanonicalTrainingDatasetReader(), FailingTrainer()
        )
    assert not (tmp_path / "job" / "result.json").exists()


async def test_checked_dataset_and_row_ceiling_gate_before_training(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    with pytest.raises(ModelingError, match="model_partition_row_limit"):
        run_training_job(
            replace(job, config=TrainingConfig(max_rows_per_partition=1)),
            CanonicalTrainingDatasetReader(),
            CatBoostRateModelTrainer(),
        )
    job.dataset.path.write_bytes(b"corrupt")
    with pytest.raises(ModelingError, match="training_dataset_invalid"):
        run_training_job(job, CanonicalTrainingDatasetReader(), CatBoostRateModelTrainer())


@requires_ml
async def test_worker_protocol_roundtrip_rejects_paths_extra_fields_and_metadata(
    tmp_path: Path,
) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    assert parse_worker_job(worker_job_projection(job)) == job
    result = run_training_job(job, CanonicalTrainingDatasetReader(), CatBoostRateModelTrainer())
    encoded = worker_result_projection(result)
    assert parse_worker_result(encoded, job) == result
    for bad in (
        dict(encoded, unexpected=True),
        dict(encoded, models=[{"head": "pace", "filename": "../pace.cbm"}]),
    ):
        with pytest.raises(ModelingError, match="model_worker_protocol_invalid"):
            parse_worker_result(cast(CanonicalObject, bad), job)


@requires_ml
async def test_actual_child_entry_writes_checked_result_and_only_safe_stdout(
    tmp_path: Path,
) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    path = job.work_dir / "job.json"
    path.write_bytes(canonical_json_bytes(worker_job_projection(job)) + b"\n")
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "USERPROFILE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["PYTHONUNBUFFERED"] = "1"
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "nba_live_totals.infrastructure.ml.training_worker",
            "--job",
            str(path),
        ],
        env=environment,
        cwd=job.work_dir,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b'{"status":"succeeded"}\n'
    parsed = parse_worker_result(json.loads((job.work_dir / "result.json").read_bytes()), job)
    assert len(parsed.models) == 3
    assert parsed.models[0].tree_count <= 12


@pytest.mark.parametrize(
    "field,value", [("has_time", 1), ("thread_count", True), ("learning_rate", "bad")]
)
async def test_worker_job_rejects_numeric_substitutes_for_fixed_booleans(
    tmp_path: Path, field: str, value: str | int | bool
) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    encoded = worker_job_projection(job)
    config = cast(CanonicalObject, encoded["training_config"])
    config[field] = value
    with pytest.raises(ModelingError, match="model_worker_protocol_invalid"):
        parse_worker_job(encoded)
