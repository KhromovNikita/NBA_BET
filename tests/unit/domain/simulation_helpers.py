"""Current-state fixtures with no future score or period-end dependency."""

from dataclasses import replace

from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.model_features import model_input_sha256, project_model_features
from nba_live_totals.domain.model_predictions import PredictedRate, StateModelPrediction
from tests.unit.domain.baseline_helpers import make_state
from tests.unit.domain.modeling_helpers import make_target_case


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
