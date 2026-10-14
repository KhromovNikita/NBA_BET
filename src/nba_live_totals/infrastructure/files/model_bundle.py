"""One checked ZIP_STORED model artifact, published through one atomic replace."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import cast
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    LoadedModelBundle,
    ModelBundleManifest,
    ModelHeadMetadata,
    TrainModelsRequest,
    ValidatedTrainingDataset,
    model_semantic_revision,
)
from nba_live_totals.application.modeling.ports import RateModel
from nba_live_totals.application.modeling.worker_models import (
    MAX_BUNDLE_BYTES,
    MAX_JSON_BYTES,
    MAX_MODEL_BYTES,
    TrainingWorkerResult,
    ValidationProbe,
)
from nba_live_totals.domain.baseline_hashing import (
    baseline_config_projection,
    baseline_config_sha256,
)
from nba_live_totals.domain.canonical import CanonicalObject, CanonicalValue, canonical_json_bytes
from nba_live_totals.domain.features import GameStateFeature
from nba_live_totals.domain.model_evaluation import (
    EvaluationReport,
    EvaluationScope,
    evaluation_projection,
)
from nba_live_totals.domain.model_features import (
    model_input_sha256,
    project_model_features,
    require_sha256,
)
from nba_live_totals.domain.model_predictions import (
    PredictedRate,
    StateModelPrediction,
    encode_model_rate,
)
from nba_live_totals.domain.model_targets import ModelHead, horizon_period
from nba_live_totals.domain.model_training import (
    TemporalPartition,
    training_config_projection,
    training_config_sha256,
)
from nba_live_totals.infrastructure.files.training_dataset import (
    parse_summary,
    summary_projection,
    versions_projection,
)
from nba_live_totals.infrastructure.ml.catboost_adapter import (
    ensure_ml_dependencies,
    load_rate_model,
)
from nba_live_totals.infrastructure.ml.worker_protocol import (
    parse_evaluation,
    parse_runtime,
    parse_training_config,
    runtime_projection,
)

_ENTRIES = ("manifest.json", "metrics.json", "pace.cbm", "home_ppp.cbm", "away_ppp.cbm")
_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _object(value: CanonicalValue) -> CanonicalObject:
    if type(value) is not dict:
        raise ValueError("expected canonical object")
    return value


def _list(value: CanonicalValue) -> list[CanonicalValue]:
    if type(value) is not list:
        raise ValueError("expected canonical array")
    return value


def _str(value: CanonicalValue) -> str:
    if type(value) is not str:
        raise ValueError("expected exact text")
    return value


def _int(value: CanonicalValue) -> int:
    if type(value) is not int:
        raise ValueError("expected exact integer")
    return value


def manifest_projection(manifest: ModelBundleManifest) -> CanonicalObject:
    return {
        "bundle_version": manifest.bundle_version,
        "label_version": manifest.label_version,
        "projection_version": manifest.projection_version,
        "split_version": manifest.split_version,
        "training_version": manifest.training_version,
        "prediction_policy_version": manifest.prediction_policy_version,
        "model_revision_sha256": manifest.model_revision_sha256,
        "dataset": summary_projection(manifest.dataset),
        "dataset_artifact_sha256": manifest.dataset.dataset_artifact_sha256,
        "descriptor": manifest.descriptor.projection(),
        "descriptor_sha256": manifest.descriptor.sha256,
        "baseline_config": baseline_config_projection(manifest.baseline_config),
        "baseline_config_sha256": baseline_config_sha256(manifest.baseline_config),
        "training_config": training_config_projection(manifest.training_config),
        "training_config_sha256": training_config_sha256(manifest.training_config),
        "feature_versions": versions_projection(manifest.feature_versions),
        "runtime": runtime_projection(manifest.runtime),
        "heads": [
            {
                "head": h.head.value,
                "filename": f"{h.head.value}.cbm",
                "sha256": h.sha256,
                "size": h.size,
                "best_iteration": h.best_iteration,
                "tree_count": h.tree_count,
            }
            for h in manifest.heads
        ],
        "resolved_parameters": {
            head.value: value for head, value in manifest.resolved_parameters.items()
        },
        "metrics_sha256": manifest.metrics_sha256,
        "metrics_size": manifest.metrics_size,
    }


def parse_manifest(record: CanonicalObject) -> ModelBundleManifest:
    dataset = parse_summary(_object(record["dataset"]), _str(record["dataset_artifact_sha256"]))
    heads = []
    for value in _list(record["heads"]):
        h = _object(value)
        head = ModelHead(_str(h["head"]))
        if h["filename"] != f"{head.value}.cbm":
            raise ValueError("native filename mismatch")
        heads.append(
            ModelHeadMetadata(
                head,
                _str(h["sha256"]),
                _int(h["size"]),
                _int(h["best_iteration"]),
                _int(h["tree_count"]),
            )
        )
    parameters = _object(record["resolved_parameters"])
    manifest = ModelBundleManifest(
        dataset,
        dataset.descriptor,
        dataset.baseline_config,
        parse_training_config(_object(record["training_config"])),
        dataset.feature_versions,
        parse_runtime(_object(record["runtime"])),
        tuple(heads),
        {ModelHead(key): _object(value) for key, value in parameters.items()},
        _str(record["metrics_sha256"]),
        _int(record["metrics_size"]),
        _str(record["model_revision_sha256"]),
        _str(record["label_version"]),
        _str(record["projection_version"]),
        _str(record["split_version"]),
        _str(record["training_version"]),
        _str(record["bundle_version"]),
        _str(record["prediction_policy_version"]),
    )
    if canonical_json_bytes(manifest_projection(manifest)) != canonical_json_bytes(record):
        raise ValueError("model manifest schema or derived hashes mismatch")
    return manifest


class CatBoostStatePredictor:
    def __init__(
        self,
        manifest: ModelBundleManifest,
        artifact_sha256: str,
        models: Mapping[ModelHead, RateModel],
    ) -> None:
        require_sha256(artifact_sha256)
        if set(models) != set(ModelHead):
            raise ValueError("predictor requires three model heads")
        self._manifest, self._artifact = manifest, artifact_sha256
        self._models = MappingProxyType(dict(models))
        for metadata in manifest.heads:
            model = models[metadata.head]
            if (
                model.feature_names != tuple(c.name for c in manifest.descriptor.columns)
                or model.tree_count != metadata.tree_count
            ):
                raise ModelingError("model_native_schema_mismatch")

    def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
        horizon_period(state)
        vector = project_model_features(state, self._manifest.baseline_config)
        reason = (
            "invalid_feature_state"
            if not state.is_valid
            else ("no_future_time" if not state.seconds_remaining_game_ms else None)
        )
        if reason is not None:
            missing = PredictedRate(None, reason, False)
            rates = (missing, missing, missing)
        else:
            outputs = []
            for head in ModelHead:
                raw = self._models[head].predict((vector,), self._manifest.descriptor)
                if len(raw) != 1:
                    raise ModelingError("model_native_output_invalid")
                try:
                    outputs.append(encode_model_rate(raw[0]))
                except ValueError:
                    raise ModelingError("model_native_output_invalid") from None
            rates = (outputs[0], outputs[1], outputs[2])
        return StateModelPrediction(
            state,
            self._manifest.model_revision_sha256,
            self._artifact,
            model_input_sha256(vector),
            *rates,
        )


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, _TIMESTAMP)
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def _json(content: bytes) -> CanonicalObject:
    record = _object(cast(CanonicalValue, json.loads(content)))
    if canonical_json_bytes(record) + b"\n" != content:
        raise ValueError("noncanonical bundle JSON")
    return record


def _metrics_match(report: EvaluationReport, manifest: ModelBundleManifest) -> None:
    expected = tuple(
        (head, partition, scope)
        for head in ModelHead
        for partition in (TemporalPartition.VALIDATION, TemporalPartition.TEST)
        for scope in EvaluationScope
    )
    if tuple((c.head, c.partition, c.scope) for c in report.cohorts) != expected:
        raise ValueError("incomplete bundle evaluation")
    for head_index, head in enumerate(ModelHead):
        for partition in (TemporalPartition.VALIDATION, TemporalPartition.TEST):
            selected = tuple(
                c for c in report.cohorts if c.head is head and c.partition is partition
            )
            all_rows, regulation, ot = selected
            counts = next(c for c in manifest.dataset.counts if c.partition is partition)
            expected_head = counts.heads[head_index]
            if (
                (
                    all_rows.total_row_count,
                    all_rows.total_game_count,
                    all_rows.model.row_count,
                    all_rows.model.game_count,
                    all_rows.excluded_counts,
                )
                != (
                    counts.states,
                    counts.games,
                    expected_head.eligible_rows,
                    expected_head.eligible_games,
                    expected_head.excluded_counts,
                )
                or regulation.total_row_count + ot.total_row_count != all_rows.total_row_count
                or regulation.model.row_count + ot.model.row_count != all_rows.model.row_count
            ):
                raise ValueError("bundle evaluation coverage contradicts dataset")


class VerifiedModelBundleStore:
    def __init__(self) -> None:
        self._request: TrainModelsRequest | None = None
        self._ready: Path | None = None
        self._manifest: ModelBundleManifest | None = None
        self._artifact_sha256: str | None = None
        self._worker: asyncio.Task[None] | None = None
        self._published = False

    async def _io(self, operation: Callable[[], None]) -> None:
        if self._worker is not None:
            raise ModelingError("model_bundle_busy")
        worker = asyncio.create_task(asyncio.to_thread(operation))
        self._worker = worker
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        except ModelingError:
            raise
        except OSError:
            raise ModelingError("model_bundle_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, ArithmeticError, BadZipFile:
            raise ModelingError("model_bundle_invalid") from None
        finally:
            self._worker = None

    def load(self, path: Path) -> LoadedModelBundle:
        return self._load(path)

    def _load(self, path: Path, probes: tuple[ValidationProbe, ...] = ()) -> LoadedModelBundle:
        try:
            # Pin and copy one open descriptor. Native loading uses exactly these hashed bytes.
            with tempfile.TemporaryDirectory(prefix="nba-model-load-") as directory:
                root = Path(directory).resolve()
                if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(
                    "nba-model-load-"
                ):
                    raise ValueError("invalid owned model-loading workspace")
                pinned = root / "bundle.nbamodel"
                artifact, size = hashlib.sha256(), 0
                with path.open("rb") as source, pinned.open("xb") as output:
                    if os.fstat(source.fileno()).st_size > MAX_BUNDLE_BYTES:
                        raise ValueError("oversized model bundle")
                    while block := source.read(1_048_576):
                        size += len(block)
                        if size > MAX_BUNDLE_BYTES:
                            raise ValueError("oversized streamed model bundle")
                        artifact.update(block)
                        output.write(block)
                models: dict[ModelHead, RateModel] = {}
                with ZipFile(pinned) as archive:
                    if tuple(archive.namelist()) != _ENTRIES or archive.comment:
                        raise ValueError("unsupported bundle entries")
                    for info in archive.infolist():
                        ceiling = (
                            MAX_MODEL_BYTES if info.filename.endswith(".cbm") else MAX_JSON_BYTES
                        )
                        if (
                            info.compress_type != ZIP_STORED
                            or info.file_size > ceiling
                            or info.compress_size != info.file_size
                            or info.date_time != _TIMESTAMP
                            or info.is_dir()
                            or stat.S_ISLNK(info.external_attr >> 16)
                            or info.flag_bits & 1
                        ):
                            raise ValueError("unsupported ZIP entry metadata")
                    with archive.open("manifest.json") as stream:
                        manifest_bytes = stream.read(MAX_JSON_BYTES + 1)
                    if len(manifest_bytes) > MAX_JSON_BYTES:
                        raise ValueError("oversized manifest")
                    manifest = parse_manifest(_json(manifest_bytes))
                    with archive.open("metrics.json") as stream:
                        metrics = stream.read(MAX_JSON_BYTES + 1)
                    if (
                        len(metrics) != manifest.metrics_size
                        or len(metrics) > MAX_JSON_BYTES
                        or hashlib.sha256(metrics).hexdigest() != manifest.metrics_sha256
                    ):
                        raise ValueError("metrics content mismatch")
                    _metrics_match(parse_evaluation(_json(metrics)), manifest)
                    # Validate all native content before importing/loading any native model.
                    for metadata in manifest.heads:
                        filename = f"{metadata.head.value}.cbm"
                        digest, count = hashlib.sha256(), 0
                        with (
                            archive.open(filename) as stream,
                            (root / filename).open("xb") as output,
                        ):
                            while block := stream.read(1_048_576):
                                count += len(block)
                                if count > MAX_MODEL_BYTES:
                                    raise ValueError("oversized native content")
                                digest.update(block)
                                output.write(block)
                        if count != metadata.size or digest.hexdigest() != metadata.sha256:
                            raise ValueError("native content checksum mismatch")
                runtime = ensure_ml_dependencies()
                if runtime.catboost != manifest.runtime.catboost:
                    raise ModelingError("ml_engine_incompatible")
                for metadata in manifest.heads:
                    model = load_rate_model(
                        root / f"{metadata.head.value}.cbm", manifest.descriptor
                    )
                    if model.tree_count != metadata.tree_count:
                        raise ModelingError("model_native_schema_mismatch")
                    models[metadata.head] = model
                for probe in probes:
                    values = models[probe.head].predict(probe.vectors, manifest.descriptor)
                    encoded = tuple(encode_model_rate(value).value_scaled for value in values)
                    if encoded != probe.values_scaled:
                        raise ModelingError("model_probe_mismatch")
                artifact_digest = artifact.hexdigest()
                return LoadedModelBundle(
                    manifest,
                    artifact_digest,
                    CatBoostStatePredictor(manifest, artifact_digest, models),
                )
        except ModelingError:
            raise
        except OSError:
            raise ModelingError("model_bundle_io_error") from None
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            ArithmeticError,
            BadZipFile,
            RuntimeError,
        ):
            raise ModelingError("model_bundle_invalid") from None

    async def prepare(
        self,
        request: TrainModelsRequest,
        dataset: ValidatedTrainingDataset,
        worker_result: TrainingWorkerResult,
    ) -> ModelBundleManifest:
        if self._request is not None:
            raise ModelingError("model_bundle_already_started")
        self._request = request
        await self._io(lambda: self._prepare(dataset, worker_result))
        assert self._manifest is not None
        return self._manifest

    def _prepare(self, dataset: ValidatedTrainingDataset, result: TrainingWorkerResult) -> None:
        assert self._request is not None
        request = self._request
        metrics = canonical_json_bytes(evaluation_projection(result.evaluation)) + b"\n"
        heads = tuple(
            ModelHeadMetadata(
                model.head, model.sha256, model.size, model.best_iteration, model.tree_count
            )
            for model in result.models
        )
        manifest = ModelBundleManifest(
            dataset.summary,
            dataset.summary.descriptor,
            dataset.summary.baseline_config,
            request.config,
            dataset.summary.feature_versions,
            result.runtime,
            heads,
            result.resolved_parameters,
            hashlib.sha256(metrics).hexdigest(),
            len(metrics),
            model_semantic_revision(dataset.summary, request.config, result.runtime.catboost),
        )
        _metrics_match(result.evaluation, manifest)
        manifest_bytes = canonical_json_bytes(manifest_projection(manifest)) + b"\n"
        if len(manifest_bytes) > MAX_JSON_BYTES:
            raise ValueError("oversized model manifest")
        fd, name = tempfile.mkstemp(
            prefix=".nbamodel-", suffix=".tmp", dir=request.output_path.parent
        )
        os.close(fd)
        self._ready = Path(name)
        with ZipFile(self._ready, "w", compression=ZIP_STORED) as archive:
            archive.writestr(_zip_info("manifest.json"), manifest_bytes)
            archive.writestr(_zip_info("metrics.json"), metrics)
            for model in result.models:
                filename = f"{model.head.value}.cbm"
                if model.path.name != filename or model.path == self._ready:
                    raise ValueError("unexpected native model source filename")
                digest, count = hashlib.sha256(), 0
                with (
                    model.path.open("rb") as source,
                    archive.open(_zip_info(filename), "w") as output,
                ):
                    while block := source.read(1_048_576):
                        count += len(block)
                        if count > MAX_MODEL_BYTES:
                            raise ValueError("oversized native model source")
                        digest.update(block)
                        output.write(block)
                if count != model.size or digest.hexdigest() != model.sha256:
                    raise ValueError("native source bytes changed")
        with self._ready.open("r+b") as stream:
            os.fsync(stream.fileno())
        loaded = self._load(self._ready, result.probes)
        if loaded.manifest != manifest:
            raise ValueError("bundle readback manifest mismatch")
        self._manifest, self._artifact_sha256 = manifest, loaded.artifact_sha256

    def publish(self) -> str:
        if (
            self._request is None
            or self._ready is None
            or self._manifest is None
            or self._artifact_sha256 is None
            or self._published
        ):
            raise ModelingError("model_bundle_not_prepared")
        try:
            os.replace(self._ready, self._request.output_path)
        except OSError:
            raise ModelingError("model_bundle_io_error") from None
        self._published = True
        return self._artifact_sha256

    async def abort(self) -> None:
        await self._io(self._cleanup)

    def _cleanup(self) -> None:
        if self._ready is not None:
            self._ready.unlink(missing_ok=True)
