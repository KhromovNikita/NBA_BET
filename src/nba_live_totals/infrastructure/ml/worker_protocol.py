"""Strict canonical messages for one owned offline CPU worker."""

from decimal import Decimal
from pathlib import Path

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import ValidatedTrainingDataset
from nba_live_totals.application.modeling.worker_models import (
    HeadModelFile,
    RuntimeVersions,
    TrainingWorkerJob,
    TrainingWorkerResult,
    ValidationProbe,
)
from nba_live_totals.domain.baselines import BaselineMethod
from nba_live_totals.domain.canonical import CanonicalObject, CanonicalValue, canonical_json_bytes
from nba_live_totals.domain.model_evaluation import (
    EvaluationCohort,
    EvaluationReport,
    EvaluationScope,
    MatchedComparison,
    MetricScores,
    evaluation_projection,
)
from nba_live_totals.domain.model_features import ModelFeatureVector
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import (
    TemporalPartition,
    TrainingConfig,
    training_config_projection,
)
from nba_live_totals.infrastructure.files.training_dataset import parse_summary, summary_projection


def _object(value: CanonicalValue) -> CanonicalObject:
    if type(value) is not dict:
        raise ValueError("expected a canonical object")
    return value


def _list(value: CanonicalValue) -> list[CanonicalValue]:
    if type(value) is not list:
        raise ValueError("expected a canonical array")
    return value


def _str(value: CanonicalValue) -> str:
    if type(value) is not str:
        raise ValueError("expected exact text")
    return value


def _int(value: CanonicalValue) -> int:
    if type(value) is not int:
        raise ValueError("expected exact integer")
    return value


def _optional_int(value: CanonicalValue) -> int | None:
    return None if value is None else _int(value)


def parse_training_config(data: CanonicalObject) -> TrainingConfig:
    config = TrainingConfig(
        _int(data["iterations"]),
        _int(data["depth"]),
        Decimal(_str(data["learning_rate"])),
        Decimal(_str(data["l2_leaf_reg"])),
        _int(data["random_seed"]),
        _int(data["early_stopping_rounds"]),
        _int(data["max_rows_per_partition"]),
    )
    if canonical_json_bytes(training_config_projection(config)) != canonical_json_bytes(data):
        raise ValueError("unsupported or noncanonical CPU training recipe")
    return config


def runtime_projection(runtime: RuntimeVersions) -> CanonicalObject:
    return {
        "python": runtime.python,
        "catboost": runtime.catboost,
        "numpy": runtime.numpy,
        "platform": runtime.platform,
        "architecture": runtime.architecture,
    }


def parse_runtime(data: CanonicalObject) -> RuntimeVersions:
    runtime = RuntimeVersions(
        *(_str(data[key]) for key in ("python", "catboost", "numpy", "platform", "architecture"))
    )
    if runtime_projection(runtime) != data:
        raise ValueError("runtime schema mismatch")
    return runtime


def worker_job_projection(job: TrainingWorkerJob) -> CanonicalObject:
    return {
        "schema": "training-worker-job-v1",
        "dataset_path": str(job.dataset.path),
        "dataset_summary": summary_projection(job.dataset.summary),
        "dataset_artifact_sha256": job.dataset.summary.dataset_artifact_sha256,
        "work_dir": str(job.work_dir),
        "training_config": training_config_projection(job.config),
    }


def parse_worker_job(data: CanonicalObject) -> TrainingWorkerJob:
    try:
        path = Path(_str(data["dataset_path"]))
        work_dir = Path(_str(data["work_dir"]))
        summary = parse_summary(
            _object(data["dataset_summary"]), _str(data["dataset_artifact_sha256"])
        )
        job = TrainingWorkerJob(
            ValidatedTrainingDataset(path, summary),
            work_dir,
            parse_training_config(_object(data["training_config"])),
        )
        if job.dataset.path != job.work_dir / "training.jsonl" or canonical_json_bytes(
            worker_job_projection(job)
        ) != canonical_json_bytes(data):
            raise ValueError("CPU worker paths or schema mismatch")
        return job
    except ValueError, TypeError, KeyError, IndexError, ArithmeticError:
        raise ModelingError("model_worker_protocol_invalid") from None


def _scores(data: CanonicalObject) -> MetricScores:
    scores = MetricScores(
        _int(data["row_count"]),
        _int(data["game_count"]),
        _optional_int(data["mae_scaled"]),
        _optional_int(data["rmse_scaled"]),
        _optional_int(data["game_balanced_mae_scaled"]),
        _optional_int(data["game_balanced_rmse_scaled"]),
        _int(data["negative_prediction_count"]),
        None if data["unavailable_reason"] is None else _str(data["unavailable_reason"]),
    )
    # evaluation_projection's final equality check closes this nested schema too.
    return scores


def parse_evaluation(data: CanonicalObject) -> EvaluationReport:
    cohorts = []
    for item in _list(data["cohorts"]):
        c = _object(item)
        comparisons = []
        for value in _list(c["comparisons"]):
            m = _object(value)
            comparisons.append(
                MatchedComparison(
                    BaselineMethod(_str(m["method"])),
                    _scores(_object(m["model"])),
                    _scores(_object(m["baseline"])),
                )
            )
        exclusions = _object(c["excluded_counts"])
        cohorts.append(
            EvaluationCohort(
                ModelHead(_str(c["head"])),
                TemporalPartition(_str(c["partition"])),
                EvaluationScope(_str(c["scope"])),
                _int(c["total_row_count"]),
                _int(c["total_game_count"]),
                tuple(sorted((key, _int(value)) for key, value in exclusions.items())),
                _scores(_object(c["model"])),
                tuple(comparisons),
            )
        )
    report = EvaluationReport(tuple(cohorts))
    if canonical_json_bytes(evaluation_projection(report)) != canonical_json_bytes(data):
        raise ValueError("evaluation schema mismatch")
    return report


def worker_result_projection(result: TrainingWorkerResult) -> CanonicalObject:
    return {
        "schema": "training-worker-result-v1",
        "models": [
            {
                "head": model.head.value,
                "filename": f"{model.head.value}.cbm",
                "sha256": model.sha256,
                "size": model.size,
                "best_iteration": model.best_iteration,
                "tree_count": model.tree_count,
            }
            for model in result.models
        ],
        "evaluation": evaluation_projection(result.evaluation),
        "runtime": runtime_projection(result.runtime),
        "resolved_parameters": {
            head.value: values for head, values in result.resolved_parameters.items()
        },
        "probes": [
            {
                "head": probe.head.value,
                "vectors": [
                    {"descriptor_sha256": vector.descriptor_sha256, "values": list(vector.values)}
                    for vector in probe.vectors
                ],
                "values_scaled": list(probe.values_scaled),
            }
            for probe in result.probes
        ],
    }


def parse_worker_result(data: CanonicalObject, job: TrainingWorkerJob) -> TrainingWorkerResult:
    try:
        models = []
        for item in _list(data["models"]):
            m = _object(item)
            head = ModelHead(_str(m["head"]))
            if m["filename"] != f"{head.value}.cbm":
                raise ValueError("worker model filename mismatch")
            path = job.work_dir / f"{head.value}.cbm"
            if path.resolve().parent != job.work_dir:
                raise ValueError("worker model path escapes workspace")
            models.append(
                HeadModelFile(
                    head,
                    path,
                    _str(m["sha256"]),
                    _int(m["size"]),
                    _int(m["best_iteration"]),
                    _int(m["tree_count"]),
                )
            )
        probes = []
        for item in _list(data["probes"]):
            p = _object(item)
            vectors = []
            for value in _list(p["vectors"]):
                v = _object(value)
                vectors.append(
                    ModelFeatureVector(
                        _str(v["descriptor_sha256"]),
                        tuple(_optional_int(x) for x in _list(v["values"])),
                    )
                )
            probes.append(
                ValidationProbe(
                    ModelHead(_str(p["head"])),
                    tuple(vectors),
                    tuple(_int(value) for value in _list(p["values_scaled"])),
                )
            )
        params = _object(data["resolved_parameters"])
        result = TrainingWorkerResult(
            tuple(models),
            parse_evaluation(_object(data["evaluation"])),
            parse_runtime(_object(data["runtime"])),
            {ModelHead(key): _object(value) for key, value in params.items()},
            tuple(probes),
        )
        if any(m.tree_count > job.config.iterations for m in result.models) or any(
            v.descriptor_sha256 != job.dataset.summary.descriptor.sha256
            for p in result.probes
            for v in p.vectors
        ):
            raise ValueError("worker metadata contradicts job")
        expected = tuple(
            (head, partition, scope)
            for head in ModelHead
            for partition in (TemporalPartition.VALIDATION, TemporalPartition.TEST)
            for scope in EvaluationScope
        )
        if tuple((c.head, c.partition, c.scope) for c in result.evaluation.cohorts) != expected:
            raise ValueError("worker evaluation is incomplete")
        if canonical_json_bytes(worker_result_projection(result)) != canonical_json_bytes(data):
            raise ValueError("worker result schema mismatch")
        return result
    except ValueError, TypeError, KeyError, IndexError, OverflowError:
        raise ModelingError("model_worker_protocol_invalid") from None
