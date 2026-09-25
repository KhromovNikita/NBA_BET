"""Causal live pace, PPP and event-rate behavior tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nba_live_totals.domain.features import (
    FeatureStatDelta,
    LiveFeatures,
    NormalizedFeatureEvents,
    build_live_features,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession

from .feature_helpers import GAME_ID, make_game, make_possession


def _delta(sequence_number: int, **updates: object) -> FeatureStatDelta:
    values: dict[str, object] = {
        "sequence_number": sequence_number,
        "boundary": EventBoundary(sequence_number, BoundarySide.AFTER),
    }
    values.update(updates)
    return FeatureStatDelta(**values)  # type: ignore[arg-type]


def _events(*deltas: FeatureStatDelta) -> NormalizedFeatureEvents:
    return NormalizedFeatureEvents(GAME_ID, deltas)


def _possession(
    number: int,
    *,
    end_sequence: int,
    start_elapsed_ms: int,
    end_elapsed_ms: int,
    offense_team_id: int,
    offense_points: int,
    defense_points: int = 0,
    home_possessions: int,
    away_possessions: int,
    period: int = 1,
) -> Possession:
    defense_team_id = 20 if offense_team_id == 10 else 10
    period_length = 720_000 if period <= 4 else 300_000
    period_start_elapsed = (
        (period - 1) * 720_000 if period <= 4 else 2_880_000 + (period - 5) * 300_000
    )
    start_clock = period_length - (start_elapsed_ms - period_start_elapsed)
    end_clock = period_length - (end_elapsed_ms - period_start_elapsed)
    scheduled = 2_880_000 + 300_000 * max(period - 4, 0)
    return make_possession(
        possession_number=number,
        offense_team_id=offense_team_id,
        defense_team_id=defense_team_id,
        offense_possession_number=(home_possessions if offense_team_id == 10 else away_possessions),
        period=period,
        start_boundary=EventBoundary(end_sequence - 1, BoundarySide.BEFORE),
        end_boundary=EventBoundary(end_sequence, BoundarySide.AFTER),
        start_seconds_remaining_period_ms=start_clock,
        end_seconds_remaining_period_ms=end_clock,
        seconds_remaining_game_ms=scheduled - end_elapsed_ms,
        home_score_before=0,
        away_score_before=0,
        home_score=0,
        away_score=0,
        score_diff=0,
        offense_points=offense_points,
        defense_points=defense_points,
        home_possessions=home_possessions,
        away_possessions=away_possessions,
        source_event_count=2,
    )


def test_before_after_prefixes_and_future_mutation_are_causal() -> None:
    possessions = (
        _possession(
            1,
            end_sequence=2,
            start_elapsed_ms=0,
            end_elapsed_ms=20_000,
            offense_team_id=10,
            offense_points=2,
            defense_points=9,
            home_possessions=1,
            away_possessions=0,
        ),
    )
    base_events = _events(
        _delta(1, team_id=10, field_goal_attempts=1, three_point_attempts=1),
        _delta(2),
        _delta(3, team_id=10, turnovers=1),
    )
    boundaries = (
        EventBoundary(1, BoundarySide.BEFORE),
        EventBoundary(2, BoundarySide.BEFORE),
        EventBoundary(2, BoundarySide.AFTER),
    )

    result = build_live_features(make_game(), possessions, base_events, boundaries)
    mutated = replace(base_events.deltas[2], turnovers=99)
    changed = build_live_features(
        make_game(), possessions, _events(*base_events.deltas[:2], mutated), boundaries
    )

    assert result[0] == LiveFeatures()
    assert result[1].home_three_point_attempt_rate == 1_000_000
    assert result[1].current_pace is None
    assert result[2].current_pace == 72_000_000
    assert result[2].home_ppp == 2_000_000
    assert result == changed


def test_live_features_calculate_prefix_pace_ppp_and_event_rates() -> None:
    possessions = (
        _possession(
            1,
            end_sequence=2,
            start_elapsed_ms=0,
            end_elapsed_ms=10_000,
            offense_team_id=10,
            offense_points=2,
            home_possessions=1,
            away_possessions=0,
        ),
        _possession(
            2,
            end_sequence=4,
            start_elapsed_ms=10_000,
            end_elapsed_ms=20_000,
            offense_team_id=20,
            offense_points=3,
            home_possessions=1,
            away_possessions=1,
        ),
        _possession(
            3,
            end_sequence=6,
            start_elapsed_ms=20_000,
            end_elapsed_ms=30_000,
            offense_team_id=10,
            offense_points=0,
            defense_points=100,
            home_possessions=2,
            away_possessions=1,
        ),
        _possession(
            4,
            end_sequence=8,
            start_elapsed_ms=30_000,
            end_elapsed_ms=40_000,
            offense_team_id=20,
            offense_points=1,
            home_possessions=2,
            away_possessions=2,
        ),
    )
    events = _events(
        _delta(1, team_id=10, field_goal_attempts=1, three_point_attempts=1),
        _delta(2, team_id=10, free_throw_attempts=1),
        _delta(3, team_id=20, field_goal_attempts=1),
        _delta(4, team_id=10, turnovers=1),
        _delta(5, team_id=10, field_goal_attempts=1),
        _delta(6, team_id=10, offensive_rebounds=1),
        _delta(7, team_id=20, defensive_rebounds=2),
        _delta(8, team_id=10, defensive_rebounds=1),
    )

    live = build_live_features(
        make_game(), possessions, events, (EventBoundary(8, BoundarySide.AFTER),)
    )[0]

    assert live.current_pace == 144_000_000
    assert live.pace_last_5 == 144_000_000
    assert live.pace_last_5_possessions_used == 4
    assert live.pace_last_10_possessions_used == 4
    assert live.pace_last_20_possessions_used == 4
    assert live.home_ppp == 1_000_000
    assert live.away_ppp == 2_000_000
    assert live.home_ppp_last_5 == 1_000_000
    assert live.away_ppp_last_20 == 2_000_000
    assert live.home_ppp_last_5_possessions_used == 2
    assert live.away_ppp_last_20_possessions_used == 2
    assert live.home_three_point_attempt_rate == 500_000
    assert live.home_free_throw_rate == 500_000
    assert live.home_turnover_rate == 500_000
    assert live.home_offensive_rebound_rate == 333_333
    assert live.away_offensive_rebound_rate == 0


def test_rolling_windows_use_combined_and_team_specific_possessions() -> None:
    possessions = tuple(
        _possession(
            number,
            end_sequence=number * 2,
            start_elapsed_ms=(number - 1) * 10_000,
            end_elapsed_ms=number * 10_000 if number < 6 else 70_000,
            offense_team_id=10 if number % 2 else 20,
            offense_points=number,
            home_possessions=(number + 1) // 2,
            away_possessions=number // 2,
        )
        for number in range(1, 7)
    )
    events = _events(*(_delta(number) for number in range(1, 13)))

    live = build_live_features(
        make_game(), possessions, events, (EventBoundary(12, BoundarySide.AFTER),)
    )[0]

    assert live.pace_last_5 == 120_000_000
    assert live.pace_last_5_possessions_used == 5
    assert live.pace_last_10 == 123_428_571
    assert live.pace_last_10_possessions_used == 6
    assert live.pace_last_20 == 123_428_571
    assert live.home_ppp_last_5 == 3_000_000
    assert live.away_ppp_last_5 == 4_000_000
    assert live.home_ppp_last_5_possessions_used == 3
    assert live.away_ppp_last_5_possessions_used == 3


def test_started_overtime_extends_schedule_but_regulation_does_not_assume_it() -> None:
    events = _events(_delta(1), _delta(2))
    regulation = _possession(
        1,
        end_sequence=2,
        start_elapsed_ms=2_810_000,
        end_elapsed_ms=2_820_000,
        offense_team_id=10,
        offense_points=0,
        home_possessions=1,
        away_possessions=0,
        period=4,
    )
    overtime = _possession(
        1,
        end_sequence=2,
        start_elapsed_ms=2_930_000,
        end_elapsed_ms=2_940_000,
        offense_team_id=10,
        offense_points=0,
        home_possessions=1,
        away_possessions=0,
        period=5,
    )
    boundary = (EventBoundary(2, BoundarySide.AFTER),)

    regulation_live = build_live_features(make_game(), (regulation,), events, boundary)[0]
    overtime_live = build_live_features(make_game(), (overtime,), events, boundary)[0]

    assert regulation_live.current_pace == 510_638
    assert overtime_live.current_pace == 489_796


def test_zero_duration_rolling_pace_is_null_but_preserves_sample_count() -> None:
    possessions = (
        _possession(
            1,
            end_sequence=2,
            start_elapsed_ms=10_000,
            end_elapsed_ms=10_000,
            offense_team_id=10,
            offense_points=0,
            home_possessions=1,
            away_possessions=0,
        ),
        _possession(
            2,
            end_sequence=4,
            start_elapsed_ms=10_000,
            end_elapsed_ms=10_000,
            offense_team_id=20,
            offense_points=0,
            home_possessions=1,
            away_possessions=1,
        ),
    )
    events = _events(*(_delta(number) for number in range(1, 5)))

    live = build_live_features(
        make_game(), possessions, events, (EventBoundary(4, BoundarySide.AFTER),)
    )[0]

    assert live.current_pace == 288_000_000
    assert live.pace_last_5 is None
    assert live.pace_last_5_possessions_used == 2
    assert live.pace_last_10 is None
    assert live.pace_last_10_possessions_used == 2


def test_live_feature_inputs_require_ordered_causal_boundaries() -> None:
    events = _events(_delta(1), _delta(2))

    with pytest.raises(ValueError, match="boundaries must be strictly increasing"):
        build_live_features(
            make_game(),
            (),
            events,
            (
                EventBoundary(2, BoundarySide.AFTER),
                EventBoundary(1, BoundarySide.BEFORE),
            ),
        )

    with pytest.raises(ValueError, match="boundary exceeds event range"):
        build_live_features(make_game(), (), events, (EventBoundary(3, BoundarySide.BEFORE),))
