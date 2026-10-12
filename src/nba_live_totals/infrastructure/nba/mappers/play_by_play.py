"""PlayByPlayV3 raw-payload mapper."""

from nba_live_totals.domain import (
    GameId,
    PlayByPlayEvent,
    SourceDocument,
    validate_play_by_play,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers.common import (
    optional_int,
    optional_str,
    parse_clock_milliseconds,
    provider_flag,
    require_document_identity,
    require_int,
    require_list,
    require_mapping,
    require_str,
    required_value,
    schema_mismatch,
)


def _nullable_identity(value: object) -> int | None:
    identity = optional_int(value)
    return None if identity == 0 else identity


def _score(value: object) -> int | None:
    if value is None or value == "":
        return None
    score = require_str(value)
    if not score.isdigit():
        raise schema_mismatch()
    return int(score)


def _points_total(value: object) -> int:
    return 0 if value is None else require_int(value)


def _event(value: object, *, game_id: GameId, sequence_number: int) -> PlayByPlayEvent:
    action = require_mapping(value)
    clock = require_str(required_value(action, "clock"))
    return PlayByPlayEvent(
        game_id=game_id,
        sequence_number=sequence_number,
        action_number=require_int(required_value(action, "actionNumber")),
        action_id=optional_int(required_value(action, "actionId")),
        period=require_int(required_value(action, "period")),
        clock=clock,
        seconds_remaining_period_ms=parse_clock_milliseconds(clock),
        team_id=_nullable_identity(required_value(action, "teamId")),
        person_id=_nullable_identity(required_value(action, "personId")),
        action_type=require_str(required_value(action, "actionType")),
        sub_type=require_str(required_value(action, "subType")),
        description=require_str(required_value(action, "description")),
        home_score=_score(required_value(action, "scoreHome")),
        away_score=_score(required_value(action, "scoreAway")),
        shot_result=optional_str(required_value(action, "shotResult")),
        shot_distance=optional_int(required_value(action, "shotDistance")),
        legacy_x=optional_int(required_value(action, "xLegacy")),
        legacy_y=optional_int(required_value(action, "yLegacy")),
        location=optional_str(required_value(action, "location")),
        is_field_goal=provider_flag(required_value(action, "isFieldGoal")),
        points_total=_points_total(required_value(action, "pointsTotal")),
        video_available=provider_flag(required_value(action, "videoAvailable")),
    )


def _parse_play_by_play(document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
    require_document_identity(document, endpoint="playbyplayv3")
    game = require_mapping(required_value(document.payload, "game"))
    game_id = GameId(require_str(required_value(game, "gameId")))
    if document.resource_key != f"play_by_play:{game_id.value}":
        raise schema_mismatch()
    actions = require_list(required_value(game, "actions"))
    return validate_play_by_play(
        (
            _event(action, game_id=game_id, sequence_number=sequence_number)
            for sequence_number, action in enumerate(actions, start=1)
        ),
        completed=True,
    )


def parse_play_by_play(document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
    """Map one persisted PlayByPlayV3 document into ordered events."""
    try:
        return _parse_play_by_play(document)
    except ProviderSchemaError:
        raise
    except KeyError, TypeError, ValueError:
        pass
    raise schema_mismatch()
