"""Narrow typed boundary for the pinned optional CatBoost CPU runtime."""

from __future__ import annotations

import importlib
import math
import platform
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib import util
from pathlib import Path
from typing import Protocol, cast

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import HeadRows
from nba_live_totals.application.modeling.worker_models import CATBOOST_VERSION, RuntimeVersions
from nba_live_totals.domain.canonical import CanonicalObject, CanonicalValue
from nba_live_totals.domain.model_features import FeatureProjectionDescriptor, ModelFeatureVector
from nba_live_totals.domain.model_predictions import encode_model_rate
from nba_live_totals.domain.model_targets import HeadTarget, ModelHead
from nba_live_totals.domain.model_training import (
    TemporalPartition,
    TrainingConfig,
    balanced_weights,
    decimal_string,
    training_config_projection,
)

type NativeParameter = str | int | float | bool


class _NativeArray(Protocol):
    def tolist(self) -> list[object]: ...


class _NativeRegressor(Protocol):
    feature_names_: list[str]
    tree_count_: int

    def fit(
        self,
        X: object,
        *,
        eval_set: object,
        early_stopping_rounds: int,
        use_best_model: bool,
        verbose: bool,
    ) -> object: ...
    def predict(self, data: object, *, thread_count: int) -> _NativeArray: ...
    def get_best_iteration(self) -> int: ...
    def get_all_params(self) -> dict[str, object]: ...
    def save_model(self, path: str, *, format: str) -> None: ...
    def load_model(self, path: str, *, format: str) -> None: ...


class _CatBoostModule(Protocol):
    __version__: str

    def CatBoostRegressor(self, **parameters: NativeParameter) -> _NativeRegressor: ...
    def Pool(
        self,
        data: list[list[float]],
        *,
        label: list[float] | None = None,
        weight: list[float] | None = None,
        feature_names: list[str],
    ) -> object: ...


class _NumpyModule(Protocol):
    __version__: str


def ensure_ml_dependencies() -> RuntimeVersions:
    try:
        if util.find_spec("catboost") is None or util.find_spec("numpy") is None:
            raise ModelingError("ml_dependencies_missing")
        catboost = cast(_CatBoostModule, importlib.import_module("catboost"))
        numpy = cast(_NumpyModule, importlib.import_module("numpy"))
        if catboost.__version__ != CATBOOST_VERSION:
            raise ModelingError("ml_engine_incompatible")
        return RuntimeVersions(
            platform.python_version(),
            catboost.__version__,
            numpy.__version__,
            platform.system(),
            platform.machine(),
        )
    except ImportError, ModuleNotFoundError:
        raise ModelingError("ml_dependencies_missing") from None
    except ValueError:
        raise ModelingError("ml_engine_incompatible") from None


def _module() -> _CatBoostModule:
    ensure_ml_dependencies()
    return cast(_CatBoostModule, importlib.import_module("catboost"))


def _matrix(
    vectors: Sequence[ModelFeatureVector], descriptor: FeatureProjectionDescriptor
) -> list[list[float]]:
    result = []
    for vector in vectors:
        if (
            not isinstance(vector, ModelFeatureVector)
            or vector.descriptor_sha256 != descriptor.sha256
        ):
            raise ModelingError("model_projection_mismatch")
        values = []
        for value, column in zip(vector.values, descriptor.columns, strict=True):
            decoded = float("nan") if value is None else value / column.divisor
            if value is not None and not math.isfinite(decoded):
                raise ModelingError("model_numeric_input_invalid")
            values.append(decoded)
        result.append(values)
    return result


def _diagnostic(value: object) -> CanonicalValue:
    if value is None or type(value) in (str, int, bool):
        return cast(str | int | bool | None, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ModelingError("model_native_parameters_invalid")
        return decimal_string(Decimal(str(value)))
    if type(value) is list or type(value) is tuple:
        return [_diagnostic(v) for v in cast(Sequence[object], value)]
    if type(value) is dict:
        mapping = cast(Mapping[object, object], value)
        if any(type(key) is not str for key in mapping):
            raise ModelingError("model_native_parameters_invalid")
        return {cast(str, key): _diagnostic(v) for key, v in mapping.items()}
    raise ModelingError("model_native_parameters_invalid")


class CatBoostRateModel:
    def __init__(
        self,
        native: _NativeRegressor,
        descriptor: FeatureProjectionDescriptor,
        parameters: Mapping[str, NativeParameter] | None = None,
    ) -> None:
        self._native, self._descriptor = native, descriptor
        if (
            tuple(native.feature_names_) != tuple(c.name for c in descriptor.columns)
            or type(native.tree_count_) is not int
            or not 1 <= native.tree_count_ <= 100_000
        ):
            raise ModelingError("model_native_schema_mismatch")
        self._parameters = dict(parameters or {})

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self._native.feature_names_)

    @property
    def best_iteration(self) -> int:
        # Loaded CBM may return -1. The authoritative fitted value lives in the manifest.
        return self._native.get_best_iteration()

    @property
    def tree_count(self) -> int:
        return self._native.tree_count_

    @property
    def resolved_parameters(self) -> CanonicalObject:
        native = dict(self._parameters)
        actual = self._native.get_all_params()
        values: dict[str, object] = dict(native)
        values.update(actual)
        for key in ("metadata", "train_dir", "snapshot_file", "input_borders", "output_borders"):
            values.pop(key, None)
        return {key: _diagnostic(value) for key, value in values.items()}

    def predict(
        self, vectors: Sequence[ModelFeatureVector], descriptor: FeatureProjectionDescriptor
    ) -> tuple[float, ...]:
        if descriptor != self._descriptor:
            raise ModelingError("model_projection_mismatch")
        if not vectors:
            return ()
        try:
            pool = _module().Pool(
                _matrix(vectors, descriptor), feature_names=list(self.feature_names)
            )
            raw = self._native.predict(pool, thread_count=1).tolist()
            if (
                type(raw) is not list
                or len(raw) != len(vectors)
                or any(type(value) is not float for value in raw)
            ):
                raise ModelingError("model_native_output_invalid")
            result = cast(tuple[float, ...], tuple(raw))
            for value in result:
                encode_model_rate(value)
            return result
        except ModelingError:
            raise
        except Exception:
            raise ModelingError("model_native_prediction_failed") from None

    def save(self, path: Path) -> None:
        try:
            self._native.save_model(str(path), format="cbm")
        except Exception:
            raise ModelingError("model_native_save_failed") from None


def _pool(
    rows: HeadRows, descriptor: FeatureProjectionDescriptor, module: _CatBoostModule, max_rows: int
) -> object:
    if not 1 <= rows.row_count <= max_rows:
        raise ModelingError("model_partition_row_limit")
    vectors: list[ModelFeatureVector] = []
    targets: list[float] = []
    game_ids = []
    counts = Counter(rows.game_counts)
    previous = None
    index = tuple(ModelHead).index(rows.head)
    for row in rows.iter_rows():
        key = (row.started_at, row.game_id.value, row.state_number)
        if (
            row.partition is not rows.partition
            or not row.eligibility[index].eligible
            or (previous is not None and key <= previous)
        ):
            raise ModelingError("model_head_rows_invalid")
        target = cast(HeadTarget, getattr(row.targets, rows.head.value))
        if target.value_scaled is None or len(vectors) >= rows.row_count:
            raise ModelingError("model_head_rows_invalid")
        counts[row.game_id] -= 1
        vectors.append(row.vector)
        targets.append(target.value_scaled / 1_000_000)
        game_ids.append(row.game_id)
        previous = key
    if len(vectors) != rows.row_count or any(count != 0 for count in counts.values()):
        raise ModelingError("model_head_rows_invalid")
    weights = [float(weight) for weight in balanced_weights(game_ids)]
    if any(not math.isfinite(value) for value in targets) or any(
        not math.isfinite(value) or value <= 0 for value in weights
    ):
        raise ModelingError("model_numeric_input_invalid")
    return module.Pool(
        _matrix(vectors, descriptor),
        label=targets,
        weight=weights,
        feature_names=[c.name for c in descriptor.columns],
    )


class CatBoostRateModelTrainer:
    def fit_head(
        self,
        train: HeadRows,
        validation: HeadRows,
        descriptor: FeatureProjectionDescriptor,
        config: TrainingConfig,
    ) -> CatBoostRateModel:
        if (
            train.partition is not TemporalPartition.TRAIN
            or validation.partition is not TemporalPartition.VALIDATION
            or train.head is not validation.head
        ):
            raise ModelingError("model_training_partition_invalid")
        if (
            train.row_count > config.max_rows_per_partition
            or validation.row_count > config.max_rows_per_partition
        ):
            raise ModelingError("model_partition_row_limit")
        if not math.isfinite(float(config.learning_rate)) or float(config.learning_rate) <= 0:
            raise ModelingError("model_numeric_input_invalid")
        module = _module()
        train_pool = _pool(train, descriptor, module, config.max_rows_per_partition)
        validation_pool = _pool(validation, descriptor, module, config.max_rows_per_partition)
        canonical = training_config_projection(config)
        parameters: dict[str, NativeParameter] = {
            key: cast(NativeParameter, value)
            for key, value in canonical.items()
            if key not in ("version", "max_rows_per_partition", "early_stopping_rounds")
        }
        parameters["learning_rate"] = float(config.learning_rate)
        parameters["l2_leaf_reg"] = float(config.l2_leaf_reg)
        try:
            native = module.CatBoostRegressor(**parameters)
            native.fit(
                train_pool,
                eval_set=validation_pool,
                early_stopping_rounds=config.early_stopping_rounds,
                use_best_model=True,
                verbose=False,
            )
            model = CatBoostRateModel(native, descriptor, parameters)
            if not 0 <= model.best_iteration < model.tree_count <= config.iterations:
                raise ModelingError("model_native_selection_invalid")
            return model
        except ModelingError:
            raise
        except Exception:
            raise ModelingError("model_native_training_failed") from None


def load_rate_model(path: Path, descriptor: FeatureProjectionDescriptor) -> CatBoostRateModel:
    module = _module()
    try:
        native = module.CatBoostRegressor()
        native.load_model(str(path), format="cbm")
        return CatBoostRateModel(native, descriptor)
    except ModelingError:
        raise
    except Exception:
        raise ModelingError("model_native_load_failed") from None
