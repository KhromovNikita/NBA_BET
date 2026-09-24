"""Possession-end feature states use the lineup covering each exact boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nba_live_totals.domain import features
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureResultStatus,
    FeatureStateKind,
)
from nba_live_totals.domain.games import GameStatus
from nba_live_totals.domain.lineups import LineupStatus, find_lineup_segment
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.feature_helpers import GAME_ID, OTHER_GAME_ID, make_game, make_possession
from tests.unit.domain.lineup_helpers import (
    AWAY_PLAYERS,
    HOME_PLAYERS,
    make_invalid_state,
    make_segment,
    make_state,
)


def before(sequence: int) -> EventBoundary:
    return EventBoundary(sequence, BoundarySide.BEFORE)


def after(sequence: int) -> EventBoundary:
    return EventBoundary(sequence, BoundarySide.AFTER)


def test_join_uses_exact_end_boundary_across_substitutions_periods_and_final_event() -> None:
    # Event 2 changes the home lineup inside possession 1; events 4 and 5
    # are sequential substitutions at the same clock inside possession 2.
    first_home = frozenset({101, 102, 103, 104, 106})
    second_home = frozenset({101, 102, 103, 106, 107})
    final_away = frozenset({201, 202, 203, 204, 206})
    segments = (
        make_segment(end_boundary=before(2)),
        make_segment(
            segment_number=2,
            start_boundary=after(2),
            end_boundary=before(4),
            start_seconds_remaining_period_ms=700_000,
            end_seconds_remaining_period_ms=700_000,
            home_state=make_state(first_home),
        ),
        make_segment(
            segment_number=3,
            start_boundary=after(4),
            end_boundary=before(5),
            start_seconds_remaining_period_ms=700_000,
            end_seconds_remaining_period_ms=700_000,
            home_state=make_state(second_home),
        ),
        make_segment(
            segment_number=4,
            start_boundary=after(5),
            end_boundary=before(8),
            start_seconds_remaining_period_ms=700_000,
            home_state=make_state(second_home),
            away_state=make_state(final_away),
            end_period=5,
        ),
        make_segment(
            segment_number=5,
            start_boundary=after(8),
            end_boundary=after(9),
            home_state=make_state(first_home),
            away_state=make_state(final_away),
            start_period=5,
            end_period=5,
        ),
    )
    possessions = (
        make_possession(),
        make_possession(
            possession_number=2,
            offense_team_id=20,
            defense_team_id=10,
            end_boundary=after(5),
            away_score=3,
            home_score=2,
            score_diff=-1,
            home_possessions=1,
            away_possessions=1,
        ),
        make_possession(
            possession_number=3,
            period=4,
            end_boundary=after(7),
            seconds_remaining_game_ms=12_000,
            end_seconds_remaining_period_ms=12_000,
            home_score=88,
            away_score=87,
            score_diff=1,
            home_possessions=44,
            away_possessions=43,
        ),
        make_possession(
            possession_number=4,
            period=5,
            end_boundary=after(9),
            seconds_remaining_game_ms=0,
            end_seconds_remaining_period_ms=0,
            home_score=100,
            away_score=90,
            score_diff=10,
            home_possessions=50,
            away_possessions=49,
        ),
    )

    joined = features.join_possessions_to_lineups(make_game(), possessions, segments)

    assert isinstance(joined, tuple)
    assert [row.state_number for row in joined] == [0, 1, 2, 3, 4]
    assert [row.possession_number for row in joined] == [0, 1, 2, 3, 4]
    assert [row.boundary for row in joined] == [before(1), after(3), after(5), after(7), after(9)]
    assert joined[0].state_kind is FeatureStateKind.PREGAME
    assert joined[0].home_player_ids == tuple(sorted(HOME_PLAYERS))
    assert joined[0].away_player_ids == tuple(sorted(AWAY_PLAYERS))
    assert (
        joined[0].period,
        joined[0].seconds_remaining_game_ms,
        joined[0].seconds_remaining_period_ms,
        joined[0].home_score,
        joined[0].away_score,
        joined[0].score_diff,
        joined[0].home_possessions,
        joined[0].away_possessions,
    ) == (0, 2_880_000, 720_000, 0, 0, 0, 0, 0)
    assert [row.state_kind for row in joined[1:]] == [FeatureStateKind.AFTER_POSSESSION] * 4
    assert [row.home_player_ids for row in joined[1:]] == [
        tuple(sorted(first_home)),
        tuple(sorted(second_home)),
        tuple(sorted(second_home)),
        tuple(sorted(first_home)),
    ]
    assert joined[1].home_player_ids != joined[0].home_player_ids
    assert joined[2].away_player_ids == tuple(sorted(final_away))
    assert [row.period for row in joined[1:]] == [1, 1, 4, 5]
    assert [row.seconds_remaining_period_ms for row in joined[1:]] == [700_000, 700_000, 12_000, 0]
    assert [row.seconds_remaining_game_ms for row in joined[1:]] == [
        2_860_000,
        2_860_000,
        12_000,
        0,
    ]
    assert [row.home_score for row in joined[1:]] == [2, 2, 88, 100]
    assert [row.away_score for row in joined[1:]] == [0, 3, 87, 90]
    assert [row.score_diff for row in joined[1:]] == [2, -1, 1, 10]
    assert [row.home_possessions for row in joined[1:]] == [1, 1, 44, 50]
    assert [row.away_possessions for row in joined[1:]] == [0, 1, 43, 49]
    for row in joined:
        oracle = find_lineup_segment(segments, row.boundary)
        assert row.home_player_ids == tuple(sorted(oracle.home_state.player_ids))
        assert row.away_player_ids == tuple(sorted(oracle.away_state.player_ids))
        assert row.home_lineup is not None
        assert row.home_lineup.team_id == 10
        assert row.home_lineup.player_ids == row.home_player_ids
        assert row.home_lineup.profiled_player_count == 0
        assert row.home_lineup.lineup_avg_off_rating is None
        assert row.away_lineup is not None
        assert row.away_lineup.team_id == 20
        assert row.away_lineup.player_ids == row.away_player_ids
        assert row.away_lineup.profiled_player_count == 0
        assert row.away_lineup.lineup_avg_pace_context is None
        assert row.home_pregame is None
        assert row.away_pregame is None
        assert row.live is None
        assert row.offense_difference is None


def test_empty_possessions_still_yield_the_pregame_boundary() -> None:
    joined = features.join_possessions_to_lineups(make_game(), (), (make_segment(),))

    assert len(joined) == 1
    assert joined[0].boundary == before(1)
    assert joined[0].state_number == 0


@pytest.mark.parametrize(
    ("possessions", "segments", "message"),
    [
        ((make_possession(possession_number=2),), (make_segment(),), "possession number"),
        ((make_possession(game_id=OTHER_GAME_ID),), (make_segment(),), "game ID"),
        ((make_possession(offense_team_id=30),), (make_segment(),), "teams"),
        ((make_possession(defense_team_id=30),), (make_segment(),), "teams"),
        ((make_possession(),), (make_segment(start_boundary=after(1)),), "exactly one"),
        ((make_possession(),), (make_segment(end_boundary=before(3)),), "exactly one"),
        (
            (make_possession(),),
            (
                make_segment(end_boundary=after(3)),
                make_segment(segment_number=2, start_boundary=after(3), end_boundary=after(4)),
            ),
            "exactly one",
        ),
        (
            (make_possession(),),
            (make_segment(segment_number=2), make_segment(segment_number=1)),
            "segment order",
        ),
    ],
)
def test_join_rejects_structural_violations_before_producing_states(
    possessions: tuple[object, ...], segments: tuple[object, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        features.join_possessions_to_lineups(make_game(), possessions, segments)  # type: ignore[arg-type]


def test_join_rejects_nonfinal_game_and_nonmonotonic_end_boundaries() -> None:
    with pytest.raises(ValueError, match="final"):
        features.join_possessions_to_lineups(
            replace(make_game(), status=GameStatus.LIVE), (), (make_segment(),)
        )
    with pytest.raises(ValueError, match="boundary order"):
        features.join_possessions_to_lineups(
            make_game(),
            (make_possession(end_boundary=after(3)), make_possession(possession_number=2)),
            (make_segment(),),
        )


def test_invalid_lineup_recovers_without_erasing_causal_dataset_issue() -> None:
    segments = (
        make_segment(end_boundary=before(2)),
        make_segment(
            segment_number=2,
            start_boundary=after(2),
            end_boundary=before(4),
            home_state=make_invalid_state(reason="missing_home_sub", boundary=after(2)),
        ),
        make_segment(
            segment_number=3,
            start_boundary=after(4),
            end_boundary=before(6),
            home_state=make_invalid_state(reason="missing_home_sub", boundary=after(2)),
            away_state=make_invalid_state(reason="missing_away_sub", boundary=after(4)),
        ),
        make_segment(
            segment_number=4,
            start_boundary=after(6),
            end_boundary=after(7),
            home_state=make_state(HOME_PLAYERS),
            away_state=make_state(AWAY_PLAYERS),
        ),
    )
    possessions = (
        make_possession(),
        make_possession(possession_number=2, end_boundary=after(5)),
        make_possession(possession_number=3, end_boundary=after(7)),
    )

    states = features.join_possessions_to_lineups(make_game(), possessions, segments)
    dataset = FeatureDataset(
        GAME_ID,
        states,
        (),
        ("external_issue", "missing_home_sub", "external_issue"),
    )

    assert states[1].home_lineup_status is LineupStatus.INVALID
    assert states[1].home_player_ids == ()
    assert states[1].home_invalid_reason == "missing_home_sub"
    assert states[1].home_invalid_since == after(2)
    assert states[1].home_lineup is not None
    assert states[1].home_lineup.player_ids == ()
    assert states[1].home_lineup.profiled_player_count == 0
    assert states[1].home_lineup.lineup_avg_off_rating is None
    assert states[1].away_player_ids == tuple(sorted(AWAY_PLAYERS))
    assert states[2].away_lineup_status is LineupStatus.INVALID
    assert states[2].away_player_ids == ()
    assert states[2].away_invalid_reason == "missing_away_sub"
    assert states[2].away_invalid_since == after(4)
    assert states[2].away_lineup is not None
    assert states[2].away_lineup.player_ids == ()
    assert states[2].away_lineup.profiled_player_count == 0
    assert states[2].away_lineup.lineup_avg_pace_context is None
    assert states[3].home_player_ids == tuple(sorted(HOME_PLAYERS))
    assert states[3].away_player_ids == tuple(sorted(AWAY_PLAYERS))
    assert states[3].home_invalid_reason is None
    assert states[3].away_invalid_since is None
    assert dataset.status is FeatureResultStatus.INVALID
    assert dataset.issues == ("missing_home_sub", "missing_away_sub", "external_issue")


def test_causal_issue_precedes_explicit_codes_when_combined_list_is_capped() -> None:
    states = features.join_possessions_to_lineups(
        make_game(),
        (make_possession(),),
        (
            make_segment(
                end_boundary=after(3),
                home_state=make_invalid_state(reason="initial_home_unknown", boundary=before(1)),
            ),
        ),
    )
    explicit = tuple(f"external_{number:02d}" for number in range(40))

    dataset = FeatureDataset(GAME_ID, states, (), explicit)

    assert dataset.issues == ("initial_home_unknown", *explicit[:31])
