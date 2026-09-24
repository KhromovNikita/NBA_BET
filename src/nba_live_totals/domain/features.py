"""Immutable provider-neutral inputs and outputs for feature calculation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from nba_live_totals.domain.games import Game
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.lineups import LineupSegment, LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession

FEATURE_SCALE = 1_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_INCLUSION_REASONS = frozenset({"home_team_window", "away_team_window", "player_history"})


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an exact integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _optional_integer(value: object, name: str, *, minimum: int | None = None) -> None:
    if value is not None:
        _integer(value, name, minimum=minimum)


def _boolean(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _instance[T](value: object, expected: type[T], name: str) -> T:
    if not isinstance(value, expected):
        raise ValueError(f"{name} has an invalid type")
    return value


def _digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _safe_code(value: object, name: str) -> None:
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe issue code")


def _tuple_of[T](values: object, expected: type[T], name: str) -> tuple[T, ...]:
    try:
        result = tuple(cast("tuple[object, ...]", values))
    except TypeError as error:
        raise ValueError(f"{name} must be iterable") from error
    for value in result:
        _instance(value, expected, name)
    return cast("tuple[T, ...]", result)


def _positive_ids(values: object, name: str) -> tuple[int, ...]:
    try:
        result = tuple(cast("tuple[object, ...]", values))
    except TypeError as error:
        raise ValueError(f"{name} must be iterable") from error
    for value in result:
        _integer(value, name, minimum=1)
    ids = cast("tuple[int, ...]", result)
    if len(set(ids)) != len(ids):
        raise ValueError(f"{name} must be unique")
    return tuple(sorted(ids))


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _enum[T: StrEnum](value: object, enum_type: type[T], name: str) -> T:
    try:
        return enum_type(cast("str", value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} is invalid") from error


def _validate_optional_metrics(value: object, *, except_names: frozenset[str]) -> None:
    for item in fields(cast(Any, value)):
        if item.name not in except_names:
            _optional_integer(getattr(value, item.name), item.name.replace("_", " "))


def scaled_ratio(numerator: int, denominator: int, *, multiplier: int = 1) -> int | None:
    """Return multiplier*numerator/denominator at FEATURE_SCALE, half away from zero."""
    _integer(numerator, "numerator")
    _integer(denominator, "denominator")
    _integer(multiplier, "multiplier")
    if denominator == 0:
        return None
    dividend = FEATURE_SCALE * multiplier * numerator
    sign = -1 if (dividend < 0) != (denominator < 0) else 1
    magnitude, remainder = divmod(abs(dividend), abs(denominator))
    return sign * (magnitude + (2 * remainder >= abs(denominator)))


@dataclass(frozen=True, slots=True)
class FeatureVersions:
    join_version: str = "feature-join-v1"
    formula_version: str = "feature-formulas-v1"
    event_normalizer_version: str = "nba-feature-events-v1"
    output_schema_version: str = "game-state-features-v1"
    player_output_schema_version: str = "player-pregame-features-v1"

    def __post_init__(self) -> None:
        for item in fields(self):
            version = getattr(self, item.name)
            if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
                raise ValueError(f"{item.name} must be a bounded lowercase safe version")


class FeatureStateKind(StrEnum):
    PREGAME = "pregame"
    AFTER_POSSESSION = "after_possession"


class FeatureResultStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class HistoricalTeamBox:
    game_id: GameId
    team_id: int
    seconds_played: int | None = None
    field_goals_made: int | None = None
    field_goals_attempted: int | None = None
    three_pointers_made: int | None = None
    three_pointers_attempted: int | None = None
    free_throws_made: int | None = None
    free_throws_attempted: int | None = None
    rebounds_offensive: int | None = None
    rebounds_defensive: int | None = None
    rebounds_total: int | None = None
    assists: int | None = None
    turnovers: int | None = None
    points: int | None = None

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        _integer(self.team_id, "team ID", minimum=1)
        for item in fields(self):
            if item.name not in {"game_id", "team_id"}:
                _optional_integer(getattr(self, item.name), item.name.replace("_", " "), minimum=0)


@dataclass(frozen=True, slots=True)
class HistoricalPlayerBox:
    game_id: GameId
    team_id: int
    player_id: int
    played: bool
    seconds_played: int | None = None
    field_goals_made: int | None = None
    field_goals_attempted: int | None = None
    three_pointers_made: int | None = None
    three_pointers_attempted: int | None = None
    free_throws_made: int | None = None
    free_throws_attempted: int | None = None
    rebounds_offensive: int | None = None
    rebounds_defensive: int | None = None
    rebounds_total: int | None = None
    assists: int | None = None
    turnovers: int | None = None
    points: int | None = None

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        _integer(self.team_id, "team ID", minimum=1)
        _integer(self.player_id, "player ID", minimum=1)
        _boolean(self.played, "played")
        for item in fields(self):
            if item.name not in {"game_id", "team_id", "player_id", "played"}:
                _optional_integer(getattr(self, item.name), item.name.replace("_", " "), minimum=0)


@dataclass(frozen=True, slots=True)
class FeatureDependency:
    dependency_game_id: GameId
    started_at: datetime
    game_context_sha256: str
    applied_box_score_document_id: UUID
    applied_box_score_sha256: str
    canonical_team_box_sha256: str
    canonical_player_box_sha256: str
    possession_snapshot_id: UUID
    parse_revision_id: str
    possession_input_token_sha256: str
    possession_output_sha256: str
    ordinal: int
    inclusion_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _instance(self.dependency_game_id, GameId, "dependency game ID")
        object.__setattr__(self, "started_at", _utc(self.started_at, "start time"))
        for name in (
            "game_context_sha256",
            "applied_box_score_sha256",
            "canonical_team_box_sha256",
            "canonical_player_box_sha256",
            "parse_revision_id",
            "possession_input_token_sha256",
            "possession_output_sha256",
        ):
            _digest(getattr(self, name), name)
        _instance(self.applied_box_score_document_id, UUID, "box document UUID")
        _instance(self.possession_snapshot_id, UUID, "possession snapshot UUID")
        _integer(self.ordinal, "ordinal", minimum=0)
        try:
            reasons = tuple(self.inclusion_reasons)
        except TypeError as error:
            raise ValueError("inclusion reasons must be iterable") from error
        if not reasons or any(reason not in _INCLUSION_REASONS for reason in reasons):
            raise ValueError("inclusion reasons must use approved codes")
        object.__setattr__(self, "inclusion_reasons", tuple(sorted(set(reasons))))


@dataclass(frozen=True, slots=True)
class FeatureDependencyManifest:
    dependencies: tuple[FeatureDependency, ...]

    def __post_init__(self) -> None:
        dependencies = _tuple_of(self.dependencies, FeatureDependency, "dependencies")
        ordered = tuple(
            sorted(dependencies, key=lambda row: (row.started_at, row.dependency_game_id.value))
        )
        if len({row.dependency_game_id for row in ordered}) != len(ordered):
            raise ValueError("duplicate dependency games")
        if any(row.ordinal != ordinal for ordinal, row in enumerate(ordered)):
            raise ValueError("dependency ordinals must follow canonical order")
        object.__setattr__(self, "dependencies", ordered)


@dataclass(frozen=True, slots=True)
class FeatureStatDelta:
    sequence_number: int
    boundary: EventBoundary
    team_id: int | None = None
    field_goal_attempts: int = 0
    three_point_attempts: int = 0
    free_throw_attempts: int = 0
    turnovers: int = 0
    offensive_rebounds: int = 0
    defensive_rebounds: int = 0

    def __post_init__(self) -> None:
        _integer(self.sequence_number, "sequence number", minimum=1)
        _instance(self.boundary, EventBoundary, "boundary")
        if self.boundary != EventBoundary(self.sequence_number, BoundarySide.AFTER):
            raise ValueError("stat delta requires AFTER(sequence_number) boundary")
        _optional_integer(self.team_id, "team ID", minimum=1)
        has_stat = False
        for item in fields(self):
            if item.name not in {"sequence_number", "boundary", "team_id"}:
                count = _integer(getattr(self, item.name), item.name.replace("_", " "), minimum=0)
                has_stat = has_stat or count > 0
        if has_stat and self.team_id is None:
            raise ValueError("stat delta with counts requires a team ID")


@dataclass(frozen=True, slots=True)
class NormalizedFeatureEvents:
    game_id: GameId
    deltas: tuple[FeatureStatDelta, ...]

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        deltas = _tuple_of(self.deltas, FeatureStatDelta, "stat deltas")
        if tuple(delta.sequence_number for delta in deltas) != tuple(range(1, len(deltas) + 1)):
            raise ValueError("stat deltas require contiguous sequence numbers")
        object.__setattr__(self, "deltas", deltas)


@dataclass(frozen=True, slots=True)
class TeamPregameFeatures:
    team_id: int
    is_home: bool
    games_used_5: int = 0
    games_used_10: int = 0
    games_used_20: int = 0
    off_rating_5: int | None = None
    off_rating_10: int | None = None
    off_rating_20: int | None = None
    def_rating_5: int | None = None
    def_rating_10: int | None = None
    def_rating_20: int | None = None
    pace_5: int | None = None
    pace_10: int | None = None
    pace_20: int | None = None
    rest_days: int | None = None
    back_to_back: bool = False

    def __post_init__(self) -> None:
        _integer(self.team_id, "team ID", minimum=1)
        _boolean(self.is_home, "is home")
        _boolean(self.back_to_back, "back to back")
        for window in (5, 10, 20):
            used = _integer(getattr(self, f"games_used_{window}"), "games used", minimum=0)
            if used > window:
                raise ValueError("games used exceeds window")
        _optional_integer(self.rest_days, "rest days", minimum=0)
        _validate_optional_metrics(
            self,
            except_names=frozenset(
                {
                    "team_id",
                    "is_home",
                    "back_to_back",
                    "rest_days",
                    "games_used_5",
                    "games_used_10",
                    "games_used_20",
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class PlayerPregameFeatures:
    game_id: GameId
    player_id: int
    team_id: int
    is_home: bool
    games_used: int = 0
    seconds_played: int = 0
    estimated_possessions: int | None = None
    minutes_avg: int | None = None
    points_per_100: int | None = None
    off_rating: int | None = None
    def_rating: int | None = None
    usage_rate: int | None = None
    assist_rate: int | None = None
    turnover_rate: int | None = None
    three_point_attempt_rate: int | None = None
    three_point_percentage: int | None = None
    free_throw_rate: int | None = None
    rebound_rate: int | None = None
    pace_context: int | None = None

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        _integer(self.player_id, "player ID", minimum=1)
        _integer(self.team_id, "team ID", minimum=1)
        _boolean(self.is_home, "is home")
        _integer(self.games_used, "games used", minimum=0)
        _integer(self.seconds_played, "seconds played", minimum=0)
        _validate_optional_metrics(
            self,
            except_names=frozenset(
                {"game_id", "player_id", "team_id", "is_home", "games_used", "seconds_played"}
            ),
        )


@dataclass(frozen=True, slots=True)
class LineupAggregateFeatures:
    team_id: int
    player_ids: tuple[int, ...]
    profiled_player_count: int = 0
    lineup_avg_off_rating: int | None = None
    lineup_avg_def_rating: int | None = None
    lineup_avg_usage_rate: int | None = None
    lineup_avg_three_point_attempt_rate: int | None = None
    lineup_avg_free_throw_rate: int | None = None
    lineup_avg_minutes: int | None = None
    lineup_avg_pace_context: int | None = None

    def __post_init__(self) -> None:
        _integer(self.team_id, "team ID", minimum=1)
        object.__setattr__(self, "player_ids", _positive_ids(self.player_ids, "player ID"))
        count = _integer(self.profiled_player_count, "profiled player count", minimum=0)
        if count > len(self.player_ids):
            raise ValueError("profiled player count exceeds lineup size")
        _validate_optional_metrics(
            self,
            except_names=frozenset({"team_id", "player_ids", "profiled_player_count"}),
        )


@dataclass(frozen=True, slots=True)
class LiveFeatures:
    current_pace: int | None = None
    pace_last_5: int | None = None
    pace_last_10: int | None = None
    pace_last_20: int | None = None
    pace_last_5_possessions_used: int = 0
    pace_last_10_possessions_used: int = 0
    pace_last_20_possessions_used: int = 0
    home_ppp: int | None = None
    away_ppp: int | None = None
    home_ppp_last_5: int | None = None
    home_ppp_last_10: int | None = None
    home_ppp_last_20: int | None = None
    away_ppp_last_5: int | None = None
    away_ppp_last_10: int | None = None
    away_ppp_last_20: int | None = None
    home_ppp_last_5_possessions_used: int = 0
    home_ppp_last_10_possessions_used: int = 0
    home_ppp_last_20_possessions_used: int = 0
    away_ppp_last_5_possessions_used: int = 0
    away_ppp_last_10_possessions_used: int = 0
    away_ppp_last_20_possessions_used: int = 0
    home_three_point_attempt_rate: int | None = None
    away_three_point_attempt_rate: int | None = None
    home_free_throw_rate: int | None = None
    away_free_throw_rate: int | None = None
    home_turnover_rate: int | None = None
    away_turnover_rate: int | None = None
    home_offensive_rebound_rate: int | None = None
    away_offensive_rebound_rate: int | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if name.endswith("possessions_used"):
                _integer(value, name.replace("_", " "), minimum=0)
            else:
                _optional_integer(value, name.replace("_", " "))


@dataclass(frozen=True, slots=True)
class FeatureCalculationInput:
    target_game: Game
    possessions: tuple[Possession, ...]
    lineup_segments: tuple[LineupSegment, ...]
    historical_team_boxes: tuple[HistoricalTeamBox, ...]
    historical_player_boxes: tuple[HistoricalPlayerBox, ...]
    history_possessions: tuple[Possession, ...]

    def __post_init__(self) -> None:
        _instance(self.target_game, Game, "target game")
        for name, expected in (
            ("possessions", Possession),
            ("lineup_segments", LineupSegment),
            ("historical_team_boxes", HistoricalTeamBox),
            ("historical_player_boxes", HistoricalPlayerBox),
            ("history_possessions", Possession),
        ):
            object.__setattr__(self, name, _tuple_of(getattr(self, name), expected, name))


@dataclass(frozen=True, slots=True)
class GameStateFeature:
    game_id: GameId
    state_number: int
    state_kind: FeatureStateKind
    possession_number: int
    boundary: EventBoundary
    home_lineup_status: LineupStatus = LineupStatus.VALID
    away_lineup_status: LineupStatus = LineupStatus.VALID
    home_player_ids: tuple[int, ...] = ()
    away_player_ids: tuple[int, ...] = ()
    home_invalid_reason: str | None = None
    home_invalid_since: EventBoundary | None = None
    away_invalid_reason: str | None = None
    away_invalid_since: EventBoundary | None = None
    period: int = 0
    seconds_remaining_game_ms: int = 0
    seconds_remaining_period_ms: int = 0
    home_score: int = 0
    away_score: int = 0
    score_diff: int = 0
    home_possessions: int = 0
    away_possessions: int = 0
    home_pregame: TeamPregameFeatures | None = None
    away_pregame: TeamPregameFeatures | None = None
    home_lineup: LineupAggregateFeatures | None = None
    away_lineup: LineupAggregateFeatures | None = None
    live: LiveFeatures | None = None
    offense_difference: int | None = None
    defense_difference: int | None = None
    pace_difference: int | None = None

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        _integer(self.state_number, "state number", minimum=0)
        _integer(self.possession_number, "possession number", minimum=0)
        kind = _enum(self.state_kind, FeatureStateKind, "state kind")
        object.__setattr__(self, "state_kind", kind)
        if (kind is FeatureStateKind.PREGAME) != (self.state_number == self.possession_number == 0):
            raise ValueError("pregame state requires zero state and possession numbers")
        if (
            kind is FeatureStateKind.AFTER_POSSESSION
            and self.state_number != self.possession_number
        ):
            raise ValueError("after-possession state and possession numbers must match")
        _instance(self.boundary, EventBoundary, "boundary")
        for side in ("home", "away"):
            status = _enum(getattr(self, f"{side}_lineup_status"), LineupStatus, "lineup status")
            object.__setattr__(self, f"{side}_lineup_status", status)
            ids = _positive_ids(getattr(self, f"{side}_player_ids"), "lineup player ID")
            object.__setattr__(self, f"{side}_player_ids", ids)
            reason = getattr(self, f"{side}_invalid_reason")
            since = getattr(self, f"{side}_invalid_since")
            if status is LineupStatus.INVALID:
                if ids:
                    raise ValueError("invalid lineup must have empty player IDs")
                if reason is None:
                    raise ValueError("invalid lineup requires an invalid reason")
                _safe_code(reason, "invalid reason")
                _instance(since, EventBoundary, "invalid since boundary")
            else:
                if len(ids) != 5:
                    raise ValueError("valid lineup must contain five players")
                if reason is not None or since is not None:
                    raise ValueError("valid lineup cannot carry invalid metadata")
        for name in (
            "period",
            "seconds_remaining_game_ms",
            "seconds_remaining_period_ms",
            "home_score",
            "away_score",
            "home_possessions",
            "away_possessions",
        ):
            _integer(getattr(self, name), name.replace("_", " "), minimum=0)
        _integer(self.score_diff, "score difference")
        if self.score_diff != self.home_score - self.away_score:
            raise ValueError("score difference must match the scores")
        for name, expected in (
            ("home_pregame", TeamPregameFeatures),
            ("away_pregame", TeamPregameFeatures),
            ("home_lineup", LineupAggregateFeatures),
            ("away_lineup", LineupAggregateFeatures),
            ("live", LiveFeatures),
        ):
            value = getattr(self, name)
            if value is not None:
                _instance(value, expected, name)
        for name in ("offense_difference", "defense_difference", "pace_difference"):
            _optional_integer(getattr(self, name), name.replace("_", " "))

    @property
    def is_valid(self) -> bool:
        return (
            self.home_lineup_status is LineupStatus.VALID
            and self.away_lineup_status is LineupStatus.VALID
        )


@dataclass(frozen=True, slots=True)
class FeatureDataset:
    game_id: GameId
    states: tuple[GameStateFeature, ...]
    players: tuple[PlayerPregameFeatures, ...]
    issues: tuple[str, ...] = ()
    status: FeatureResultStatus = field(init=False)

    def __post_init__(self) -> None:
        _instance(self.game_id, GameId, "game ID")
        states = sorted(
            _tuple_of(self.states, GameStateFeature, "states"), key=lambda row: row.state_number
        )
        players = sorted(
            _tuple_of(self.players, PlayerPregameFeatures, "players"), key=lambda row: row.player_id
        )
        if not states or tuple(row.state_number for row in states) != tuple(range(len(states))):
            raise ValueError("state numbers must be contiguous from zero")
        if len({row.player_id for row in players}) != len(players):
            raise ValueError("duplicate player profiles")
        if any(row.game_id != self.game_id for row in states) or any(
            row.game_id != self.game_id for row in players
        ):
            raise ValueError("feature rows must belong to the dataset game")
        try:
            codes = tuple(self.issues)
        except TypeError as error:
            raise ValueError("issues must be iterable") from error
        for code in codes:
            _safe_code(code, "issue")
        object.__setattr__(self, "states", tuple(states))
        object.__setattr__(self, "players", tuple(players))
        object.__setattr__(self, "issues", tuple(dict.fromkeys(codes))[:32])
        object.__setattr__(
            self,
            "status",
            FeatureResultStatus.VALID
            if all(row.is_valid for row in states)
            else FeatureResultStatus.INVALID,
        )


__all__ = [
    "FEATURE_SCALE",
    "FeatureCalculationInput",
    "FeatureDataset",
    "FeatureDependency",
    "FeatureDependencyManifest",
    "FeatureResultStatus",
    "FeatureStatDelta",
    "FeatureStateKind",
    "FeatureVersions",
    "GameStateFeature",
    "HistoricalPlayerBox",
    "HistoricalTeamBox",
    "LineupAggregateFeatures",
    "LiveFeatures",
    "NormalizedFeatureEvents",
    "PlayerPregameFeatures",
    "TeamPregameFeatures",
    "scaled_ratio",
]
