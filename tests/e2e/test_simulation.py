"""Native Phase 2→8 CLI cycle on owned stores with exact population identities."""

from dataclasses import replace
from pathlib import Path

import pytest

from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.simulation import SimulationStatus, make_simulation_frame
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.simulation_export import CanonicalSimulationReader
from nba_live_totals.infrastructure.simulation.numpy_engine import NumpySimulationEngine
from tests.integration.simulation_harness import GAMES, simulation_acceptance

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


@pytest.mark.parametrize("recovery", [False, True])
async def test_native_simulation_preserves_source_storage(tmp_path: Path, recovery: bool) -> None:
    pytest.importorskip("catboost", reason="optional ML extra required")
    async with simulation_acceptance(recovery=recovery) as harness:
        sources = await harness.seed_pipeline()
        first_features = await harness.source.read_features(sources[0])
        assert first_features.states[0].home_pregame is not None
        assert first_features.states[0].home_pregame.games_used_20 == 0
        target_features = await harness.source.read_features(sources[-1])
        invalid = [s.state_number for s in target_features.states if not s.is_valid]
        if recovery:
            assert invalid and any(
                s.is_valid and s.state_number > max(invalid) for s in target_features.states
            )
        calls = tuple(harness.fixture.fetches)
        before = await harness.storage_projection()
        artifacts = await harness.run_clis(tmp_path)
        manifest = artifacts.all_states.manifest
        assert manifest.counts.games == 5 and manifest.counts.states == sum(
            s.reference.state_row_count for s in sources
        )
        assert manifest.counts.unavailable > 0
        reader = CanonicalSimulationReader()
        rows = list(reader.iter_outcomes(artifacts.all_states))
        assert {r.frame.period for r in rows} >= {0, 4, 5, 6}
        assert all(
            r.resolved + r.unresolved == r.requested
            for r in rows
            if r.status is not SimulationStatus.UNAVAILABLE
        )
        assert all(
            r.status is SimulationStatus.UNAVAILABLE
            for r in rows
            if r.frame.seconds_remaining_game_ms == 0
        )
        assert any(r.frame.state_number == 0 and r.frame.is_available for r in rows)
        if recovery:
            target = [r for r in rows if r.frame.game_id == GAMES[-1]]
            assert all(
                r.status is SimulationStatus.UNAVAILABLE
                for r in target
                if r.frame.state_number in invalid
            )
            assert any(
                r.status is not SimulationStatus.UNAVAILABLE
                for r in target
                if r.frame.state_number > max(invalid)
            )
        selected = next(reader.iter_outcomes(artifacts.selected_state))
        matching = next(
            r
            for r in rows
            if (r.frame.game_id, r.frame.state_number)
            == (selected.frame.game_id, selected.frame.state_number)
        )
        assert matching == selected
        # The actual second-OT suffix/final score never enters the current first-OT frame.
        pinned = VerifiedModelBundleStore().load(artifacts.bundle_path)
        overtime = await harness.source.read_features(sources[1])
        index = next(
            i
            for i, s in enumerate(overtime.states)
            if s.period == 5 and s.seconds_remaining_game_ms > 0 and s.is_valid
        )
        future = overtime.states[-1]
        changed_future = replace(
            future, home_score=future.home_score + 50, score_diff=future.score_diff + 50
        )
        changed = FeatureDataset(
            overtime.game_id,
            (*overtime.states[:-1], changed_future),
            overtime.players,
            overtime.issues,
        )
        original_prediction = pinned.predictor.predict_state(overtime.states[index])
        changed_prediction = pinned.predictor.predict_state(changed.states[index])
        assert original_prediction == changed_prediction
        engine = NumpySimulationEngine()
        original_frame = make_simulation_frame(original_prediction, manifest.config)
        changed_frame = make_simulation_frame(changed_prediction, manifest.config)
        first = engine.simulate(original_frame, manifest.config, stop_requested=lambda: False)
        second = engine.simulate(changed_frame, manifest.config, stop_requested=lambda: False)
        assert (
            first
            == second
            == next(
                r
                for r in rows
                if r.frame.game_id == GAMES[1]
                and r.frame.state_number == original_frame.state_number
            )
        )
        assert tuple(harness.fixture.fetches) == calls
        assert await harness.storage_projection() == before
