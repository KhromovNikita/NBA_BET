import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    LoadedModelBundle,
    ModelBundleManifest,
    ModelHeadMetadata,
    PredictModelsRequest,
    model_semantic_revision,
)
from nba_live_totals.application.modeling.worker_models import RuntimeVersions
from nba_live_totals.domain.features import GameStateFeature
from nba_live_totals.domain.identifiers import SeasonId
from nba_live_totals.domain.model_features import model_input_sha256, project_model_features
from nba_live_totals.domain.model_predictions import (
    PredictedRate,
    StateModelPrediction,
    encode_model_rate,
)
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TrainingConfig
from nba_live_totals.infrastructure.files.model_predictions import AtomicModelPredictionExporter
from tests.unit.application.modeling_fakes import MemoryModelSource
from tests.unit.infrastructure.ml.job_helpers import write_tiny_dataset


class FixedPredictor:
    def __init__(self, manifest: ModelBundleManifest) -> None:
        self.manifest = manifest

    def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
        reason = (
            "invalid_feature_state"
            if not state.is_valid
            else ("no_future_time" if not state.seconds_remaining_game_ms else None)
        )
        if reason is not None:
            missing = PredictedRate(None, reason, False)
            rates = (missing, missing, missing)
        else:
            rates = (encode_model_rate(100.0), encode_model_rate(1.25), encode_model_rate(1.0))
        vector = project_model_features(state, self.manifest.baseline_config)
        return StateModelPrediction(
            state, self.manifest.model_revision_sha256, "b" * 64, model_input_sha256(vector), *rates
        )


async def fixed_bundle(tmp_path: Path) -> LoadedModelBundle:
    dataset = await write_tiny_dataset(tmp_path / "fixture-training")
    config = TrainingConfig(iterations=12, early_stopping_rounds=3)
    runtime = RuntimeVersions("3.14.0", "1.2.10", "2.4.0", "fixture", "x86_64")
    heads = tuple(ModelHeadMetadata(head, "a" * 64, 1, 0, 1) for head in ModelHead)
    manifest = ModelBundleManifest(
        dataset.summary,
        dataset.summary.descriptor,
        dataset.summary.baseline_config,
        config,
        dataset.summary.feature_versions,
        runtime,
        heads,
        {head: {} for head in ModelHead},
        "c" * 64,
        1,
        model_semantic_revision(dataset.summary, config, runtime.catboost),
    )
    return LoadedModelBundle(manifest, "b" * 64, FixedPredictor(manifest))


async def write_fixed(
    path: Path,
    bundle: LoadedModelBundle,
    source: MemoryModelSource,
    exporter: AtomicModelPredictionExporter | None = None,
) -> None:
    writer = exporter or AtomicModelPredictionExporter()
    request = PredictModelsRequest((SeasonId("2025-26"),), path.parent / "fixture.nbamodel", path)
    reference = next(iter(source.references.values()))
    features = next(iter(source.inputs.values())).features
    await writer.begin(request, bundle)
    await writer.append_game(
        reference, (bundle.predictor.predict_state(state) for state in features.states)
    )
    await writer.prepare((reference,))
    writer.publish()
    await writer.abort()


async def test_literal_prediction_layout_masks_and_hashes(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)
    path = tmp_path / "predictions"
    await write_fixed(path, bundle, MemoryModelSource())
    golden = Path(__file__).parents[3] / "fixtures/nba/modeling/predictions.expected.jsonl"
    assert path.read_bytes() == golden.read_bytes()
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert records[1]["rates"]["pace"]["value_scaled"] == 100_000_000
    assert records[-1]["rates"]["pace"]["unavailable_reason"] == "no_future_time"
    assert (
        records[0]["body_sha256"]
        == hashlib.sha256(b"".join(path.read_bytes().splitlines(keepends=True)[1:])).hexdigest()
    )


async def test_cancel_joins_prediction_worker_before_temp_cleanup(tmp_path: Path) -> None:
    started, release = Event(), Event()

    class Paused(AtomicModelPredictionExporter):
        def _prepare(self) -> None:
            started.set()
            release.wait(5)
            super()._prepare()

    bundle = await fixed_bundle(tmp_path)
    source = MemoryModelSource()
    writer = Paused()
    path = tmp_path / "predictions"
    reference = next(iter(source.references.values()))
    features = next(iter(source.inputs.values())).features
    await writer.begin(PredictModelsRequest((SeasonId("2025-26"),), tmp_path / "m", path), bundle)
    await writer.append_game(
        reference, (bundle.predictor.predict_state(state) for state in features.states)
    )
    task = asyncio.create_task(writer.prepare((reference,)))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await writer.abort()
    assert not path.exists()


async def test_negative_flags_are_persisted_and_overflow_rolls_back(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)

    class Negative(FixedPredictor):
        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            row = super().predict_state(state)
            return (
                replace(row, home_ppp=encode_model_rate(-0.25))
                if row.home_ppp.value_scaled is not None
                else row
            )

    negative = replace(bundle, predictor=Negative(bundle.manifest))
    path = tmp_path / "negative"
    await write_fixed(path, negative, MemoryModelSource())
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert records[0]["negative_prediction_counts"]["home_ppp"] == 7
    assert records[1]["rates"]["home_ppp"] == {
        "value_scaled": 0,
        "unavailable_reason": None,
        "was_clipped_to_zero": True,
    }

    class Overflow(FixedPredictor):
        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            if state.state_number == 2:
                encode_model_rate(1e30)
            return super().predict_state(state)

    path = tmp_path / "old"
    path.write_bytes(b"old")
    writer = AtomicModelPredictionExporter()
    with pytest.raises(ModelingError, match="model_prediction_invalid"):
        await write_fixed(
            path, replace(bundle, predictor=Overflow(bundle.manifest)), MemoryModelSource(), writer
        )
    await writer.abort()
    assert path.read_bytes() == b"old"


async def test_denied_prediction_replace_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await fixed_bundle(tmp_path)
    source = MemoryModelSource()
    writer = AtomicModelPredictionExporter()
    path = tmp_path / "old"
    path.write_bytes(b"old")

    def deny(*args: object) -> None:
        raise PermissionError("synthetic sharing lock")

    monkeypatch.setattr("nba_live_totals.infrastructure.files.model_predictions.os.replace", deny)
    with pytest.raises(ModelingError, match="model_prediction_io_error"):
        await write_fixed(path, bundle, source, writer)
    await writer.abort()
    assert path.read_bytes() == b"old"
