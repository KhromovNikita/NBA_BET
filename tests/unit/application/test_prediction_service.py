import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import util
from pathlib import Path
from threading import Event
from weakref import ReferenceType, ref

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    LoadedModelBundle,
    ModelSourceReference,
    PredictModelsRequest,
)
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.ports import ModelPredictor
from nba_live_totals.application.modeling.prediction_service import PredictModelsService
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureVersions,
    GameStateFeature,
    TeamPregameFeatures,
)
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.model_predictions import StateModelPrediction
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.model_predictions import AtomicModelPredictionExporter
from tests.unit.application.modeling_fakes import (
    MemoryModelSource,
    make_model_source_reference,
    remap_training_input,
)
from tests.unit.domain.modeling_helpers import make_training_input
from tests.unit.infrastructure.files.test_model_bundle import make_bundle

pytestmark = pytest.mark.skipif(
    util.find_spec("catboost") is None, reason="optional ml extra is not installed"
)


def make_prediction_request(path: Path) -> PredictModelsRequest:
    return PredictModelsRequest((SeasonId("2025-26"),), path.parent / "m.nbamodel", path)


async def make_prediction_service(tmp_path: Path) -> tuple[MemoryModelSource, PredictModelsService]:
    await make_bundle(tmp_path / "m.nbamodel")
    source = MemoryModelSource()
    return source, PredictModelsService(
        source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
    )


async def test_default_owned_io_still_joins_uncooperative_operation_on_repeated_cancel() -> None:
    started, gate, finished = Event(), Event(), Event()

    def operation() -> int:
        started.set()
        try:
            if not gate.wait(5):
                raise AssertionError("prediction I/O barrier not released")
            return 7
        finally:
            finished.set()

    pending = asyncio.create_task(run_owned_io(operation))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not finished.is_set()
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set()


async def test_predict_uses_feature_path_never_future_targets(tmp_path: Path) -> None:
    source, service = await make_prediction_service(tmp_path)
    result = await service.predict(make_prediction_request(tmp_path / "predictions.jsonl"))
    assert source.events.count("read_training") == 0
    assert result.states == 8 and result.available == 7 and result.unavailable == 1
    assert result.exit_code == 1
    assert source.events == ["select", "resolve", "read_features", "recheck", "recheck"]


async def test_final_pointer_race_preserves_old_prediction_file(tmp_path: Path) -> None:
    source, service = await make_prediction_service(tmp_path)
    source.fail_final = True
    path = tmp_path / "predictions.jsonl"
    path.write_bytes(b"old")
    with pytest.raises(ModelingError, match="model_source_changed"):
        await service.predict(make_prediction_request(path))
    assert path.read_bytes() == b"old"


async def test_future_state_mutation_keeps_current_rates_and_input_hash(tmp_path: Path) -> None:
    source, service = await make_prediction_service(tmp_path)
    first = tmp_path / "first"
    await service.predict(make_prediction_request(first))
    game_id = next(iter(source.inputs))
    inputs = source.inputs[game_id]
    state = inputs.features.states[6]
    changed = replace(
        state,
        home_player_ids=(),
        home_lineup_status=LineupStatus.INVALID,
        home_invalid_reason="missing_substitution",
        home_invalid_since=state.boundary,
    )
    features = FeatureDataset(
        game_id, (*inputs.features.states[:6], changed, inputs.features.states[7]), ()
    )
    source.inputs[game_id] = replace(inputs, features=features)
    source.references[game_id] = make_model_source_reference(inputs.game, features)
    second = tmp_path / "second"
    await PredictModelsService(
        source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
    ).predict(make_prediction_request(second))
    a = [json.loads(line) for line in first.read_bytes().splitlines()]
    b = [json.loads(line) for line in second.read_bytes().splitlines()]
    assert a[3]["rates"] == b[3]["rates"]
    assert a[3]["input_sha256"] == b[3]["input_sha256"]
    assert a[0]["sources"] != b[0]["sources"]
    assert b[7]["rates"]["pace"]["unavailable_reason"] == "invalid_feature_state"


async def test_same_loaded_model_gives_equal_prediction_bytes_at_other_paths(
    tmp_path: Path,
) -> None:
    source, service = await make_prediction_service(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    await service.predict(make_prediction_request(first))
    await PredictModelsService(
        source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
    ).predict(make_prediction_request(second))
    assert first.read_bytes() == second.read_bytes()


async def test_empty_source_does_not_publish_prediction_file(tmp_path: Path) -> None:
    source, service = await make_prediction_service(tmp_path)
    source.empty = True
    path = tmp_path / "predictions.jsonl"
    with pytest.raises(ModelingError, match="no_eligible_games"):
        await service.predict(make_prediction_request(path))
    assert not path.exists()


async def test_cold_valid_history_predicts_and_version_mismatch_fails_before_read(
    tmp_path: Path,
) -> None:
    source, _ = await make_prediction_service(tmp_path)
    game_id = next(iter(source.inputs))
    inputs = source.inputs[game_id]
    features = FeatureDataset(
        game_id,
        tuple(
            replace(
                state,
                home_pregame=TeamPregameFeatures(10, True),
                away_pregame=TeamPregameFeatures(20, False),
            )
            for state in inputs.features.states
        ),
        (),
    )
    source.inputs[game_id] = replace(inputs, features=features)
    source.references[game_id] = make_model_source_reference(inputs.game, features)
    path = tmp_path / "cold"
    result = await PredictModelsService(
        source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
    ).predict(make_prediction_request(path))
    assert result.available == 7
    ref = source.references[game_id]
    token = replace(
        ref.reference.attempt.input_token,
        versions=FeatureVersions(formula_version="unsupported-v1"),
    )
    attempt = replace(
        ref.reference.attempt, input_token=token, feature_revision_id=token.feature_revision_id
    )
    source.references[game_id] = replace(ref, reference=replace(ref.reference, attempt=attempt))
    old = tmp_path / "old"
    old.write_bytes(b"old")
    before = source.events.count("read_features")
    with pytest.raises(ModelingError, match="model_feature_versions_incompatible"):
        await PredictModelsService(
            source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
        ).predict(make_prediction_request(old))
    assert source.events.count("read_features") == before
    assert old.read_bytes() == b"old"


async def test_each_feature_dataset_is_released_before_next_game(tmp_path: Path) -> None:
    await make_bundle(tmp_path / "m.nbamodel")

    @dataclass(frozen=True, slots=True, weakref_slot=True)
    class TrackedFeatures(FeatureDataset):
        pass

    class TrackingSource(MemoryModelSource):
        previous: ReferenceType[TrackedFeatures] | None = None

        async def resolve(self, game_id: GameId) -> ModelSourceReference:
            assert self.previous is None or self.previous() is None
            return await super().resolve(game_id)

        async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
            original = await super().read_features(source)
            tracked = TrackedFeatures(
                original.game_id, original.states, original.players, original.issues
            )
            self.previous = ref(tracked)
            return tracked

    first = make_training_input()
    second = remap_training_input(first, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC))
    source = TrackingSource((first, second))
    result = await PredictModelsService(
        source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
    ).predict(make_prediction_request(tmp_path / "out"))
    assert result.games == 2 and result.states == 16


async def test_duplicate_games_are_rejected_before_export(tmp_path: Path) -> None:
    source, _ = await make_prediction_service(tmp_path)

    class DuplicateSource(MemoryModelSource):
        async def select_games(
            self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
        ) -> tuple[GameId, ...]:
            selected = await super().select_games(seasons, game_id)
            return (*selected, *selected)

    path = tmp_path / "old"
    path.write_bytes(b"old")
    with pytest.raises(ModelingError, match="model_duplicate_games"):
        await PredictModelsService(
            DuplicateSource(tuple(source.inputs.values())),
            VerifiedModelBundleStore(),
            AtomicModelPredictionExporter(),
        ).predict(make_prediction_request(path))
    assert path.read_bytes() == b"old"


async def test_canceled_native_prediction_joins_before_removing_files(tmp_path: Path) -> None:
    source, _ = await make_prediction_service(tmp_path)
    started, release = Event(), Event()

    class PausedPredictor:
        def __init__(self, inner: ModelPredictor) -> None:
            self.inner = inner

        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            started.set()
            release.wait(5)
            return self.inner.predict_state(state)

    class PausedStore(VerifiedModelBundleStore):
        def load(self, path: Path) -> LoadedModelBundle:
            bundle = super().load(path)
            return replace(bundle, predictor=PausedPredictor(bundle.predictor))

    writer = AtomicModelPredictionExporter()
    path = tmp_path / "old"
    path.write_bytes(b"old")
    task = asyncio.create_task(
        PredictModelsService(source, PausedStore(), writer).predict(make_prediction_request(path))
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert writer._body is not None and writer._body.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not writer._body.exists()
    assert path.read_bytes() == b"old"
