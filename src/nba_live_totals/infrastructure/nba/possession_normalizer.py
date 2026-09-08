"""Versioned NBA PlayByPlayV3 normalization into possession-domain signals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from nba_live_totals.application.possessions import GamePbpSnapshot
from nba_live_totals.domain import (
    ActionConsumption,
    Administrative,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowKind,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    Goaltending,
    GoaltendingSide,
    IssueSeverity,
    JsonObject,
    JumpBall,
    NormalizedAction,
    NormalizedGameActions,
    PeriodEnd,
    PeriodStart,
    PlayByPlayEvent,
    PossessionIssue,
    PossessionSignal,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    ResumeDisposition,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
    canonical_json_checksum,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers.common import parse_clock_milliseconds

NORMALIZER_VERSION = "nba-pbp-v1"

_BLOCK_PATTERN = re.compile(r"^(?P<player>[^\r\n]+) BLOCK \((?P<count>[0-9]+) BLK\)$")
_STEAL_PATTERN = re.compile(r"^(?P<player>[^\r\n]+) STEAL \((?P<count>[0-9]+) STL\)$")
_JUMP_BALL_PATTERN = re.compile(
    r"^Jump Ball (?P<jumper_a>[^\r\n:]+) vs\. "
    r"(?P<jumper_b>[^\r\n:]+): Tip to (?P<recipient>[^\r\n:]+)$"
)
_PLAYER_REBOUND_PATTERN = re.compile(
    r"^(?P<player>[^\r\n]+) REBOUND "
    r"\(Off:(?P<off>[0-9]+) Def:(?P<def>[0-9]+)\)$"
)
_TEAM_REBOUND_PATTERN = re.compile(r"^(?P<team_name>[^\r\n]+) Rebound$")
_ASCII_SCORE_PATTERN = re.compile(r"^[0-9]+$")
_FIELD_GOAL_TYPES = frozenset({"made shot", "missed shot", "2pt", "3pt"})
_TIMEOUT_SUBTYPES = frozenset({"regular", "short", "official"})
_SUBSTITUTION_SUBTYPES = frozenset({"", "in", "out"})
_REPLAY_SUBTYPES = frozenset(
    {
        "",
        "coach challenge support ruling",
        "coach challenge overturn ruling",
        "support ruling",
        "overturn ruling",
        "replay center",
        "review",
    }
)
_SAFE_UNSUPPORTED_ADMINISTRATIVE_FORMS = frozenset({("game information", "notice")})
_TURNOVER_KINDS = {
    "bad pass": "bad_pass",
    "lost ball": "lost_ball",
    "traveling": "traveling",
    "offensive foul turnover": "offensive_foul_turnover",
    "double dribble": "double_dribble",
    "discontinue dribble": "discontinue_dribble",
    "3 second violation": "three_second_violation",
    "5 second violation": "five_second_violation",
    "8 second violation": "eight_second_violation",
    "shot clock violation": "shot_clock_violation",
    "shot clock": "shot_clock",
    "inbound turnover": "inbound_turnover",
    "backcourt turnover": "backcourt_turnover",
    "backcourt": "backcourt",
    "offensive goaltending": "offensive_goaltending",
    "lane violation": "lane_violation",
    "kicked ball violation": "kicked_ball_violation",
    "palming": "palming",
    "5 second inbound": "five_second_inbound",
    "step out of bounds": "step_out_of_bounds",
    "out of bounds lost ball turnover": "out_of_bounds_lost_ball_turnover",
    "out of bounds - bad pass turnover": "out_of_bounds_bad_pass_turnover",
}
_FOUL_KINDS = {
    "personal": "personal",
    "shooting": "shooting",
    "loose ball": "loose_ball",
    "offensive": "offensive",
    "offensive charge": "offensive_charge",
    "intentional": "intentional",
    "away from play": "away_from_play",
    "clear path": "clear_path",
    "double technical": "double_technical",
    "technical": "technical",
    "flagrant 1": "flagrant_1",
    "flagrant type 1": "flagrant_type_1",
    "flagrant 2": "flagrant_2",
    "flagrant type 2": "flagrant_type_2",
    "defense 3 second": "defense_three_second",
    "taunting": "taunting",
    "excess timeout": "excess_timeout",
    "charge": "charge",
    "block": "block",
    "personal take": "personal_take",
    "shooting block": "shooting_block",
}
_OFFENSIVE_FOUL_SUBTYPES = frozenset({"offensive", "offensive charge", "charge"})
_SHOOTING_FOUL_SUBTYPES = frozenset({"shooting", "shooting block"})
_RETAINED_VIOLATION_SUBTYPES = frozenset({"delay of game", "kicked ball", "kicked ball violation"})
_FREE_THROW_FORMS = {
    "free throw 1 of 1": (FreeThrowKind.ORDINARY, 1, 1),
    "free throw 1 of 2": (FreeThrowKind.ORDINARY, 1, 2),
    "free throw 2 of 2": (FreeThrowKind.ORDINARY, 2, 2),
    "free throw 1 of 3": (FreeThrowKind.ORDINARY, 1, 3),
    "free throw 2 of 3": (FreeThrowKind.ORDINARY, 2, 3),
    "free throw 3 of 3": (FreeThrowKind.ORDINARY, 3, 3),
    "free throw technical": (FreeThrowKind.TECHNICAL, 1, 1),
    "free throw technical 1 of 2": (FreeThrowKind.TECHNICAL, 1, 2),
    "free throw technical 2 of 2": (FreeThrowKind.TECHNICAL, 2, 2),
    "free throw flagrant 1 of 1": (FreeThrowKind.FLAGRANT, 1, 1),
    "free throw flagrant 1 of 2": (FreeThrowKind.FLAGRANT, 1, 2),
    "free throw flagrant 2 of 2": (FreeThrowKind.FLAGRANT, 2, 2),
    "free throw flagrant 1 of 3": (FreeThrowKind.FLAGRANT, 1, 3),
    "free throw flagrant 2 of 3": (FreeThrowKind.FLAGRANT, 2, 3),
    "free throw flagrant 3 of 3": (FreeThrowKind.FLAGRANT, 3, 3),
    "free throw clear path 1 of 2": (FreeThrowKind.CLEAR_PATH, 1, 2),
    "free throw clear path 2 of 2": (FreeThrowKind.CLEAR_PATH, 2, 2),
    "free throw away from play 1 of 1": (FreeThrowKind.AWAY_FROM_PLAY, 1, 1),
    "free throw transition take 1 of 1": (FreeThrowKind.TRANSITION_TAKE, 1, 1),
}
_MAX_ISSUES = 32


class _RawDataError(Exception):
    """Internal marker for a provider value that cannot enter the safe result."""


@dataclass(frozen=True, slots=True)
class _RawAction:
    sequence_number: int
    action_number: int
    action_id: int | None
    period: int
    clock: str
    clock_ms: int
    team_id: int | None
    person_id: int | None
    action_type: str
    sub_type: str
    description: str
    home_score: int | None
    away_score: int | None
    shot_result: str | None
    shot_distance: int | None
    legacy_x: int | None
    legacy_y: int | None
    location: str | None
    is_field_goal: bool
    points_total: int
    video_available: bool
    shot_value: int | None

    def matches(self, event: PlayByPlayEvent) -> bool:
        """Cross-check every Phase 2 canonical field at this provider position."""
        return (
            self.sequence_number == event.sequence_number
            and self.action_number == event.action_number
            and self.action_id == event.action_id
            and self.period == event.period
            and self.clock == event.clock
            and self.clock_ms == event.seconds_remaining_period_ms
            and self.team_id == event.team_id
            and self.person_id == event.person_id
            and self.action_type == event.action_type
            and self.sub_type == event.sub_type
            and self.description == event.description
            and self.home_score == event.home_score
            and self.away_score == event.away_score
            and self.shot_result == event.shot_result
            and self.shot_distance == event.shot_distance
            and self.legacy_x == event.legacy_x
            and self.legacy_y == event.legacy_y
            and self.location == event.location
            and self.is_field_goal == event.is_field_goal
            and self.points_total == event.points_total
            and self.video_available == event.video_available
        )


@dataclass(frozen=True, slots=True)
class _Semantics:
    consumption: ActionConsumption
    signals: tuple[PossessionSignal, ...]
    issue: tuple[IssueSeverity, str] | None = None
    checkpoint_combined_score: int | None = None
    score_after: tuple[int, int] | None = None


@dataclass(slots=True)
class _FreeThrowTrip:
    trip_id: str
    shooting_team: int
    kind: FreeThrowKind
    total: int
    resume_disposition: ResumeDisposition
    resume_team: int | None
    first_sequence_number: int
    next_attempt: int = 1
    last_attempt: int | None = None


def _mapping(value: object) -> JsonObject:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise _RawDataError
    return cast(JsonObject, value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _RawDataError
    return cast(list[object], value)


def _value(mapping: JsonObject, key: str) -> object:
    if key not in mapping:
        raise _RawDataError
    return mapping[key]


def _string(value: object) -> str:
    if type(value) is not str:
        raise _RawDataError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise _RawDataError
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _identity(value: object) -> int | None:
    identity = _optional_integer(value)
    if identity in {None, 0}:
        return None
    if identity < 0:
        raise _RawDataError
    return identity


def _score(value: object) -> int | None:
    if value is None or value == "":
        return None
    text = _string(value)
    if _ASCII_SCORE_PATTERN.fullmatch(text) is None:
        raise _RawDataError
    return int(text)


def _flag(value: object) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    if type(value) is str and value in {"0", "1"}:
        return value == "1"
    raise _RawDataError


def _shot_value(value: object) -> int | None:
    parsed = _optional_integer(value)
    return None if parsed in {None, 0} else parsed


def _raw_action(value: object, *, sequence_number: int) -> _RawAction:
    action = _mapping(value)
    clock = _string(_value(action, "clock"))
    points_total = _integer(_value(action, "pointsTotal"))
    if points_total < 0:
        raise _RawDataError
    try:
        clock_ms = parse_clock_milliseconds(clock)
    except ProviderSchemaError:
        raise _RawDataError from None
    return _RawAction(
        sequence_number=sequence_number,
        action_number=_integer(_value(action, "actionNumber")),
        action_id=_optional_integer(_value(action, "actionId")),
        period=_integer(_value(action, "period")),
        clock=clock,
        clock_ms=clock_ms,
        team_id=_identity(_value(action, "teamId")),
        person_id=_identity(_value(action, "personId")),
        action_type=_string(_value(action, "actionType")),
        sub_type=_string(_value(action, "subType")),
        description=_string(_value(action, "description")),
        home_score=_score(_value(action, "scoreHome")),
        away_score=_score(_value(action, "scoreAway")),
        shot_result=_optional_string(_value(action, "shotResult")),
        shot_distance=_optional_integer(_value(action, "shotDistance")),
        legacy_x=_optional_integer(_value(action, "xLegacy")),
        legacy_y=_optional_integer(_value(action, "yLegacy")),
        location=_optional_string(_value(action, "location")),
        is_field_goal=_flag(_value(action, "isFieldGoal")),
        points_total=points_total,
        video_available=_flag(_value(action, "videoAvailable")),
        shot_value=_shot_value(_value(action, "shotValue")),
    )


def _label(value: str) -> str:
    return value.strip().casefold()


def _opponent(team_id: int, *, home_team_id: int, away_team_id: int) -> int:
    return away_team_id if team_id == home_team_id else home_team_id


def _unsupported(*, control_relevant: bool) -> _Semantics:
    return _Semantics(
        consumption=ActionConsumption.UNSUPPORTED,
        signals=(Unsupported(control_relevant=control_relevant),),
        issue=(
            IssueSeverity.ERROR if control_relevant else IssueSeverity.WARNING,
            "unknown_control_action" if control_relevant else "unsupported_administrative_action",
        ),
    )


def _has_non_field_goal_scoring_data(raw: _RawAction) -> bool:
    return (
        raw.is_field_goal
        or raw.shot_value is not None
        or raw.points_total != 0
        or (raw.shot_result is not None and _label(raw.shot_result) != "")
    )


def _has_neutral_control_fields(raw: _RawAction) -> bool:
    return not _has_non_field_goal_scoring_data(raw)


def _same_clock(left: _RawAction, right: _RawAction) -> bool:
    return left.period == right.period and left.clock_ms == right.clock_ms


def _free_throw_details(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> tuple[FreeThrowKind, int, int, bool] | None:
    if (
        _label(raw.action_type) != "free throw"
        or raw.is_field_goal
        or raw.team_id not in {home_team_id, away_team_id}
        or raw.person_id is None
        or raw.person_id in {home_team_id, away_team_id}
        or raw.shot_value is not None
        or raw.shot_result not in {None, ""}
    ):
        return None
    form = _FREE_THROW_FORMS.get(_label(raw.sub_type))
    if form is None:
        return None
    subtype = re.escape(raw.sub_type.strip())
    if re.fullmatch(rf"^[^\r\n]+ {subtype} \([0-9]+ PTS\)$", raw.description):
        return (*form, True)
    if re.fullmatch(rf"^MISS [^\r\n]+ {subtype}$", raw.description) and raw.points_total == 0:
        return (*form, False)
    return None


def _free_throw_ordinal(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> tuple[int, int] | None:
    details = _free_throw_details(
        raw,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    if details is None or details[0] is not FreeThrowKind.ORDINARY:
        return None
    return details[1], details[2]


def _free_throw_is_missed(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> bool:
    details = _free_throw_details(
        raw,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    return details is not None and not details[3]


def _is_made_field_goal_for_team(
    raw: _RawAction,
    *,
    team_id: int,
) -> bool:
    action_type = _label(raw.action_type)
    result = "" if raw.shot_result is None else _label(raw.shot_result)
    return (
        raw.team_id == team_id
        and action_type in _FIELD_GOAL_TYPES
        and raw.is_field_goal
        and raw.shot_value in {2, 3}
        and result == "made"
        and (action_type not in {"2pt", "3pt"} or raw.shot_value == int(action_type[0]))
    )


def _and_one_link(
    raw_actions: list[_RawAction],
    index: int,
    *,
    home_team_id: int,
    away_team_id: int,
) -> int | None:
    raw = raw_actions[index]
    details = _free_throw_details(
        raw,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    if details is None or details[:3] != (FreeThrowKind.ORDINARY, 1, 1):
        return None
    assert raw.team_id is not None
    candidates: list[int] = []
    for candidate_index in _same_clock_indices(raw_actions, index):
        if candidate_index >= index:
            break
        field_goal = raw_actions[candidate_index]
        if not _is_made_field_goal_for_team(field_goal, team_id=raw.team_id):
            continue
        intervening = raw_actions[candidate_index + 1 : index]
        has_shooting_foul = any(
            _label(item.action_type) == "foul"
            and _label(item.sub_type) in _SHOOTING_FOUL_SUBTYPES
            and item.team_id
            == _opponent(
                raw.team_id,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
            and _has_neutral_control_fields(item)
            for item in intervening
        )
        has_control_barrier = any(
            _is_control_event(
                item,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                previous_score=(0, 0),
            )
            for item in intervening
        )
        if has_shooting_foul and not has_control_barrier:
            candidates.append(field_goal.sequence_number)
    return candidates[0] if len(candidates) == 1 else None


def _is_safe_free_throw_context(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> bool:
    action_type = _label(raw.action_type)
    sub_type = _label(raw.sub_type)
    if action_type == "free throw":
        return True
    if action_type == "foul":
        return (
            sub_type in _FOUL_KINDS
            and raw.team_id in {home_team_id, away_team_id}
            and _has_neutral_control_fields(raw)
        )
    if action_type == "timeout":
        return sub_type in _TIMEOUT_SUBTYPES and not _has_non_field_goal_scoring_data(raw)
    if action_type == "substitution":
        return sub_type in _SUBSTITUTION_SUBTYPES and not _has_non_field_goal_scoring_data(raw)
    if action_type == "instant replay":
        return sub_type in _REPLAY_SUBTYPES and not _has_non_field_goal_scoring_data(raw)
    if action_type == "violation":
        return sub_type in {
            "lane violation",
            "double lane violation",
        } and _has_neutral_control_fields(raw)
    return (
        action_type == "rebound"
        and raw.team_id is None
        and raw.person_id in {home_team_id, away_team_id}
        and _TEAM_REBOUND_PATTERN.fullmatch(raw.description) is not None
        and _has_neutral_control_fields(raw)
    )


def _ordinary_trip_resume(
    raw_actions: list[_RawAction],
    index: int,
    *,
    home_team_id: int,
    away_team_id: int,
) -> ResumeDisposition | None:
    raw = raw_actions[index]
    details = _free_throw_details(
        raw,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    if details is None or details[0] is not FreeThrowKind.ORDINARY:
        return None
    _, _, total, _ = details
    final_index: int | None = None
    final_attempt: tuple[FreeThrowKind, int, int, bool] | None = None
    for candidate_index in _same_clock_indices(raw_actions, index):
        if candidate_index < index:
            continue
        candidate = raw_actions[candidate_index]
        candidate_details = _free_throw_details(
            candidate,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if (
            candidate_details is not None
            and candidate_details[0] is FreeThrowKind.ORDINARY
            and candidate_details[1:3] == (total, total)
            and candidate.team_id == raw.team_id
        ):
            final_attempt = candidate_details
            final_index = candidate_index
    if final_attempt is None:
        return None
    assert final_index is not None
    if any(
        not _is_safe_free_throw_context(
            raw_actions[candidate_index],
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        for candidate_index in range(index + 1, final_index)
    ):
        return None
    if not final_attempt[3] and final_index + 2 < len(raw_actions):
        violation = raw_actions[final_index + 1]
        award = raw_actions[final_index + 2]
        if (
            _same_clock(raw, violation)
            and _same_clock(violation, award)
            and violation.team_id == raw.team_id
            and _label(violation.action_type) == "violation"
            and _label(violation.sub_type) == "lane violation"
            and _has_neutral_control_fields(violation)
            and award.team_id == raw.team_id
            and _label(award.action_type) == "turnover"
            and _label(award.sub_type) == "lane violation"
            and _has_neutral_control_fields(award)
        ):
            return ResumeDisposition.OPPONENT_INBOUND
    return (
        ResumeDisposition.OPPONENT_INBOUND if final_attempt[3] else ResumeDisposition.LIVE_REBOUND
    )


def _free_throw_resume(
    raw_actions: list[_RawAction],
    index: int,
    *,
    kind: FreeThrowKind,
    home_team_id: int,
    away_team_id: int,
) -> tuple[ResumeDisposition, int | None] | None:
    raw = raw_actions[index]
    if kind is FreeThrowKind.ORDINARY:
        resume = _ordinary_trip_resume(
            raw_actions,
            index,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        return None if resume is None else (resume, None)
    if kind is FreeThrowKind.TECHNICAL:
        return ResumeDisposition.RESUME_INTERRUPTED, None
    assert raw.team_id is not None
    return ResumeDisposition.RETAIN_TEAM, raw.team_id


def _free_throw_semantics(
    raw: _RawAction,
    *,
    raw_actions: list[_RawAction],
    index: int,
    game_id: str,
    previous_score: tuple[int, int],
    home_team_id: int,
    away_team_id: int,
    trip_stack: list[_FreeThrowTrip],
) -> _Semantics:
    details = _free_throw_details(
        raw,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    if details is None:
        return _unsupported(control_relevant=True)
    kind, attempt, total, made = details
    assert raw.team_id is not None
    trip: _FreeThrowTrip
    start: FreeThrowTripStart | None = None
    if not trip_stack or (kind is FreeThrowKind.TECHNICAL and attempt == 1):
        if attempt != 1:
            return _unsupported(control_relevant=True)
        if kind is FreeThrowKind.ORDINARY and total == 1:
            if (
                _and_one_link(
                    raw_actions,
                    index,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                )
                is None
            ):
                return _unsupported(control_relevant=True)
            kind = FreeThrowKind.AND_ONE
        elif trip_stack and kind is not FreeThrowKind.TECHNICAL:
            return _unsupported(control_relevant=True)
        resume = _free_throw_resume(
            raw_actions,
            index,
            kind=FreeThrowKind.ORDINARY if kind is FreeThrowKind.AND_ONE else kind,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if resume is None:
            return _Semantics(
                ActionConsumption.UNSUPPORTED,
                (Unsupported(control_relevant=True),),
                issue=(IssueSeverity.ERROR, "unresolved_free_throw_trip"),
            )
        disposition, resume_team = resume
        trip = _FreeThrowTrip(
            trip_id=f"ft:{game_id}:{raw.sequence_number}:{len(trip_stack) + 1}",
            shooting_team=raw.team_id,
            kind=kind,
            total=total,
            resume_disposition=disposition,
            resume_team=resume_team,
            first_sequence_number=raw.sequence_number,
        )
        interrupted_trip_id = trip_stack[-1].trip_id if trip_stack else None
        trip_stack.append(trip)
        start = FreeThrowTripStart(
            trip.trip_id,
            trip.shooting_team,
            trip.kind,
            trip.total,
            trip.resume_disposition,
            trip.resume_team,
            interrupted_trip_id,
        )
    else:
        trip = trip_stack[-1]
        if (
            kind is not trip.kind
            or raw.team_id != trip.shooting_team
            or total != trip.total
            or attempt != trip.next_attempt
        ):
            return _unsupported(control_relevant=True)

    expected_home_delta = 1 if made and raw.team_id == home_team_id else 0
    expected_away_delta = 1 if made and raw.team_id == away_team_id else 0
    score_after = (
        previous_score[0] + expected_home_delta,
        previous_score[1] + expected_away_delta,
    )
    checkpoints_valid = raw.home_score in {None, score_after[0]} and raw.away_score in {
        None,
        score_after[1],
    }
    points_valid = raw.points_total == sum(score_after) if made else raw.points_total == 0
    issue = (
        None if checkpoints_valid and points_valid else (IssueSeverity.ERROR, "invalid_score_delta")
    )
    signals: list[PossessionSignal] = [] if start is None else [start]
    signals.append(FreeThrowAttempt(trip.trip_id, attempt, total, made, int(made)))
    trip.last_attempt = attempt
    trip.next_attempt = attempt + 1
    if attempt == total:
        signals.append(FreeThrowTripEnd(trip.trip_id, trip.resume_disposition, trip.resume_team))
        trip_stack.pop()
    return _Semantics(
        ActionConsumption.TRANSITION,
        tuple(signals),
        issue=issue,
        checkpoint_combined_score=raw.points_total if made else None,
        score_after=score_after,
    )


def _control_team(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> int | None:
    action_type = _label(raw.action_type)
    sub_type = _label(raw.sub_type)
    if raw.team_id not in {home_team_id, away_team_id}:
        return None
    assert raw.team_id is not None
    if action_type in _FIELD_GOAL_TYPES:
        result = "" if raw.shot_result is None else _label(raw.shot_result)
        if raw.is_field_goal and raw.shot_value in {2, 3} and result in {"made", "missed"}:
            return raw.team_id
    if action_type == "turnover" and sub_type in _TURNOVER_KINDS:
        if not _has_neutral_control_fields(raw):
            return None
        return _opponent(
            raw.team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    return None


def _is_control_event(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
    previous_score: tuple[int, int],
) -> bool:
    action_type = _label(raw.action_type)
    sub_type = _label(raw.sub_type)
    if action_type == "timeout":
        return not (sub_type in _TIMEOUT_SUBTYPES and not _has_non_field_goal_scoring_data(raw))
    if action_type == "substitution":
        return not (
            sub_type in _SUBSTITUTION_SUBTYPES and not _has_non_field_goal_scoring_data(raw)
        )
    if action_type == "foul":
        return not (
            sub_type in _FOUL_KINDS
            and raw.team_id in {home_team_id, away_team_id}
            and _has_neutral_control_fields(raw)
        )
    if action_type == "instant replay":
        return not (
            sub_type in _REPLAY_SUBTYPES
            and not _has_non_field_goal_scoring_data(raw)
            and raw.home_score in {None, previous_score[0]}
            and raw.away_score in {None, previous_score[1]}
        )
    if action_type == "" and sub_type == "":
        return not (
            (
                _BLOCK_PATTERN.fullmatch(raw.description) is not None
                and not _has_non_field_goal_scoring_data(raw)
            )
            or (
                _STEAL_PATTERN.fullmatch(raw.description) is not None
                and raw.team_id in {home_team_id, away_team_id}
                and _has_neutral_control_fields(raw)
            )
        )
    return (action_type, sub_type) not in _SAFE_UNSUPPORTED_ADMINISTRATIVE_FORMS


def _is_valid_jump_ball(
    raw: _RawAction,
    *,
    home_team_id: int,
    away_team_id: int,
) -> bool:
    return (
        _label(raw.action_type) == "jump ball"
        and _label(raw.sub_type) in {"", "recovered"}
        and raw.team_id in {None, home_team_id, away_team_id}
        and _JUMP_BALL_PATTERN.fullmatch(raw.description) is not None
        and _has_neutral_control_fields(raw)
    )


def _same_clock_indices(raw_actions: list[_RawAction], index: int) -> range:
    raw = raw_actions[index]
    start = index
    while start > 0 and _same_clock(raw_actions[start - 1], raw):
        start -= 1
    end = index + 1
    while end < len(raw_actions) and _same_clock(raw_actions[end], raw):
        end += 1
    return range(start, end)


def _offensive_foul_link(
    raw_actions: list[_RawAction],
    index: int,
) -> tuple[int | None, bool]:
    raw = raw_actions[index]
    candidates = [
        other.sequence_number
        for candidate_index in _same_clock_indices(raw_actions, index)
        if candidate_index != index
        for other in (raw_actions[candidate_index],)
        if _label(other.action_type) == "turnover"
        and _label(other.sub_type) == "offensive foul turnover"
        and other.team_id == raw.team_id
    ]
    return (candidates[0], False) if len(candidates) == 1 else (None, True)


def _steal_link(
    raw_actions: list[_RawAction],
    index: int,
    *,
    home_team_id: int,
    away_team_id: int,
    previous_score: tuple[int, int],
) -> tuple[int | None, bool]:
    raw = raw_actions[index]
    if raw.team_id not in {home_team_id, away_team_id}:
        return None, True
    candidates = [
        (candidate_index, other.sequence_number)
        for candidate_index in _same_clock_indices(raw_actions, index)
        if candidate_index < index
        for other in (raw_actions[candidate_index],)
        if _label(other.action_type) == "turnover"
        and _label(other.sub_type) in _TURNOVER_KINDS
        and other.team_id in {home_team_id, away_team_id}
        and other.team_id != raw.team_id
    ]
    if len(candidates) != 1:
        return None, True
    candidate_index, sequence_number = candidates[0]
    if any(
        _is_control_event(
            raw_actions[intervening_index],
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            previous_score=previous_score,
        )
        for intervening_index in range(candidate_index + 1, index)
    ):
        return None, True
    return sequence_number, False


def _team_rebound_disposition(
    raw_actions: list[_RawAction],
    index: int,
    *,
    rebound_team: int,
    home_team_id: int,
    away_team_id: int,
    previous_score: tuple[int, int],
) -> ReboundDisposition | None:
    raw = raw_actions[index]
    if index > 0 and index + 1 < len(raw_actions):
        previous = raw_actions[index - 1]
        following = raw_actions[index + 1]
        previous_details = _free_throw_details(
            previous,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        following_details = _free_throw_details(
            following,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if (
            _same_clock(previous, raw)
            and _same_clock(raw, following)
            and previous_details is not None
            and following_details is not None
            and previous_details[1] < previous_details[2]
            and following_details[:3]
            == (
                previous_details[0],
                previous_details[1] + 1,
                previous_details[2],
            )
            and previous.team_id == following.team_id
            and not previous_details[3]
        ):
            return ReboundDisposition.FT_PLACEHOLDER
    for candidate_index in range(index + 1, len(raw_actions)):
        following = raw_actions[candidate_index]
        if not _same_clock(raw, following):
            break
        control_team = _control_team(
            following,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if control_team is not None:
            return (
                ReboundDisposition.RETAIN_TEAM
                if control_team == rebound_team
                else ReboundDisposition.AWARD_OPPONENT
            )
        if _is_control_event(
            following,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            previous_score=previous_score,
        ):
            return None
    return None


def _rebound_semantics(
    raw: _RawAction,
    *,
    raw_actions: list[_RawAction],
    index: int,
    home_team_id: int,
    away_team_id: int,
    previous_score: tuple[int, int],
    rebound_counters: dict[int, tuple[int, int]],
) -> _Semantics:
    if (
        raw.team_id in {home_team_id, away_team_id}
        and raw.person_id is not None
        and raw.person_id not in {home_team_id, away_team_id}
    ):
        match = _PLAYER_REBOUND_PATTERN.fullmatch(raw.description)
        if match is None:
            return _unsupported(control_relevant=True)
        assert raw.team_id is not None
        current = (int(match.group("off")), int(match.group("def")))
        previous = rebound_counters.get(raw.person_id, (0, 0))
        rebound_counters[raw.person_id] = current
        delta = (current[0] - previous[0], current[1] - previous[1])
        provider_side = (
            ProviderReboundSide.OFFENSIVE
            if delta == (1, 0)
            else ProviderReboundSide.DEFENSIVE
            if delta == (0, 1)
            else ProviderReboundSide.UNKNOWN
        )
        return _Semantics(
            ActionConsumption.TRANSITION,
            (
                Rebound(
                    rebounding_team=raw.team_id,
                    actor=ReboundActor.PLAYER,
                    provider_side=provider_side,
                    disposition=ReboundDisposition.NORMAL_LIVE,
                ),
            ),
        )
    if raw.team_id is None and raw.person_id in {home_team_id, away_team_id}:
        if _TEAM_REBOUND_PATTERN.fullmatch(raw.description) is None:
            return _unsupported(control_relevant=True)
        assert raw.person_id is not None
        disposition = _team_rebound_disposition(
            raw_actions,
            index,
            rebound_team=raw.person_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            previous_score=previous_score,
        )
        if disposition is None:
            return _unsupported(control_relevant=True)
        return _Semantics(
            ActionConsumption.ANNOTATION
            if disposition is ReboundDisposition.FT_PLACEHOLDER
            else ActionConsumption.TRANSITION,
            (
                Rebound(
                    rebounding_team=raw.person_id,
                    actor=ReboundActor.TEAM,
                    provider_side=ProviderReboundSide.UNKNOWN,
                    disposition=disposition,
                ),
            ),
        )
    return _unsupported(control_relevant=True)


def _turnover_semantics(
    raw: _RawAction,
    *,
    sub_type: str,
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    turnover_kind = _TURNOVER_KINDS.get(sub_type)
    if (
        turnover_kind is None
        or raw.team_id not in {home_team_id, away_team_id}
        or not _has_neutral_control_fields(raw)
    ):
        return _unsupported(control_relevant=True)
    assert raw.team_id is not None
    gained_by = _opponent(
        raw.team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    turnover = Turnover(
        committing_team=raw.team_id,
        gained_by=gained_by,
        turnover_kind=turnover_kind,
    )
    signals: tuple[PossessionSignal, ...]
    if sub_type == "offensive goaltending":
        signals = (
            Goaltending(
                team=raw.team_id,
                side=GoaltendingSide.OFFENSIVE,
                awarded_points=0,
            ),
            turnover,
        )
    else:
        signals = (turnover,)
    return _Semantics(ActionConsumption.TRANSITION, signals)


def _foul_semantics(
    raw: _RawAction,
    *,
    sub_type: str,
    raw_actions: list[_RawAction],
    index: int,
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    foul_kind = _FOUL_KINDS.get(sub_type)
    if (
        foul_kind is None
        or raw.team_id not in {home_team_id, away_team_id}
        or not _has_neutral_control_fields(raw)
    ):
        return _unsupported(control_relevant=True)
    assert raw.team_id is not None
    shooting_team = (
        _opponent(
            raw.team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if sub_type in _SHOOTING_FOUL_SUBTYPES
        else None
    )
    related_sequence_number = None
    relationship_invalid = False
    if sub_type in _OFFENSIVE_FOUL_SUBTYPES:
        related_sequence_number, relationship_invalid = _offensive_foul_link(raw_actions, index)
    return _Semantics(
        ActionConsumption.ADMINISTRATIVE,
        (
            Foul(
                committing_team=raw.team_id,
                foul_kind=foul_kind,
                shooting_team=shooting_team,
                related_sequence_number=related_sequence_number,
            ),
        ),
        issue=(IssueSeverity.ERROR, "unknown_control_action") if relationship_invalid else None,
    )


def _defensive_goaltending_semantics(
    raw: _RawAction,
    *,
    previous_score: tuple[int, int],
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    if (
        raw.team_id not in {home_team_id, away_team_id}
        or raw.is_field_goal
        or raw.shot_value is not None
        or raw.shot_result not in {None, ""}
        or raw.home_score is None
        or raw.away_score is None
    ):
        return _unsupported(control_relevant=True)
    assert raw.team_id is not None
    assert raw.home_score is not None
    assert raw.away_score is not None
    home_delta = raw.home_score - previous_score[0]
    away_delta = raw.away_score - previous_score[1]
    scored_team = _opponent(
        raw.team_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    awarded_points = home_delta if scored_team == home_team_id else away_delta
    other_delta = away_delta if scored_team == home_team_id else home_delta
    if (
        awarded_points not in {2, 3}
        or other_delta != 0
        or raw.points_total != raw.home_score + raw.away_score
    ):
        return _unsupported(control_relevant=True)
    return _Semantics(
        ActionConsumption.TRANSITION,
        (
            Goaltending(
                team=raw.team_id,
                side=GoaltendingSide.DEFENSIVE,
                awarded_points=awarded_points,
            ),
        ),
        checkpoint_combined_score=raw.points_total,
        score_after=(raw.home_score, raw.away_score),
    )


def _lane_violation_disposition(
    raw_actions: list[_RawAction],
    index: int,
    *,
    home_team_id: int,
    away_team_id: int,
) -> ViolationDisposition | None:
    if index == 0 or index + 1 >= len(raw_actions):
        return None
    raw = raw_actions[index]
    previous = raw_actions[index - 1]
    following = raw_actions[index + 1]
    if not (_same_clock(previous, raw) and _same_clock(raw, following)):
        return None
    previous_ordinal = _free_throw_ordinal(
        previous,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    if previous_ordinal is None or not _free_throw_is_missed(
        previous,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    ):
        return None
    sub_type = _label(raw.sub_type)
    following_ordinal = _free_throw_ordinal(
        following,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    is_retry = following_ordinal == previous_ordinal and following.team_id == previous.team_id
    if sub_type == "double lane violation":
        if is_retry:
            return ViolationDisposition.RETRY
        if _is_valid_jump_ball(
            following,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        ):
            return ViolationDisposition.JUMP_BALL
        return None
    if sub_type != "lane violation" or raw.team_id is None:
        return None
    if raw.team_id != previous.team_id and is_retry:
        return ViolationDisposition.RETRY
    if (
        raw.team_id == previous.team_id
        and previous_ordinal[0] == previous_ordinal[1]
        and _label(following.action_type) == "turnover"
        and _label(following.sub_type) == "lane violation"
        and following.team_id == raw.team_id
    ):
        return ViolationDisposition.OPPONENT_AWARD
    return None


def _violation_semantics(
    raw: _RawAction,
    *,
    sub_type: str,
    raw_actions: list[_RawAction],
    index: int,
    previous_score: tuple[int, int],
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    if sub_type == "defensive goaltending":
        return _defensive_goaltending_semantics(
            raw,
            previous_score=previous_score,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    if not _has_neutral_control_fields(raw):
        return _unsupported(control_relevant=True)
    if sub_type in _RETAINED_VIOLATION_SUBTYPES:
        if raw.team_id not in {home_team_id, away_team_id}:
            return _unsupported(control_relevant=True)
        return _Semantics(
            ActionConsumption.ANNOTATION,
            (Violation(raw.team_id, ViolationDisposition.RETAIN_OFFENSE),),
        )
    if sub_type in {"lane violation", "double lane violation"}:
        if sub_type == "lane violation" and raw.team_id not in {
            home_team_id,
            away_team_id,
        }:
            return _unsupported(control_relevant=True)
        if sub_type == "double lane violation" and raw.team_id is not None:
            return _unsupported(control_relevant=True)
        disposition = _lane_violation_disposition(
            raw_actions,
            index,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        if disposition is None:
            return _unsupported(control_relevant=True)
        return _Semantics(
            ActionConsumption.ANNOTATION,
            (Violation(raw.team_id, disposition),),
        )
    if sub_type == "jump ball violation":
        if raw.team_id not in {home_team_id, away_team_id}:
            return _unsupported(control_relevant=True)
        following = raw_actions[index + 1] if index + 1 < len(raw_actions) else None
        if (
            following is None
            or not _same_clock(raw, following)
            or not _is_valid_jump_ball(
                following,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
        ):
            return _unsupported(control_relevant=True)
        return _Semantics(
            ActionConsumption.ANNOTATION,
            (Violation(raw.team_id, ViolationDisposition.JUMP_BALL),),
        )
    return _unsupported(control_relevant=True)


def _field_goal_semantics(
    raw: _RawAction,
    *,
    action_type: str,
    raw_actions: list[_RawAction],
    index: int,
    previous_score: tuple[int, int],
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    result = "" if raw.shot_result is None else _label(raw.shot_result)
    if action_type == "made shot":
        expected_result = "made"
    elif action_type == "missed shot":
        expected_result = "missed"
    else:
        expected_result = result
    required_value = 2 if action_type == "2pt" else 3 if action_type == "3pt" else None
    valid = (
        raw.is_field_goal
        and raw.team_id in {home_team_id, away_team_id}
        and raw.shot_value in {2, 3}
        and result in {"made", "missed"}
        and result == expected_result
        and (required_value is None or raw.shot_value == required_value)
    )
    if not valid:
        return _unsupported(control_relevant=True)

    assert raw.team_id is not None
    assert raw.shot_value is not None
    made = result == "made"
    expected_home_delta = raw.shot_value if made and raw.team_id == home_team_id else 0
    expected_away_delta = raw.shot_value if made and raw.team_id == away_team_id else 0
    score_after = (
        previous_score[0] + expected_home_delta,
        previous_score[1] + expected_away_delta,
    )
    checkpoints_valid = raw.home_score in {None, score_after[0]} and raw.away_score in {
        None,
        score_after[1],
    }
    points_valid = raw.points_total == sum(score_after) if made else raw.points_total == 0
    reconciliation_issue = (
        None if checkpoints_valid and points_valid else (IssueSeverity.ERROR, "invalid_score_delta")
    )
    and_one_candidate = made and any(
        _and_one_link(
            raw_actions,
            candidate_index,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        == raw.sequence_number
        for candidate_index in _same_clock_indices(raw_actions, index)
        if candidate_index > index
    )
    return _Semantics(
        consumption=ActionConsumption.TRANSITION,
        signals=(
            FieldGoalAttempt(
                team=raw.team_id,
                value=raw.shot_value,
                made=made,
                and_one_candidate=and_one_candidate,
            ),
        ),
        issue=reconciliation_issue,
        checkpoint_combined_score=raw.points_total if made else None,
        score_after=score_after,
    )


def _semantics(
    raw: _RawAction,
    *,
    raw_actions: list[_RawAction],
    index: int,
    previous_score: tuple[int, int],
    home_team_id: int,
    away_team_id: int,
    rebound_counters: dict[int, tuple[int, int]],
    trip_stack: list[_FreeThrowTrip],
    game_id: str,
) -> _Semantics:
    action_type = _label(raw.action_type)
    sub_type = _label(raw.sub_type)
    if action_type in _FIELD_GOAL_TYPES:
        return _field_goal_semantics(
            raw,
            action_type=action_type,
            raw_actions=raw_actions,
            index=index,
            previous_score=previous_score,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    if action_type == "free throw":
        return _free_throw_semantics(
            raw,
            raw_actions=raw_actions,
            index=index,
            game_id=game_id,
            previous_score=previous_score,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            trip_stack=trip_stack,
        )
    if action_type == "rebound" and sub_type in {"unknown", "normal rebound"}:
        if not _has_neutral_control_fields(raw):
            return _unsupported(control_relevant=True)
        return _rebound_semantics(
            raw,
            raw_actions=raw_actions,
            index=index,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            previous_score=previous_score,
            rebound_counters=rebound_counters,
        )
    if action_type == "turnover":
        return _turnover_semantics(
            raw,
            sub_type=sub_type,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    if action_type == "foul":
        return _foul_semantics(
            raw,
            sub_type=sub_type,
            raw_actions=raw_actions,
            index=index,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    if action_type == "violation":
        return _violation_semantics(
            raw,
            sub_type=sub_type,
            raw_actions=raw_actions,
            index=index,
            previous_score=previous_score,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
    if action_type == "jump ball":
        if _is_valid_jump_ball(
            raw,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        ):
            return _Semantics(ActionConsumption.TRANSITION, (JumpBall(),))
        return _unsupported(control_relevant=True)
    if action_type == "period":
        if (
            sub_type == "start"
            and raw.team_id is None
            and raw.person_id is None
            and not _has_non_field_goal_scoring_data(raw)
        ):
            return _Semantics(ActionConsumption.TRANSITION, (PeriodStart(),))
        if (
            sub_type == "end"
            and raw.team_id is None
            and raw.person_id is None
            and not _has_non_field_goal_scoring_data(raw)
        ):
            return _Semantics(ActionConsumption.TRANSITION, (PeriodEnd(),))
        return _unsupported(control_relevant=True)
    if action_type == "timeout" and sub_type in _TIMEOUT_SUBTYPES:
        if not _has_non_field_goal_scoring_data(raw):
            return _Semantics(
                ActionConsumption.ADMINISTRATIVE,
                (Administrative("timeout"),),
            )
        return _unsupported(control_relevant=True)
    if action_type == "substitution" and sub_type in _SUBSTITUTION_SUBTYPES:
        if not _has_non_field_goal_scoring_data(raw):
            return _Semantics(
                ActionConsumption.ADMINISTRATIVE,
                (Administrative("substitution"),),
            )
        return _unsupported(control_relevant=True)
    if action_type == "instant replay" and sub_type in _REPLAY_SUBTYPES:
        checkpoints_unchanged = raw.home_score in {None, previous_score[0]} and raw.away_score in {
            None,
            previous_score[1],
        }
        if not _has_non_field_goal_scoring_data(raw) and checkpoints_unchanged:
            return _Semantics(
                ActionConsumption.ADMINISTRATIVE,
                (Administrative("instant_replay"),),
            )
        return _unsupported(control_relevant=True)
    if action_type == "" and sub_type == "":
        if _BLOCK_PATTERN.fullmatch(raw.description) is not None:
            if not _has_non_field_goal_scoring_data(raw):
                return _Semantics(
                    ActionConsumption.ADMINISTRATIVE,
                    (Administrative("block"),),
                )
            return _unsupported(control_relevant=True)
        if _STEAL_PATTERN.fullmatch(raw.description) is not None:
            if raw.team_id not in {home_team_id, away_team_id} or not _has_neutral_control_fields(
                raw
            ):
                return _unsupported(control_relevant=True)
            assert raw.team_id is not None
            related_sequence_number, orphan = _steal_link(
                raw_actions,
                index,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                previous_score=previous_score,
            )
            return _Semantics(
                ActionConsumption.ANNOTATION,
                (StealAnnotation(raw.team_id, related_sequence_number),),
                issue=(IssueSeverity.WARNING, "orphan_steal_annotation") if orphan else None,
            )
    if (action_type, sub_type) in _SAFE_UNSUPPORTED_ADMINISTRATIVE_FORMS:
        return _unsupported(control_relevant=False)
    return _unsupported(control_relevant=True)


def _issue(
    severity: IssueSeverity,
    code: str,
    *,
    sequence_number: int | None = None,
    related_sequence_number: int | None = None,
) -> PossessionIssue:
    return PossessionIssue(
        severity=severity,
        code=code,
        sequence_number=sequence_number,
        related_sequence_number=related_sequence_number,
    )


def _invalid_result(
    snapshot: GamePbpSnapshot,
    code: str,
    *,
    sequence_number: int | None = None,
) -> NormalizedGameActions:
    return NormalizedGameActions(
        game_id=snapshot.game.game_id,
        actions=(),
        issues=(
            _issue(
                IssueSeverity.ERROR,
                code,
                sequence_number=sequence_number,
            ),
        ),
    )


class NBAPlayByPlayV3PossessionNormalizer:
    """Normalize the closed ``nba-pbp-v1`` basic action matrix."""

    version = NORMALIZER_VERSION

    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions:
        """Cross-check one applied V3 document and normalize in provider array order."""
        if not isinstance(snapshot, GamePbpSnapshot):
            raise ValueError("snapshot must be a GamePbpSnapshot")
        document = snapshot.source_document
        payload = document.serializable_payload()
        expected_resource = f"play_by_play:{snapshot.game.game_id.value}"
        if (
            document.source != "nba.com"
            or document.endpoint != "playbyplayv3"
            or document.resource_key != expected_resource
            or document.checksum_sha256 != snapshot.applied_pbp_sha256
            or canonical_json_checksum(payload) != document.checksum_sha256
        ):
            return _invalid_result(snapshot, "raw_canonical_mismatch")

        try:
            meta = _mapping(_value(payload, "meta"))
            if type(_value(meta, "version")) is not int or _value(meta, "version") != 1:
                raise _RawDataError
            game = _mapping(_value(payload, "game"))
            raw_game_id = _string(_value(game, "gameId"))
            raw_values = _list(_value(game, "actions"))
        except _RawDataError:
            return _invalid_result(snapshot, "raw_canonical_mismatch")
        if raw_game_id != snapshot.game.game_id.value:
            return _invalid_result(snapshot, "mixed_game")
        if len(raw_values) != len(snapshot.canonical_events):
            return _invalid_result(snapshot, "raw_canonical_mismatch")

        raw_actions: list[_RawAction] = []
        for sequence_number, (raw_value, event) in enumerate(
            zip(raw_values, snapshot.canonical_events, strict=True),
            start=1,
        ):
            try:
                raw = _raw_action(raw_value, sequence_number=sequence_number)
            except _RawDataError:
                return _invalid_result(
                    snapshot,
                    "raw_canonical_mismatch",
                    sequence_number=sequence_number,
                )
            if not raw.matches(event):
                return _invalid_result(
                    snapshot,
                    "raw_canonical_mismatch",
                    sequence_number=sequence_number,
                )
            raw_actions.append(raw)

        actions: list[NormalizedAction] = []
        issues: list[PossessionIssue] = []
        first_action_number: dict[int, int] = {}
        score_state = (0, 0)
        rebound_counters: dict[int, tuple[int, int]] = {}
        trip_stack: list[_FreeThrowTrip] = []
        for index, raw in enumerate(raw_actions):
            related_sequence = first_action_number.setdefault(
                raw.action_number, raw.sequence_number
            )
            if related_sequence != raw.sequence_number and len(issues) < _MAX_ISSUES:
                issues.append(
                    _issue(
                        IssueSeverity.WARNING,
                        "duplicate_provider_action",
                        sequence_number=raw.sequence_number,
                        related_sequence_number=related_sequence,
                    )
                )
            semantics = _semantics(
                raw,
                raw_actions=raw_actions,
                index=index,
                previous_score=score_state,
                home_team_id=snapshot.game.home_team_id,
                away_team_id=snapshot.game.away_team_id,
                rebound_counters=rebound_counters,
                trip_stack=trip_stack,
                game_id=snapshot.game.game_id.value,
            )
            if (
                semantics.signals
                and type(semantics.signals[0]) is Violation
                and semantics.signals[0].disposition is ViolationDisposition.RETRY
                and trip_stack
                and trip_stack[-1].last_attempt is not None
            ):
                trip_stack[-1].next_attempt = trip_stack[-1].last_attempt
            if (raw.home_score is None or raw.away_score is None) and len(issues) < _MAX_ISSUES:
                issues.append(
                    _issue(
                        IssueSeverity.WARNING,
                        "missing_score_checkpoint",
                        sequence_number=raw.sequence_number,
                    )
                )
            if semantics.issue is not None and len(issues) < _MAX_ISSUES:
                issues.append(
                    _issue(
                        semantics.issue[0],
                        semantics.issue[1],
                        sequence_number=raw.sequence_number,
                    )
                )
            actions.append(
                NormalizedAction(
                    game_id=snapshot.game.game_id,
                    sequence_number=raw.sequence_number,
                    provider_action_number=raw.action_number,
                    provider_action_id=raw.action_id,
                    period=raw.period,
                    clock_ms=raw.clock_ms,
                    actor_team_id=raw.team_id,
                    actor_person_id=raw.person_id,
                    checkpoint_home_score=raw.home_score,
                    checkpoint_away_score=raw.away_score,
                    checkpoint_combined_score=semantics.checkpoint_combined_score,
                    consumption=semantics.consumption,
                    signals=semantics.signals,
                )
            )
            if semantics.score_after is not None:
                score_state = semantics.score_after
        for trip in trip_stack:
            if len(issues) >= _MAX_ISSUES:
                break
            issues.append(
                _issue(
                    IssueSeverity.ERROR,
                    "unresolved_free_throw_trip",
                    sequence_number=trip.first_sequence_number,
                )
            )
        return NormalizedGameActions(
            game_id=snapshot.game.game_id,
            actions=tuple(actions),
            issues=tuple(issues),
        )


__all__ = ["NORMALIZER_VERSION", "NBAPlayByPlayV3PossessionNormalizer"]
