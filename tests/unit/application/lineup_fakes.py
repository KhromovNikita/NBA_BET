"""Focused fixtures and protocol fakes for lineup application contract tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from nba_live_totals.application.lineups import GameLineupInputSnapshot
from nba_live_totals.domain import (
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.lineup_hashing import canonical_player_boxes_checksum
from nba_live_totals.domain.lineups import LineupPlayerBox, LineupVersions

VERSIONS = LineupVersions(
    normalizer_version="nba-pbp-v3-lineup-v1",
    reconstructor_version="causal-lineup-v1",
    rules_version="nba-5x5-v1",
    output_schema_version="lineup-segments-v1",
)
DEFAULT_GAME_ID = GameId("0022500123")


def uuid4_value(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def make_game(game_id: GameId = DEFAULT_GAME_ID) -> GameContext:
    return GameContext(
        game_id=game_id,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=10,
        away_team_id=20,
        home_score=2,
        away_score=0,
    )


def make_document(
    game_id: GameId,
    resource: str,
    *,
    payload_extra: JsonObject | None = None,
) -> SourceDocument:
    payload: JsonObject = {"game": {"gameId": game_id.value}, "resource": resource}
    if payload_extra is not None:
        payload.update(payload_extra)
    endpoint = "boxscoretraditionalv3" if resource == "box_score" else "playbyplayv3"
    return SourceDocument(
        source="nba_stats",
        endpoint=endpoint,
        resource_key=f"{resource}:{game_id.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": game_id.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_player_boxes(game_id: GameId) -> tuple[LineupPlayerBox, ...]:
    return (
        LineupPlayerBox(game_id, 10, 101, "G", True, True, None),
        LineupPlayerBox(game_id, 20, 201, "G", True, True, None),
    )


def make_events(game_id: GameId) -> tuple[PlayByPlayEvent, ...]:
    return (
        PlayByPlayEvent(
            game_id=game_id,
            sequence_number=1,
            action_number=1,
            action_id=None,
            period=1,
            clock="PT12M00.00S",
            seconds_remaining_period_ms=720_000,
            team_id=None,
            person_id=None,
            action_type="period",
            sub_type="start",
            description="provider text must not escape",
            home_score=0,
            away_score=0,
            shot_result=None,
            shot_distance=None,
            legacy_x=None,
            legacy_y=None,
            location=None,
            is_field_goal=False,
            points_total=0,
            video_available=False,
        ),
    )


def make_snapshot() -> GameLineupInputSnapshot:
    game = make_game()
    boxes = make_player_boxes(game.game_id)
    events = make_events(game.game_id)
    box_document = make_document(game.game_id, "box_score")
    pbp_document = make_document(game.game_id, "play_by_play")
    return GameLineupInputSnapshot(
        game=game,
        applied_box_score_document_id=uuid4_value(1),
        applied_box_score_sha256=box_document.checksum_sha256,
        applied_pbp_document_id=uuid4_value(2),
        applied_pbp_sha256=pbp_document.checksum_sha256,
        canonical_player_boxes=boxes,
        canonical_events=events,
        canonical_player_box_sha256=canonical_player_boxes_checksum(boxes),
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        box_score_source_document=box_document,
        pbp_source_document=pbp_document,
    )
