"""Immutable domain models for NBA schedules and games."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from nba_live_totals.domain.identifiers import GameId, SeasonId


class SeasonType(StrEnum):
    """Included NBA tournament types."""

    REGULAR_SEASON = "regular_season"
    PLAYOFFS = "playoffs"


class GameStatus(StrEnum):
    """Normalized game lifecycle statuses."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _require_positive_integer(value: int, *, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)


def _normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "datetime must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Team:
    """NBA team identity and provider-updatable display metadata."""

    team_id: int
    city: str
    name: str
    tricode: str
    slug: str

    def __post_init__(self) -> None:
        _require_positive_integer(self.team_id, field_name="team ID")


@dataclass(frozen=True, slots=True)
class Player:
    """NBA player identity and provider-updatable display metadata."""

    player_id: int
    first_name: str
    family_name: str
    full_name: str

    def __post_init__(self) -> None:
        _require_positive_integer(self.player_id, field_name="player ID")


@dataclass(frozen=True, slots=True)
class Game:
    """A scheduled or completed NBA game."""

    game_id: GameId
    season: SeasonId
    season_type: SeasonType
    game_date: date
    started_at: datetime
    home_team_id: int
    away_team_id: int
    home_score: int | None
    away_score: int | None
    status: GameStatus
    arena_name: str | None
    arena_city: str | None
    arena_state: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", _normalize_utc_datetime(self.started_at))
        _require_positive_integer(self.home_team_id, field_name="home team ID")
        _require_positive_integer(self.away_team_id, field_name="away team ID")
        if self.home_team_id == self.away_team_id:
            msg = "home and away teams must be distinct"
            raise ValueError(msg)
        for score in (self.home_score, self.away_score):
            if score is not None and (type(score) is not int or score < 0):
                msg = "scores must be nonnegative integers"
                raise ValueError(msg)
        if self.status is GameStatus.FINAL and (self.home_score is None or self.away_score is None):
            msg = "final games require both scores"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SeasonSchedule:
    """Teams and games discovered for a single NBA season."""

    season: SeasonId
    teams: tuple[Team, ...]
    games: tuple[Game, ...]

    def __post_init__(self) -> None:
        if len({team.team_id for team in self.teams}) != len(self.teams):
            msg = "schedule contains duplicate team IDs"
            raise ValueError(msg)
        if len({game.game_id for game in self.games}) != len(self.games):
            msg = "schedule contains duplicate game IDs"
            raise ValueError(msg)
