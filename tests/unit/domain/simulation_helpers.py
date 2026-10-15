"""Current-state fixtures with no future score or period-end dependency."""

from dataclasses import replace
from typing import TYPE_CHECKING

from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.model_features import model_input_sha256, project_model_features
from nba_live_totals.domain.model_predictions import PredictedRate, StateModelPrediction
from tests.unit.domain.baseline_helpers import make_state
from tests.unit.domain.modeling_helpers import make_target_case

if TYPE_CHECKING:
    from nba_live_totals.domain.simulation import SimulationOutcome


def make_simulation_prediction(
    *,
    pace_scaled: int = 100_000_000,
    home_ppp_scaled: int = 1_141_111,
    away_ppp_scaled: int = 1_141_111,
    period: int = 4,
    remaining_ms: int = 60_000,
    home_score: int = 10,
    away_score: int = 9,
) -> StateModelPrediction:
    if period == 0:
        state = make_state(
            state_number=0,
            seconds_remaining_game_ms=2_880_000,
            seconds_remaining_period_ms=720_000,
            home_score=home_score,
            away_score=away_score,
            score_diff=home_score - away_score,
        )
    else:
        base, _ = make_target_case()
        state = replace(
            base,
            period=period,
            seconds_remaining_game_ms=remaining_ms,
            seconds_remaining_period_ms=remaining_ms - 720_000 * max(4 - period, 0),
            home_score=home_score,
            away_score=away_score,
            score_diff=home_score - away_score,
        )
    reason = "no_future_time" if remaining_ms == 0 and period != 0 else None
    rates = tuple(
        PredictedRate(value if reason is None else None, reason, False)
        for value in (pace_scaled, home_ppp_scaled, away_ppp_scaled)
    )
    return StateModelPrediction(
        state,
        "a" * 64,
        "b" * 64,
        model_input_sha256(project_model_features(state, BaselineConfig())),
        *rates,
    )


def make_simulation_outcome(
    histogram: tuple[tuple[int, int], ...] = ((2, 1), (3, 1), (4, 2)),
    *,
    requested: int = 4,
    unresolved: int = 0,
) -> SimulationOutcome:
    """Hand-counted mathematical populations, not native sampling/calibration evidence."""
    from nba_live_totals.domain.simulation import (
        SimulationConfig,
        SimulationOutcome,
        SimulationRuntime,
        SimulationStatus,
        make_simulation_frame,
    )
    from nba_live_totals.domain.simulation_hashing import (
        simulation_revision_sha256,
        state_seed_sha256,
    )
    from nba_live_totals.domain.simulation_pmf import build_score_pmf

    config = SimulationConfig(simulations=requested, batch_size=min(1024, requested))
    frame = make_simulation_frame(make_simulation_prediction(home_score=0, away_score=0), config)
    runtime = SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    resolved = requested - unresolved
    pmf = build_score_pmf(1141111, config.score_prior_weights)
    return SimulationOutcome(
        frame=frame,
        status=SimulationStatus.INCOMPLETE if unresolved else SimulationStatus.COMPLETE,
        requested=requested,
        executed=requested,
        resolved=resolved,
        unresolved=unresolved,
        unavailable_reason=None,
        unresolved_counts=(("overtime_limit", unresolved),) if unresolved else (),
        new_ot_counts=(resolved,) + (0,) * 10,
        home_pmf=pmf,
        away_pmf=pmf,
        seed_sha256=state_seed_sha256(frame, config, runtime),
        simulation_revision_sha256=simulation_revision_sha256(frame, config, runtime),
        home_histogram=histogram,
        away_histogram=((0, resolved),) if resolved else (),
        total_histogram=histogram,
    )
