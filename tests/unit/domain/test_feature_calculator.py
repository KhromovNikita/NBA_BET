"""The composed calculator decorates causal joined states without guessing lineups."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from nba_live_totals.domain.features import (
    DeterministicFeatureCalculator,
    FeatureCalculationInput,
    FeatureResultStatus,
    FeatureStatDelta,
    HistoricalPlayerBox,
    HistoricalTeamBox,
    LiveFeatures,
    NormalizedFeatureEvents,
)
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.feature_helpers import GAME_ID, make_game, make_possession
from tests.unit.domain.lineup_helpers import (
    AWAY_PLAYERS,
    HOME_PLAYERS,
    make_invalid_state,
    make_segment,
    make_state,
)


def _after(sequence: int) -> EventBoundary:
    return EventBoundary(sequence, BoundarySide.AFTER)


def test_calculator_composes_prefix_states_invalid_recovery_and_unique_sorted_players() -> None:
    initial_home = frozenset({101, 102, 103, 104, 105})
    recovered_home = frozenset({102, 103, 104, 105, 106})
    away = frozenset({201, 202, 203, 204, 205})
    segments = (
        make_segment(end_boundary=EventBoundary(2, BoundarySide.BEFORE)),
        make_segment(
            segment_number=2,
            start_boundary=_after(2),
            end_boundary=EventBoundary(4, BoundarySide.BEFORE),
            home_state=make_invalid_state(reason="missing_home_sub", boundary=_after(2)),
        ),
        make_segment(
            segment_number=3,
            start_boundary=_after(4),
            end_boundary=_after(5),
            home_state=make_state(recovered_home),
            away_state=make_state(away),
        ),
    )
    possessions = (
        make_possession(),
        make_possession(
            possession_number=2,
            offense_team_id=20,
            defense_team_id=10,
            offense_possession_number=1,
            start_boundary=EventBoundary(4, BoundarySide.BEFORE),
            end_boundary=_after(5),
            start_seconds_remaining_period_ms=700_000,
            end_seconds_remaining_period_ms=680_000,
            seconds_remaining_game_ms=2_840_000,
            home_score_before=2,
            away_score_before=0,
            home_score=2,
            away_score=3,
            score_diff=-1,
            offense_points=3,
            home_possessions=1,
            away_possessions=1,
            source_event_count=2,
        ),
    )
    events = NormalizedFeatureEvents(
        GAME_ID,
        tuple(FeatureStatDelta(sequence, _after(sequence)) for sequence in range(1, 6)),
    )
    inputs = FeatureCalculationInput(
        target_game=make_game(),
        possessions=possessions,
        lineup_segments=segments,
        historical_team_boxes=(),
        historical_player_boxes=(),
        history_possessions=(),
    )

    result = DeterministicFeatureCalculator().calculate(inputs, events)

    assert [state.state_number for state in result.states] == [0, 1, 2]
    assert result.states[0].home_player_ids == tuple(sorted(initial_home))
    assert result.states[1].home_lineup_status is LineupStatus.INVALID
    assert result.states[1].home_player_ids == ()
    assert result.states[1].home_invalid_reason == "missing_home_sub"
    assert result.states[1].home_invalid_since == _after(2)
    assert result.states[2].home_player_ids == tuple(sorted(recovered_home))
    assert result.states[0].home_pregame == result.states[2].home_pregame
    assert result.states[0].away_pregame == result.states[2].away_pregame
    assert result.states[0].live == LiveFeatures()
    assert result.states[1].live is not None
    assert result.states[1].live.home_ppp == 2_000_000
    assert result.states[1].live.away_ppp is None
    assert result.states[2].live is not None
    assert result.states[2].live.away_ppp == 3_000_000
    assert result.states[0].home_lineup is not None
    assert result.states[0].home_lineup.profiled_player_count == 5
    assert result.states[1].home_lineup is not None
    assert result.states[1].home_lineup.player_ids == ()
    assert result.states[1].home_lineup.profiled_player_count == 0
    assert result.states[1].away_lineup is not None
    assert result.states[1].away_lineup.profiled_player_count == 5
    assert result.states[2].home_lineup is not None
    assert result.states[2].home_lineup.profiled_player_count == 5
    assert result.states[2].offense_difference is None
    assert result.status is FeatureResultStatus.INVALID
    assert result.issues == ("missing_home_sub",)
    expected_ids = sorted({*initial_home, *recovered_home, *away})
    assert [profile.player_id for profile in result.players] == expected_ids
    assert len(result.players) == len(set(expected_ids))
    assert all(profile.games_used == 0 for profile in result.players)


def test_calculator_differences_are_home_minus_away_including_negative_results() -> None:
    started_at = datetime(2025, 10, 1, tzinfo=UTC)
    history_game_id = replace(GAME_ID, value="0022400001")
    boxes = (
        HistoricalTeamBox(
            history_game_id,
            30,
            started_at,
            started_at.date(),
            True,
            field_goals_made=40,
            field_goals_attempted=80,
            free_throws_attempted=20,
            rebounds_total=40,
            turnovers=10,
            points=100,
        ),
        HistoricalTeamBox(
            history_game_id,
            31,
            started_at,
            started_at.date(),
            False,
            field_goals_made=35,
            field_goals_attempted=75,
            free_throws_attempted=15,
            rebounds_total=35,
            turnovers=9,
            points=90,
        ),
    )
    player_boxes = tuple(
        HistoricalPlayerBox(
            history_game_id,
            30 if player_id in HOME_PLAYERS else 31,
            player_id,
            True,
            seconds_played=576,
            field_goals_made=2,
            field_goals_attempted=10,
            three_pointers_made=1,
            three_pointers_attempted=3,
            free_throws_attempted=0,
            rebounds_total=5,
            assists=3,
            turnovers=0,
            points=5 if player_id in HOME_PLAYERS else 10,
        )
        for player_id in (*HOME_PLAYERS, *AWAY_PLAYERS)
    )
    history_possession = make_possession(
        game_id=history_game_id,
        offense_team_id=30,
        defense_team_id=31,
        home_possessions=100,
        away_possessions=90,
    )
    base = FeatureCalculationInput(
        target_game=make_game(),
        possessions=(),
        lineup_segments=(make_segment(),),
        historical_team_boxes=boxes,
        historical_player_boxes=player_boxes,
        history_possessions=(history_possession,),
    )
    events = NormalizedFeatureEvents(GAME_ID, (FeatureStatDelta(1, _after(1)),))
    result = DeterministicFeatureCalculator().calculate(base, events)

    assert result.states[0].offense_difference == -50_000_000
    assert result.states[0].defense_difference == 0
    assert result.states[0].pace_difference == 0
    assert result.states[0].home_lineup is not None
    assert result.states[0].away_lineup is not None
    assert result.states[0].home_lineup.profiled_player_count == 5
    assert result.states[0].away_lineup.profiled_player_count == 5
