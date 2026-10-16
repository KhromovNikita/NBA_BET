"""Sequential causal inference and publication, with native-free port tests."""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import util
from pathlib import Path
from threading import Event
from weakref import ReferenceType, ref

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import LoadedModelBundle, ModelSourceReference
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import SimulateTotalsResult
from nba_live_totals.application.simulation.service import SimulateTotalsService
from nba_live_totals.domain.features import FeatureDataset, GameStateFeature
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_predictions import StateModelPrediction
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
)
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.simulation_export import (
    AtomicSimulationExporter,
    CanonicalSimulationReader,
)
from nba_live_totals.infrastructure.simulation.executor import OwnedSimulationExecutor
from nba_live_totals.infrastructure.simulation.numpy_engine import NumpySimulationEngine
from tests.unit.application.modeling_fakes import MemoryModelSource, remap_training_input
from tests.unit.application.simulation_fakes import (
    MemorySimulationExecutor,
    MemorySimulationSource,
    make_constant_outcome,
    make_simulation_request,
)
from tests.unit.domain.modeling_helpers import make_training_input
from tests.unit.infrastructure.files.test_model_bundle import make_bundle
from tests.unit.infrastructure.files.test_model_predictions import FixedPredictor, fixed_bundle


class FixedStore(VerifiedModelBundleStore):
    def __init__(self, bundle: LoadedModelBundle) -> None:
        self.bundle, self.calls = bundle, 0

    def load(self, path: Path) -> LoadedModelBundle:
        self.calls += 1
        return self.bundle


def fake_executor() -> MemorySimulationExecutor:
    executor = MemorySimulationExecutor(
        lambda frame, config: make_constant_outcome(frame, config, executor.identity)
    )
    return executor


async def make_simulation_service(
    directory: Path,
) -> tuple[MemoryModelSource, SimulateTotalsService]:
    await make_bundle(directory / "m.nbamodel")
    source = MemoryModelSource()
    return source, SimulateTotalsService(
        source,
        VerifiedModelBundleStore(),
        OwnedSimulationExecutor(NumpySimulationEngine()),
        AtomicSimulationExporter(),
    )


@pytest.mark.skipif(util.find_spec("catboost") is None, reason="optional ml extra is not installed")
async def test_simulation_service_never_reads_future_training_targets(tmp_path: Path) -> None:
    source, service = await make_simulation_service(tmp_path)
    result = await service.simulate(make_simulation_request(tmp_path / "out"))
    assert "read_training" not in source.events
    assert result.counts.states == 8 and result.counts.unavailable >= 1 and result.exit_code == 1


async def test_native_free_ports_are_sequential_and_load_once(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)
    events: list[str] = []

    class ObservedPredictor(FixedPredictor):
        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            events.append(f"predict:{state.state_number}")
            return super().predict_state(state)

    class Exporter(AtomicSimulationExporter):
        async def append_state(self, outcome: SimulationOutcome) -> None:
            events.append(f"append:{outcome.frame.state_number}")
            await super().append_state(outcome)

    executor = fake_executor()
    operation = executor.operation

    def observe(frame: SimulationFrame, config: SimulationConfig) -> SimulationOutcome:
        events.append(f"simulate:{frame.state_number}")
        return operation(frame, config)

    executor.operation = observe
    store = FixedStore(replace(bundle, predictor=ObservedPredictor(bundle.manifest)))
    source = MemorySimulationSource()
    result = await SimulateTotalsService(source, store, executor, Exporter()).simulate(
        make_simulation_request(tmp_path / "out")
    )
    assert store.calls == 1 and result.counts.states == 8
    assert len(executor.calls) == 7 and executor.active == 0
    assert events[:6] == [
        "predict:0",
        "simulate:0",
        "append:0",
        "predict:1",
        "simulate:1",
        "append:1",
    ]
    assert events[-2:] == ["predict:7", "append:7"]
    assert source.delegate.events == ["select", "resolve", "read_features", "recheck", "recheck"]


async def test_selected_state_equals_season_population(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)
    source = MemorySimulationSource()
    first, second = tmp_path / "all", tmp_path / "one"
    await SimulateTotalsService(
        source, FixedStore(bundle), fake_executor(), AtomicSimulationExporter()
    ).simulate(make_simulation_request(first))
    request = replace(
        make_simulation_request(second), game_id=next(iter(source.delegate.inputs)), state_number=3
    )
    await SimulateTotalsService(
        source, FixedStore(bundle), fake_executor(), AtomicSimulationExporter()
    ).simulate(request)
    reader = CanonicalSimulationReader()
    assert list(reader.iter_outcomes(reader.validate(first)))[3] == next(
        reader.iter_outcomes(reader.validate(second))
    )


@pytest.mark.parametrize("moment", ["after_game", "after_prepare"])
async def test_source_race_aborts_and_preserves_previous_file(tmp_path: Path, moment: str) -> None:
    bundle = await fixed_bundle(tmp_path)

    class Racing(MemorySimulationSource):
        async def recheck(self, source: ModelSourceReference) -> None:
            if moment == "after_game" or self.delegate.rechecks == 1:
                self.delegate.replace_snapshot_uuid(source.game.game_id)
            await super().recheck(source)

    path = tmp_path / "old"
    path.write_bytes(b"old")
    with pytest.raises(ModelingError, match="model_source_changed"):
        await SimulateTotalsService(
            Racing(), FixedStore(bundle), fake_executor(), AtomicSimulationExporter()
        ).simulate(make_simulation_request(path))
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize(
    "mutation",
    ["input", "artifact", "recipe", "state", "missing", "duplicate", "version", "empty", "games"],
)
async def test_invalid_identity_or_selection_never_publishes(tmp_path: Path, mutation: str) -> None:
    bundle = await fixed_bundle(tmp_path)

    class BadPredictor(FixedPredictor):
        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            row = super().predict_state(state)
            if mutation == "input":
                return replace(row, input_sha256="0" * 64)
            if mutation == "artifact":
                return replace(row, model_artifact_sha256="0" * 64)
            if mutation == "recipe":
                return replace(row, model_revision_sha256="0" * 64)
            if mutation == "state":
                return replace(
                    row, state=replace(row.state, home_score=1, score_diff=1 - row.state.away_score)
                )
            return row

    class BadSource(MemorySimulationSource):
        async def select_games(
            self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
        ) -> tuple[GameId, ...]:
            values = await super().select_games(seasons, game_id)
            return (
                ()
                if mutation == "empty"
                else ((*values, *values) if mutation == "games" else values)
            )

        async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
            original = await super().read_features(source)
            if mutation == "duplicate":
                return FeatureDataset(
                    original.game_id, (original.states[0], *original.states[:-1]), ()
                )
            return original

    source = BadSource()
    if mutation == "version":
        item = next(iter(source.delegate.references.values()))
        source.delegate.references[item.game.game_id] = replace(
            item,
            possession_versions=replace(item.possession_versions, parser_version="unsupported-v2"),
        )
    path = tmp_path / "old"
    path.write_bytes(b"old")
    request = make_simulation_request(path)
    if mutation == "missing":
        request = replace(request, game_id=next(iter(source.delegate.references)), state_number=8)
    with pytest.raises(SimulationError):
        await SimulateTotalsService(
            source,
            FixedStore(replace(bundle, predictor=BadPredictor(bundle.manifest))),
            fake_executor(),
            AtomicSimulationExporter(),
        ).simulate(request)
    assert path.read_bytes() == b"old"


async def test_one_feature_dataset_is_released_before_next_game(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)

    @dataclass(frozen=True, slots=True, weakref_slot=True)
    class Tracked(FeatureDataset):
        pass

    class Tracking(MemorySimulationSource):
        previous: ReferenceType[Tracked] | None = None

        async def resolve(self, game_id: GameId) -> ModelSourceReference:
            assert self.previous is None or self.previous() is None
            return await super().resolve(game_id)

        async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
            original = await super().read_features(source)
            tracked = Tracked(original.game_id, original.states, original.players, original.issues)
            self.previous = ref(tracked)
            return tracked

    original = make_training_input()
    second = remap_training_input(
        original, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC)
    )
    source = Tracking(MemoryModelSource((original, second)))
    result = await SimulateTotalsService(
        source, FixedStore(bundle), fake_executor(), AtomicSimulationExporter()
    ).simulate(make_simulation_request(tmp_path / "out"))
    assert result.counts.games == 2 and result.counts.states == 16


async def test_cancelled_inference_finishes_before_abort(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)
    started, release, finished = Event(), Event(), Event()

    class Paused(FixedPredictor):
        def predict_state(self, state: GameStateFeature) -> StateModelPrediction:
            started.set()
            assert release.wait(5)
            try:
                return super().predict_state(state)
            finally:
                finished.set()

    class Observed(AtomicSimulationExporter):
        async def abort(self) -> None:
            assert finished.is_set()
            await super().abort()

    path = tmp_path / "old"
    path.write_bytes(b"old")
    service = SimulateTotalsService(
        MemorySimulationSource(),
        FixedStore(replace(bundle, predictor=Paused(bundle.manifest))),
        fake_executor(),
        Observed(),
    )
    task = asyncio.create_task(service.simulate(make_simulation_request(path)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("precommit", [False, True])
async def test_cleanup_error_cannot_mask_committed_result_or_original_error(
    tmp_path: Path, precommit: bool
) -> None:
    bundle = await fixed_bundle(tmp_path)

    class CleanupFails(AtomicSimulationExporter):
        async def abort(self) -> None:
            await super().abort()
            raise OSError("controlled cleanup failure")

    source = MemorySimulationSource()
    source.delegate.empty = precommit
    service = SimulateTotalsService(source, FixedStore(bundle), fake_executor(), CleanupFails())
    path = tmp_path / "out"
    if precommit:
        with pytest.raises(SimulationError, match="simulation_empty_selection"):
            await service.simulate(make_simulation_request(path))
    else:
        result = await service.simulate(make_simulation_request(path))
        assert isinstance(result, SimulateTotalsResult) and path.exists()


async def test_unavailable_state_bypasses_executor_and_runtime_is_captured_once(
    tmp_path: Path,
) -> None:
    from nba_live_totals.domain.lineups import LineupStatus
    from tests.unit.application.modeling_fakes import make_model_source_reference

    bundle = await fixed_bundle(tmp_path)
    source = MemorySimulationSource()
    game_id = next(iter(source.delegate.inputs))
    inputs = source.delegate.inputs[game_id]
    state = inputs.features.states[3]
    changed = replace(
        state,
        home_player_ids=(),
        home_lineup_status=LineupStatus.INVALID,
        home_invalid_reason="missing_substitution",
        home_invalid_since=state.boundary,
    )
    features = FeatureDataset(
        game_id, (*inputs.features.states[:3], changed, *inputs.features.states[4:]), ()
    )
    source.delegate.inputs[game_id] = replace(inputs, features=features)
    source.delegate.references[game_id] = make_model_source_reference(inputs.game, features)

    class CountedExecutor(MemorySimulationExecutor):
        runtime_calls = 0

        async def runtime(self) -> SimulationRuntime:
            self.runtime_calls += 1
            return await super().runtime()

    executor = CountedExecutor(
        lambda frame, config: make_constant_outcome(frame, config, executor.identity)
    )
    path = tmp_path / "out"
    result = await SimulateTotalsService(
        source, FixedStore(bundle), executor, AtomicSimulationExporter()
    ).simulate(make_simulation_request(path))
    assert executor.runtime_calls == 1 and result.counts.unavailable == 2
    assert [number for _, number in executor.calls] == [0, 1, 2, 4, 5, 6]
    reader = CanonicalSimulationReader()
    row = list(reader.iter_outcomes(reader.validate(path)))[3]
    assert row.unavailable_reason == "invalid_feature_state" and row.executed == 0


async def test_out_of_order_games_are_rejected_before_second_read(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)

    class Reversed(MemorySimulationSource):
        async def select_games(
            self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
        ) -> tuple[GameId, ...]:
            return tuple(reversed(await super().select_games(seasons, game_id)))

    original = make_training_input()
    second = remap_training_input(
        original, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC)
    )
    source = Reversed(MemoryModelSource((original, second)))
    path = tmp_path / "old"
    path.write_bytes(b"old")
    with pytest.raises(SimulationError, match="simulation_source_order"):
        await SimulateTotalsService(
            source, FixedStore(bundle), fake_executor(), AtomicSimulationExporter()
        ).simulate(make_simulation_request(path))
    assert path.read_bytes() == b"old" and source.delegate.events.count("read_features") == 1
