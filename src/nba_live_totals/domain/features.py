"""Immutable provider-neutral inputs and outputs for feature calculation."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any, cast
from uuid import UUID

from nba_live_totals.domain.games import Game, GameStatus
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
    started_at: datetime
    game_date: date
    is_home: bool
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
        object.__setattr__(self, "started_at", _utc(self.started_at, "start time"))
        if type(self.game_date) is not date:
            raise ValueError("game date must be an exact date")
        _boolean(self.is_home, "is home")
        for item in fields(self):
            if item.name not in {"game_id", "team_id", "started_at", "game_date", "is_home"}:
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
class _TeamGameCounts:
    team_points: int | None
    opponent_points: int | None
    team_possessions: int
    opponent_possessions: int
    total_possessions: int
    duration_seconds: int


def _window_rating(points: Iterable[int | None], possessions: Iterable[int]) -> int | None:
    values = tuple(points)
    if any(value is None for value in values):
        return None
    return scaled_ratio(100 * sum(cast("tuple[int, ...]", values)), sum(possessions))


def build_team_pregame_features(
    target: Game,
    historical_team_boxes: Iterable[HistoricalTeamBox],
    history_possessions: Iterable[Possession],
    *,
    team_id: int,
) -> TeamPregameFeatures:
    """Calculate causal team windows from completed, structurally sound history."""
    _instance(target, Game, "target")
    if target.status is not GameStatus.FINAL:
        raise ValueError("target game must be final")
    _integer(team_id, "team ID", minimum=1)
    if team_id not in (target.home_team_id, target.away_team_id):
        raise ValueError("team ID must belong to target game")

    boxes = tuple(historical_team_boxes)
    eligible = {
        box.game_id
        for box in boxes
        if box.team_id == team_id
        and box.game_id != target.game_id
        and box.started_at < target.started_at
    }
    boxes_by_game: dict[GameId, list[HistoricalTeamBox]] = {game_id: [] for game_id in eligible}
    for box in boxes:
        if box.game_id in eligible:
            boxes_by_game[box.game_id].append(box)
    possessions_by_game: dict[GameId, list[Possession]] = {game_id: [] for game_id in eligible}
    for possession in history_possessions:
        if possession.game_id in eligible:
            possessions_by_game[possession.game_id].append(possession)

    observations: list[tuple[datetime, str, date, _TeamGameCounts]] = []
    for game_id in eligible:
        pair = boxes_by_game[game_id]
        rows = possessions_by_game[game_id]
        if (
            len(pair) != 2
            or len({box.team_id for box in pair}) != 2
            or pair[0].started_at != pair[1].started_at
            or pair[0].game_date != pair[1].game_date
            or pair[0].is_home == pair[1].is_home
            or not rows
            or tuple(row.possession_number for row in rows) != tuple(range(1, len(rows) + 1))
        ):
            raise ValueError("historical game structure is invalid")
        team_box = next(box for box in pair if box.team_id == team_id)
        opponent_box = next(box for box in pair if box.team_id != team_id)
        if any(
            {row.offense_team_id, row.defense_team_id} != {team_id, opponent_box.team_id}
            for row in rows
        ):
            raise ValueError("historical game structure is invalid")
        final = rows[-1]
        counts = _TeamGameCounts(
            team_points=team_box.points,
            opponent_points=opponent_box.points,
            team_possessions=(
                final.home_possessions if team_box.is_home else final.away_possessions
            ),
            opponent_possessions=(
                final.away_possessions if team_box.is_home else final.home_possessions
            ),
            total_possessions=final.home_possessions + final.away_possessions,
            duration_seconds=2_880 + 300 * max(final.period - 4, 0),
        )
        observations.append((team_box.started_at, game_id.value, team_box.game_date, counts))

    observations.sort(key=lambda item: (item[0], item[1]))
    result: dict[str, int | bool | None] = {
        "team_id": team_id,
        "is_home": team_id == target.home_team_id,
    }
    for size in (5, 10, 20):
        window = [item[3] for item in observations[-size:]]
        result[f"games_used_{size}"] = len(window)
        result[f"off_rating_{size}"] = (
            _window_rating(
                (item.team_points for item in window), (item.team_possessions for item in window)
            )
            if window
            else None
        )
        result[f"def_rating_{size}"] = (
            _window_rating(
                (item.opponent_points for item in window),
                (item.opponent_possessions for item in window),
            )
            if window
            else None
        )
        result[f"pace_{size}"] = (
            scaled_ratio(
                2_880 * sum(item.total_possessions for item in window),
                2 * sum(item.duration_seconds for item in window),
            )
            if window
            else None
        )
    if observations:
        latest_date = observations[-1][2]
        delta = (target.game_date - latest_date).days
        result["rest_days"] = max(0, delta - 1)
        result["back_to_back"] = delta == 1
    return TeamPregameFeatures(**result)  # type: ignore[arg-type]


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


@dataclass(slots=True)
class _LiveTeamTotals:
    possessions: int = 0
    points: int = 0
    field_goal_attempts: int = 0
    three_point_attempts: int = 0
    free_throw_attempts: int = 0
    turnovers: int = 0
    offensive_rebounds: int = 0
    defensive_rebounds: int = 0


def _period_length_ms(period: int) -> int:
    return 720_000 if period <= 4 else 300_000


def _period_start_elapsed_ms(period: int) -> int:
    return (period - 1) * 720_000 if period <= 4 else 2_880_000 + (period - 5) * 300_000


def _possession_start_elapsed_ms(possession: Possession) -> int:
    length = _period_length_ms(possession.period)
    return (
        _period_start_elapsed_ms(possession.period)
        + length
        - possession.start_seconds_remaining_period_ms
    )


def _possession_end_elapsed_ms(possession: Possession) -> int:
    scheduled = 2_880_000 + 300_000 * max(possession.period - 4, 0)
    return scheduled - possession.seconds_remaining_game_ms


def _validate_possession_clock(possession: Possession) -> None:
    period_length = _period_length_ms(possession.period)
    if possession.start_seconds_remaining_period_ms > period_length:
        raise ValueError("possession start clock exceeds period duration")
    if possession.end_seconds_remaining_period_ms > period_length:
        raise ValueError("possession end clock exceeds period duration")
    if possession.start_seconds_remaining_period_ms < possession.end_seconds_remaining_period_ms:
        raise ValueError("possession start clock must not be below end clock")
    expected_game_clock = (
        possession.end_seconds_remaining_period_ms + (4 - possession.period) * 720_000
        if possession.period <= 4
        else possession.end_seconds_remaining_period_ms
    )
    if possession.seconds_remaining_game_ms != expected_game_clock:
        raise ValueError("possession game clock is inconsistent with period clock")


def _pace(possessions: tuple[Possession, ...]) -> int | None:
    if not possessions:
        return None
    duration = _possession_end_elapsed_ms(possessions[-1]) - _possession_start_elapsed_ms(
        possessions[0]
    )
    if duration <= 0:
        return None
    return scaled_ratio(2_880_000 * len(possessions), 2 * duration)


def _rate(numerator: int, denominator: int) -> int | None:
    return scaled_ratio(numerator, denominator) if denominator > 0 else None


def build_live_features(
    game: Game,
    possessions: Iterable[Possession],
    events: NormalizedFeatureEvents,
    boundaries: Iterable[EventBoundary],
) -> tuple[LiveFeatures, ...]:
    """Calculate causal live prefixes in one ordered pass over events and possessions."""
    _instance(game, Game, "game")
    if game.status is not GameStatus.FINAL:
        raise ValueError("game must be final")
    _instance(events, NormalizedFeatureEvents, "events")
    if events.game_id != game.game_id:
        raise ValueError("events must belong to game")
    ordered_possessions = tuple(possessions)
    for possession in ordered_possessions:
        _instance(possession, Possession, "possession")
    if tuple(item.possession_number for item in ordered_possessions) != tuple(
        range(1, len(ordered_possessions) + 1)
    ):
        raise ValueError("possessions must be contiguous and ordered")
    if any(item.game_id != game.game_id for item in ordered_possessions):
        raise ValueError("possessions must belong to game")
    teams = {game.home_team_id, game.away_team_id}
    if any({item.offense_team_id, item.defense_team_id} != teams for item in ordered_possessions):
        raise ValueError("possession teams must match game")
    if any(
        left.end_boundary >= right.end_boundary for left, right in pairwise(ordered_possessions)
    ):
        raise ValueError("possession boundaries must be strictly increasing")
    for possession in ordered_possessions:
        _validate_possession_clock(possession)
    if any(delta.team_id is not None and delta.team_id not in teams for delta in events.deltas):
        raise ValueError("event team must match game")

    ordered_boundaries = tuple(boundaries)
    for boundary in ordered_boundaries:
        _instance(boundary, EventBoundary, "boundary")
    if any(left >= right for left, right in pairwise(ordered_boundaries)):
        raise ValueError("boundaries must be strictly increasing")
    maximum = len(events.deltas)
    if any(boundary.sequence_number > maximum for boundary in ordered_boundaries):
        raise ValueError("boundary exceeds event range")
    if any(item.end_boundary.sequence_number > maximum for item in ordered_possessions):
        raise ValueError("possession boundary exceeds event range")

    totals = {
        game.home_team_id: _LiveTeamTotals(),
        game.away_team_id: _LiveTeamTotals(),
    }
    recent_combined: deque[Possession] = deque(maxlen=20)
    recent_points: dict[int, deque[int]] = {
        game.home_team_id: deque(maxlen=20),
        game.away_team_id: deque(maxlen=20),
    }
    event_index = 0
    possession_index = 0
    results: list[LiveFeatures] = []

    for boundary in ordered_boundaries:
        while event_index < len(events.deltas) and events.deltas[event_index].boundary <= boundary:
            delta = events.deltas[event_index]
            if delta.team_id is not None:
                team = totals[delta.team_id]
                team.field_goal_attempts += delta.field_goal_attempts
                team.three_point_attempts += delta.three_point_attempts
                team.free_throw_attempts += delta.free_throw_attempts
                team.turnovers += delta.turnovers
                team.offensive_rebounds += delta.offensive_rebounds
                team.defensive_rebounds += delta.defensive_rebounds
            event_index += 1
        while (
            possession_index < len(ordered_possessions)
            and ordered_possessions[possession_index].end_boundary <= boundary
        ):
            possession = ordered_possessions[possession_index]
            team = totals[possession.offense_team_id]
            team.possessions += 1
            team.points += possession.offense_points
            recent_combined.append(possession)
            recent_points[possession.offense_team_id].append(possession.offense_points)
            possession_index += 1

        completed = tuple(recent_combined)
        home = totals[game.home_team_id]
        away = totals[game.away_team_id]
        values: dict[str, int | None] = {}
        if possession_index:
            elapsed = _possession_end_elapsed_ms(ordered_possessions[possession_index - 1])
            values["current_pace"] = (
                scaled_ratio(2_880_000 * possession_index, 2 * elapsed) if elapsed > 0 else None
            )
        for size in (5, 10, 20):
            pace_window = completed[-size:]
            values[f"pace_last_{size}"] = _pace(pace_window)
            values[f"pace_last_{size}_possessions_used"] = len(pace_window)
        for label, team_id, total in (
            ("home", game.home_team_id, home),
            ("away", game.away_team_id, away),
        ):
            values[f"{label}_ppp"] = _rate(total.points, total.possessions)
            points = tuple(recent_points[team_id])
            for size in (5, 10, 20):
                window = points[-size:]
                values[f"{label}_ppp_last_{size}"] = _rate(sum(window), len(window))
                values[f"{label}_ppp_last_{size}_possessions_used"] = len(window)
            values[f"{label}_three_point_attempt_rate"] = _rate(
                total.three_point_attempts, total.field_goal_attempts
            )
            values[f"{label}_free_throw_rate"] = _rate(
                total.free_throw_attempts, total.field_goal_attempts
            )
            values[f"{label}_turnover_rate"] = _rate(total.turnovers, total.possessions)
        values["home_offensive_rebound_rate"] = _rate(
            home.offensive_rebounds, home.offensive_rebounds + away.defensive_rebounds
        )
        values["away_offensive_rebound_rate"] = _rate(
            away.offensive_rebounds, away.offensive_rebounds + home.defensive_rebounds
        )
        results.append(LiveFeatures(**values))  # type: ignore[arg-type]
    return tuple(results)


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


def join_possessions_to_lineups(
    game: Game,
    possessions: tuple[Possession, ...],
    segments: tuple[LineupSegment, ...],
) -> tuple[GameStateFeature, ...]:
    """Join ordered possession ends to their inclusive lineup segments in linear time."""
    _instance(game, Game, "game")
    if game.status is not GameStatus.FINAL:
        raise ValueError("feature state join requires a final game")
    ordered_possessions = _tuple_of(possessions, Possession, "possessions")
    ordered_segments = _tuple_of(segments, LineupSegment, "lineup segments")
    teams = {game.home_team_id, game.away_team_id}
    previous_boundary = EventBoundary(1, BoundarySide.BEFORE)
    for number, row in enumerate(ordered_possessions, start=1):
        if row.possession_number != number:
            raise ValueError("possession numbers must be one-based and contiguous")
        if row.game_id != game.game_id:
            raise ValueError("possession game ID must match the game")
        if {row.offense_team_id, row.defense_team_id} != teams:
            raise ValueError("possession teams must match the game teams")
        if row.end_boundary <= previous_boundary:
            raise ValueError("possession end boundary order must be increasing")
        previous_boundary = row.end_boundary
    for number, segment in enumerate(ordered_segments, start=1):
        if segment.segment_number != number:
            raise ValueError("lineup segment order must be one-based and increasing")
        if number > 1 and segment.start_boundary <= ordered_segments[number - 2].end_boundary:
            raise ValueError("lineup boundary must match exactly one segment")

    states: list[GameStateFeature] = []
    segment_index = 0
    for state_number in range(len(ordered_possessions) + 1):
        possession = ordered_possessions[state_number - 1] if state_number else None
        boundary = (
            possession.end_boundary
            if possession is not None
            else EventBoundary(1, BoundarySide.BEFORE)
        )
        while (
            segment_index < len(ordered_segments)
            and ordered_segments[segment_index].end_boundary < boundary
        ):
            segment_index += 1
        if (
            segment_index == len(ordered_segments)
            or ordered_segments[segment_index].start_boundary > boundary
        ):
            raise ValueError("lineup boundary must match exactly one segment")
        segment = ordered_segments[segment_index]
        home = segment.home_state
        away = segment.away_state
        home_ids = tuple(sorted(home.player_ids))
        away_ids = tuple(sorted(away.player_ids))
        states.append(
            GameStateFeature(
                game_id=game.game_id,
                state_number=state_number,
                state_kind=(
                    FeatureStateKind.PREGAME
                    if possession is None
                    else FeatureStateKind.AFTER_POSSESSION
                ),
                possession_number=state_number,
                boundary=boundary,
                home_lineup_status=home.status,
                away_lineup_status=away.status,
                home_player_ids=home_ids,
                away_player_ids=away_ids,
                home_invalid_reason=home.invalid_reason,
                home_invalid_since=home.invalid_since,
                away_invalid_reason=away.invalid_reason,
                away_invalid_since=away.invalid_since,
                period=0 if possession is None else possession.period,
                seconds_remaining_game_ms=(
                    2_880_000 if possession is None else possession.seconds_remaining_game_ms
                ),
                seconds_remaining_period_ms=(
                    720_000 if possession is None else possession.end_seconds_remaining_period_ms
                ),
                home_score=0 if possession is None else possession.home_score,
                away_score=0 if possession is None else possession.away_score,
                score_diff=0 if possession is None else possession.score_diff,
                home_possessions=0 if possession is None else possession.home_possessions,
                away_possessions=0 if possession is None else possession.away_possessions,
                home_lineup=LineupAggregateFeatures(game.home_team_id, home_ids),
                away_lineup=LineupAggregateFeatures(game.away_team_id, away_ids),
            )
        )
    return tuple(states)


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
        causal_codes = (
            reason
            for state in states
            for status, reason in (
                (state.home_lineup_status, state.home_invalid_reason),
                (state.away_lineup_status, state.away_invalid_reason),
            )
            if status is LineupStatus.INVALID and reason is not None
        )
        object.__setattr__(self, "states", tuple(states))
        object.__setattr__(self, "players", tuple(players))
        object.__setattr__(self, "issues", tuple(dict.fromkeys((*causal_codes, *codes)))[:32])
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
    "build_live_features",
    "build_team_pregame_features",
    "join_possessions_to_lineups",
    "scaled_ratio",
]
