from dataclasses import replace

import pytest

from nba_live_totals.domain.baselines import MAX_INT64
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.model_targets import (
    HeadTarget,
    calculate_remaining_targets,
    horizon_period,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.feature_helpers import OTHER_GAME_ID
from tests.unit.domain.modeling_helpers import make_target_case, make_training_input


def test_remaining_rates_use_exact_suffix_and_known_clock() -> None:
    state, endpoint = make_target_case()
    rates = calculate_remaining_targets(state, endpoint)
    assert rates.pace.value_scaled == 120_000_000
    assert rates.home_ppp.value_scaled == 1_666_667
    assert rates.away_ppp.value_scaled == 1_500_000
    assert rates.home_ppp.unavailable_reason is None


def test_current_ot_rejects_next_ot_endpoint() -> None:
    state, endpoint = make_target_case()
    state = replace(state, period=5)
    rates = calculate_remaining_targets(state, replace(endpoint, period=5))
    assert rates.pace.value_scaled == 120_000_000
    assert horizon_period(state) == 5
    with pytest.raises(ValueError):
        calculate_remaining_targets(state, replace(endpoint, period=6))


def test_terminal_invalid_zero_and_missing() -> None:
    state, endpoint = make_target_case()
    terminal = replace(state, seconds_remaining_game_ms=0, seconds_remaining_period_ms=0)
    rates = calculate_remaining_targets(terminal, endpoint)
    assert rates.pace == rates.home_ppp == rates.away_ppp == HeadTarget(None, "no_future_time")
    invalid = replace(
        state,
        home_lineup_status=LineupStatus.INVALID,
        home_player_ids=(),
        home_invalid_reason="missing_substitution",
        home_invalid_since=state.boundary,
    )
    rates = calculate_remaining_targets(invalid, endpoint)
    assert (
        rates.pace == rates.home_ppp == rates.away_ppp == HeadTarget(None, "invalid_feature_state")
    )
    no_home = replace(endpoint, home_possessions=1, home_score=2)
    rates = calculate_remaining_targets(state, no_home)
    assert rates.home_ppp == HeadTarget(None, "no_future_possessions")
    assert rates.away_ppp.value_scaled == 1_500_000
    assert (
        calculate_remaining_targets(state, replace(endpoint, home_score=2)).home_ppp.value_scaled
        == 0
    )
    no_observations = replace(
        endpoint, home_possessions=1, away_possessions=1, home_score=2, away_score=1
    )
    assert calculate_remaining_targets(state, no_observations).pace.value_scaled == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"home_possessions": 0},
        {"away_score": 0},
        {"game_id": OTHER_GAME_ID},
        {"boundary": EventBoundary(1, BoundarySide.BEFORE)},
        {"period": 5},
    ],
)
def test_negative_suffix_and_boundary_mismatch_raise(changes: dict[str, object]) -> None:
    state, endpoint = make_target_case()
    with pytest.raises(ValueError):
        calculate_remaining_targets(state, replace(endpoint, **changes))  # type: ignore[arg-type]


def test_scaled_overflow_and_invalid_reason_values_reject() -> None:
    state, endpoint = make_target_case()
    with pytest.raises(ValueError):
        calculate_remaining_targets(state, replace(endpoint, home_score=MAX_INT64))
    for value in (True, -1, 1.0, "1"):
        with pytest.raises(ValueError):
            HeadTarget(value, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HeadTarget(0, "no_future_time")
    with pytest.raises(ValueError):
        HeadTarget(None, None)


def test_future_invalid_lineups_do_not_mask_valid_current_target() -> None:
    inputs = make_training_input()
    states = tuple(
        replace(
            state,
            home_lineup_status=LineupStatus.INVALID,
            home_player_ids=(),
            home_invalid_reason="missing_substitution",
            home_invalid_since=state.boundary,
        )
        if state.state_number > 2
        else state
        for state in inputs.features.states
    )
    changed = replace(inputs, features=FeatureDataset(inputs.game.game_id, states, ()))
    rates = calculate_remaining_targets(changed.features.states[2], changed.period_ends[0])
    assert rates.home_ppp.value_scaled == 1_666_667
    assert rates.pace.value_scaled == 120_000_000


def test_input_validates_published_state_possession_and_endpoint_alignment() -> None:
    inputs = make_training_input()
    state = inputs.features.states[2]
    changed = replace(state, home_score=3, score_diff=2)
    states = (*inputs.features.states[:2], changed, *inputs.features.states[3:])
    with pytest.raises(ValueError):
        replace(inputs, features=FeatureDataset(inputs.game.game_id, states, ()))
    with pytest.raises(ValueError):
        replace(inputs, period_ends=(*inputs.period_ends, *inputs.period_ends))
    with pytest.raises(ValueError):
        replace(inputs, period_ends=())
    with pytest.raises(ValueError):
        replace(inputs, game=replace(inputs.game, home_score=8))


def test_known_empty_overtime_can_end_without_a_new_possession_state() -> None:
    inputs = make_training_input()
    last = replace(inputs.period_ends[0], period=5, boundary=EventBoundary(101, BoundarySide.AFTER))
    result = replace(inputs, period_ends=(*inputs.period_ends, last))
    assert result.period_ends[-1].home_possessions == 4
