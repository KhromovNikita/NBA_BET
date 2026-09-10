from dataclasses import FrozenInstanceError, replace

import pytest

from nba_live_totals.domain.possession_parser import AnalyticalPossessionParser
from nba_live_totals.domain.possessions import (
    Administrative,
    BoundarySide,
    ControlAward,
    EventBoundary,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    Goaltending,
    GoaltendingSide,
    IssueSeverity,
    JumpBall,
    NormalizedGameActions,
    ParseStatus,
    PeriodEnd,
    PeriodStart,
    PossessionEndReason,
    PossessionIssue,
    ReboundActor,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
)
from tests.unit.domain.possession_helpers import action, game, rebound, shot
from tests.unit.domain.test_possessions import GAME_ID, make_parse_context, parse_synthetic_game


def test_opening_control_miss_offensive_rebound_make_is_one_possession() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(JumpBall(10), shot(10, made=False), rebound(10), shot(10)),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.offense_team_id, row.defense_team_id) == (10, 20)
    assert row.start_boundary == EventBoundary(2, BoundarySide.BEFORE)
    assert row.end_boundary == EventBoundary(5, BoundarySide.AFTER)
    assert row.source_event_count == 4
    assert row.end_reason is PossessionEndReason.MADE_FIELD_GOAL
    assert (row.home_score_before, row.home_score, row.offense_points) == (0, 2, 2)


def test_defensive_rebound_switches_at_same_boundary_and_counts_event_only_once() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), game(shot(10, made=False), rebound(20), shot(20))
    )
    first, second = result.possessions
    assert first.end_reason is PossessionEndReason.DEFENSIVE_REBOUND
    assert first.end_boundary == second.start_boundary == EventBoundary(3, BoundarySide.AFTER)
    assert (first.source_event_count, second.source_event_count) == (2, 1)
    assert (first.offense_team_id, second.offense_team_id) == (10, 20)


def test_control_award_switches_at_same_boundary_and_does_not_consume_twice() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(ControlAward(10, "opening_control"), ControlAward(20, "award"), shot(20)),
    )
    first, second = result.possessions
    assert first.end_reason is PossessionEndReason.CONTROL_AWARD
    assert first.end_boundary == second.start_boundary == EventBoundary(3, BoundarySide.AFTER)
    assert (first.source_event_count, second.source_event_count) == (2, 1)
    assert (first.offense_team_id, second.offense_team_id) == (10, 20)


def test_turnover_and_linked_steal_switch_once_with_contiguous_counters() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            Turnover(10, 20, "lost_ball"),
            StealAnnotation(20, 2),
            Turnover(20, 10, "bad_pass"),
            shot(10),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert [r.possession_number for r in result.possessions] == [1, 2, 3]
    assert [r.offense_possession_number for r in result.possessions] == [1, 1, 2]
    assert [(r.home_possessions, r.away_possessions) for r in result.possessions] == [
        (1, 0),
        (1, 1),
        (2, 1),
    ]
    assert [r.source_event_count for r in result.possessions] == [1, 2, 1]


def test_made_field_goal_entitlement_does_not_open_phantom_possession() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(shot(10), Administrative("timeout"), Foul(20, "personal")),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert result.possessions[0].source_event_count == 1


def test_inferred_shot_owner_warns_but_foul_team_cannot_establish_owner() -> None:
    parser = AnalyticalPossessionParser()
    inferred = parse_synthetic_game(parser, game(shot(10)))
    assert inferred.status is ParseStatus.VALID
    assert inferred.output_sha256 is not None
    assert len(inferred.possessions) == 1
    row = inferred.possessions[0]
    assert (row.offense_team_id, row.home_score, row.away_score, row.offense_points) == (
        10,
        2,
        0,
        2,
    )
    assert PossessionIssue(IssueSeverity.WARNING, "offense_inferred", 2) in inferred.issues
    ambiguous = parser.parse(make_parse_context(), game(Foul(10, "personal"), rebound(20)))
    assert ambiguous.status is ParseStatus.INVALID
    assert "unknown_owner" in {issue.code for issue in ambiguous.issues}
    assert ambiguous.possessions == ()


@pytest.mark.parametrize("ending", [shot(20), Turnover(20, 10, "lost_ball")])
def test_pending_jump_resolves_before_action_then_applies_action_once(
    ending: object,
) -> None:
    stream = game(shot(10, made=False), JumpBall(), ending)  # type: ignore[arg-type]
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    first, second, *remaining = result.possessions
    assert first.end_reason is PossessionEndReason.JUMP_BALL_CHANGE
    assert first.end_boundary == second.start_boundary == EventBoundary(4, BoundarySide.BEFORE)
    assert second.end_boundary == EventBoundary(4, BoundarySide.AFTER)
    assert (first.source_event_count, second.source_event_count) == (2, 1)
    assert second.offense_team_id == 20
    assert len(remaining) <= 1


def test_rejump_and_same_team_winner_continue_possession() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(JumpBall(10), JumpBall(), JumpBall(), JumpBall(10), shot(10)),
    )
    assert len(result.possessions) == 1
    assert result.possessions[0].source_event_count == 5


def test_duplicate_provider_ids_and_clock_ties_preserve_sequence_and_determinism() -> None:
    stream = game(shot(10), shot(20), shot(10))
    parser = AnalyticalPossessionParser()
    result = parse_synthetic_game(parser, stream)
    assert result == parse_synthetic_game(parser, stream)
    assert [r.end_boundary.sequence_number for r in result.possessions] == [2, 3, 4]
    assert [r.offense_team_id for r in result.possessions] == [10, 20, 10]
    assert result.output_sha256 is not None
    with pytest.raises(FrozenInstanceError):
        result.possessions[0].source_event_count = 9  # type: ignore[misc]


@pytest.mark.parametrize("signal", [Unsupported(True), shot(99), ControlAward(99, "award")])
def test_unknown_control_or_foreign_team_fails_closed(signal: object) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(signal, shot(10)),  # type: ignore[arg-type]
    )
    assert result.status is ParseStatus.INVALID
    assert result.output_sha256 is None
    assert result.possessions == ()


def test_full_warning_budget_cannot_hide_fatal_error() -> None:
    stream = replace(
        game(Unsupported(True)),
        issues=tuple(PossessionIssue(IssueSeverity.WARNING, "clock_tie", n) for n in range(1, 33)),
    )
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
    assert len(result.issues) == 32
    assert any(i.code == "unknown_control_action" for i in result.issues)


def test_unknown_administrative_signal_warns_without_changing_control() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), game(ControlAward(10, "award"), Unsupported(False), shot(10))
    )
    assert result.status is ParseStatus.VALID
    assert result.possessions[0].source_event_count == 3
    assert "unsupported_administrative_action" in {i.code for i in result.issues}


def test_unresolved_jump_and_incomplete_input_fail_closed() -> None:
    parser = AnalyticalPossessionParser()
    pending = parser.parse(make_parse_context(), game(JumpBall()))
    assert pending.status is ParseStatus.INVALID
    assert "unresolved_jump_ball" in {i.code for i in pending.issues}
    partial = parser.parse(
        make_parse_context(), NormalizedGameActions(GAME_ID, (action(1, shot(10)),))
    )
    assert partial.status is ParseStatus.INVALID
    assert "incomplete_play_by_play" in {i.code for i in partial.issues}


def test_team_rebound_waits_for_explicit_control_before_switch() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            shot(10, made=False),
            rebound(20, actor=ReboundActor.TEAM),
            Administrative("timeout"),
            ControlAward(20, "award"),
            shot(20),
        ),
    )
    first, second = result.possessions
    assert first.end_boundary == second.start_boundary == EventBoundary(5, BoundarySide.AFTER)
    assert first.end_reason is PossessionEndReason.DEFENSIVE_REBOUND
    assert (first.source_event_count, second.source_event_count) == (4, 1)


def test_team_rebound_then_period_end_closes_only_shooting_possession() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(shot(10, made=False), rebound(20, actor=ReboundActor.TEAM)),
    )
    assert len(result.possessions) == 1
    assert result.possessions[0].end_reason is PossessionEndReason.PERIOD_END


def test_retained_violation_and_orphan_steal_do_not_switch_control() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "award"),
            Violation(20, ViolationDisposition.RETAIN_OFFENSE),
            StealAnnotation(20),
            shot(10),
        ),
    )
    assert len(result.possessions) == 1
    assert result.possessions[0].source_event_count == 4
    assert "orphan_steal_annotation" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    ("signal", "reason", "points"),
    [
        (Goaltending(20, GoaltendingSide.DEFENSIVE, 2), PossessionEndReason.MADE_FIELD_GOAL, 2),
        (Goaltending(10, GoaltendingSide.OFFENSIVE, 0), PossessionEndReason.TURNOVER, 0),
        (Violation(10, ViolationDisposition.OPPONENT_AWARD), PossessionEndReason.TURNOVER, 0),
    ],
)
def test_goaltending_and_offensive_violation_follow_ordinary_end_semantics(
    signal: object,
    reason: PossessionEndReason,
    points: int,
) -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(ControlAward(10, "award"), signal),  # type: ignore[arg-type]
    )
    assert result.possessions[0].end_reason is reason
    assert result.possessions[0].offense_points == points


@pytest.mark.parametrize(
    ("escape", "final_shot"),
    [
        (JumpBall(10), shot(10)),
        (Goaltending(20, GoaltendingSide.DEFENSIVE, 2), shot(20)),
    ],
)
def test_deferred_free_throw_path_cannot_escape_via_ordinary_transition(
    escape: object, final_shot: FieldGoalAttempt
) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(
            ControlAward(10, "opening_control"),
            FreeThrowAttempt("ft:0022500123:3:0", 1, 1, made=True, awarded_points=1),
            escape,  # type: ignore[arg-type]
            final_shot,
        ),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {issue.code for issue in result.issues}


def test_unresolved_jump_with_active_frame_fails_closed_at_period_end() -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(ControlAward(10, "opening_control"), JumpBall())
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_jump_ball" in {issue.code for issue in result.issues}


def test_pending_jump_winner_resolves_before_winning_jump_action_once() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), game(shot(10, made=False), JumpBall(), JumpBall(20), shot(20))
    )
    first, second = result.possessions
    assert first.end_boundary == second.start_boundary == EventBoundary(4, BoundarySide.BEFORE)
    assert first.end_reason is PossessionEndReason.JUMP_BALL_CHANGE
    assert (first.source_event_count, second.source_event_count) == (2, 2)
    assert (first.offense_team_id, second.offense_team_id) == (10, 20)


def test_and_one_candidate_fails_closed_until_free_throw_procedure_is_supported() -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(FieldGoalAttempt(10, 2, made=True, and_one_candidate=True))
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {issue.code for issue in result.issues}


def test_active_possession_cannot_cross_period_without_a_closing_boundary() -> None:
    stream = NormalizedGameActions(
        GAME_ID,
        (
            action(1, PeriodStart(), period=1),
            action(2, ControlAward(10, "opening_control"), period=1),
            action(3, shot(10), period=2),
            action(4, PeriodEnd(), period=2),
            action(5, PeriodStart(), period=3),
            action(6, PeriodEnd(), period=3),
            action(7, PeriodStart(), period=4),
            action(8, PeriodEnd(), period=4),
        ),
    )
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
