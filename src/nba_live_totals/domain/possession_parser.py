"""Pure reconstruction of ordinary, provider-neutral analytical possessions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nba_live_totals.domain.possession_hashing import possession_output_checksum
from nba_live_totals.domain.possessions import (
    Administrative,
    BoundarySide,
    ControlAward,
    EventBoundary,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    Goaltending,
    GoaltendingSide,
    IssueSeverity,
    JumpBall,
    NormalizedAction,
    NormalizedGameActions,
    ParseStatus,
    PeriodEnd,
    PeriodStart,
    Possession,
    PossessionEndReason,
    PossessionIssue,
    PossessionParseContext,
    PossessionParseResult,
    PossessionSignal,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
)

_MAX_ISSUES = 32


class _State(StrEnum):
    WAITING_FOR_CONTROL = "waiting_for_control"
    CONTROLLED = "controlled"
    AWAITING_REBOUND = "awaiting_rebound"
    FT_PROCEDURE = "ft_procedure"
    JUMP_PENDING = "jump_pending"
    COMPLETE = "complete"


@dataclass(slots=True)
class _Frame:
    offense_team_id: int
    defense_team_id: int
    period: int
    start_boundary: EventBoundary
    start_clock_ms: int
    home_score_before: int
    away_score_before: int
    source_event_count: int = 0
    last_consumed_sequence: int = 0


class AnalyticalPossessionParser:
    """Build immutable possessions from a sequence-ordered semantic action stream."""

    def parse(
        self,
        context: PossessionParseContext,
        normalized: NormalizedGameActions,
    ) -> PossessionParseResult:
        machine = _PossessionMachine(context, normalized)
        return machine.run()


class _PossessionMachine:
    def __init__(self, context: PossessionParseContext, normalized: NormalizedGameActions) -> None:
        self._context = context
        self._normalized = normalized
        self._issues = list(normalized.issues)
        self._possessions: list[Possession] = []
        self._state = _State.WAITING_FOR_CONTROL
        self._frame: _Frame | None = None
        self._entitlement_team: int | None = None
        self._team_rebound_pending = False
        self._home_score = 0
        self._away_score = 0
        self._home_possessions = 0
        self._away_possessions = 0
        self._last_period: int | None = None
        self._last_clock_ms: int | None = None

    def run(self) -> PossessionParseResult:
        if self._normalized.game_id != self._context.game.game_id:
            self._error("mixed_game")
        expected_sequence = 1
        for action in self._normalized.actions:
            if action.sequence_number != expected_sequence:
                self._error("sequence_gap", action.sequence_number)
                break
            expected_sequence += 1
            self._apply_action(action)
            if self._has_error:
                break

        if not self._has_error and self._state is not _State.COMPLETE:
            self._error("incomplete_play_by_play")
        if self._has_error:
            return PossessionParseResult(
                context=self._context,
                status=ParseStatus.INVALID,
                possessions=(),
                issues=tuple(self._issues),
                output_sha256=None,
            )
        if not self._possessions:
            self._error("incomplete_play_by_play")
            return PossessionParseResult(
                context=self._context,
                status=ParseStatus.INVALID,
                possessions=(),
                issues=tuple(self._issues),
                output_sha256=None,
            )
        possessions = tuple(self._possessions)
        return PossessionParseResult(
            context=self._context,
            status=ParseStatus.VALID,
            possessions=possessions,
            issues=tuple(self._issues),
            output_sha256=possession_output_checksum(
                parse_revision_id=self._context.parse_revision_id,
                game=self._context.game,
                possessions=possessions,
            ),
        )

    @property
    def _has_error(self) -> bool:
        return any(issue.severity is IssueSeverity.ERROR for issue in self._issues)

    def _issue(
        self,
        severity: IssueSeverity,
        code: str,
        sequence_number: int | None = None,
        related_sequence_number: int | None = None,
    ) -> None:
        issue = PossessionIssue(severity, code, sequence_number, related_sequence_number)
        if issue in self._issues:
            return
        if len(self._issues) >= _MAX_ISSUES:
            if severity is IssueSeverity.WARNING:
                return
            for index, existing in enumerate(self._issues):
                if existing.severity is IssueSeverity.WARNING:
                    del self._issues[index]
                    break
            else:
                return
        self._issues.append(issue)

    def _error(self, code: str, sequence_number: int | None = None) -> None:
        self._issue(IssueSeverity.ERROR, code, sequence_number)

    def _warning(self, code: str, sequence_number: int | None = None) -> None:
        self._issue(IssueSeverity.WARNING, code, sequence_number)

    def _team_is_valid(self, team_id: int) -> bool:
        return team_id in {self._context.game.home_team_id, self._context.game.away_team_id}

    def _other_team(self, team_id: int) -> int:
        return (
            self._context.game.away_team_id
            if team_id == self._context.game.home_team_id
            else self._context.game.home_team_id
        )

    def _start(
        self,
        team_id: int,
        action: NormalizedAction,
        side: BoundarySide,
        *,
        inferred: bool = False,
    ) -> None:
        if not self._team_is_valid(team_id):
            self._error("unknown_owner", action.sequence_number)
            return
        if self._entitlement_team is not None and self._entitlement_team != team_id:
            self._error("conflicting_control", action.sequence_number)
            return
        if inferred:
            self._warning("offense_inferred", action.sequence_number)
        self._frame = _Frame(
            offense_team_id=team_id,
            defense_team_id=self._other_team(team_id),
            period=action.period,
            start_boundary=EventBoundary(action.sequence_number, side),
            start_clock_ms=action.clock_ms,
            home_score_before=self._home_score,
            away_score_before=self._away_score,
            last_consumed_sequence=action.sequence_number if side is BoundarySide.AFTER else 0,
        )
        self._state = _State.CONTROLLED
        self._entitlement_team = None
        self._team_rebound_pending = False

    def _consume(self, action: NormalizedAction) -> None:
        if self._frame is not None and self._frame.last_consumed_sequence != action.sequence_number:
            self._frame.source_event_count += 1
            self._frame.last_consumed_sequence = action.sequence_number

    def _award_points(self, team_id: int, points: int, action: NormalizedAction) -> None:
        if points == 0:
            return
        if team_id == self._context.game.home_team_id:
            self._home_score += points
        elif team_id == self._context.game.away_team_id:
            self._away_score += points
        else:
            self._error("unknown_owner", action.sequence_number)

    def _finish(
        self, action: NormalizedAction, reason: PossessionEndReason, side: BoundarySide
    ) -> None:
        frame = self._frame
        if frame is None:
            self._error("unknown_owner", action.sequence_number)
            return
        if frame.offense_team_id == self._context.game.home_team_id:
            self._home_possessions += 1
        else:
            self._away_possessions += 1
        home_delta = self._home_score - frame.home_score_before
        away_delta = self._away_score - frame.away_score_before
        offense_points = (
            home_delta if frame.offense_team_id == self._context.game.home_team_id else away_delta
        )
        defense_points = (
            away_delta if frame.offense_team_id == self._context.game.home_team_id else home_delta
        )
        self._possessions.append(
            Possession(
                game_id=self._context.game.game_id,
                possession_number=len(self._possessions) + 1,
                offense_team_id=frame.offense_team_id,
                defense_team_id=frame.defense_team_id,
                offense_possession_number=(
                    self._home_possessions
                    if frame.offense_team_id == self._context.game.home_team_id
                    else self._away_possessions
                ),
                period=frame.period,
                start_boundary=frame.start_boundary,
                end_boundary=EventBoundary(action.sequence_number, side),
                start_seconds_remaining_period_ms=frame.start_clock_ms,
                end_seconds_remaining_period_ms=action.clock_ms,
                seconds_remaining_game_ms=self._seconds_remaining_game(
                    action.period, action.clock_ms
                ),
                home_score_before=frame.home_score_before,
                away_score_before=frame.away_score_before,
                home_score=self._home_score,
                away_score=self._away_score,
                score_diff=self._home_score - self._away_score,
                offense_points=offense_points,
                defense_points=defense_points,
                home_possessions=self._home_possessions,
                away_possessions=self._away_possessions,
                end_reason=reason,
                source_event_count=frame.source_event_count,
            )
        )
        self._frame = None
        self._team_rebound_pending = False

    @staticmethod
    def _seconds_remaining_game(period: int, clock_ms: int) -> int:
        return clock_ms + (4 - period) * 720_000 if period <= 4 else clock_ms

    def _change_control(
        self,
        team_id: int,
        action: NormalizedAction,
        reason: PossessionEndReason,
    ) -> None:
        frame = self._frame
        if frame is None:
            self._start(team_id, action, BoundarySide.BEFORE)
            return
        if team_id == frame.offense_team_id:
            self._state = _State.CONTROLLED
            self._team_rebound_pending = False
            return
        self._finish(action, reason, BoundarySide.AFTER)
        if not self._has_error:
            self._start(team_id, action, BoundarySide.AFTER)

    def _resolve_jump_before_action(self, action: NormalizedAction) -> None:
        if self._state is not _State.JUMP_PENDING:
            return
        owner = self._explicit_owner(action)
        if owner is None:
            return
        frame = self._frame
        if frame is None:
            self._start(owner, action, BoundarySide.BEFORE, inferred=True)
            return
        if owner == frame.offense_team_id:
            self._state = _State.CONTROLLED
            return
        self._finish(action, PossessionEndReason.JUMP_BALL_CHANGE, BoundarySide.BEFORE)
        if not self._has_error:
            self._start(owner, action, BoundarySide.BEFORE)

    @staticmethod
    def _explicit_owner(action: NormalizedAction) -> int | None:
        for signal in action.signals:
            match signal:
                case ControlAward(team=team):
                    return team
                case JumpBall(winner_team=team) if team is not None:
                    return team
                case FieldGoalAttempt(team=team) | Turnover(committing_team=team):
                    return team
                case Goaltending(team=team, side=GoaltendingSide.OFFENSIVE):
                    return team
                case Goaltending(team=team, side=GoaltendingSide.DEFENSIVE):
                    return None
                case Violation(team=team, disposition=ViolationDisposition.OPPONENT_AWARD) if (
                    team is not None
                ):
                    return team
        return None

    def _apply_action(self, action: NormalizedAction) -> None:
        if self._state is _State.COMPLETE:
            self._error("incomplete_play_by_play", action.sequence_number)
            return
        if self._last_period is not None:
            if action.period < self._last_period:
                self._error("decreasing_period", action.sequence_number)
                return
            if action.period > self._last_period and (
                self._frame is not None or self._state in {_State.JUMP_PENDING, _State.FT_PROCEDURE}
            ):
                self._error("incomplete_play_by_play", action.sequence_number)
                return
            if (
                action.period == self._last_period
                and self._last_clock_ms is not None
                and action.clock_ms > self._last_clock_ms
            ):
                self._warning("clock_increase", action.sequence_number)
        self._last_period = action.period
        self._last_clock_ms = action.clock_ms
        self._resolve_jump_before_action(action)
        self._consume(action)
        for signal in action.signals:
            self._apply_signal(action, signal)
            if self._has_error:
                return

    def _apply_signal(self, action: NormalizedAction, signal: PossessionSignal) -> None:
        match signal:
            case PeriodStart():
                self._period_start(action)
            case PeriodEnd():
                self._period_end(action)
            case FieldGoalAttempt():
                self._field_goal(action, signal)
            case Rebound():
                self._rebound(action, signal)
            case Turnover():
                self._turnover(action, signal)
            case StealAnnotation():
                self._steal(action, signal)
            case Foul():
                pass
            case FreeThrowTripStart() | FreeThrowAttempt() | FreeThrowTripEnd():
                self._free_throw(action, signal)
            case JumpBall():
                self._jump_ball(action, signal)
            case Violation():
                self._violation(action, signal)
            case Goaltending():
                self._goaltending(action, signal)
            case ControlAward():
                self._control_award(action, signal)
            case Administrative():
                pass
            case Unsupported(control_relevant=True):
                self._error("unknown_control_action", action.sequence_number)
            case Unsupported(control_relevant=False):
                self._warning("unsupported_administrative_action", action.sequence_number)
            case _:
                self._error("unknown_control_action", action.sequence_number)

    def _period_start(self, action: NormalizedAction) -> None:
        if self._frame is not None or self._state in {_State.JUMP_PENDING, _State.FT_PROCEDURE}:
            self._error("conflicting_control", action.sequence_number)
        self._entitlement_team = None

    def _period_end(self, action: NormalizedAction) -> None:
        if self._state is _State.JUMP_PENDING:
            self._error("unresolved_jump_ball", action.sequence_number)
            return
        if self._state is _State.FT_PROCEDURE:
            self._error("unresolved_free_throw_trip", action.sequence_number)
            return
        if self._frame is not None:
            self._finish(action, PossessionEndReason.PERIOD_END, BoundarySide.AFTER)
        self._entitlement_team = None
        self._team_rebound_pending = False
        self._state = _State.COMPLETE if action.period >= 4 else _State.WAITING_FOR_CONTROL

    def _field_goal(self, action: NormalizedAction, signal: FieldGoalAttempt) -> None:
        if not self._team_is_valid(signal.team):
            self._error("unknown_owner", action.sequence_number)
            return
        if self._state is _State.WAITING_FOR_CONTROL:
            self._start(
                signal.team, action, BoundarySide.BEFORE, inferred=self._entitlement_team is None
            )
        if self._state is not _State.CONTROLLED or self._frame is None:
            self._error("unresolved_rebound", action.sequence_number)
            return
        if signal.team != self._frame.offense_team_id:
            self._error("conflicting_control", action.sequence_number)
            return
        self._consume(action)
        if signal.made:
            if signal.and_one_candidate:
                self._error("unresolved_free_throw_trip", action.sequence_number)
                return
            self._award_points(signal.team, signal.value, action)
            self._finish(action, PossessionEndReason.MADE_FIELD_GOAL, BoundarySide.AFTER)
            self._state = _State.WAITING_FOR_CONTROL
            self._entitlement_team = self._other_team(signal.team)
        else:
            self._state = _State.AWAITING_REBOUND

    def _rebound(self, action: NormalizedAction, signal: Rebound) -> None:
        if not self._team_is_valid(signal.rebounding_team):
            self._error("unknown_owner", action.sequence_number)
            return
        if self._state is not _State.AWAITING_REBOUND or self._frame is None:
            self._error(
                "unknown_owner"
                if self._state is _State.WAITING_FOR_CONTROL
                else "unresolved_rebound",
                action.sequence_number,
            )
            return
        self._consume(action)
        if signal.disposition is not ReboundDisposition.NORMAL_LIVE:
            self._error("unresolved_rebound", action.sequence_number)
            return
        if signal.rebounding_team == self._frame.offense_team_id:
            self._state = _State.CONTROLLED
            return
        if signal.actor is ReboundActor.TEAM:
            self._team_rebound_pending = True
            return
        self._change_control(
            signal.rebounding_team,
            action,
            PossessionEndReason.DEFENSIVE_REBOUND,
        )

    def _turnover(self, action: NormalizedAction, signal: Turnover) -> None:
        if not self._team_is_valid(signal.committing_team):
            self._error("unknown_owner", action.sequence_number)
            return
        gained_by = (
            signal.gained_by
            if signal.gained_by is not None
            else self._other_team(signal.committing_team)
        )
        if not self._team_is_valid(gained_by) or gained_by == signal.committing_team:
            self._error("conflicting_control", action.sequence_number)
            return
        if self._state is _State.WAITING_FOR_CONTROL:
            self._start(signal.committing_team, action, BoundarySide.BEFORE, inferred=True)
        if self._state is not _State.CONTROLLED or self._frame is None:
            self._error("unknown_owner", action.sequence_number)
            return
        if self._frame.offense_team_id != signal.committing_team:
            self._error("conflicting_control", action.sequence_number)
            return
        self._consume(action)
        self._change_control(gained_by, action, PossessionEndReason.TURNOVER)

    def _steal(self, action: NormalizedAction, signal: StealAnnotation) -> None:
        if not self._team_is_valid(signal.team):
            self._error("unknown_owner", action.sequence_number)
            return
        if signal.related_sequence_number is None:
            self._warning("orphan_steal_annotation", action.sequence_number)

    def _jump_ball(self, action: NormalizedAction, signal: JumpBall) -> None:
        if signal.winner_team is not None and not self._team_is_valid(signal.winner_team):
            self._error("unknown_owner", action.sequence_number)
            return
        if signal.winner_team is None:
            self._state = _State.JUMP_PENDING
            return
        if self._frame is None:
            self._start(signal.winner_team, action, BoundarySide.BEFORE)
            self._consume(action)
            return
        self._consume(action)
        self._change_control(signal.winner_team, action, PossessionEndReason.JUMP_BALL_CHANGE)

    def _control_award(self, action: NormalizedAction, signal: ControlAward) -> None:
        if not self._team_is_valid(signal.team):
            self._error("unknown_owner", action.sequence_number)
            return
        if self._state is _State.WAITING_FOR_CONTROL:
            self._start(signal.team, action, BoundarySide.BEFORE)
            self._consume(action)
            return
        if self._state is _State.AWAITING_REBOUND and not self._team_rebound_pending:
            self._error("unresolved_rebound", action.sequence_number)
            return
        if self._state not in {_State.CONTROLLED, _State.AWAITING_REBOUND}:
            self._error("unknown_owner", action.sequence_number)
            return
        self._consume(action)
        self._change_control(
            signal.team,
            action,
            (
                PossessionEndReason.DEFENSIVE_REBOUND
                if self._team_rebound_pending
                else PossessionEndReason.CONTROL_AWARD
            ),
        )

    def _violation(self, action: NormalizedAction, signal: Violation) -> None:
        if signal.team is not None and not self._team_is_valid(signal.team):
            self._error("unknown_owner", action.sequence_number)
            return
        if signal.disposition is ViolationDisposition.RETAIN_OFFENSE:
            return
        if signal.disposition is ViolationDisposition.JUMP_BALL:
            self._state = _State.JUMP_PENDING
            return
        if signal.team is None:
            self._error("unknown_owner", action.sequence_number)
            return
        self._turnover(action, Turnover(signal.team, self._other_team(signal.team), "violation"))

    def _goaltending(self, action: NormalizedAction, signal: Goaltending) -> None:
        if not self._team_is_valid(signal.team):
            self._error("unknown_owner", action.sequence_number)
            return
        if signal.side is GoaltendingSide.DEFENSIVE:
            if self._frame is None or self._frame.defense_team_id != signal.team:
                self._error("conflicting_control", action.sequence_number)
                return
            self._consume(action)
            self._award_points(self._frame.offense_team_id, signal.awarded_points, action)
            self._finish(action, PossessionEndReason.MADE_FIELD_GOAL, BoundarySide.AFTER)
            self._state = _State.WAITING_FOR_CONTROL
            self._entitlement_team = signal.team
            return
        self._turnover(action, Turnover(signal.team, self._other_team(signal.team), "goaltending"))

    def _free_throw(
        self,
        action: NormalizedAction,
        signal: FreeThrowTripStart | FreeThrowAttempt | FreeThrowTripEnd,
    ) -> None:
        del signal
        self._consume(action)
        self._state = _State.FT_PROCEDURE
        self._error("unresolved_free_throw_trip", action.sequence_number)


__all__ = ["AnalyticalPossessionParser"]
