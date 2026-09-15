"""Causal behavior tests for provider-neutral lineup reconstruction."""

from __future__ import annotations

import pytest

from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import (
    FullLineupReset,
    InvalidTeamTransition,
    LineupStatus,
    Substitute,
    find_lineup_segment,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.lineup_helpers import (
    AWAY_PLAYERS,
    HOME_PLAYERS,
    make_lineup_event,
    make_lineup_roster,
    make_normalized_game,
    make_reconstruction_context,
)


def reconstruct(*events: object, **normalized_overrides: object):  # type: ignore[no-untyped-def]
    normalized = make_normalized_game(
        *events,  # type: ignore[arg-type]
        **normalized_overrides,  # type: ignore[arg-type]
    )
    return DeterministicLineupReconstructor().reconstruct(make_reconstruction_context(), normalized)


def test_single_substitution_replaces_one_player_at_after_boundary() -> None:
    result = reconstruct(
        make_lineup_event(1),
        make_lineup_event(2, Substitute(10, 101, 106), clock_ms=700_000),
        make_lineup_event(3, clock_ms=690_000),
    )

    assert result.status is LineupStatus.VALID
    assert len(result.segments) == 2
    assert result.segments[0].home_state.player_ids == HOME_PLAYERS
    assert result.segments[0].end_boundary == EventBoundary(2, BoundarySide.BEFORE)
    assert result.segments[1].start_boundary == EventBoundary(2, BoundarySide.AFTER)
    assert result.segments[1].home_state.player_ids == frozenset({102, 103, 104, 105, 106})
    assert result.segments[1].away_state.player_ids == AWAY_PLAYERS


def test_same_clock_substitutions_preserve_intermediate_state_in_sequence_order() -> None:
    result = reconstruct(
        make_lineup_event(1),
        make_lineup_event(2, Substitute(10, 101, 106), clock_ms=600_000),
        make_lineup_event(3, Substitute(10, 102, 101), clock_ms=600_000),
    )

    assert [segment.home_state.player_ids for segment in result.segments] == [
        HOME_PLAYERS,
        frozenset({102, 103, 104, 105, 106}),
        frozenset({101, 103, 104, 105, 106}),
    ]
    assert result.segments[1].start_boundary == EventBoundary(2, BoundarySide.AFTER)
    assert result.segments[1].end_boundary == EventBoundary(3, BoundarySide.BEFORE)
    assert result.segments[1].start_seconds_remaining_period_ms == 600_000
    assert result.segments[1].end_seconds_remaining_period_ms == 600_000


def test_period_and_overtime_events_carry_state_and_no_sub_game_has_one_segment() -> None:
    result = reconstruct(
        make_lineup_event(1, period=1, clock_ms=720_000),
        make_lineup_event(2, period=2, clock_ms=720_000),
        make_lineup_event(3, period=5, clock_ms=300_000),
    )

    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.start_period == 1
    assert segment.end_period == 5
    assert segment.start_seconds_remaining_period_ms == 720_000
    assert segment.end_seconds_remaining_period_ms == 300_000
    assert segment.home_state.player_ids == HOME_PLAYERS


def test_substituted_lineup_persists_across_later_regulation_and_overtime_events() -> None:
    substituted = frozenset({102, 103, 104, 105, 106})
    result = reconstruct(
        make_lineup_event(1, Substitute(10, 101, 106), period=1, clock_ms=600_000),
        make_lineup_event(2, period=2, clock_ms=720_000),
        make_lineup_event(3, period=4, clock_ms=0),
        make_lineup_event(4, period=5, clock_ms=300_000),
    )

    assert len(result.segments) == 2
    assert result.segments[-1].home_state.player_ids == substituted
    assert result.segments[-1].end_period == 5
    assert result.segments[-1].end_seconds_remaining_period_ms == 300_000


def test_period_and_overtime_events_do_not_reset_or_recover_an_invalid_side() -> None:
    result = reconstruct(
        make_lineup_event(
            1,
            InvalidTeamTransition(10, "outgoing_label_mismatch"),
            period=1,
            clock_ms=600_000,
        ),
        make_lineup_event(2, period=2, clock_ms=720_000),
        make_lineup_event(3, period=5, clock_ms=300_000),
    )

    invalid = result.segments[-1].home_state
    assert invalid.status is LineupStatus.INVALID
    assert invalid.player_ids == frozenset()
    assert invalid.invalid_reason == "outgoing_label_mismatch"
    assert invalid.invalid_since == EventBoundary(1, BoundarySide.AFTER)
    assert result.segments[-1].away_state.player_ids == AWAY_PLAYERS
    assert result.segments[-1].end_period == 5


@pytest.mark.parametrize(
    ("transition", "reason"),
    [
        (Substitute(10, 101, 107), "roster_team_mismatch"),
        (Substitute(10, 101, 102), "incoming_already_on_court"),
        (Substitute(10, 101, 101), "same_player_substitution"),
        (Substitute(10, 101, 201), "roster_team_mismatch"),
        (Substitute(10, 101, 106), "ineligible_incoming_player"),
    ],
)
def test_invalid_substitution_invalidates_only_its_known_team(
    transition: Substitute, reason: str
) -> None:
    home_roster = make_lineup_roster(10)
    if reason == "ineligible_incoming_player":
        home_roster = make_lineup_roster(10, eligible_player_ids=HOME_PLAYERS)

    result = reconstruct(
        make_lineup_event(1),
        make_lineup_event(2, transition),
        home_roster=home_roster,
    )

    invalid = result.segments[-1].home_state
    assert invalid.status is LineupStatus.INVALID
    assert invalid.player_ids == frozenset()
    assert invalid.invalid_reason == reason
    assert invalid.invalid_since == EventBoundary(2, BoundarySide.AFTER)
    assert result.segments[-1].away_state.player_ids == AWAY_PLAYERS


def test_outgoing_roster_player_missing_from_court_is_reported_causally() -> None:
    result = reconstruct(
        make_lineup_event(1, Substitute(10, 101, 106)),
        make_lineup_event(2, Substitute(10, 101, 102)),
    )

    assert result.segments[-1].home_state.invalid_reason == "outgoing_not_on_court"
    assert result.segments[-1].home_state.invalid_since == EventBoundary(2, BoundarySide.AFTER)


def test_wrong_team_outgoing_player_has_exact_localized_reason() -> None:
    result = reconstruct(
        make_lineup_event(1),
        make_lineup_event(2, Substitute(10, 201, 106)),
    )

    assert result.segments[-1].home_state.invalid_reason == "roster_team_mismatch"
    assert result.segments[-1].home_state.invalid_since == EventBoundary(2, BoundarySide.AFTER)
    assert result.segments[-1].away_state.player_ids == AWAY_PLAYERS


@pytest.mark.parametrize(
    "reason",
    ["unsupported_substitution_form", "outgoing_label_mismatch"],
)
def test_known_home_normalizer_failures_preserve_exact_allowlisted_reason(reason: str) -> None:
    result = reconstruct(make_lineup_event(1, InvalidTeamTransition(10, reason)))

    assert result.segments[-1].home_state.invalid_reason == reason
    assert result.segments[-1].home_state.invalid_since == EventBoundary(1, BoundarySide.AFTER)
    assert result.segments[-1].away_state.player_ids == AWAY_PLAYERS


def test_known_away_failure_leaves_home_lineup_valid() -> None:
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(20, "ambiguous_incoming_player"))
    )

    assert result.segments[-1].home_state.player_ids == HOME_PLAYERS
    assert result.segments[-1].away_state.invalid_reason == "ambiguous_incoming_player"
    assert result.segments[-1].away_state.invalid_since == EventBoundary(1, BoundarySide.AFTER)


def test_unknown_or_unprovable_team_invalidates_both_sides() -> None:
    for transition in (
        Substitute(30, 301, 306),
        InvalidTeamTransition(None, "unknown_substitution_team"),
    ):
        result = reconstruct(make_lineup_event(1), make_lineup_event(2, transition))

        assert result.segments[-1].home_state.invalid_reason == "unknown_substitution_team"
        assert result.segments[-1].away_state.invalid_reason == "unknown_substitution_team"


def test_known_team_error_and_absorbing_invalid_state_do_not_stop_other_team() -> None:
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(10, "missing_outgoing_player_id")),
        make_lineup_event(2, Substitute(10, 101, 106)),
        make_lineup_event(3, Substitute(20, 201, 206)),
    )

    assert len(result.segments) == 3
    home_after_first = result.segments[1].home_state
    assert home_after_first.invalid_reason == "missing_outgoing_player_id"
    assert home_after_first.invalid_since == EventBoundary(1, BoundarySide.AFTER)
    assert result.segments[2].home_state == home_after_first
    assert result.segments[2].away_state.player_ids == frozenset({202, 203, 204, 205, 206})


def test_later_distinct_fatal_signal_preserves_first_reason_and_since_boundary() -> None:
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(10, "missing_outgoing_player_id")),
        make_lineup_event(2, InvalidTeamTransition(10, "outgoing_label_mismatch")),
    )

    assert len(result.segments) == 2
    invalid = result.segments[-1].home_state
    assert invalid.invalid_reason == "missing_outgoing_player_id"
    assert invalid.invalid_since == EventBoundary(1, BoundarySide.AFTER)
    assert [(issue.reason, issue.sequence_number) for issue in result.issues] == [
        ("missing_outgoing_player_id", 1)
    ]


def test_unknown_team_after_known_failure_preserves_first_reason_and_invalidates_other() -> None:
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(10, "missing_outgoing_player_id")),
        make_lineup_event(2, InvalidTeamTransition(None, "unknown_substitution_team")),
    )

    home = result.segments[-1].home_state
    away = result.segments[-1].away_state
    assert home.invalid_reason == "missing_outgoing_player_id"
    assert home.invalid_since == EventBoundary(1, BoundarySide.AFTER)
    assert away.invalid_reason == "unknown_substitution_team"
    assert away.invalid_since == EventBoundary(2, BoundarySide.AFTER)
    assert [(issue.team_id, issue.reason, issue.sequence_number) for issue in result.issues] == [
        (10, "missing_outgoing_player_id", 1),
        (20, "unknown_substitution_team", 2),
    ]


def test_valid_reset_recovers_but_does_not_erase_earlier_invalid_result() -> None:
    recovered = (101, 102, 103, 104, 106)
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(10, "unknown_incoming_player")),
        make_lineup_event(2, FullLineupReset(10, recovered)),
    )

    assert result.segments[-1].home_state.status is LineupStatus.VALID
    assert result.segments[-1].home_state.player_ids == frozenset(recovered)
    assert result.status is LineupStatus.INVALID
    assert [(issue.team_id, issue.reason) for issue in result.issues] == [
        (10, "unknown_incoming_player")
    ]


@pytest.mark.parametrize(
    "reset",
    [
        FullLineupReset(10, (101, 102, 102, 103, 104, 105)),
        FullLineupReset(10, (101, 102, 103, 104, 106)),
    ],
)
def test_invalid_reset_is_rejected_and_cannot_recover_invalid_state(
    reset: FullLineupReset,
) -> None:
    home_roster = make_lineup_roster(10, eligible_player_ids=HOME_PLAYERS)
    result = reconstruct(
        make_lineup_event(1, InvalidTeamTransition(10, "ambiguous_incoming_player")),
        make_lineup_event(2, reset),
        home_roster=home_roster,
    )

    assert len(result.segments) == 2
    state = result.segments[-1].home_state
    assert state.invalid_reason == "ambiguous_incoming_player"
    assert state.invalid_since == EventBoundary(1, BoundarySide.AFTER)


def test_invalid_reset_makes_a_valid_team_invalid() -> None:
    result = reconstruct(
        make_lineup_event(1),
        make_lineup_event(2, FullLineupReset(10, (101, 102, 102, 103, 104))),
    )

    assert result.segments[-1].home_state.invalid_reason == "invalid_full_lineup_reset"


def test_invalid_initial_starters_begin_before_first_event_independently() -> None:
    home_roster = make_lineup_roster(10, starter_player_ids={101, 102, 103, 104})
    result = reconstruct(make_lineup_event(1), home_roster=home_roster)

    segment = result.segments[0]
    assert segment.home_state.invalid_reason == "invalid_initial_lineup"
    assert segment.home_state.invalid_since == EventBoundary(1, BoundarySide.BEFORE)
    assert segment.away_state.player_ids == AWAY_PLAYERS
    assert result.status is LineupStatus.INVALID


def test_future_contradiction_leaves_causal_prefix_byte_identical() -> None:
    prefix_events = (
        make_lineup_event(1),
        make_lineup_event(2, Substitute(10, 101, 106)),
        make_lineup_event(3, Substitute(20, 201, 206)),
        make_lineup_event(4, period=2),
    )
    prefix = reconstruct(*prefix_events)
    contradicted = reconstruct(
        *prefix_events,
        make_lineup_event(5, Substitute(10, 101, 102), period=2, clock_ms=700_000),
    )

    assert contradicted.segments[:2] == prefix.segments[:-1]
    assert contradicted.segments[2].start_boundary == prefix.segments[-1].start_boundary
    assert contradicted.segments[2].home_state == prefix.segments[-1].home_state
    assert contradicted.segments[2].away_state == prefix.segments[-1].away_state

    before = find_lineup_segment(contradicted.segments, EventBoundary(5, BoundarySide.BEFORE))
    after = find_lineup_segment(contradicted.segments, EventBoundary(5, BoundarySide.AFTER))
    assert before.home_state.player_ids == frozenset({102, 103, 104, 105, 106})
    assert before.home_state.status is LineupStatus.VALID
    assert after.home_state.status is LineupStatus.INVALID
    assert after.home_state.invalid_reason == "outgoing_not_on_court"
    assert after.home_state.invalid_since == EventBoundary(5, BoundarySide.AFTER)


def test_reconstructor_rejects_non_allowlisted_invalid_reason() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        reconstruct(make_lineup_event(1, InvalidTeamTransition(10, "unknown_team")))
