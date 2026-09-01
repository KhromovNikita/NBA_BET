"""BoxScoreTraditionalV3 raw-payload mapper."""

from nba_live_totals.domain import (
    BoxScore,
    GameId,
    JsonObject,
    Player,
    PlayerBoxScore,
    SourceDocument,
    Team,
    TeamBoxScore,
    TraditionalStats,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers.common import (
    decimal_or_none,
    optional_int,
    optional_str,
    parse_iso_duration_seconds,
    provider_flag,
    require_document_identity,
    require_int,
    require_list,
    require_mapping,
    require_str,
    required_value,
    schema_mismatch,
)


def _display_text(value: object) -> str | None:
    text = optional_str(value)
    return text if text else None


def _seconds_played(value: object) -> int | None:
    text = optional_str(value)
    if not text:
        return None
    if text.startswith("PT"):
        return parse_iso_duration_seconds(text)
    parts = text.split(":")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise schema_mismatch()
    minutes, seconds = (int(part) for part in parts)
    if seconds >= 60:
        raise schema_mismatch()
    return minutes * 60 + seconds


def _team(value: object) -> Team:
    provider_team = require_mapping(value)
    return Team(
        team_id=require_int(required_value(provider_team, "teamId")),
        city=require_str(required_value(provider_team, "teamCity")),
        name=require_str(required_value(provider_team, "teamName")),
        tricode=require_str(required_value(provider_team, "teamTricode")),
        slug=require_str(required_value(provider_team, "teamSlug")),
    )


def _statistics(value: object) -> TraditionalStats:
    statistics = require_mapping(value)
    return TraditionalStats(
        seconds_played=_seconds_played(required_value(statistics, "minutes")),
        field_goals_made=optional_int(required_value(statistics, "fieldGoalsMade")),
        field_goals_attempted=optional_int(required_value(statistics, "fieldGoalsAttempted")),
        field_goals_percentage=decimal_or_none(required_value(statistics, "fieldGoalsPercentage")),
        three_pointers_made=optional_int(required_value(statistics, "threePointersMade")),
        three_pointers_attempted=optional_int(required_value(statistics, "threePointersAttempted")),
        three_pointers_percentage=decimal_or_none(
            required_value(statistics, "threePointersPercentage")
        ),
        free_throws_made=optional_int(required_value(statistics, "freeThrowsMade")),
        free_throws_attempted=optional_int(required_value(statistics, "freeThrowsAttempted")),
        free_throws_percentage=decimal_or_none(required_value(statistics, "freeThrowsPercentage")),
        rebounds_offensive=optional_int(required_value(statistics, "reboundsOffensive")),
        rebounds_defensive=optional_int(required_value(statistics, "reboundsDefensive")),
        rebounds_total=optional_int(required_value(statistics, "reboundsTotal")),
        assists=optional_int(required_value(statistics, "assists")),
        steals=optional_int(required_value(statistics, "steals")),
        blocks=optional_int(required_value(statistics, "blocks")),
        turnovers=optional_int(required_value(statistics, "turnovers")),
        fouls_personal=optional_int(required_value(statistics, "foulsPersonal")),
        points=optional_int(required_value(statistics, "points")),
        plus_minus=decimal_or_none(required_value(statistics, "plusMinusPoints")),
    )


def _player_rows(
    provider_team: JsonObject, *, game_id: GameId, team_id: int
) -> list[PlayerBoxScore]:
    rows: list[PlayerBoxScore] = []
    for value in require_list(required_value(provider_team, "players")):
        provider_player = require_mapping(value)
        position = _display_text(required_value(provider_player, "position"))
        provider_statistics = require_mapping(required_value(provider_player, "statistics"))
        starter_value = provider_player.get("starter")
        played_value = provider_player.get("played")
        starter = position is not None if starter_value is None else provider_flag(starter_value)
        played = bool(provider_statistics) if played_value is None else provider_flag(played_value)
        comment = _display_text(required_value(provider_player, "comment"))
        if (not played or comment is not None) and not provider_statistics:
            statistics = TraditionalStats.empty()
        else:
            statistics = _statistics(provider_statistics)
        first_name = require_str(required_value(provider_player, "firstName"))
        family_name = require_str(required_value(provider_player, "familyName"))
        rows.append(
            PlayerBoxScore(
                game_id=game_id,
                player=Player(
                    player_id=require_int(required_value(provider_player, "personId")),
                    first_name=first_name,
                    family_name=family_name,
                    full_name=f"{first_name} {family_name}".strip(),
                ),
                team_id=team_id,
                position=position,
                jersey_number=require_str(required_value(provider_player, "jerseyNum")),
                starter=starter,
                played=played,
                comment=comment,
                statistics=statistics,
            )
        )
    return rows


def _parse_box_score(document: SourceDocument) -> BoxScore:
    require_document_identity(document, endpoint="boxscoretraditionalv3")
    provider_box_score = require_mapping(required_value(document.payload, "boxScoreTraditional"))
    game_id = GameId(require_str(required_value(provider_box_score, "gameId")))
    if document.resource_key != f"box_score:{game_id.value}":
        raise schema_mismatch()

    home_team_id = require_int(required_value(provider_box_score, "homeTeamId"))
    away_team_id = require_int(required_value(provider_box_score, "awayTeamId"))
    team_rows: list[TeamBoxScore] = []
    player_rows: list[PlayerBoxScore] = []
    scores: list[int] = []
    for key, expected_team_id in (
        ("homeTeam", home_team_id),
        ("awayTeam", away_team_id),
    ):
        provider_team = require_mapping(required_value(provider_box_score, key))
        team = _team(provider_team)
        if team.team_id != expected_team_id:
            raise schema_mismatch()
        statistics = _statistics(required_value(provider_team, "statistics"))
        if statistics.points is None:
            raise schema_mismatch()
        scores.append(statistics.points)
        team_rows.append(TeamBoxScore(game_id=game_id, team=team, statistics=statistics))
        player_rows.extend(_player_rows(provider_team, game_id=game_id, team_id=team.team_id))

    return BoxScore(
        game_id=game_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_score=scores[0],
        away_score=scores[1],
        team_rows=tuple(team_rows),
        player_rows=tuple(player_rows),
    )


def parse_box_score(document: SourceDocument) -> BoxScore:
    """Map one persisted BoxScoreTraditionalV3 document into a box score."""
    try:
        return _parse_box_score(document)
    except ProviderSchemaError:
        raise
    except (KeyError, TypeError, ValueError):
        pass
    raise schema_mismatch()
