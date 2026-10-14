import hashlib
import importlib
import json
from contextlib import nullcontext
from dataclasses import replace
from importlib import util
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import TrainModelsRequest
from nba_live_totals.domain.canonical import canonical_json_bytes, canonical_json_sha256
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TrainingConfig
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from nba_live_totals.infrastructure.ml.catboost_adapter import CatBoostRateModelTrainer
from nba_live_totals.infrastructure.ml.training_worker import run_training_job
from tests.unit.domain.modeling_helpers import make_target_case
from tests.unit.infrastructure.ml.job_helpers import write_tiny_dataset
from tests.unit.infrastructure.ml.test_training_worker import make_job

pytestmark = pytest.mark.skipif(
    util.find_spec("catboost") is None, reason="optional ml extra is not installed"
)


def make_train_request(path: Path) -> TrainModelsRequest:
    return TrainModelsRequest(
        path.parent / "training.jsonl", path, TrainingConfig(iterations=12, early_stopping_rounds=3)
    )


async def make_bundle(path: Path) -> Path:
    dataset = await write_tiny_dataset(path.parent / "training.jsonl")
    result = run_training_job(
        make_job(dataset, path.parent / "worker"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    store = VerifiedModelBundleStore()
    await store.prepare(make_train_request(path), dataset, result)
    store.publish()
    await store.abort()
    return path


async def test_native_bundle_round_trip_preserves_encoded_predictions(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training.jsonl")
    result = run_training_job(
        make_job(dataset, tmp_path / "worker"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    path = tmp_path / "m.nbamodel"
    store = VerifiedModelBundleStore()
    manifest = await store.prepare(make_train_request(path), dataset, result)
    digest = store.publish()
    loaded = store.load(path)
    assert loaded.artifact_sha256 == digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert loaded.manifest.model_revision_sha256 == manifest.model_revision_sha256
    assert loaded.manifest == manifest
    state, _ = make_target_case()
    prediction = loaded.predictor.predict_state(state)
    assert prediction.home_ppp.value_scaled is not None
    assert prediction.input_sha256
    for index, head in enumerate(ModelHead):
        assert getattr(prediction, head.value).value_scaled == result.probes[index].values_scaled[2]
    terminal = replace(state, seconds_remaining_game_ms=0, seconds_remaining_period_ms=0)
    assert loaded.predictor.predict_state(terminal).pace.unavailable_reason == "no_future_time"
    with ZipFile(path) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "metrics.json",
            "pace.cbm",
            "home_ppp.cbm",
            "away_ppp.cbm",
        ]
        assert all(
            info.compress_type == ZIP_STORED and info.date_time == (1980, 1, 1, 0, 0, 0)
            for info in archive.infolist()
        )
    await store.abort()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "duplicate",
        "traversal",
        "compression",
        "symlink",
        "engine",
        "wrong_hash",
        "oversized",
        "native_corrupt",
        "source_hash",
        "semantic_body",
    ],
)
async def test_loader_rejects_valid_crc_but_invalid_bundle_contract(
    tmp_path: Path, mutation: str
) -> None:
    path = await make_bundle(tmp_path / "m.nbamodel")
    with ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    manifest = json.loads(entries[0][1])
    if mutation == "engine":
        manifest["runtime"]["catboost"] = "0.0.0"
    if mutation == "source_hash":
        manifest["dataset"]["source_sha256"] = "0" * 64
    if mutation == "semantic_body":
        manifest["dataset"]["body_sha256"] = "0" * 64
        manifest["dataset"]["semantic_body_sha256"] = "0" * 64
    if mutation == "wrong_hash":
        manifest["heads"][0]["sha256"] = "0" * 64
    if mutation == "oversized":
        manifest["heads"][0]["size"] = 128 * 1024**2 + 1
    if mutation == "native_corrupt":
        entries[2] = ("pace.cbm", b"wrong native data")
        manifest["heads"][0]["sha256"] = hashlib.sha256(entries[2][1]).hexdigest()
        manifest["heads"][0]["size"] = len(entries[2][1])
    entries[0] = (
        "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )
    if mutation == "extra":
        entries.append(("extra", b"extra"))
    if mutation == "duplicate":
        entries.append(entries[0])
    if mutation == "traversal":
        entries[2] = ("../pace.cbm", entries[2][1])
    warning = (
        pytest.warns(UserWarning, match="Duplicate name")
        if mutation == "duplicate"
        else nullcontext()
    )
    with warning, ZipFile(path, "w") as archive:
        for name, content in entries:
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED if mutation == "compression" else ZIP_STORED
            info.external_attr = (0o120777 if mutation == "symlink" else 0o100600) << 16
            archive.writestr(info, content)
    with pytest.raises(ModelingError):
        VerifiedModelBundleStore().load(path)


async def test_probe_mismatch_and_denied_replace_preserve_previous_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training.jsonl")
    result = run_training_job(
        make_job(dataset, tmp_path / "worker"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    path = tmp_path / "old.nbamodel"
    path.write_bytes(b"old")
    bad_probe = replace(
        result.probes[0], values_scaled=tuple(v + 1 for v in result.probes[0].values_scaled)
    )
    bad = replace(result, probes=(bad_probe, *result.probes[1:]))
    store = VerifiedModelBundleStore()
    with pytest.raises(ModelingError, match="model_probe_mismatch"):
        await store.prepare(make_train_request(path), dataset, bad)
    await store.abort()
    assert path.read_bytes() == b"old"
    store = VerifiedModelBundleStore()
    await store.prepare(make_train_request(path), dataset, result)

    def deny(*args: object) -> None:
        raise PermissionError("synthetic sharing lock")

    monkeypatch.setattr("nba_live_totals.infrastructure.files.model_bundle.os.replace", deny)
    with pytest.raises(ModelingError, match="model_bundle_io_error"):
        store.publish()
    await store.abort()
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("mutation", ["native_names", "descriptor_names"])
async def test_valid_zip_crc_with_wrong_native_columns_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    path = await make_bundle(tmp_path / "m.nbamodel")
    with ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    manifest = json.loads(entries[0][1])
    if mutation == "native_names":
        native_path = tmp_path / "wrong.cbm"
        native_path.write_bytes(entries[2][1])
        native = importlib.import_module("catboost").CatBoostRegressor()
        native.load_model(str(native_path), format="cbm")
        native.set_feature_names(["wrong.period", *native.feature_names_[1:]])
        native.save_model(str(native_path), format="cbm")
        changed = native_path.read_bytes()
        entries[2] = ("pace.cbm", changed)
        manifest["heads"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
        manifest["heads"][0]["size"] = len(changed)
    else:
        for record in (manifest, manifest["dataset"]):
            record["descriptor"]["columns"][0]["name"] = "wrong.period"
            record["descriptor_sha256"] = canonical_json_sha256(record["descriptor"])
    entries[0] = ("manifest.json", canonical_json_bytes(manifest) + b"\n")
    with ZipFile(path, "w") as archive:
        for name, content in entries:
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, content)
    with pytest.raises(ModelingError):
        VerifiedModelBundleStore().load(path)


async def test_same_worker_bytes_give_identical_bundle_at_different_paths(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training.jsonl")
    result = run_training_job(
        make_job(dataset, tmp_path / "worker"),
        CanonicalTrainingDatasetReader(),
        CatBoostRateModelTrainer(),
    )
    first, second = tmp_path / "first.nbamodel", tmp_path / "second.nbamodel"
    for path in (first, second):
        store = VerifiedModelBundleStore()
        await store.prepare(make_train_request(path), dataset, result)
        store.publish()
        await store.abort()
    assert first.read_bytes() == second.read_bytes()
