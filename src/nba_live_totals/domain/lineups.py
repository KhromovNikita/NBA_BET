"""Immutable provider-neutral contracts for lineup reconstruction."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, GameContext

_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_PERIOD = 65_535
_MAX_ISSUES = 32


def _require_exact_int(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    integer = _require_exact_int(value, field_name=field_name)
    if integer <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return integer


def _require_nonnegative_int(value: object, *, field_name: str) -> int:
    integer = _require_exact_int(value, field_name=field_name)
    if integer < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return integer


def _require_period(value: object, *, field_name: str) -> None:
    period = _require_positive_int(value, field_name=field_name)
    if period > _MAX_PERIOD:
        raise ValueError(f"{field_name} must be at most {_MAX_PERIOD}")


def _require_exact_bool(value: object, *, field_name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")


def _require_optional_string(value: object, *, field_name: str) -> None:
    if value is not None and type(value) is not str:
        raise ValueError(f"{field_name} must be a string or null")


def _require_safe_identifier(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SAFE_IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe code")


def _require_version(value: object) -> None:
    if type(value) is not str or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("version must be a bounded lowercase safe identifier")


def _require_sha256(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_instance[InstanceValue](
    value: object,
    expected_type: type[InstanceValue],
    *,
    field_name: str,
) -> InstanceValue:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} has an invalid type")
    return value


def _normalize_enum[EnumValue: StrEnum](
    value: object,
    enum_type: type[EnumValue],
    *,
    field_name: str,
) -> EnumValue:
    try:
        return enum_type(cast(str, value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} is invalid") from error


def _snapshot_positive_ids(values: object, *, field_name: str) -> frozenset[int]:
    try:
        snapshot = frozenset(cast(Iterable[object], values))
    except TypeError as error:
        raise ValueError(f"{field_name} must be an iterable of player IDs") from error
    for player_id in snapshot:
        _require_positive_int(player_id, field_name="player ID")
    return cast(frozenset[int], snapshot)


@dataclass(frozen=True, slots=True)
class LineupVersions:
    normalizer_version: str
    reconstructor_version: str
    rules_version: str
    output_schema_version: str

    def __post_init__(self) -> None:
        for version in (
            self.normalizer_version,
            self.reconstructor_version,
            self.rules_version,
            self.output_schema_version,
        ):
            _require_version(version)


@dataclass(frozen=True, slots=True)
class LineupPlayerBox:
    game_id: GameId
    team_id: int
    player_id: int
    position: str | None
    starter: bool
    played: bool
    comment: str | None

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        _require_positive_int(self.team_id, field_name="team ID")
        _require_positive_int(self.player_id, field_name="player ID")
        _require_optional_string(self.position, field_name="position")
        _require_exact_bool(self.starter, field_name="starter")
        _require_exact_bool(self.played, field_name="played")
        _require_optional_string(self.comment, field_name="comment")


@dataclass(frozen=True, slots=True)
class TeamRoster:
    team_id: int
    roster_player_ids: frozenset[int]
    eligible_player_ids: frozenset[int]
    starter_player_ids: frozenset[int]

    def __post_init__(self) -> None:
        _require_positive_int(self.team_id, field_name="team ID")
        roster = _snapshot_positive_ids(self.roster_player_ids, field_name="roster player IDs")
        eligible = _snapshot_positive_ids(
            self.eligible_player_ids, field_name="eligible player IDs"
        )
        starters = _snapshot_positive_ids(self.starter_player_ids, field_name="starter player IDs")
        if not eligible <= roster:
            raise ValueError("eligible player IDs must belong to the roster")
        if not starters <= eligible:
            raise ValueError("starter player IDs must be eligible roster players")
        object.__setattr__(self, "roster_player_ids", roster)
        object.__setattr__(self, "eligible_player_ids", eligible)
        object.__setattr__(self, "starter_player_ids", starters)


class LineupStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class LineupIssue:
    team_id: int
    reason: str
    sequence_number: int
    boundary_side: BoundarySide

    def __post_init__(self) -> None:
        _require_positive_int(self.team_id, field_name="team ID")
        _require_safe_identifier(self.reason, field_name="issue reason")
        _require_positive_int(self.sequence_number, field_name="issue sequence number")
        side = _normalize_enum(self.boundary_side, BoundarySide, field_name="issue boundary side")
        object.__setattr__(self, "boundary_side", side)


@dataclass(frozen=True, slots=True)
class Substitute:
    team_id: int
    outgoing_player_id: int
    incoming_player_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.team_id, field_name="team ID")
        _require_positive_int(self.outgoing_player_id, field_name="outgoing player ID")
        _require_positive_int(self.incoming_player_id, field_name="incoming player ID")


@dataclass(frozen=True, slots=True)
class InvalidTeamTransition:
    team_id: int | None
    code: str

    def __post_init__(self) -> None:
        if self.team_id is not None:
            _require_positive_int(self.team_id, field_name="team ID")
        _require_safe_identifier(self.code, field_name="transition code")


@dataclass(frozen=True, slots=True)
class FullLineupReset:
    team_id: int
    player_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.team_id, field_name="team ID")
        try:
            player_ids = tuple(self.player_ids)
        except TypeError as error:
            raise ValueError("reset player IDs must be an iterable") from error
        for player_id in player_ids:
            _require_positive_int(player_id, field_name="reset player ID")
        object.__setattr__(self, "player_ids", player_ids)


@dataclass(frozen=True, slots=True)
class NoLineupChange:
    pass


type LineupTransition = Substitute | InvalidTeamTransition | FullLineupReset | NoLineupChange

_LINEUP_TRANSITION_TYPES = (
    Substitute,
    InvalidTeamTransition,
    FullLineupReset,
    NoLineupChange,
)


@dataclass(frozen=True, slots=True)
class NormalizedLineupEvent:
    sequence_number: int
    period: int
    seconds_remaining_period_ms: int
    transition: LineupTransition

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence_number, field_name="sequence number")
        _require_period(self.period, field_name="period")
        _require_nonnegative_int(self.seconds_remaining_period_ms, field_name="clock milliseconds")
        if type(self.transition) not in _LINEUP_TRANSITION_TYPES:
            raise ValueError("lineup transition has an invalid type")


@dataclass(frozen=True, slots=True)
class NormalizedGameLineups:
    game_id: GameId
    home_roster: TeamRoster
    away_roster: TeamRoster
    events: tuple[NormalizedLineupEvent, ...]

    def __post_init__(self) -> None:
        _require_instance(self.game_id, GameId, field_name="game ID")
        home = _require_instance(self.home_roster, TeamRoster, field_name="home roster")
        away = _require_instance(self.away_roster, TeamRoster, field_name="away roster")
        if home.team_id == away.team_id:
            raise ValueError("home and away rosters must have distinct teams")
        try:
            events = tuple(self.events)
        except TypeError as error:
            raise ValueError("normalized lineup events must be an iterable") from error
        if not events:
            raise ValueError("normalized lineup events must not be empty")
        for expected_sequence, event in enumerate(events, start=1):
            _require_instance(event, NormalizedLineupEvent, field_name="normalized lineup event")
            if event.sequence_number != expected_sequence:
                raise ValueError(
                    "normalized lineup events must have one-based contiguous sequences"
                )
        object.__setattr__(self, "events", events)


@dataclass(frozen=True, slots=True)
class TeamLineupState:
    status: LineupStatus
    player_ids: frozenset[int]
    invalid_reason: str | None
    invalid_since: EventBoundary | None

    def __post_init__(self) -> None:
        status = _normalize_enum(self.status, LineupStatus, field_name="lineup status")
        player_ids = _snapshot_positive_ids(self.player_ids, field_name="lineup player IDs")
        if status is LineupStatus.VALID:
            if len(player_ids) != 5:
                raise ValueError("valid lineup must contain exactly five players")
            if self.invalid_reason is not None or self.invalid_since is not None:
                raise ValueError("valid lineup must not carry invalid metadata")
        else:
            if player_ids:
                raise ValueError("invalid lineup player IDs must be empty")
            if self.invalid_reason is None:
                raise ValueError("invalid lineup requires an invalid reason")
            _require_safe_identifier(self.invalid_reason, field_name="invalid lineup reason")
            if self.invalid_since is None:
                raise ValueError("invalid lineup requires an invalid since boundary")
            _require_instance(
                self.invalid_since,
                EventBoundary,
                field_name="invalid since boundary",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "player_ids", player_ids)


@dataclass(frozen=True, slots=True)
class LineupSegment:
    segment_number: int
    start_boundary: EventBoundary
    end_boundary: EventBoundary
    start_period: int
    end_period: int
    start_seconds_remaining_period_ms: int
    end_seconds_remaining_period_ms: int
    home_state: TeamLineupState
    away_state: TeamLineupState

    def __post_init__(self) -> None:
        _require_positive_int(self.segment_number, field_name="segment number")
        start = _require_instance(self.start_boundary, EventBoundary, field_name="start boundary")
        end = _require_instance(self.end_boundary, EventBoundary, field_name="end boundary")
        if start > end:
            raise ValueError("segment start boundary must not follow end boundary")
        _require_period(self.start_period, field_name="start period")
        _require_period(self.end_period, field_name="end period")
        _require_nonnegative_int(
            self.start_seconds_remaining_period_ms,
            field_name="start clock milliseconds",
        )
        _require_nonnegative_int(
            self.end_seconds_remaining_period_ms,
            field_name="end clock milliseconds",
        )
        _require_instance(self.home_state, TeamLineupState, field_name="home lineup state")
        _require_instance(self.away_state, TeamLineupState, field_name="away lineup state")


@dataclass(frozen=True, slots=True)
class LineupReconstructionContext:
    game: GameContext
    versions: LineupVersions
    box_score_document_sha256: str
    pbp_document_sha256: str
    canonical_player_box_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str
    lineup_revision_id: str

    def __post_init__(self) -> None:
        _require_instance(self.game, GameContext, field_name="game context")
        _require_instance(self.versions, LineupVersions, field_name="versions")
        for field_name, digest in (
            ("box-score document SHA-256", self.box_score_document_sha256),
            ("PBP document SHA-256", self.pbp_document_sha256),
            ("canonical player-box SHA-256", self.canonical_player_box_sha256),
            ("canonical event SHA-256", self.canonical_event_sha256),
            ("game context SHA-256", self.game_context_sha256),
            ("lineup revision SHA-256", self.lineup_revision_id),
        ):
            _require_sha256(digest, field_name=field_name)


def _issues_from_segments(
    context: LineupReconstructionContext,
    segments: tuple[LineupSegment, ...],
) -> tuple[LineupIssue, ...]:
    issues: list[LineupIssue] = []
    seen: set[LineupIssue] = set()
    team_states = (
        (context.game.home_team_id, "home_state"),
        (context.game.away_team_id, "away_state"),
    )
    for segment in segments:
        for team_id, state_field in team_states:
            state = cast(TeamLineupState, getattr(segment, state_field))
            if state.status is LineupStatus.VALID:
                continue
            assert state.invalid_reason is not None
            assert state.invalid_since is not None
            issue = LineupIssue(
                team_id=team_id,
                reason=state.invalid_reason,
                sequence_number=state.invalid_since.sequence_number,
                boundary_side=state.invalid_since.side,
            )
            if issue not in seen:
                seen.add(issue)
                issues.append(issue)
                if len(issues) == _MAX_ISSUES:
                    return tuple(issues)
    return tuple(issues)


@dataclass(frozen=True, slots=True)
class LineupReconstructionResult:
    context: LineupReconstructionContext
    status: LineupStatus
    segments: tuple[LineupSegment, ...]
    issues: tuple[LineupIssue, ...]

    def __post_init__(self) -> None:
        context = _require_instance(
            self.context,
            LineupReconstructionContext,
            field_name="reconstruction context",
        )
        status = _normalize_enum(self.status, LineupStatus, field_name="result status")
        try:
            segments = tuple(self.segments)
            issues = tuple(self.issues)
        except TypeError as error:
            raise ValueError("result segments and issues must be iterable") from error
        if not segments:
            raise ValueError("lineup result must contain segments")
        previous_end: EventBoundary | None = None
        for expected_number, segment in enumerate(segments, start=1):
            _require_instance(segment, LineupSegment, field_name="lineup segment")
            if segment.segment_number != expected_number:
                raise ValueError("lineup result segment numbers must be contiguous")
            if previous_end is not None and segment.start_boundary <= previous_end:
                raise ValueError("lineup result segments must not overlap")
            previous_end = segment.end_boundary
        derived_status = (
            LineupStatus.VALID
            if all(
                segment.home_state.status is LineupStatus.VALID
                and segment.away_state.status is LineupStatus.VALID
                for segment in segments
            )
            else LineupStatus.INVALID
        )
        if status is not derived_status:
            raise ValueError("result status must be derived from every segment")
        for issue in issues:
            _require_instance(issue, LineupIssue, field_name="lineup issue")
        expected_issues = _issues_from_segments(context, segments)
        if issues != expected_issues:
            raise ValueError(
                "result issues must equal the bounded deterministic segment projection"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "issues", issues)


__all__ = [
    "FullLineupReset",
    "InvalidTeamTransition",
    "LineupIssue",
    "LineupPlayerBox",
    "LineupReconstructionContext",
    "LineupReconstructionResult",
    "LineupSegment",
    "LineupStatus",
    "LineupTransition",
    "LineupVersions",
    "NoLineupChange",
    "NormalizedGameLineups",
    "NormalizedLineupEvent",
    "Substitute",
    "TeamLineupState",
    "TeamRoster",
]
