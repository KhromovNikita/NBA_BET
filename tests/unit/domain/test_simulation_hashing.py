"""Seed causality and literal projections prevent future/provenance-dependent draws."""

import hashlib
import json
from dataclasses import replace
from uuid import UUID

import pytest

from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.canonical import CanonicalObject
from nba_live_totals.domain.model_features import model_input_sha256, project_model_features
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationQuery,
    SimulationRuntime,
    make_simulation_frame,
)
from nba_live_totals.domain.simulation_hashing import (
    simulation_config_projection,
    simulation_config_sha256,
    simulation_frame_projection,
    simulation_query_projection,
    simulation_query_sha256,
    simulation_revision_sha256,
    simulation_run_revision,
    simulation_runtime_projection,
    state_seed_sha256,
)
from tests.unit.application.modeling_fakes import make_model_source_reference
from tests.unit.domain.modeling_helpers import make_training_input
from tests.unit.domain.simulation_helpers import make_simulation_prediction

CONFIG: CanonicalObject = {
    "policy_version": "possession-simulation-v1",
    "simulations": 10000,
    "seed": 42,
    "batch_size": 1024,
    "max_overtimes": 10,
    "max_possessions_per_horizon": 10000,
    "score_prior_weights": [460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1],
}
RUNTIME: CanonicalObject = {
    "python_version": "3.14.8",
    "numpy_version": "2.5.2",
    "system": "Windows",
    "machine": "AMD64",
    "engine_version": "numpy-poisson-multinomial-v1",
}
FRAME: CanonicalObject = {
    "game_id": "0022500001",
    "state_number": 2,
    "possession_number": 2,
    "state_kind": "after_possession",
    "boundary": {"sequence_number": 4, "side": "after"},
    "period": 4,
    "seconds_remaining_game_ms": 60000,
    "seconds_remaining_period_ms": 60000,
    "home_score": 10,
    "away_score": 9,
    "is_valid": True,
    "pace": {"value_scaled": 100000000, "unavailable_reason": None, "was_clipped_to_zero": False},
    "home_ppp": {"value_scaled": 1141111, "unavailable_reason": None, "was_clipped_to_zero": False},
    "away_ppp": {"value_scaled": 1141111, "unavailable_reason": None, "was_clipped_to_zero": False},
    "input_sha256": "63bc2903415f114d5e90c6fae4b1d2919adee78328bbbce461955eb09cd40108",
    "model_revision_sha256": "a" * 64,
    "model_artifact_sha256": "b" * 64,
    "unavailable_reason": None,
}


def reference_sha(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_literal_projections_and_independent_state_seed_recipe() -> None:
    config = SimulationConfig()
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    frame = make_simulation_frame(make_simulation_prediction(), config)
    assert simulation_config_projection(config) == CONFIG
    assert simulation_runtime_projection(runtime) == RUNTIME
    assert simulation_frame_projection(frame) == FRAME
    assert simulation_config_sha256(config) == reference_sha(CONFIG)
    expected = reference_sha(
        {
            "seed_policy_version": "state-pcg64-seed-v1",
            "base_seed": 42,
            "config_sha256": reference_sha(CONFIG),
            "runtime": RUNTIME,
            "frame": FRAME,
            "score_pmf_policy_version": "tilted-integer-score-pmf-v1",
        }
    )
    assert state_seed_sha256(frame, config, runtime) == expected
    assert simulation_revision_sha256(frame, config, runtime) == reference_sha(
        {
            "simulation_policy_version": "possession-simulation-v1",
            "state_seed_sha256": expected,
        }
    )
    assert expected != simulation_revision_sha256(frame, config, runtime)
    assert 0 <= int.from_bytes(bytes.fromhex(expected), "big") < 2**256


def test_query_has_own_identity_and_never_enters_seed_recipe() -> None:
    first, second = SimulationQuery(), SimulationQuery((210500000,), (0, 500000, 1000000))
    assert simulation_query_projection(second) == {
        "policy_version": "exact-simulation-summary-v1",
        "lines_scaled": [210500000],
        "quantiles_scaled": [0, 500000, 1000000],
    }
    assert simulation_query_sha256(first) != simulation_query_sha256(second)
    assert simulation_query_sha256(second) == reference_sha(simulation_query_projection(second))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("home_score", 11),
        ("away_score", 8),
        ("input_sha256", "d" * 64),
        ("model_revision_sha256", "d" * 64),
        ("model_artifact_sha256", "d" * 64),
        ("pace", PredictedRate(99000000, None, False)),
        ("home_ppp", PredictedRate(1100000, None, False)),
        ("away_ppp", PredictedRate(1100000, None, False)),
        ("seconds_remaining_game_ms", 60001),
    ],
)
def test_each_current_causal_value_changes_seed(field: str, value: object) -> None:
    config = SimulationConfig()
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    frame = make_simulation_frame(make_simulation_prediction(), config)
    changes = {field: value}
    if field == "seconds_remaining_game_ms":
        changes["seconds_remaining_period_ms"] = value
    changed = replace(frame, **changes)  # type: ignore[arg-type]
    assert state_seed_sha256(frame, config, runtime) != state_seed_sha256(changed, config, runtime)


def test_config_and_runtime_changes_modify_population_identity() -> None:
    config = SimulationConfig()
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    frame = make_simulation_frame(make_simulation_prediction(), config)
    original = state_seed_sha256(frame, config, runtime)
    for changed in (
        replace(config, seed=43),
        replace(config, batch_size=512),
        replace(config, simulations=9999),
    ):
        assert state_seed_sha256(frame, changed, runtime) != original
    for changed_runtime in (
        replace(runtime, numpy_version="2.6.0"),
        replace(runtime, system="Linux"),
    ):
        assert state_seed_sha256(frame, config, changed_runtime) != original


def test_future_suffix_final_score_and_snapshot_uuid_are_not_frame_inputs() -> None:
    original = make_training_input()
    last = original.features.states[-1]
    changed = replace(
        original,
        game=replace(original.game, home_score=9),
        features=replace(
            original.features,
            states=(
                *original.features.states[:-1],
                replace(last, home_score=9, score_diff=5),
            ),
        ),
        possessions=(
            *original.possessions[:-1],
            replace(original.possessions[-1], home_score=9, score_diff=5, offense_points=4),
        ),
        period_ends=(replace(original.period_ends[0], home_score=9),),
    )
    assert changed.game.home_score == 9 and original.game.home_score == 7
    assert original.features.states[2] == changed.features.states[2]
    source = make_model_source_reference(original.game, original.features)
    republished = replace(
        source,
        reference=replace(
            source.reference,
            attempt=replace(
                source.reference.attempt,
                snapshot_id=UUID("00000000-0000-4000-8000-000000000999"),
            ),
        ),
    )
    assert source.reference.attempt.snapshot_id != republished.reference.attempt.snapshot_id
    config = SimulationConfig()
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    prediction = replace(
        make_simulation_prediction(),
        input_sha256=model_input_sha256(
            project_model_features(original.features.states[2], BaselineConfig())
        ),
    )
    first = make_simulation_frame(replace(prediction, state=original.features.states[2]), config)
    second = make_simulation_frame(replace(prediction, state=changed.features.states[2]), config)
    assert state_seed_sha256(first, config, runtime) == state_seed_sha256(second, config, runtime)
    assert "source" not in simulation_frame_projection(first)


def test_run_identity_includes_query_scope_semantic_sources_and_observed_body() -> None:
    config, query = SimulationConfig(), SimulationQuery()
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    selectors: CanonicalObject = {"seasons": ["2025-26"], "game_id": None, "state_number": None}
    sources: tuple[CanonicalObject, ...] = ({"game_id": "0022500001", "semantic_sha256": "a" * 64},)
    digest = simulation_run_revision(selectors, config, query, runtime, sources, "b" * 64)
    assert len(digest) == 64
    assert digest != simulation_run_revision(
        selectors, config, SimulationQuery((210000000,)), runtime, sources, "b" * 64
    )
    assert digest != simulation_run_revision(selectors, config, query, runtime, sources, "c" * 64)
    assert digest != simulation_run_revision(
        selectors | {"game_id": "0022500001"}, config, query, runtime, sources, "b" * 64
    )
    with pytest.raises(ValueError):
        simulation_run_revision(
            selectors | {"output_path": "somewhere"}, config, query, runtime, sources, "b" * 64
        )
    with pytest.raises(ValueError):
        simulation_run_revision(selectors, config, query, runtime, sources, "bad")
