"""Procedure regressions with hand-derived rows and sequence boundaries."""

from dataclasses import replace

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
    FreeThrowKind,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    JumpBall,
    NormalizedAction,
    NormalizedGameActions,
    ParseStatus,
    PeriodEnd,
    PeriodStart,
    PossessionEndReason,
    PossessionSignal,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    ResumeDisposition,
    StealAnnotation,
    Turnover,
    Violation,
    ViolationDisposition,
)
from tests.unit.domain.possession_helpers import action, game, rebound, shot
from tests.unit.domain.test_possessions import GAME_ID, make_parse_context, parse_synthetic_game

TRIP = "ft:0022500123:3:1"
TECH = "ft:0022500123:4:2"


def start(
    *,
    trip: str = TRIP,
    team: int = 10,
    kind: FreeThrowKind = FreeThrowKind.ORDINARY,
    total: int = 2,
    resume: ResumeDisposition = ResumeDisposition.OPPONENT_INBOUND,
    retain: int | None = None,
    parent: str | None = None,
) -> FreeThrowTripStart:
    return FreeThrowTripStart(trip, team, kind, total, resume, retain, parent)


def attempt(ordinal: int, *, total: int = 2, made: bool = True) -> FreeThrowAttempt:
    return FreeThrowAttempt(TRIP, ordinal, total, made, int(made))


def technical(
    *, team: int = 20, trip: str = TECH, parent: str | None = None
) -> tuple[PossessionSignal, ...]:
    return (
        start(
            trip=trip,
            team=team,
            kind=FreeThrowKind.TECHNICAL,
            total=1,
            resume=ResumeDisposition.RESUME_INTERRUPTED,
            parent=parent,
        ),
        FreeThrowAttempt(trip, 1, 1, True, 1),
        FreeThrowTripEnd(trip, ResumeDisposition.RESUME_INTERRUPTED),
    )


@pytest.mark.parametrize("total", [2, 3])
def test_ordinary_trip_counts_envelopes_and_closes_only_at_final_resolution(total: int) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = [
        ControlAward(10, "opening"),
        (start(total=total), attempt(1, total=total)),
    ]
    if total == 3:
        signals.append(attempt(2, total=3))
    signals.append(
        (attempt(total, total=total), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND))
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert row.start_boundary == EventBoundary(2, BoundarySide.BEFORE)
    assert row.end_boundary == EventBoundary(4 if total == 2 else 5, BoundarySide.AFTER)
    assert row.source_event_count == (3 if total == 2 else 4)
    assert (row.home_score_before, row.home_score, row.offense_points, row.defense_points) == (
        0,
        total,
        total,
        0,
    )
    assert row.end_reason is PossessionEndReason.FINAL_FREE_THROW


@pytest.mark.parametrize("team", [10, 20])
def test_final_ft_miss_waits_for_offensive_or_defensive_rebound(team: int) -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(resume=ResumeDisposition.LIVE_REBOUND), attempt(1)),
            (attempt(2, made=False), FreeThrowTripEnd(TRIP, ResumeDisposition.LIVE_REBOUND)),
            rebound(team),
            shot(team),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == (1 if team == 10 else 2)
    first = result.possessions[0]
    assert first.offense_points == (3 if team == 10 else 1)
    assert first.source_event_count == (5 if team == 10 else 4)
    assert first.end_reason is (
        PossessionEndReason.MADE_FIELD_GOAL if team == 10 else PossessionEndReason.DEFENSIVE_REBOUND
    )
    if team == 20:
        assert (
            first.end_boundary
            == result.possessions[1].start_boundary
            == EventBoundary(5, BoundarySide.AFTER)
        )
        assert result.possessions[1].source_event_count == 1


@pytest.mark.parametrize("made", [True, False])
def test_and_one_keeps_made_field_goal_and_trip_in_one_possession(made: bool) -> None:
    resolution = ResumeDisposition.OPPONENT_INBOUND if made else ResumeDisposition.LIVE_REBOUND
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = [
        FieldGoalAttempt(10, 2, True, True),
        (
            start(kind=FreeThrowKind.AND_ONE, total=1, resume=resolution),
            attempt(1, total=1, made=made),
            FreeThrowTripEnd(TRIP, resolution),
        ),
    ]
    if not made:
        signals.extend([rebound(10), shot(10)])
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.offense_points, row.source_event_count) == ((3, 2) if made else (4, 4))
    assert row.end_reason is (
        PossessionEndReason.FINAL_FREE_THROW if made else PossessionEndReason.MADE_FIELD_GOAL
    )


@pytest.mark.parametrize(
    "kind",
    [
        FreeThrowKind.FLAGRANT,
        FreeThrowKind.CLEAR_PATH,
        FreeThrowKind.AWAY_FROM_PLAY,
        FreeThrowKind.TRANSITION_TAKE,
    ],
)
@pytest.mark.parametrize("made", [True, False])
def test_special_ft_uses_retained_team_even_after_final_miss(
    kind: FreeThrowKind, made: bool
) -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (
                start(kind=kind, total=1, resume=ResumeDisposition.RETAIN_TEAM, retain=10),
                attempt(1, total=1, made=made),
                FreeThrowTripEnd(TRIP, ResumeDisposition.RETAIN_TEAM, 10),
            ),
            shot(10),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.offense_team_id, row.offense_points, row.source_event_count) == (
        10,
        3 if made else 2,
        3,
    )
    assert row.end_reason is PossessionEndReason.MADE_FIELD_GOAL


def test_transition_take_foul_trip_retains_shooting_team_in_one_possession() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(20, "opening"),
            Foul(10, "transition_take", 20, related_sequence_number=4),
            (
                start(
                    team=20,
                    kind=FreeThrowKind.TRANSITION_TAKE,
                    total=1,
                    resume=ResumeDisposition.RETAIN_TEAM,
                    retain=20,
                ),
                attempt(1, total=1),
                FreeThrowTripEnd(TRIP, ResumeDisposition.RETAIN_TEAM, 20),
            ),
            shot(20),
        ),
    )

    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.offense_team_id, row.offense_points, row.source_event_count) == (20, 3, 4)
    assert row.end_reason is PossessionEndReason.MADE_FIELD_GOAL


def test_technical_during_possession_adds_defense_points_without_switch() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), game(ControlAward(10, "opening"), technical(), shot(10))
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (
        row.offense_team_id,
        row.offense_points,
        row.defense_points,
        row.source_event_count,
    ) == (10, 2, 1, 3)


def test_technical_between_possessions_restores_inbound_entitlement_and_baseline() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), game(shot(10), technical(team=10), shot(20))
    )
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert (first.home_score, first.away_score, first.offense_points) == (2, 0, 2)
    assert (
        second.home_score_before,
        second.away_score_before,
        second.offense_points,
        second.defense_points,
    ) == (3, 0, 2, 0)
    assert second.start_boundary == EventBoundary(4, BoundarySide.BEFORE)
    assert (first.source_event_count, second.source_event_count) == (1, 1)


def test_technical_before_opening_control_is_baseline_without_phantom_row() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(technical(team=10), ControlAward(20, "opening"), shot(20)),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.home_score_before, row.away_score_before, row.defense_points) == (1, 0, 0)
    assert row.start_boundary == EventBoundary(3, BoundarySide.BEFORE)


@pytest.mark.parametrize("opening", [False, True])
def test_unattributed_technical_baseline_fails_when_period_ends(opening: bool) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = [] if opening else [shot(10)]
    signals.append(technical())
    result = AnalyticalPossessionParser().parse(make_parse_context(), game(*signals))
    assert result.status is ParseStatus.INVALID
    assert "unattributed_administrative_score" in {i.code for i in result.issues}
    assert result.possessions == ()


@pytest.mark.parametrize("team_rebound", [False, True])
def test_technical_restores_pending_rebound_and_team_award(team_rebound: bool) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = [shot(10, made=False)]
    if team_rebound:
        signals.append(rebound(20, actor=ReboundActor.TEAM))
    signals.append(technical())
    signals.extend([ControlAward(20, "award") if team_rebound else rebound(20), shot(20)])
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert (first.offense_team_id, first.defense_points, first.source_event_count) == (
        10,
        1,
        4 if team_rebound else 3,
    )
    assert first.end_reason is PossessionEndReason.DEFENSIVE_REBOUND
    assert second.source_event_count == 1


@pytest.mark.parametrize("active", [False, True])
def test_technical_restores_pending_jump_before_next_explicit_owner(active: bool) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = (
        [shot(10, made=False)] if active else []
    )
    signals.extend([JumpBall(), technical(), shot(20)])
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    if active:
        first, second = result.possessions
        assert first.end_reason is PossessionEndReason.JUMP_BALL_CHANGE
        assert first.end_boundary == second.start_boundary == EventBoundary(5, BoundarySide.BEFORE)
        assert (first.defense_points, first.source_event_count, second.source_event_count) == (
            1,
            3,
            1,
        )
    else:
        assert len(result.possessions) == 1
        assert result.possessions[0].away_score_before == 1
        assert result.possessions[0].offense_points == 2


def test_nested_technical_restores_parent_ordinal_and_scoring_frame() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            technical(parent=TRIP),
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.offense_points, row.defense_points, row.source_event_count) == (2, 1, 4)
    assert row.end_boundary == EventBoundary(5, BoundarySide.AFTER)


@pytest.mark.parametrize(
    "bad", [ControlAward(20, "conflict"), JumpBall(20), shot(10), Turnover(10, 20, "lost_ball")]
)
def test_control_event_cannot_escape_open_technical_procedure(bad: PossessionSignal) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(ControlAward(10, "opening"), technical()[0], bad)
    )
    assert result.status is ParseStatus.INVALID
    assert "conflicting_control" in {i.code for i in result.issues}


@pytest.mark.parametrize("team", [20, None])
@pytest.mark.parametrize("ordinal", [1, 2])
def test_lane_retry_repeats_missed_ordinal_without_boundary(team: int | None, ordinal: int) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = [
        ControlAward(10, "opening"),
        (start(), attempt(1, made=ordinal != 1)),
    ]
    if ordinal == 2:
        signals.append(attempt(2, made=False))
    signals.extend([Violation(team, ViolationDisposition.RETRY), attempt(ordinal)])
    if ordinal == 1:
        signals.append((attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)))
    else:
        signals.append(FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND))
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert result.possessions[0].offense_points == 2
    assert result.possessions[0].source_event_count == (5 if ordinal == 1 else 6)


def test_forfeited_final_ft_consumes_violation_and_award_without_double_switch() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            (attempt(2, made=False), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
            Violation(10, ViolationDisposition.OPPONENT_AWARD),
            Turnover(10, 20, "lane_violation"),
            shot(20),
        ),
    )
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert first.end_boundary == second.start_boundary == EventBoundary(6, BoundarySide.AFTER)
    assert first.end_reason is PossessionEndReason.TURNOVER
    assert (first.offense_points, first.source_event_count, second.source_event_count) == (1, 5, 1)


def test_double_lane_jump_resolution_preserves_frame_until_winner() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(resume=ResumeDisposition.JUMP_BALL), attempt(1)),
            (attempt(2, made=False), FreeThrowTripEnd(TRIP, ResumeDisposition.JUMP_BALL)),
            Violation(None, ViolationDisposition.JUMP_BALL),
            JumpBall(20),
            shot(20),
        ),
    )
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert first.end_boundary == second.start_boundary == EventBoundary(6, BoundarySide.BEFORE)
    assert (
        first.end_reason,
        first.offense_points,
        first.source_event_count,
        second.source_event_count,
    ) == (PossessionEndReason.JUMP_BALL_CHANGE, 1, 4, 2)


def test_ft_placeholder_does_not_change_owner_between_attempts() -> None:
    placeholder = replace(
        rebound(20, actor=ReboundActor.TEAM), disposition=ReboundDisposition.FT_PLACEHOLDER
    )
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1, made=False)),
            placeholder,
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert (result.possessions[0].offense_points, result.possessions[0].source_event_count) == (
        1,
        4,
    )


@pytest.mark.parametrize(
    "disposition,credited",
    [(ReboundDisposition.RETAIN_TEAM, 20), (ReboundDisposition.AWARD_OPPONENT, 10)],
)
def test_explicit_team_rebound_disposition_assigns_control_once(
    disposition: ReboundDisposition, credited: int
) -> None:
    awarded: Rebound = replace(rebound(credited, actor=ReboundActor.TEAM), disposition=disposition)
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(shot(10, made=False), awarded, ControlAward(20, "award"), shot(20)),
    )
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert first.end_reason is PossessionEndReason.DEFENSIVE_REBOUND
    assert first.end_boundary == second.start_boundary == EventBoundary(3, BoundarySide.AFTER)
    assert (first.source_event_count, second.source_event_count) == (2, 2)


@pytest.mark.parametrize("winner", [10, 20])
def test_direct_jump_winner_switches_only_for_opponent(winner: int) -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(ControlAward(10, "opening"), JumpBall(winner), shot(winner)),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == (1 if winner == 10 else 2)
    if winner == 20:
        first, second = result.possessions
        assert first.end_boundary == second.start_boundary == EventBoundary(3, BoundarySide.AFTER)
        assert (first.source_event_count, second.source_event_count) == (2, 1)
    else:
        assert result.possessions[0].source_event_count == 3


@pytest.mark.parametrize("made", [True, False])
def test_buzzer_shot_closes_only_shooting_possession(made: bool) -> None:
    stream = game(shot(10, made=made))
    stream = replace(
        stream,
        actions=(stream.actions[0], replace(stream.actions[1], clock_ms=0), *stream.actions[2:]),
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert row.end_reason is (
        PossessionEndReason.MADE_FIELD_GOAL if made else PossessionEndReason.PERIOD_END
    )
    assert row.source_event_count == (1 if made else 2)


def test_exact_missing_period_boundary_closes_at_last_zero_clock_action() -> None:
    stream = game(ControlAward(10, "opening"))
    actions = list(stream.actions)
    actions[1] = replace(actions[1], clock_ms=0)
    del actions[2]
    stream = replace(
        stream, actions=tuple(replace(item, sequence_number=n) for n, item in enumerate(actions, 1))
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    row = result.possessions[0]
    assert row.end_boundary == EventBoundary(2, BoundarySide.AFTER)
    assert (
        row.period,
        row.source_event_count,
        row.end_seconds_remaining_period_ms,
        row.seconds_remaining_game_ms,
    ) == (1, 1, 0, 2_160_000)
    assert row.end_reason is PossessionEndReason.PERIOD_END
    assert "synthesized_period_boundary" in {i.code for i in result.issues}


@pytest.mark.parametrize("last", [shot(10), shot(10, made=False), JumpBall(), start()])
def test_missing_period_marker_cannot_hide_nonzero_clock_or_unresolved_state(
    last: PossessionSignal,
) -> None:
    stream = game(last)
    actions = [
        item
        for item in stream.actions
        if not (item.period == 1 and isinstance(item.signals[0], PeriodEnd))
    ]
    if not isinstance(last, type(shot(10))) or not last.made:
        actions[1] = replace(actions[1], clock_ms=0)
    stream = replace(
        stream, actions=tuple(replace(item, sequence_number=n) for n, item in enumerate(actions, 1))
    )
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
    assert result.possessions == ()


def test_missing_final_period_marker_never_synthesizes() -> None:
    stream = game(shot(10))
    stream = replace(stream, actions=stream.actions[:-1])
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
    assert "missing_final_period_end" in {i.code for i in result.issues}


@pytest.mark.parametrize("period", [5, 6, 65_535])
def test_overtime_has_no_rule_cap_and_remaining_time_is_current_period_only(period: int) -> None:
    actions: list[NormalizedAction] = []
    for number in range(1, period + 1):
        actions.append(
            replace(
                action(len(actions) + 1, PeriodStart(), period=number),
                clock_ms=300_000 if number >= 5 else 720_000,
            )
        )
        if number == period:
            actions.append(
                replace(action(len(actions) + 1, shot(10), period=number), clock_ms=12_345)
            )
        actions.append(action(len(actions) + 1, PeriodEnd(), period=number))
    result = parse_synthetic_game(
        AnalyticalPossessionParser(), NormalizedGameActions(GAME_ID, tuple(actions))
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (row.period, row.seconds_remaining_game_ms, row.source_event_count, row.home_score) == (
        period,
        12_345,
        1,
        2,
    )


@pytest.mark.parametrize(
    "bad_end",
    [
        FreeThrowTripEnd(TRIP, ResumeDisposition.LIVE_REBOUND),
        FreeThrowTripEnd(TECH, ResumeDisposition.OPPONENT_INBOUND),
    ],
)
def test_trip_end_cannot_change_declared_resolution_or_close_wrong_trip(
    bad_end: FreeThrowTripEnd,
) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(ControlAward(10, "opening"), (start(), attempt(1)), (attempt(2), bad_end)),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}


def test_restored_entitlement_rejects_wrong_team_after_technical() -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(shot(10), technical(), shot(10))
    )
    assert result.status is ParseStatus.INVALID
    assert "conflicting_control" in {i.code for i in result.issues}


def test_open_trip_at_complete_period_reports_unresolved_procedure() -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(ControlAward(10, "opening"), (start(), attempt(1)), Administrative("timeout")),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    "ending", [Turnover(10, 20, "lost_ball"), JumpBall(20), ControlAward(20, "award"), rebound(20)]
)
def test_buzzer_control_change_does_not_fabricate_opponent_period_end_row(
    ending: PossessionSignal,
) -> None:
    opening = shot(10, made=False) if isinstance(ending, Rebound) else ControlAward(10, "opening")
    stream = game(opening, ending)
    stream = replace(
        stream,
        actions=tuple(
            replace(item, clock_ms=0) if item.sequence_number == 3 else item
            for item in stream.actions
        ),
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert result.possessions[0].end_boundary == EventBoundary(3, BoundarySide.AFTER)
    assert result.possessions[0].source_event_count == 2


@pytest.mark.parametrize("made", [True, False])
def test_buzzer_final_ft_does_not_open_inbound_possession(made: bool) -> None:
    resolution = ResumeDisposition.OPPONENT_INBOUND if made else ResumeDisposition.LIVE_REBOUND
    stream = game(
        ControlAward(10, "opening"),
        (start(resume=resolution), attempt(1)),
        (attempt(2, made=made), FreeThrowTripEnd(TRIP, resolution)),
    )
    stream = replace(
        stream,
        actions=tuple(
            replace(item, clock_ms=0) if item.sequence_number == 4 else item
            for item in stream.actions
        ),
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert row.end_reason is (
        PossessionEndReason.FINAL_FREE_THROW if made else PossessionEndReason.PERIOD_END
    )
    assert (row.offense_points, row.source_event_count) == ((2, 3) if made else (1, 4))


def test_technical_inside_technical_restores_both_parent_frames() -> None:
    inner = "ft:0022500123:5:3"
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            technical(parent=TRIP)[0],
            technical(team=10, trip=inner, parent=TECH),
            technical(parent=TRIP)[1:],
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    row = result.possessions[0]
    assert (
        row.offense_team_id,
        row.offense_points,
        row.defense_points,
        row.source_event_count,
    ) == (10, 3, 1, 6)
    assert row.end_boundary == EventBoundary(7, BoundarySide.AFTER)


def test_technical_restores_pending_and_one_link_until_ordinary_trip() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            FieldGoalAttempt(10, 2, True, True),
            technical(),
            (
                start(kind=FreeThrowKind.AND_ONE, total=1),
                attempt(1, total=1),
                FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND),
            ),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert (
        result.possessions[0].offense_points,
        result.possessions[0].defense_points,
        result.possessions[0].source_event_count,
    ) == (3, 1, 3)


def test_technical_restores_pending_forfeiture_and_its_confirmation() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            (attempt(2, made=False), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
            Violation(10, ViolationDisposition.OPPONENT_AWARD),
            technical(),
            Turnover(10, 20, "lane_violation"),
            shot(20),
        ),
    )
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert (first.offense_points, first.defense_points, first.source_event_count) == (1, 1, 6)
    assert first.end_boundary == second.start_boundary == EventBoundary(7, BoundarySide.AFTER)


@pytest.mark.parametrize(
    "malformed",
    [
        (attempt(2),),
        (attempt(1), attempt(1)),
        (attempt(1), FreeThrowAttempt(TECH, 2, 2, True, 1)),
        (attempt(1), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        (attempt(1), start(trip=TECH)),
        (attempt(1), technical(parent=TECH)[0]),
    ],
)
def test_invalid_ft_ordinals_parent_or_early_end_fail_closed(
    malformed: tuple[PossessionSignal, ...],
) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(ControlAward(10, "opening"), start(), *malformed)
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}
    assert result.possessions == ()
    assert result.output_sha256 is None


def test_reusing_completed_trip_id_cannot_open_second_procedure() -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(), game(ControlAward(10, "opening"), technical(), technical(), shot(10))
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}


@pytest.mark.parametrize("ordinal", [1, 2])
def test_lane_retry_cannot_void_made_attempt(ordinal: int) -> None:
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            *((attempt(2),) if ordinal == 2 else ()),
            Violation(20, ViolationDisposition.RETRY),
        ),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}


def test_final_ft_miss_cannot_be_labeled_nonfinal_placeholder() -> None:
    placeholder = replace(
        rebound(20, actor=ReboundActor.TEAM), disposition=ReboundDisposition.FT_PLACEHOLDER
    )
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1)),
            attempt(2, made=False),
            placeholder,
            Violation(20, ViolationDisposition.RETRY),
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_rebound" in {i.code for i in result.issues}


def test_explicit_double_lane_retry_is_not_overridden_by_credited_team() -> None:
    result = parse_synthetic_game(
        AnalyticalPossessionParser(),
        game(
            ControlAward(10, "opening"),
            (start(), attempt(1, made=False)),
            Violation(10, ViolationDisposition.RETRY),
            attempt(1),
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ),
    )
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert result.possessions[0].offense_points == 2


def test_final_period_only_input_cannot_masquerade_as_complete_game() -> None:
    stream = NormalizedGameActions(
        GAME_ID,
        (
            action(1, PeriodStart(), period=4),
            action(2, shot(10), period=4),
            action(3, PeriodEnd(), period=4),
        ),
    )
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
    assert "incomplete_play_by_play" in {i.code for i in result.issues}


def test_missing_boundary_after_buzzer_make_synthesizes_without_extra_row() -> None:
    stream = game(shot(10))
    actions = [
        replace(item, clock_ms=0) if item.sequence_number == 2 else item
        for item in stream.actions
        if item.sequence_number != 3
    ]
    stream = replace(
        stream, actions=tuple(replace(item, sequence_number=n) for n, item in enumerate(actions, 1))
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == 1
    assert result.possessions[0].end_reason is PossessionEndReason.MADE_FIELD_GOAL
    assert result.possessions[0].source_event_count == 1
    assert "synthesized_period_boundary" in {i.code for i in result.issues}


def test_synthesis_cannot_skip_a_period_even_at_zero_clock() -> None:
    stream = NormalizedGameActions(
        GAME_ID,
        (
            action(1, PeriodStart()),
            replace(action(2, shot(10)), clock_ms=0),
            action(3, PeriodStart(), period=3),
            action(4, PeriodEnd(), period=3),
            action(5, PeriodStart(), period=4),
            action(6, PeriodEnd(), period=4),
        ),
    )
    result = AnalyticalPossessionParser().parse(make_parse_context(), stream)
    assert result.status is ParseStatus.INVALID
    assert "incomplete_play_by_play" in {i.code for i in result.issues}


def test_one_envelope_cannot_start_two_trips_at_the_same_depth() -> None:
    another = "ft:0022500123:4:1"
    result = AnalyticalPossessionParser().parse(
        make_parse_context(),
        game(ControlAward(10, "opening"), (*technical(), *technical(trip=another)), shot(10)),
    )
    assert result.status is ParseStatus.INVALID
    assert "unresolved_free_throw_trip" in {i.code for i in result.issues}


@pytest.mark.parametrize("active_owner", [None, 10, 20])
def test_ordinary_trip_resolves_unknown_jump_before_first_attempt_once(
    active_owner: int | None,
) -> None:
    signals: list[PossessionSignal | tuple[PossessionSignal, ...]] = []
    if active_owner is not None:
        signals.append(ControlAward(active_owner, "opening"))
    signals.extend(
        [
            JumpBall(),
            Foul(20, "shooting", shooting_team=10),
            (start(), attempt(1)),
            (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
        ]
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), game(*signals))
    assert result.status is ParseStatus.VALID
    assert len(result.possessions) == (2 if active_owner == 20 else 1)
    row = result.possessions[-1]
    assert (row.offense_team_id, row.offense_points) == (10, 2)
    assert row.end_reason is PossessionEndReason.FINAL_FREE_THROW
    if active_owner is None:
        assert row.start_boundary == EventBoundary(4, BoundarySide.BEFORE)
        assert row.end_boundary == EventBoundary(5, BoundarySide.AFTER)
        assert row.source_event_count == 2
    elif active_owner == 10:
        assert row.start_boundary == EventBoundary(2, BoundarySide.BEFORE)
        assert row.source_event_count == 5
    else:
        previous = result.possessions[0]
        assert previous.end_boundary == row.start_boundary == EventBoundary(5, BoundarySide.BEFORE)
        assert previous.end_reason is PossessionEndReason.JUMP_BALL_CHANGE
        assert (previous.source_event_count, row.source_event_count) == (3, 2)


@pytest.mark.parametrize("continues", [False, True])
def test_zero_clock_turnover_preserves_real_boundary_without_phantom_period_end(
    continues: bool,
) -> None:
    signals: list[PossessionSignal] = [
        ControlAward(10, "opening"),
        Turnover(10, 20, "lost_ball"),
    ]
    if continues:
        signals.extend([StealAnnotation(20, 3), shot(20)])
    stream = game(*signals)
    stream = replace(
        stream,
        actions=tuple(
            replace(item, clock_ms=0) if item.period == 1 and item.sequence_number >= 3 else item
            for item in stream.actions
        ),
    )
    result = parse_synthetic_game(AnalyticalPossessionParser(), stream)
    assert result.status is ParseStatus.VALID
    first = result.possessions[0]
    assert first.end_boundary == EventBoundary(3, BoundarySide.AFTER)
    assert (first.offense_team_id, first.source_event_count) == (10, 2)
    assert first.end_reason is PossessionEndReason.TURNOVER
    if continues:
        assert len(result.possessions) == 2
        second = result.possessions[1]
        assert second.start_boundary == first.end_boundary
        assert second.end_boundary == EventBoundary(5, BoundarySide.AFTER)
        assert (second.offense_team_id, second.source_event_count, second.offense_points) == (
            20,
            2,
            2,
        )
        assert second.end_reason is PossessionEndReason.MADE_FIELD_GOAL
    else:
        assert len(result.possessions) == 1
