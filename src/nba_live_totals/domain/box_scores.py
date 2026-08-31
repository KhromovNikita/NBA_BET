"""Immutable provider-neutral NBA traditional box-score models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nba_live_totals.domain.games import Player, Team
from nba_live_totals.domain.identifiers import GameId


def _require_positive_integer(value: int, *, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)


def _require_nonnegative_integer(value: int | None, *, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        msg = f"{field_name} must be a nonnegative integer"
        raise ValueError(msg)


def _require_percentage(value: Decimal | None, *, field_name: str) -> None:
    if value is not None and (
        not isinstance(value, Decimal) or not Decimal("0") <= value <= Decimal("1")
    ):
        msg = f"{field_name} must be a Decimal percentage between 0 and 1"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TraditionalStats:
    """Nullable traditional statistics, preserving missing DNP values."""

    seconds_played: int | None = None
    field_goals_made: int | None = None
    field_goals_attempted: int | None = None
    field_goals_percentage: Decimal | None = None
    three_pointers_made: int | None = None
    three_pointers_attempted: int | None = None
    three_pointers_percentage: Decimal | None = None
    free_throws_made: int | None = None
    free_throws_attempted: int | None = None
    free_throws_percentage: Decimal | None = None
    rebounds_offensive: int | None = None
    rebounds_defensive: int | None = None
    rebounds_total: int | None = None
    assists: int | None = None
    steals: int | None = None
    blocks: int | None = None
    turnovers: int | None = None
    fouls_personal: int | None = None
    points: int | None = None
    plus_minus: Decimal | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "seconds_played",
            "field_goals_made",
            "field_goals_attempted",
            "three_pointers_made",
            "three_pointers_attempted",
            "free_throws_made",
            "free_throws_attempted",
            "rebounds_offensive",
            "rebounds_defensive",
            "rebounds_total",
            "assists",
            "steals",
            "blocks",
            "turnovers",
            "fouls_personal",
            "points",
        ):
            _require_nonnegative_integer(getattr(self, field_name), field_name=field_name)
        for field_name in (
            "field_goals_percentage",
            "three_pointers_percentage",
            "free_throws_percentage",
        ):
            _require_percentage(getattr(self, field_name), field_name=field_name)
        if self.plus_minus is not None and not isinstance(self.plus_minus, Decimal):
            msg = "plus_minus must be a Decimal"
            raise ValueError(msg)
        for made, attempted, label in (
            (self.field_goals_made, self.field_goals_attempted, "field goals"),
            (self.three_pointers_made, self.three_pointers_attempted, "three pointers"),
            (self.free_throws_made, self.free_throws_attempted, "free throws"),
        ):
            if made is not None and attempted is not None and made > attempted:
                msg = f"{label} made cannot exceed attempts"
                raise ValueError(msg)

    @classmethod
    def empty(cls) -> TraditionalStats:
        """Return statistics with every value absent, as for a DNP player."""
        return cls()


@dataclass(frozen=True, slots=True)
class TeamBoxScore:
    """Traditional statistics for one team in one game."""

    game_id: GameId
    team: Team
    statistics: TraditionalStats


@dataclass(frozen=True, slots=True)
class PlayerBoxScore:
    """Traditional statistics and participation details for one player."""

    game_id: GameId
    player: Player
    team_id: int
    position: str | None
    jersey_number: str
    starter: bool
    played: bool
    comment: str | None
    statistics: TraditionalStats

    def __post_init__(self) -> None:
        _require_positive_integer(self.team_id, field_name="team ID")
        if type(self.starter) is not bool or type(self.played) is not bool:
            msg = "starter and played must be booleans"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BoxScore:
    """Validated final box-score snapshot for a completed NBA game."""

    game_id: GameId
    home_team_id: int
    away_team_id: int
    home_score: int
    away_score: int
    team_rows: tuple[TeamBoxScore, ...]
    player_rows: tuple[PlayerBoxScore, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_rows", tuple(self.team_rows))
        object.__setattr__(self, "player_rows", tuple(self.player_rows))
        _require_positive_integer(self.home_team_id, field_name="home team ID")
        _require_positive_integer(self.away_team_id, field_name="away team ID")
        if self.home_team_id == self.away_team_id:
            msg = "home and away teams must be distinct"
            raise ValueError(msg)
        if self.home_score is None or self.away_score is None:
            msg = "box score requires both final scores"
            raise ValueError(msg)
        _require_nonnegative_integer(self.home_score, field_name="home score")
        _require_nonnegative_integer(self.away_score, field_name="away score")
        if len(self.team_rows) != 2:
            msg = "box score must contain exactly two team rows"
            raise ValueError(msg)

        expected_team_ids = {self.home_team_id, self.away_team_id}
        team_rows_by_id: dict[int, TeamBoxScore] = {}
        for row in self.team_rows:
            if row.game_id != self.game_id:
                msg = "team row game ID must match box score game ID"
                raise ValueError(msg)
            team_rows_by_id[row.team.team_id] = row
        if set(team_rows_by_id) != expected_team_ids:
            msg = "team rows must match the home and away teams"
            raise ValueError(msg)

        expected_scores = {
            self.home_team_id: self.home_score,
            self.away_team_id: self.away_score,
        }
        for team_id, row in team_rows_by_id.items():
            if row.statistics.points != expected_scores[team_id]:
                msg = "team points must match final score"
                raise ValueError(msg)

        player_ids: set[int] = set()
        for player_row in self.player_rows:
            if player_row.game_id != self.game_id:
                msg = "player row game ID must match box score game ID"
                raise ValueError(msg)
            if player_row.player.player_id in player_ids:
                msg = "box score contains duplicate player rows"
                raise ValueError(msg)
            player_ids.add(player_row.player.player_id)
            if player_row.team_id not in expected_team_ids:
                msg = "player row belongs to an unknown team"
                raise ValueError(msg)
