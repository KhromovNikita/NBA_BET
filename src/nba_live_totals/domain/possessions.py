"""Immutable provider-neutral contracts for analytical possessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import total_ordering
from typing import cast

from nba_live_totals.domain.games import Game, GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_TRIP_ID_PATTERN = re.compile(r"^ft:[0-9]{10}:[1-9][0-9]*:[0-9]+$")
_MAX_PERIOD = 65_535
_MAX_ISSUES = 32


def _require_exact_int(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        msg = f"{field_name} must be an integer"
        raise ValueError(msg)
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)
    return value


def _require_nonnegative_int(value: object, *, field_name: str) -> int:
    integer = _require_exact_int(value, field_name=field_name)
    if integer < 0:
        msg = f"{field_name} must be nonnegative"
        raise ValueError(msg)
    return integer


def _require_optional_positive_int(value: object | None, *, field_name: str) -> None:
    if value is not None:
        _require_positive_int(value, field_name=field_name)


def _require_optional_nonnegative_int(value: object | None, *, field_name: str) -> None:
    if value is not None:
        _require_nonnegative_int(value, field_name=field_name)


def _require_period(value: object) -> None:
    period = _require_positive_int(value, field_name="period")
    if period > _MAX_PERIOD:
        msg = f"period must be at most {_MAX_PERIOD}"
        raise ValueError(msg)


def _require_exact_bool(value: object, *, field_name: str) -> None:
    if type(value) is not bool:
        msg = f"{field_name} must be a boolean"
        raise ValueError(msg)


def _require_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        msg = f"{field_name} must be a lowercase SHA-256 digest"
        raise ValueError(msg)


def _require_safe_identifier(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or _SAFE_IDENTIFIER_PATTERN.fullmatch(value) is None:
        msg = f"{field_name} must be a safe code"
        raise ValueError(msg)


def _require_version(value: str) -> None:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        msg = "version must be a bounded lowercase safe identifier"
        raise ValueError(msg)


def _require_trip_id(value: str) -> None:
    if not isinstance(value, str) or _TRIP_ID_PATTERN.fullmatch(value) is None:
        msg = "free-throw trip ID must use the canonical format"
        raise ValueError(msg)


def _normalize_enum[EnumValue: StrEnum](
    value: object,
    enum_type: type[EnumValue],
    *,
    field_name: str,
) -> EnumValue:
    try:
        return enum_type(cast(str, value))
    except (TypeError, ValueError) as error:
        msg = f"{field_name} is invalid"
        raise ValueError(msg) from error


def _require_instance[InstanceValue](
    value: object,
    expected_type: type[InstanceValue],
    *,
    field_name: str,
) -> InstanceValue:
    if not isinstance(value, expected_type):
        msg = f"{field_name} has an invalid type"
        raise ValueError(msg)
    return value


class BoundarySide(StrEnum):
    BEFORE = "before"
    AFTER = "after"


class ActionConsumption(StrEnum):
    TRANSITION = "transition"
    ANNOTATION = "annotation"
    ADMINISTRATIVE = "administrative"
    UNSUPPORTED = "unsupported"


class IssueSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class ParseStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


class PossessionEndReason(StrEnum):
    MADE_FIELD_GOAL = "made_field_goal"
    DEFENSIVE_REBOUND = "defensive_rebound"
    TURNOVER = "turnover"
    FINAL_FREE_THROW = "final_free_throw"
    PERIOD_END = "period_end"
    JUMP_BALL_CHANGE = "jump_ball_change"
    CONTROL_AWARD = "control_award"


class ResumeDisposition(StrEnum):
    OPPONENT_INBOUND = "opponent_inbound"
    LIVE_REBOUND = "live_rebound"
    RETAIN_TEAM = "retain_team"
    RESUME_INTERRUPTED = "resume_interrupted"
    JUMP_BALL = "jump_ball"


class ReboundActor(StrEnum):
    PLAYER = "player"
    TEAM = "team"


class ProviderReboundSide(StrEnum):
    OFFENSIVE = "offensive"
    DEFENSIVE = "defensive"
    UNKNOWN = "unknown"


class ReboundDisposition(StrEnum):
    NORMAL_LIVE = "normal_live"
    FT_PLACEHOLDER = "ft_placeholder"
    RETAIN_TEAM = "retain_team"
    AWARD_OPPONENT = "award_opponent"


class FreeThrowKind(StrEnum):
    ORDINARY = "ordinary"
    AND_ONE = "and_one"
    TECHNICAL = "technical"
    FLAGRANT = "flagrant"
    CLEAR_PATH = "clear_path"
    AWAY_FROM_PLAY = "away_from_play"
    TRANSITION_TAKE = "transition_take"


class ViolationDisposition(StrEnum):
    RETAIN_OFFENSE = "retain_offense"
    OPPONENT_AWARD = "opponent_award"
    RETRY = "retry"
    JUMP_BALL = "jump_ball"


class GoaltendingSide(StrEnum):
    OFFENSIVE = "offensive"
    DEFENSIVE = "defensive"


@dataclass(frozen=True, slots=True)
class GameContext:
    game_id: GameId
    season: SeasonId
    season_type: SeasonType
    game_date: date
    status: GameStatus
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        _require_instance(self.season, SeasonId, field_name="season")
        season_type = _normalize_enum(self.season_type, SeasonType, field_name="season type")
        status = _normalize_enum(self.status, GameStatus, field_name="game status")
        object.__setattr__(self, "season_type", season_type)
        object.__setattr__(self, "status", status)
        if status is not GameStatus.FINAL:
            raise ValueError("game context must describe a final game")
        if type(self.game_date) is not date:
            raise ValueError("game date must be a date")
        _require_positive_int(self.home_team_id, field_name="home team ID")
        _require_positive_int(self.away_team_id, field_name="away team ID")
        if self.home_team_id == self.away_team_id:
            raise ValueError("game teams must be distinct")
        _require_nonnegative_int(self.home_score, field_name="home score")
        _require_nonnegative_int(self.away_score, field_name="away score")

    @classmethod
    def from_game(cls, game: Game) -> GameContext:
        if game.status is not GameStatus.FINAL:
            raise ValueError("game context requires a final game")
        if game.home_score is None or game.away_score is None:
            raise ValueError("final game context requires scores")
        return cls(
            game_id=game.game_id,
            season=game.season,
            season_type=game.season_type,
            game_date=game.game_date,
            status=game.status,
            home_team_id=game.home_team_id,
            away_team_id=game.away_team_id,
            home_score=game.home_score,
            away_score=game.away_score,
        )


@dataclass(frozen=True, slots=True)
class PossessionVersions:
    normalizer_version: str
    parser_version: str
    rules_version: str
    output_schema_version: str

    def __post_init__(self) -> None:
        for version in (
            self.normalizer_version,
            self.parser_version,
            self.rules_version,
            self.output_schema_version,
        ):
            _require_version(version)


@dataclass(frozen=True, slots=True)
class PossessionParseContext:
    game: GameContext
    versions: PossessionVersions
    pbp_document_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str
    parse_revision_id: str

    def __post_init__(self) -> None:
        _require_instance(self.game, GameContext, field_name="game context")
        _require_instance(self.versions, PossessionVersions, field_name="versions")
        _require_sha256(self.pbp_document_sha256, field_name="PBP document SHA-256")
        _require_sha256(self.canonical_event_sha256, field_name="canonical event SHA-256")
        _require_sha256(self.game_context_sha256, field_name="game context SHA-256")
        _require_sha256(self.parse_revision_id, field_name="parse revision SHA-256")


@total_ordering
@dataclass(frozen=True, slots=True)
class EventBoundary:
    sequence_number: int
    side: BoundarySide

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence_number, field_name="boundary sequence number")
        side = _normalize_enum(self.side, BoundarySide, field_name="boundary side")
        object.__setattr__(self, "side", side)

    def _key(self) -> tuple[int, int]:
        return self.sequence_number, 0 if self.side is BoundarySide.BEFORE else 1

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, EventBoundary):
            return NotImplemented
        return self._key() < other._key()


@dataclass(frozen=True, slots=True)
class PeriodStart:
    pass


@dataclass(frozen=True, slots=True)
class PeriodEnd:
    pass


@dataclass(frozen=True, slots=True)
class FieldGoalAttempt:
    team: int
    value: int
    made: bool
    and_one_candidate: bool

    def __post_init__(self) -> None:
        _require_positive_int(self.team, field_name="field-goal team")
        if type(self.value) is not int or self.value not in (2, 3):
            raise ValueError("field-goal value must be 2 or 3")
        _require_exact_bool(self.made, field_name="made")
        _require_exact_bool(self.and_one_candidate, field_name="and-one candidate")


@dataclass(frozen=True, slots=True)
class Rebound:
    rebounding_team: int
    actor: ReboundActor
    provider_side: ProviderReboundSide
    disposition: ReboundDisposition

    def __post_init__(self) -> None:
        _require_positive_int(self.rebounding_team, field_name="rebounding team")
        object.__setattr__(
            self,
            "actor",
            _normalize_enum(self.actor, ReboundActor, field_name="rebound actor"),
        )
        object.__setattr__(
            self,
            "provider_side",
            _normalize_enum(
                self.provider_side,
                ProviderReboundSide,
                field_name="provider rebound side",
            ),
        )
        object.__setattr__(
            self,
            "disposition",
            _normalize_enum(
                self.disposition,
                ReboundDisposition,
                field_name="rebound disposition",
            ),
        )


@dataclass(frozen=True, slots=True)
class Turnover:
    committing_team: int
    gained_by: int | None
    turnover_kind: str

    def __post_init__(self) -> None:
        _require_positive_int(self.committing_team, field_name="committing team")
        _require_optional_positive_int(self.gained_by, field_name="gaining team")
        _require_safe_identifier(self.turnover_kind, field_name="turnover kind")


@dataclass(frozen=True, slots=True)
class StealAnnotation:
    team: int
    related_sequence_number: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.team, field_name="steal team")
        _require_optional_positive_int(
            self.related_sequence_number, field_name="related sequence number"
        )


@dataclass(frozen=True, slots=True)
class Foul:
    committing_team: int
    foul_kind: str
    shooting_team: int | None = None
    related_sequence_number: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.committing_team, field_name="committing team")
        _require_safe_identifier(self.foul_kind, field_name="foul kind")
        _require_optional_positive_int(self.shooting_team, field_name="shooting team")
        _require_optional_positive_int(
            self.related_sequence_number, field_name="related sequence number"
        )


@dataclass(frozen=True, slots=True)
class FreeThrowTripStart:
    trip_id: str
    shooting_team: int
    free_throw_kind: FreeThrowKind
    attempts: int
    resume_disposition: ResumeDisposition
    resume_team: int | None = None
    interrupted_trip_id: str | None = None

    def __post_init__(self) -> None:
        _require_trip_id(self.trip_id)
        _require_positive_int(self.shooting_team, field_name="shooting team")
        kind = _normalize_enum(self.free_throw_kind, FreeThrowKind, field_name="free-throw kind")
        disposition = _normalize_enum(
            self.resume_disposition,
            ResumeDisposition,
            field_name="resume disposition",
        )
        object.__setattr__(self, "free_throw_kind", kind)
        object.__setattr__(self, "resume_disposition", disposition)
        attempts = _require_positive_int(self.attempts, field_name="free-throw attempts")
        if attempts > 3:
            raise ValueError("free-throw attempts must be at most 3")
        if disposition is ResumeDisposition.RETAIN_TEAM:
            if self.resume_team is None:
                raise ValueError("retain-team disposition requires a resume team")
            _require_positive_int(self.resume_team, field_name="resume team")
        elif self.resume_team is not None:
            raise ValueError("resume team is only valid for retain-team disposition")
        if self.interrupted_trip_id is not None:
            _require_trip_id(self.interrupted_trip_id)


@dataclass(frozen=True, slots=True)
class FreeThrowAttempt:
    trip_id: str
    attempt: int
    total: int
    made: bool
    awarded_points: int

    def __post_init__(self) -> None:
        _require_trip_id(self.trip_id)
        attempt = _require_positive_int(self.attempt, field_name="free-throw attempt")
        total = _require_positive_int(self.total, field_name="free-throw total")
        if total > 3 or attempt > total:
            raise ValueError("free-throw attempt must be within total attempts")
        _require_exact_bool(self.made, field_name="free-throw made")
        points = _require_nonnegative_int(self.awarded_points, field_name="awarded points")
        if points > 1:
            raise ValueError("free-throw awarded points must be at most 1")


@dataclass(frozen=True, slots=True)
class FreeThrowTripEnd:
    trip_id: str
    resolution: ResumeDisposition
    resume_team: int | None = None

    def __post_init__(self) -> None:
        _require_trip_id(self.trip_id)
        resolution = _normalize_enum(
            self.resolution, ResumeDisposition, field_name="free-throw resolution"
        )
        object.__setattr__(self, "resolution", resolution)
        if resolution is ResumeDisposition.RETAIN_TEAM:
            if self.resume_team is None:
                raise ValueError("retain-team resolution requires a resume team")
            _require_positive_int(self.resume_team, field_name="resume team")
        elif self.resume_team is not None:
            raise ValueError("resume team is only valid for retain-team resolution")


@dataclass(frozen=True, slots=True)
class JumpBall:
    winner_team: int | None = None

    def __post_init__(self) -> None:
        _require_optional_positive_int(self.winner_team, field_name="jump-ball winner team")


@dataclass(frozen=True, slots=True)
class Violation:
    team: int | None
    disposition: ViolationDisposition

    def __post_init__(self) -> None:
        _require_optional_positive_int(self.team, field_name="violation team")
        object.__setattr__(
            self,
            "disposition",
            _normalize_enum(
                self.disposition,
                ViolationDisposition,
                field_name="violation disposition",
            ),
        )


@dataclass(frozen=True, slots=True)
class Goaltending:
    team: int
    side: GoaltendingSide
    awarded_points: int

    def __post_init__(self) -> None:
        _require_positive_int(self.team, field_name="goaltending team")
        object.__setattr__(
            self,
            "side",
            _normalize_enum(self.side, GoaltendingSide, field_name="goaltending side"),
        )
        points = _require_nonnegative_int(self.awarded_points, field_name="awarded points")
        if points > 3:
            raise ValueError("goaltending awarded points must be at most 3")


@dataclass(frozen=True, slots=True)
class ControlAward:
    team: int
    reason: str

    def __post_init__(self) -> None:
        _require_positive_int(self.team, field_name="control team")
        _require_safe_identifier(self.reason, field_name="control reason")


@dataclass(frozen=True, slots=True)
class Administrative:
    administrative_kind: str

    def __post_init__(self) -> None:
        _require_safe_identifier(self.administrative_kind, field_name="administrative kind")


@dataclass(frozen=True, slots=True)
class Unsupported:
    control_relevant: bool

    def __post_init__(self) -> None:
        _require_exact_bool(self.control_relevant, field_name="control relevance")


_POSSESSION_SIGNAL_TYPES = (
    PeriodStart,
    PeriodEnd,
    FieldGoalAttempt,
    Rebound,
    Turnover,
    StealAnnotation,
    Foul,
    FreeThrowTripStart,
    FreeThrowAttempt,
    FreeThrowTripEnd,
    JumpBall,
    Violation,
    Goaltending,
    ControlAward,
    Administrative,
    Unsupported,
)


type PossessionSignal = (
    PeriodStart
    | PeriodEnd
    | FieldGoalAttempt
    | Rebound
    | Turnover
    | StealAnnotation
    | Foul
    | FreeThrowTripStart
    | FreeThrowAttempt
    | FreeThrowTripEnd
    | JumpBall
    | Violation
    | Goaltending
    | ControlAward
    | Administrative
    | Unsupported
)


@dataclass(frozen=True, slots=True)
class NormalizedAction:
    game_id: GameId
    sequence_number: int
    provider_action_number: int
    provider_action_id: int | None
    period: int
    clock_ms: int
    actor_team_id: int | None
    actor_person_id: int | None
    checkpoint_home_score: int | None
    checkpoint_away_score: int | None
    checkpoint_combined_score: int | None
    consumption: ActionConsumption
    signals: tuple[PossessionSignal, ...]

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        _require_positive_int(self.sequence_number, field_name="sequence number")
        _require_exact_int(self.provider_action_number, field_name="provider action number")
        if self.provider_action_id is not None:
            _require_exact_int(self.provider_action_id, field_name="provider action ID")
        _require_period(self.period)
        _require_nonnegative_int(self.clock_ms, field_name="clock milliseconds")
        _require_optional_positive_int(self.actor_team_id, field_name="actor team ID")
        _require_optional_positive_int(self.actor_person_id, field_name="actor person ID")
        _require_optional_nonnegative_int(
            self.checkpoint_home_score, field_name="home score checkpoint"
        )
        _require_optional_nonnegative_int(
            self.checkpoint_away_score, field_name="away score checkpoint"
        )
        _require_optional_nonnegative_int(
            self.checkpoint_combined_score, field_name="combined score checkpoint"
        )
        consumption = _normalize_enum(
            self.consumption, ActionConsumption, field_name="action consumption"
        )
        object.__setattr__(self, "consumption", consumption)
        signal_tuple = tuple(self.signals)
        if not signal_tuple:
            raise ValueError("normalized action signals must not be empty")
        if any(type(signal) not in _POSSESSION_SIGNAL_TYPES for signal in signal_tuple):
            raise ValueError("normalized action contains an invalid possession signal")
        object.__setattr__(self, "signals", signal_tuple)


@dataclass(frozen=True, slots=True)
class PossessionIssue:
    severity: IssueSeverity
    code: str
    sequence_number: int | None = None
    related_sequence_number: int | None = None

    def __post_init__(self) -> None:
        severity = _normalize_enum(self.severity, IssueSeverity, field_name="issue severity")
        object.__setattr__(self, "severity", severity)
        _require_safe_identifier(self.code, field_name="issue code")
        _require_optional_positive_int(self.sequence_number, field_name="issue sequence number")
        _require_optional_positive_int(
            self.related_sequence_number, field_name="related sequence number"
        )


@dataclass(frozen=True, slots=True)
class NormalizedGameActions:
    game_id: GameId
    actions: tuple[NormalizedAction, ...]
    issues: tuple[PossessionIssue, ...] = ()

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        actions = tuple(self.actions)
        issues = tuple(self.issues)
        previous_sequence = 0
        for action in actions:
            _require_instance(action, NormalizedAction, field_name="normalized action")
            if action.game_id != self.game_id:
                raise ValueError("normalized actions must belong to one game")
            if action.sequence_number <= previous_sequence:
                raise ValueError("normalized actions must be in unique sequence order")
            previous_sequence = action.sequence_number
        if len(issues) > _MAX_ISSUES:
            raise ValueError(f"normalized actions may contain at most {_MAX_ISSUES} issues")
        for issue in issues:
            _require_instance(issue, PossessionIssue, field_name="possession issue")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True, slots=True)
class Possession:
    game_id: GameId
    possession_number: int
    offense_team_id: int
    defense_team_id: int
    offense_possession_number: int
    period: int
    start_boundary: EventBoundary
    end_boundary: EventBoundary
    start_seconds_remaining_period_ms: int
    end_seconds_remaining_period_ms: int
    seconds_remaining_game_ms: int
    home_score_before: int
    away_score_before: int
    home_score: int
    away_score: int
    score_diff: int
    offense_points: int
    defense_points: int
    home_possessions: int
    away_possessions: int
    end_reason: PossessionEndReason
    source_event_count: int

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        _require_positive_int(self.possession_number, field_name="possession number")
        _require_positive_int(self.offense_team_id, field_name="offense team ID")
        _require_positive_int(self.defense_team_id, field_name="defense team ID")
        if self.offense_team_id == self.defense_team_id:
            raise ValueError("offense and defense teams must be distinct")
        _require_positive_int(
            self.offense_possession_number, field_name="offense possession number"
        )
        _require_period(self.period)
        _require_instance(self.start_boundary, EventBoundary, field_name="start boundary")
        _require_instance(self.end_boundary, EventBoundary, field_name="end boundary")
        if self.start_boundary >= self.end_boundary:
            raise ValueError("possession start boundary must precede end boundary")
        for field_name, value in (
            ("start clock milliseconds", self.start_seconds_remaining_period_ms),
            ("end clock milliseconds", self.end_seconds_remaining_period_ms),
            ("game clock milliseconds", self.seconds_remaining_game_ms),
            ("home score before", self.home_score_before),
            ("away score before", self.away_score_before),
            ("home score", self.home_score),
            ("away score", self.away_score),
            ("offense points", self.offense_points),
            ("defense points", self.defense_points),
            ("home possessions", self.home_possessions),
            ("away possessions", self.away_possessions),
        ):
            _require_nonnegative_int(value, field_name=field_name)
        _require_exact_int(self.score_diff, field_name="score difference")
        if self.score_diff != self.home_score - self.away_score:
            raise ValueError("score difference must equal home score minus away score")
        _require_positive_int(self.source_event_count, field_name="source event count")
        try:
            reason = PossessionEndReason(self.end_reason)
        except ValueError as error:
            raise ValueError("possession end reason is invalid") from error
        object.__setattr__(self, "end_reason", reason)


@dataclass(frozen=True, slots=True)
class PossessionParseResult:
    context: PossessionParseContext
    status: ParseStatus
    possessions: tuple[Possession, ...]
    issues: tuple[PossessionIssue, ...]
    output_sha256: str | None

    def __post_init__(self) -> None:
        _require_instance(self.context, PossessionParseContext, field_name="parse context")
        status = _normalize_enum(self.status, ParseStatus, field_name="parse status")
        object.__setattr__(self, "status", status)
        possessions = tuple(self.possessions)
        issues = tuple(self.issues)
        if len(issues) > _MAX_ISSUES:
            raise ValueError(f"parse result may contain at most {_MAX_ISSUES} issues")
        for possession in possessions:
            _require_instance(possession, Possession, field_name="possession")
            if possession.game_id != self.context.game.game_id:
                raise ValueError("parse result possessions must belong to the context game")
        for issue in issues:
            _require_instance(issue, PossessionIssue, field_name="possession issue")
        has_error = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        if status is ParseStatus.VALID:
            if has_error:
                raise ValueError("valid result cannot contain error issues")
            if not possessions:
                raise ValueError("valid result must contain possessions")
            if self.output_sha256 is None:
                raise ValueError("valid result requires an output SHA-256")
            _require_sha256(self.output_sha256, field_name="output SHA-256")
            numbers = tuple(item.possession_number for item in possessions)
            if numbers != tuple(range(1, len(possessions) + 1)):
                raise ValueError("valid result possession numbers must be contiguous")
        else:
            if not has_error:
                raise ValueError("invalid result must contain an error issue")
            if self.output_sha256 is not None:
                raise ValueError("invalid result cannot have an output SHA-256")
        object.__setattr__(self, "possessions", possessions)
        object.__setattr__(self, "issues", issues)


__all__ = [
    "ActionConsumption",
    "Administrative",
    "BoundarySide",
    "ControlAward",
    "EventBoundary",
    "FieldGoalAttempt",
    "Foul",
    "FreeThrowAttempt",
    "FreeThrowKind",
    "FreeThrowTripEnd",
    "FreeThrowTripStart",
    "GameContext",
    "Goaltending",
    "GoaltendingSide",
    "IssueSeverity",
    "JumpBall",
    "NormalizedAction",
    "NormalizedGameActions",
    "ParseStatus",
    "PeriodEnd",
    "PeriodStart",
    "Possession",
    "PossessionEndReason",
    "PossessionIssue",
    "PossessionParseContext",
    "PossessionParseResult",
    "PossessionSignal",
    "PossessionVersions",
    "ProviderReboundSide",
    "Rebound",
    "ReboundActor",
    "ReboundDisposition",
    "ResumeDisposition",
    "StealAnnotation",
    "Turnover",
    "Unsupported",
    "Violation",
    "ViolationDisposition",
]
