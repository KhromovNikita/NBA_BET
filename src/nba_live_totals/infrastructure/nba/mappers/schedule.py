"""ScheduleLeagueV2 raw-payload mapper."""

from datetime import date, datetime

from nba_live_totals.domain import (
    Game,
    GameId,
    GameStatus,
    SeasonId,
    SeasonSchedule,
    SeasonType,
    SourceDocument,
    Team,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers.common import (
    optional_int,
    optional_str,
    parse_utc_datetime,
    require_document_identity,
    require_int,
    require_list,
    require_mapping,
    require_str,
    required_value,
    schema_mismatch,
)

_SEASON_TYPES = {
    "002": SeasonType.REGULAR_SEASON,
    "004": SeasonType.PLAYOFFS,
}
_EXCLUDED_PREFIXES = {"001", "003", "005"}


def _parse_game_date(value: object) -> date:
    text = require_str(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%m/%d/%Y %H:%M:%S").date()
    except ValueError:
        pass
    raise schema_mismatch()


def _team(value: object) -> Team:
    provider_team = require_mapping(value)
    return Team(
        team_id=require_int(required_value(provider_team, "teamId")),
        city=require_str(required_value(provider_team, "teamCity")),
        name=require_str(required_value(provider_team, "teamName")),
        tricode=require_str(required_value(provider_team, "teamTricode")),
        slug=require_str(required_value(provider_team, "teamSlug")),
    )


def _status(provider_status: int, status_text: str) -> GameStatus:
    normalized_text = status_text.casefold()
    if "postponed" in normalized_text:
        return GameStatus.POSTPONED
    if "cancelled" in normalized_text or "canceled" in normalized_text:
        return GameStatus.CANCELLED
    return {
        1: GameStatus.SCHEDULED,
        2: GameStatus.LIVE,
        3: GameStatus.FINAL,
    }.get(provider_status, GameStatus.UNKNOWN)


def _parse_schedule(document: SourceDocument) -> SeasonSchedule:
    require_document_identity(document, endpoint="scheduleleaguev2")
    league_schedule = require_mapping(required_value(document.payload, "leagueSchedule"))
    season = SeasonId(require_str(required_value(league_schedule, "seasonYear")))
    if document.resource_key != f"schedule:{season.value}":
        raise schema_mismatch()

    teams: dict[int, Team] = {}
    games: dict[str, Game] = {}
    for game_date_value in require_list(required_value(league_schedule, "gameDates")):
        game_date_group = require_mapping(game_date_value)
        game_date = _parse_game_date(required_value(game_date_group, "gameDate"))
        for game_value in require_list(required_value(game_date_group, "games")):
            provider_game = require_mapping(game_value)
            raw_game_id = require_str(required_value(provider_game, "gameId"))
            if len(raw_game_id) != 10 or not raw_game_id.isascii() or not raw_game_id.isdigit():
                raise schema_mismatch()
            prefix = raw_game_id[:3]
            if prefix in _EXCLUDED_PREFIXES:
                continue
            if prefix not in _SEASON_TYPES:
                raise ProviderSchemaError("unknown_game_type")

            game_id = GameId(raw_game_id)
            home_team_value = require_mapping(required_value(provider_game, "homeTeam"))
            away_team_value = require_mapping(required_value(provider_game, "awayTeam"))
            home_team = _team(home_team_value)
            away_team = _team(away_team_value)
            teams[home_team.team_id] = home_team
            teams[away_team.team_id] = away_team
            games[game_id.value] = Game(
                game_id=game_id,
                season=season,
                season_type=_SEASON_TYPES[prefix],
                game_date=game_date,
                started_at=parse_utc_datetime(required_value(provider_game, "gameDateTimeUTC")),
                home_team_id=home_team.team_id,
                away_team_id=away_team.team_id,
                home_score=optional_int(required_value(home_team_value, "score")),
                away_score=optional_int(required_value(away_team_value, "score")),
                status=_status(
                    require_int(required_value(provider_game, "gameStatus")),
                    require_str(required_value(provider_game, "gameStatusText")),
                ),
                arena_name=optional_str(required_value(provider_game, "arenaName")),
                arena_city=optional_str(required_value(provider_game, "arenaCity")),
                arena_state=optional_str(required_value(provider_game, "arenaState")),
            )

    return SeasonSchedule(
        season=season,
        teams=tuple(teams[team_id] for team_id in sorted(teams)),
        games=tuple(games[game_id] for game_id in sorted(games)),
    )


def parse_schedule(document: SourceDocument) -> SeasonSchedule:
    """Map one persisted ScheduleLeagueV2 document into a season schedule."""
    try:
        return _parse_schedule(document)
    except ProviderSchemaError:
        raise
    except (KeyError, TypeError, ValueError):
        pass
    raise schema_mismatch()
