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
    IssueSeverity,
    JsonObject,
    NormalizedAction,
    NormalizedGameActions,
    PeriodEnd,
    PeriodStart,
    PlayByPlayEvent,
    PossessionIssue,
    PossessionSignal,
    Unsupported,
    canonical_json_checksum,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers.common import parse_clock_milliseconds

NORMALIZER_VERSION = "nba-pbp-v1"

_BLOCK_PATTERN = re.compile(r"^(?P<player>[^\r\n]+) BLOCK \((?P<count>[0-9]+) BLK\)$")
_STEAL_PATTERN = re.compile(r"^(?P<player>[^\r\n]+) STEAL \((?P<count>[0-9]+) STL\)$")
_ASCII_SCORE_PATTERN = re.compile(r"^[0-9]+$")
_FIELD_GOAL_TYPES = frozenset({"made shot", "missed shot", "2pt", "3pt"})
_CONTROL_TYPES = frozenset({"free throw", "rebound", "turnover", "foul", "violation", "jump ball"})
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


def _field_goal_semantics(
    raw: _RawAction,
    *,
    action_type: str,
    previous_score: tuple[int, int] | None,
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
        and raw.home_score is not None
        and raw.away_score is not None
        and previous_score is not None
    )
    if not valid:
        return _unsupported(control_relevant=True)

    assert raw.team_id is not None
    assert raw.shot_value is not None
    assert raw.home_score is not None
    assert raw.away_score is not None
    assert previous_score is not None
    made = result == "made"
    expected_home_delta = raw.shot_value if made and raw.team_id == home_team_id else 0
    expected_away_delta = raw.shot_value if made and raw.team_id == away_team_id else 0
    actual_delta = (
        raw.home_score - previous_score[0],
        raw.away_score - previous_score[1],
    )
    combined = raw.home_score + raw.away_score
    points_valid = raw.points_total == combined if made else raw.points_total == 0
    if actual_delta != (expected_home_delta, expected_away_delta) or not points_valid:
        return _unsupported(control_relevant=True)
    return _Semantics(
        consumption=ActionConsumption.TRANSITION,
        signals=(
            FieldGoalAttempt(
                team=raw.team_id,
                value=raw.shot_value,
                made=made,
                and_one_candidate=False,
            ),
        ),
        checkpoint_combined_score=raw.points_total if made else None,
    )


def _semantics(
    raw: _RawAction,
    *,
    previous_score: tuple[int, int] | None,
    home_team_id: int,
    away_team_id: int,
) -> _Semantics:
    action_type = _label(raw.action_type)
    sub_type = _label(raw.sub_type)
    if action_type in _FIELD_GOAL_TYPES:
        return _field_goal_semantics(
            raw,
            action_type=action_type,
            previous_score=previous_score,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
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
        if not _has_non_field_goal_scoring_data(raw):
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
            return _unsupported(control_relevant=True)
    control_relevant = (
        action_type in _CONTROL_TYPES
        or action_type in _FIELD_GOAL_TYPES
        or action_type == "period"
        or _has_non_field_goal_scoring_data(raw)
    )
    return _unsupported(control_relevant=control_relevant)


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
        previous_score: tuple[int, int] | None = None
        for raw in raw_actions:
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
                previous_score=previous_score,
                home_team_id=snapshot.game.home_team_id,
                away_team_id=snapshot.game.away_team_id,
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
            if raw.home_score is not None and raw.away_score is not None:
                previous_score = (raw.home_score, raw.away_score)
        return NormalizedGameActions(
            game_id=snapshot.game.game_id,
            actions=tuple(actions),
            issues=tuple(issues),
        )


__all__ = ["NORMALIZER_VERSION", "NBAPlayByPlayV3PossessionNormalizer"]
