"""Strict inputs prevent silent sample changes or fabricated terminal forecasts."""

from dataclasses import FrozenInstanceError, replace

import pytest

from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.simulation import (
    MAX_SCORE,
    SimulationConfig,
    SimulationQuery,
    SimulationRuntime,
    make_simulation_frame,
)
from tests.unit.domain.simulation_helpers import make_simulation_prediction


def test_defaults_and_zero_clock_tie() -> None:
    config = SimulationConfig()
    assert (config.simulations, config.seed, config.batch_size) == (10000, 42, 1024)
    assert config.score_prior_weights == (460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)
    frame = make_simulation_frame(
        make_simulation_prediction(remaining_ms=0, home_score=10, away_score=10), config
    )
    assert frame.unavailable_reason == "no_future_time"
    assert not frame.is_available and frame.pace.value_scaled is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("simulations", 0),
        ("simulations", 1000001),
        ("simulations", True),
        ("simulations", 10000.0),
        ("seed", -1),
        ("seed", 2**64),
        ("seed", False),
        ("batch_size", 0),
        ("batch_size", 65537),
        ("batch_size", 10001),
        ("max_overtimes", 0),
        ("max_overtimes", 21),
        ("max_overtimes", True),
        ("max_possessions_per_horizon", 0),
        ("max_possessions_per_horizon", 1000001),
        ("score_prior_weights", (500000, 500000)),
        ("score_prior_weights", (500000, 500000, 0)),
        ("score_prior_weights", (499999, 500000, 2)),
        ("score_prior_weights", (499999, 500000, True)),
        ("score_prior_weights", [460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1]),
        ("score_prior_weights", (1,) * 34),
        ("policy_version", "possession-simulation-v2"),
    ],
)
def test_invalid_config_never_silently_coerces(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(SimulationConfig(), **{field: value})  # type: ignore[arg-type]


def test_config_extremes_and_immutability() -> None:
    config = SimulationConfig(
        simulations=1_000_000,
        seed=2**64 - 1,
        batch_size=65536,
        max_overtimes=20,
        max_possessions_per_horizon=1_000_000,
        score_prior_weights=(999968,) + (1,) * 32,
    )
    assert config.seed == 18446744073709551615
    with pytest.raises(FrozenInstanceError):
        config.seed = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lines_scaled", (1000000, 0)),
        ("lines_scaled", (0, 0)),
        ("lines_scaled", (-500000,)),
        ("lines_scaled", (250000,)),
        ("lines_scaled", (True,)),
        ("lines_scaled", (2**63,)),
        ("lines_scaled", tuple(n * 500000 for n in range(65))),
        ("lines_scaled", [0]),
        ("quantiles_scaled", ()),
        ("quantiles_scaled", (-1,)),
        ("quantiles_scaled", (1000001,)),
        ("quantiles_scaled", (500000, 500000)),
        ("quantiles_scaled", (True,)),
        ("quantiles_scaled", (0.5,)),
        ("quantiles_scaled", tuple(range(33))),
        ("policy_version", "exact-simulation-summary-v2"),
    ],
)
def test_query_rejects_noncanonical_or_inexact_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(SimulationQuery(), **{field: value})  # type: ignore[arg-type]


def test_query_accepts_half_lines_and_quantile_endpoints() -> None:
    query = SimulationQuery((0, 210000000, 210500000), (0, 500000, 1000000))
    assert query.quantiles_scaled == (0, 500000, 1000000)
    assert SimulationQuery().quantiles_scaled == (50000, 500000, 950000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python_version", ""),
        ("numpy_version", "2.5.2\nsecret"),
        ("system", True),
        ("machine", "x" * 129),
        ("engine_version", "other"),
    ],
)
def test_runtime_identity_cannot_hide_unsupported_or_ambiguous_values(
    field: str,
    value: object,
) -> None:
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    with pytest.raises(ValueError):
        replace(runtime, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("pace", "home", "away", "expected"),
    [
        (0, 0, 0, None),
        (1000000000, 8000000, 0, None),
        (1000000001, 0, 0, "pace_outside_runtime_limit"),
        (100000000, 8000001, 0, "ppp_outside_score_support"),
        (1000000001, 0, 8000001, "ppp_outside_score_support"),
    ],
)
def test_rates_outside_declared_support_are_retained_unavailable(
    pace: int,
    home: int,
    away: int,
    expected: str | None,
) -> None:
    frame = make_simulation_frame(
        make_simulation_prediction(
            pace_scaled=pace,
            home_ppp_scaled=home,
            away_ppp_scaled=away,
        ),
        SimulationConfig(),
    )
    assert frame.unavailable_reason == expected
    assert frame.is_available == (expected is None)
    assert frame.home_ppp.value_scaled == home


def test_support_uses_configured_prior_not_default_k() -> None:
    config = SimulationConfig(score_prior_weights=(250000, 500000, 250000))
    frame = make_simulation_frame(make_simulation_prediction(home_ppp_scaled=2000001), config)
    assert frame.unavailable_reason == "ppp_outside_score_support"


def test_invalid_lineup_is_unavailable_and_preserves_model_identity() -> None:
    original = make_simulation_prediction()
    state = replace(
        original.state,
        home_lineup_status=LineupStatus.INVALID,
        home_player_ids=(),
        home_invalid_reason="unknown_player",
        home_invalid_since=original.state.boundary,
    )
    missing = PredictedRate(None, "invalid_feature_state", False)
    prediction = replace(original, state=state, pace=missing, home_ppp=missing, away_ppp=missing)
    frame = make_simulation_frame(prediction, SimulationConfig())
    assert frame.unavailable_reason == "invalid_feature_state"
    assert frame.model_revision_sha256 == "a" * 64
    assert frame.model_artifact_sha256 == "b" * 64


@pytest.mark.parametrize(
    ("period", "remaining"), [(0, 2880000), (2, 1500000), (4, 1), (5, 300000), (6, 1)]
)
def test_pregame_regulation_and_current_ot_clocks(period: int, remaining: int) -> None:
    frame = make_simulation_frame(
        make_simulation_prediction(
            period=period,
            remaining_ms=remaining,
            home_score=0,
            away_score=0,
        ),
        SimulationConfig(),
    )
    assert frame.period == period and frame.seconds_remaining_game_ms == remaining
    assert frame.is_available


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seconds_remaining_period_ms", 60001),
        ("period", 0),
        ("state_number", True),
        ("possession_number", 100),
        ("is_valid", 1),
        ("home_score", -1),
        ("home_score", MAX_SCORE),
        ("input_sha256", "x" * 64),
        ("model_artifact_sha256", "b" * 63),
        ("unavailable_reason", "no_future_time"),
    ],
)
def test_direct_frame_rejects_structural_corruption(field: str, value: object) -> None:
    frame = make_simulation_frame(make_simulation_prediction(), SimulationConfig())
    with pytest.raises(ValueError):
        replace(frame, **{field: value})  # type: ignore[arg-type]


def test_large_current_scores_fail_before_quality_mask_and_clipping_is_retained() -> None:
    prediction = make_simulation_prediction(home_score=MAX_SCORE, away_score=1, remaining_ms=0)
    with pytest.raises(ValueError):
        make_simulation_frame(prediction, SimulationConfig())
    prediction = replace(make_simulation_prediction(), pace=PredictedRate(0, None, True))
    frame = make_simulation_frame(prediction, SimulationConfig())
    assert frame.pace.was_clipped_to_zero and frame.pace.value_scaled == 0
