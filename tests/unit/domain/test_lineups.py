"""Behavior tests for immutable provider-neutral lineup contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from nba_live_totals.domain.lineups import (
    FullLineupReset,
    InvalidTeamTransition,
    LineupIssue,
    LineupPlayerBox,
    LineupReconstructionResult,
    LineupStatus,
    NoLineupChange,
    NormalizedGameLineups,
    NormalizedLineupEvent,
    Substitute,
    TeamLineupState,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.lineup_helpers import (
    AWAY_PLAYERS,
    GAME_ID,
    HOME_PLAYERS,
    make_invalid_state,
    make_reconstruction_context,
    make_roster,
    make_segment,
    make_state,
    make_versions,
)


def test_versions_require_bounded_lowercase_safe_values() -> None:
    versions = make_versions()

    assert versions.normalizer_version == "nba-pbp-v1"
    for field_name, value in (
        ("normalizer_version", ""),
        ("reconstructor_version", "contains space"),
        ("rules_version", "NBA-2025-v1"),
        ("output_schema_version", "x" * 129),
    ):
        with pytest.raises(ValueError, match="version"):
            make_versions(**{field_name: value})


def test_player_box_validates_exact_primitives_and_preserves_nullable_strings() -> None:
    null_strings = LineupPlayerBox(GAME_ID, 10, 101, None, True, True, None)
    empty_strings = LineupPlayerBox(GAME_ID, 10, 101, "", False, False, "")

    assert null_strings.position is null_strings.comment is None
    assert empty_strings.position == empty_strings.comment == ""
    with pytest.raises(ValueError, match="team ID"):
        LineupPlayerBox(GAME_ID, True, 101, None, True, True, None)
    with pytest.raises(ValueError, match="player ID"):
        LineupPlayerBox(GAME_ID, 10, 0, None, True, True, None)
    with pytest.raises(ValueError, match="starter"):
        LineupPlayerBox(GAME_ID, 10, 101, None, 1, True, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="position"):
        LineupPlayerBox(GAME_ID, 10, 101, 3, True, True, None)  # type: ignore[arg-type]


def test_roster_snapshots_sets_and_retains_bad_starter_cardinality() -> None:
    roster_values = [101, 102, 103, 104, 105, 106]
    eligible_values = [101, 102, 103, 104, 105]
    starter_values = [101, 102, 103, 104]

    roster = make_roster(
        roster_player_ids=roster_values,
        eligible_player_ids=eligible_values,
        starter_player_ids=starter_values,
    )
    roster_values.clear()
    eligible_values.clear()
    starter_values.clear()

    assert roster.roster_player_ids == frozenset({101, 102, 103, 104, 105, 106})
    assert roster.eligible_player_ids == frozenset({101, 102, 103, 104, 105})
    assert roster.starter_player_ids == frozenset({101, 102, 103, 104})
    with pytest.raises(ValueError, match="eligible"):
        make_roster(eligible_player_ids={101, 999})
    with pytest.raises(ValueError, match="starter"):
        make_roster(starter_player_ids={101, 999})
    with pytest.raises(ValueError, match="player ID"):
        make_roster(roster_player_ids={101, True})


def test_transition_contracts_are_closed_safe_and_preserve_reset_duplicates() -> None:
    assert Substitute(10, 101, 106).team_id == 10
    assert InvalidTeamTransition(None, "unknown_substitution_team").team_id is None
    assert NoLineupChange() == NoLineupChange()

    reset_values = [101, 102, 102, 103, 104, 105]
    reset = FullLineupReset(10, reset_values)  # type: ignore[arg-type]
    reset_values.clear()
    assert reset.player_ids == (101, 102, 102, 103, 104, 105)

    with pytest.raises(ValueError, match="incoming player ID"):
        Substitute(10, 101, False)
    with pytest.raises(ValueError, match="code"):
        InvalidTeamTransition(10, "raw provider text!")
    with pytest.raises(ValueError, match="player ID"):
        FullLineupReset(10, (101, 0))


def test_normalized_game_requires_one_based_contiguous_complete_event_envelopes() -> None:
    first = NormalizedLineupEvent(1, 1, 720_000, NoLineupChange())
    second = NormalizedLineupEvent(2, 1, 719_000, Substitute(10, 101, 106))
    events = [first, second]
    normalized = NormalizedGameLineups(
        GAME_ID,
        make_roster(10),
        make_roster(20),
        events,  # type: ignore[arg-type]
    )
    events.clear()

    assert normalized.events == (first, second)
    with pytest.raises(ValueError, match="one-based contiguous"):
        NormalizedGameLineups(GAME_ID, make_roster(10), make_roster(20), (second,))
    with pytest.raises(ValueError, match="events"):
        NormalizedGameLineups(GAME_ID, make_roster(10), make_roster(20), ())
    with pytest.raises(ValueError, match="transition"):
        NormalizedLineupEvent(1, 1, 720_000, "substitution")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="clock"):
        NormalizedLineupEvent(1, 1, True, NoLineupChange())


def test_team_state_enforces_exact_valid_or_invalid_representation() -> None:
    valid = make_state(HOME_PLAYERS)
    invalid = make_invalid_state()

    assert valid.status is LineupStatus.VALID
    assert invalid.status is LineupStatus.INVALID
    assert invalid.player_ids == frozenset()
    with pytest.raises(ValueError, match="exactly five"):
        make_state(frozenset({101, 102, 103, 104}))
    with pytest.raises(ValueError, match="empty"):
        TeamLineupState(
            LineupStatus.INVALID,
            {101},  # type: ignore[arg-type]  # exercises runtime snapshotting
            "outgoing_not_on_court",
            EventBoundary(2, BoundarySide.AFTER),
        )
    with pytest.raises(ValueError, match="reason"):
        TeamLineupState(
            LineupStatus.INVALID,
            frozenset(),
            None,
            EventBoundary(2, BoundarySide.AFTER),
        )
    with pytest.raises(ValueError, match="since"):
        TeamLineupState(LineupStatus.INVALID, frozenset(), "outgoing_not_on_court", None)
    with pytest.raises(ValueError, match="must not"):
        TeamLineupState(
            LineupStatus.VALID,
            HOME_PLAYERS,
            "outgoing_not_on_court",
            EventBoundary(2, BoundarySide.AFTER),
        )


def test_segment_keeps_endpoint_metadata_and_requires_ordered_boundaries() -> None:
    segment = make_segment(
        start_period=1,
        end_period=2,
        start_seconds_remaining_period_ms=1_000,
        end_seconds_remaining_period_ms=720_000,
    )

    assert segment.start_period == 1
    assert segment.end_period == 2
    assert segment.start_seconds_remaining_period_ms == 1_000
    assert segment.end_seconds_remaining_period_ms == 720_000
    with pytest.raises(ValueError, match="boundary"):
        make_segment(
            start_boundary=EventBoundary(3, BoundarySide.BEFORE),
            end_boundary=EventBoundary(2, BoundarySide.AFTER),
        )
    with pytest.raises(ValueError, match="period"):
        make_segment(start_period=True)
    with pytest.raises(ValueError, match="clock"):
        make_segment(end_seconds_remaining_period_ms=-1)


def test_context_validates_nested_types_and_every_content_digest() -> None:
    context = make_reconstruction_context()

    assert context.lineup_revision_id == "f" * 64
    with pytest.raises(ValueError, match="game context"):
        make_reconstruction_context(game=object())
    with pytest.raises(ValueError, match="versions"):
        make_reconstruction_context(versions=object())
    with pytest.raises(ValueError, match="SHA-256"):
        make_reconstruction_context(canonical_event_sha256="A" * 64)


def test_result_validates_segment_order_status_and_deterministic_issues() -> None:
    context = make_reconstruction_context()
    first = make_segment(
        end_boundary=EventBoundary(2, BoundarySide.BEFORE),
        home_state=make_invalid_state(),
    )
    second = make_segment(
        segment_number=2,
        start_boundary=EventBoundary(2, BoundarySide.AFTER),
        end_boundary=EventBoundary(3, BoundarySide.AFTER),
        home_state=make_state(HOME_PLAYERS),
    )
    issue = LineupIssue(10, "invalid_initial_lineup", 1, BoundarySide.BEFORE)
    segments = [first, second]
    issues = [issue]
    result = LineupReconstructionResult(
        context=context,
        status=LineupStatus.INVALID,
        segments=segments,  # type: ignore[arg-type]
        issues=issues,  # type: ignore[arg-type]
    )
    segments.clear()
    issues.clear()

    assert result.status is LineupStatus.INVALID
    assert result.segments == (first, second)
    assert result.issues == (issue,)
    with pytest.raises(ValueError, match="status"):
        LineupReconstructionResult(context, LineupStatus.VALID, (first, second), (issue,))
    with pytest.raises(ValueError, match="issues"):
        LineupReconstructionResult(context, LineupStatus.INVALID, (first, second), ())
    with pytest.raises(ValueError, match="contiguous"):
        LineupReconstructionResult(
            context,
            LineupStatus.INVALID,
            (first, make_segment(segment_number=3, home_state=make_invalid_state())),
            (issue,),
        )
    with pytest.raises(ValueError, match="overlap"):
        LineupReconstructionResult(
            context,
            LineupStatus.VALID,
            (
                make_segment(end_boundary=EventBoundary(2, BoundarySide.AFTER)),
                make_segment(
                    segment_number=2,
                    start_boundary=EventBoundary(2, BoundarySide.AFTER),
                ),
            ),
            (),
        )


def test_result_bounds_issue_projection_to_first_32_unique_invalid_transitions() -> None:
    context = make_reconstruction_context()
    segments = []
    expected = []
    for index in range(33):
        sequence = index + 1
        boundary = EventBoundary(sequence, BoundarySide.BEFORE)
        state = make_invalid_state(reason=f"failure_{sequence}", boundary=boundary)
        segments.append(
            make_segment(
                segment_number=sequence,
                start_boundary=boundary,
                end_boundary=boundary,
                home_state=state,
            )
        )
        if sequence <= 32:
            expected.append(LineupIssue(10, f"failure_{sequence}", sequence, BoundarySide.BEFORE))

    result = LineupReconstructionResult(
        context,
        LineupStatus.INVALID,
        segments,  # type: ignore[arg-type]  # exercises runtime snapshotting
        expected,  # type: ignore[arg-type]  # exercises runtime snapshotting
    )

    assert len(result.issues) == 32
    assert result.issues[-1].reason == "failure_32"


def test_issue_shape_is_exact_and_domain_values_are_frozen_and_slotted() -> None:
    issue = LineupIssue(10, "outgoing_not_on_court", 3, BoundarySide.AFTER)
    state = make_state()

    assert tuple(field.name for field in fields(LineupIssue)) == (
        "team_id",
        "reason",
        "sequence_number",
        "boundary_side",
    )
    assert issue.boundary_side is BoundarySide.AFTER
    assert not hasattr(state, "__dict__")
    with pytest.raises(FrozenInstanceError):
        state.status = LineupStatus.INVALID  # type: ignore[misc]
    with pytest.raises(ValueError, match="sequence"):
        LineupIssue(10, "outgoing_not_on_court", False, BoundarySide.AFTER)
    with pytest.raises(ValueError, match="reason"):
        LineupIssue(10, "raw: provider text", 3, BoundarySide.AFTER)


def test_result_accepts_a_fully_valid_five_on_five_history() -> None:
    result = LineupReconstructionResult(
        make_reconstruction_context(),
        LineupStatus.VALID,
        (make_segment(home_state=make_state(HOME_PLAYERS), away_state=make_state(AWAY_PLAYERS)),),
        (),
    )

    assert result.status is LineupStatus.VALID
