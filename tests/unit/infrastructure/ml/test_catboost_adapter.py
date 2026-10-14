import math
import subprocess
import sys
from dataclasses import replace
from importlib import util
from pathlib import Path
from unittest.mock import Mock

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.model_features import model_feature_descriptor
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition, TrainingConfig
from nba_live_totals.infrastructure.ml import catboost_adapter
from nba_live_totals.infrastructure.ml.catboost_adapter import (
    CatBoostRateModel,
    CatBoostRateModelTrainer,
    ensure_ml_dependencies,
    load_rate_model,
)
from tests.unit.infrastructure.ml.ml_helpers import tiny_head_rows

requires_ml = pytest.mark.skipif(
    util.find_spec("catboost") is None or util.find_spec("numpy") is None,
    reason="optional ml extra is not installed",
)


@requires_ml
def test_native_cpu_fit_respects_frozen_feature_names_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = TrainingConfig(iterations=12, early_stopping_rounds=3)
    descriptor = model_feature_descriptor(BaselineConfig())
    train = tiny_head_rows(TemporalPartition.TRAIN, ModelHead.PACE)
    validation = tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE)
    model = CatBoostRateModelTrainer().fit_head(train, validation, descriptor, config)
    assert model.feature_names == tuple(column.name for column in descriptor.columns)
    assert 1 <= model.tree_count <= 12
    for name, expected in {
        "task_type": "CPU",
        "loss_function": "RMSE",
        "eval_metric": "RMSE",
        "thread_count": 1,
        "has_time": True,
        "boosting_type": "Plain",
        "bootstrap_type": "No",
        "nan_mode": "Min",
        "random_strength": 0,
    }.items():
        assert model.resolved_parameters[name] == expected
    vectors = tuple(row.vector for row in validation.iter_rows())
    predicted = model.predict(vectors, descriptor)
    assert len(predicted) == len(vectors)
    repeated = CatBoostRateModelTrainer().fit_head(train, validation, descriptor, config)
    assert repeated.predict(vectors, descriptor) == predicted
    model_path = tmp_path / "pace.cbm"
    model.save(model_path)
    loaded = load_rate_model(model_path, descriptor)
    assert loaded.feature_names == model.feature_names
    assert loaded.tree_count == model.tree_count
    assert loaded.predict(vectors, descriptor) == predicted
    assert not (tmp_path / "catboost_info").exists()


@requires_ml
def test_wrong_partition_head_descriptor_and_ceiling_reject_before_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = model_feature_descriptor(BaselineConfig())
    train = tiny_head_rows(TemporalPartition.TRAIN, ModelHead.PACE)
    validation = tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE)
    for wrong_train, wrong_validation, config in (
        (validation, validation, TrainingConfig()),
        (train, tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.HOME_PPP), TrainingConfig()),
        (train, validation, TrainingConfig(max_rows_per_partition=1)),
    ):
        with pytest.raises(ModelingError):
            CatBoostRateModelTrainer().fit_head(wrong_train, wrong_validation, descriptor, config)
    with pytest.raises(ModelingError):
        CatBoostRateModelTrainer().fit_head(
            train,
            validation,
            model_feature_descriptor(BaselineConfig(ppp_prior_possessions=40)),
            TrainingConfig(),
        )


def test_missing_extra_is_lazy_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(util, "find_spec", lambda name: None)
    with pytest.raises(ModelingError, match="ml_dependencies_missing"):
        ensure_ml_dependencies()


@requires_ml
def test_native_load_rejects_wrong_named_projection(tmp_path: Path) -> None:
    descriptor = model_feature_descriptor(BaselineConfig())
    model = CatBoostRateModelTrainer().fit_head(
        tiny_head_rows(TemporalPartition.TRAIN, ModelHead.HOME_PPP),
        tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.HOME_PPP),
        descriptor,
        TrainingConfig(iterations=3, early_stopping_rounds=1),
    )
    path = tmp_path / "home.cbm"
    model.save(path)
    wrong = replace(
        descriptor,
        columns=tuple(
            replace(c, name="renamed") if i == 0 else c for i, c in enumerate(descriptor.columns)
        ),
    )
    with pytest.raises(ModelingError, match="model_native_schema_mismatch"):
        load_rate_model(path, wrong)


@requires_ml
def test_missing_inputs_become_nan_and_all_pools_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = model_feature_descriptor(BaselineConfig())
    real_module = catboost_adapter._module()
    module = Mock(wraps=real_module)
    monkeypatch.setattr(catboost_adapter, "_module", lambda: module)
    model = CatBoostRateModelTrainer().fit_head(
        tiny_head_rows(TemporalPartition.TRAIN, ModelHead.PACE),
        tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE),
        descriptor,
        TrainingConfig(iterations=3, early_stopping_rounds=1),
    )
    assert module.Pool.call_count == 2
    train_call = module.Pool.call_args_list[0]
    index = tuple(c.name for c in descriptor.columns).index("live.current_pace")
    assert math.isnan(train_call.args[0][0][index])
    assert train_call.kwargs["feature_names"] == [c.name for c in descriptor.columns]
    assert all(weight > 0 for weight in train_call.kwargs["weight"])
    assert module.CatBoostRegressor.call_args.kwargs["allow_writing_files"] is False
    for pool_call in module.Pool.call_args_list:
        assert pool_call.kwargs.get("thread_count") == 1
    vector = next(tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE).iter_rows()).vector
    model.predict((vector,), descriptor)
    assert module.Pool.call_count == 3
    assert module.Pool.call_args.kwargs.get("thread_count") == 1


@pytest.mark.parametrize("raw", [[float("nan")], [float("inf")], [1e30], [], [True], [[1.0]]])
@requires_ml
def test_malformed_native_output_fails_entire_prediction(raw: list[object]) -> None:
    descriptor = model_feature_descriptor(BaselineConfig())
    native = Mock(feature_names_=[c.name for c in descriptor.columns], tree_count_=1)
    native.predict.return_value.tolist.return_value = raw
    model = CatBoostRateModel(native, descriptor)
    vector = next(tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE).iter_rows()).vector
    with pytest.raises(ModelingError):
        model.predict((vector,), descriptor)


def test_importing_adapter_does_not_import_optional_native_modules() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nba_live_totals.infrastructure.ml.catboost_adapter; "
            "assert 'catboost' not in sys.modules; assert 'numpy' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_inference_explicitly_uses_the_fixed_single_thread_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = model_feature_descriptor(BaselineConfig())
    native = Mock(feature_names_=[c.name for c in descriptor.columns], tree_count_=1)
    native.predict.return_value.tolist.return_value = [1.0]
    module = Mock()
    monkeypatch.setattr(catboost_adapter, "_module", lambda: module)
    model = CatBoostRateModel(native, descriptor)
    vector = next(tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE).iter_rows()).vector
    model.predict((vector,), descriptor)
    assert native.predict.call_args.kwargs["thread_count"] == 1
    assert module.Pool.call_args.kwargs.get("thread_count") == 1
