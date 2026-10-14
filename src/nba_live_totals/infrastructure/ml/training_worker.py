"""Internal owned CPU child: fit on train/validation, then evaluate held-out rows."""

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator, Sequence
from itertools import islice
from pathlib import Path
from typing import cast

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.ports import (
    RateModel,
    RateModelTrainer,
    TrainingDatasetReader,
)
from nba_live_totals.application.modeling.worker_models import (
    MAX_JSON_BYTES,
    MAX_MODEL_BYTES,
    HeadModelFile,
    TrainingWorkerJob,
    TrainingWorkerResult,
    ValidationProbe,
)
from nba_live_totals.domain.baselines import BaselineMetric
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes
from nba_live_totals.domain.model_evaluation import (
    EvaluationCohort,
    EvaluationReport,
    EvaluationRow,
    evaluate_rows,
)
from nba_live_totals.domain.model_predictions import PredictedRate, encode_model_rate
from nba_live_totals.domain.model_targets import HeadTarget, ModelHead
from nba_live_totals.domain.model_training import TemporalPartition
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from nba_live_totals.infrastructure.ml.catboost_adapter import (
    CatBoostRateModelTrainer,
    ensure_ml_dependencies,
)
from nba_live_totals.infrastructure.ml.worker_protocol import (
    parse_worker_job,
    parse_worker_result,
    worker_result_projection,
)


def _evaluate(
    job: TrainingWorkerJob,
    reader: TrainingDatasetReader,
    model: RateModel,
    head: ModelHead,
    partition: TemporalPartition,
) -> tuple[EvaluationCohort, ...]:
    index = tuple(ModelHead).index(head)

    def records() -> Iterator[EvaluationRow]:
        rows = reader.iter_rows(job.dataset, partition)
        while batch := tuple(islice(rows, 4096)):
            selected = tuple(
                i for i, row in enumerate(batch) if row.is_valid and row.remaining_time_ms > 0
            )
            vectors = tuple(batch[i].vector for i in selected)
            raw = model.predict(vectors, job.dataset.summary.descriptor)
            if len(raw) != len(vectors):
                raise ModelingError("model_native_output_invalid")
            predictions = {
                i: encode_model_rate(value) for i, value in zip(selected, raw, strict=True)
            }
            for i, row in enumerate(batch):
                target = cast(HeadTarget, getattr(row.targets, head.value))
                prediction = (
                    predictions[i]
                    if i in predictions
                    else PredictedRate(
                        None,
                        "invalid_feature_state" if not row.is_valid else "no_future_time",
                        False,
                    )
                )
                baselines = tuple(
                    cast(BaselineMetric, getattr(baseline, head.value)).value
                    for baseline in row.baselines
                )
                yield EvaluationRow(
                    row.game_id,
                    row.period,
                    target.value_scaled,
                    prediction,
                    (baselines[0], baselines[1], baselines[2]),
                    row.eligibility[index].reason,
                )

    return evaluate_rows(head, partition, records())


def _native_file(head: ModelHead, path: Path, model: RateModel) -> HeadModelFile:
    model.save(path)
    digest, size = hashlib.sha256(), 0
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
        while block := stream.read(1_048_576):
            size += len(block)
            if size > MAX_MODEL_BYTES:
                raise ModelingError("model_native_file_too_large")
            digest.update(block)
    return HeadModelFile(
        head, path, digest.hexdigest(), size, model.best_iteration, model.tree_count
    )


def run_training_job(
    job: TrainingWorkerJob, reader: TrainingDatasetReader, trainer: RateModelTrainer
) -> TrainingWorkerResult:
    try:
        if job.dataset.path != job.work_dir / "training.jsonl" or not job.work_dir.is_dir():
            raise ModelingError("model_worker_protocol_invalid")
        checked = reader.validate(job.dataset.path)
        if checked != job.dataset:
            raise ModelingError("training_dataset_invalid")
        if not checked.summary.trainable:
            raise ModelingError("insufficient_training_data")
        if any(
            head.eligible_rows > job.config.max_rows_per_partition
            for count in checked.summary.counts
            if count.partition is not TemporalPartition.EMBARGO
            for head in count.heads
        ):
            raise ModelingError("model_partition_row_limit")
        runtime = ensure_ml_dependencies()
        files: list[HeadModelFile] = []
        cohorts: list[EvaluationCohort] = []
        probes: list[ValidationProbe] = []
        parameters: dict[ModelHead, CanonicalObject] = {}
        for head in ModelHead:
            train = reader.head_rows(checked, TemporalPartition.TRAIN, head)
            validation = reader.head_rows(checked, TemporalPartition.VALIDATION, head)
            model = trainer.fit_head(train, validation, checked.summary.descriptor, job.config)
            path = job.work_dir / f"{head.value}.cbm"
            if path.resolve().parent != job.work_dir:
                raise ModelingError("model_worker_protocol_invalid")
            files.append(_native_file(head, path, model))
            vectors = tuple(row.vector for row in islice(validation.iter_rows(), 8))
            raw = model.predict(vectors, checked.summary.descriptor)
            if len(raw) != len(vectors):
                raise ModelingError("model_native_output_invalid")
            encoded = tuple(encode_model_rate(value).value_scaled for value in raw)
            assert all(value is not None for value in encoded)
            probes.append(ValidationProbe(head, vectors, cast(tuple[int, ...], encoded)))
            parameters[head] = model.resolved_parameters
            for partition in (TemporalPartition.VALIDATION, TemporalPartition.TEST):
                cohorts.extend(_evaluate(job, reader, model, head, partition))
            del model, train, validation
        result = TrainingWorkerResult(
            tuple(files), EvaluationReport(tuple(cohorts)), runtime, parameters, tuple(probes)
        )
        # The in-process executor observes the same strict boundary as the parent.
        return parse_worker_result(worker_result_projection(result), job)
    except ModelingError:
        raise
    except Exception:
        raise ModelingError("model_training_failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal owned offline CPU worker")
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        path = Path(args.job).resolve()
        with path.open("rb") as stream:
            content = stream.read(MAX_JSON_BYTES + 1)
        data = cast(CanonicalObject, json.loads(content))
        if len(content) > MAX_JSON_BYTES or canonical_json_bytes(data) + b"\n" != content:
            raise ModelingError("model_worker_protocol_invalid")
        job = parse_worker_job(data)
        if path != job.work_dir / "job.json":
            raise ModelingError("model_worker_protocol_invalid")
        result = run_training_job(job, CanonicalTrainingDatasetReader(), CatBoostRateModelTrainer())
        content = canonical_json_bytes(worker_result_projection(result)) + b"\n"
        if len(content) > MAX_JSON_BYTES:
            raise ModelingError("model_worker_protocol_invalid")
        temporary = job.work_dir / ".result.tmp"
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, job.work_dir / "result.json")
        sys.stdout.buffer.write(canonical_json_bytes({"status": "succeeded"}) + b"\n")
        return 0
    except Exception as error:
        code = error.code if isinstance(error, ModelingError) else "model_worker_failed"
        sys.stdout.buffer.write(canonical_json_bytes({"status": "failed", "code": code}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
