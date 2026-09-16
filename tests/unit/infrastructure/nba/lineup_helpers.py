"""Factories for immutable Phase 4 lineup input snapshots."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

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
from nba_live_totals.domain.lineups import LineupPlayerBox
from nba_live_totals.infrastructure.nba.mappers import parse_box_score, parse_play_by_play

ROOT = Path(__file__).parents[3] / "fixtures" / "nba"
GAME_ID = GameId("0022500123")


def fixture(name: str) -> JsonObject:
    value = json.loads((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def document(
    payload: JsonObject,
    *,
    endpoint: str,
    resource_key: str,
    source: str = "nba.com",
) -> SourceDocument:
    return SourceDocument(
        source=source,
        endpoint=endpoint,
        resource_key=resource_key,
        requested_at=datetime(2026, 1, 2, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_snapshot(
    *,
    box_payload: JsonObject | None = None,
    pbp_payload: JsonObject | None = None,
    canonical_events: tuple[PlayByPlayEvent, ...] | None = None,
    canonical_player_boxes: tuple[LineupPlayerBox, ...] | None = None,
) -> GameLineupInputSnapshot:
    box_payload = deepcopy(box_payload or fixture("lineups/box_score_v1.json"))
    pbp_payload = deepcopy(pbp_payload or fixture("lineups/play_by_play_v1.json"))
    box_document = document(
        box_payload,
        endpoint="boxscoretraditionalv3",
        resource_key=f"box_score:{GAME_ID.value}",
    )
    pbp_document = document(
        pbp_payload,
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{GAME_ID.value}",
    )
    parsed_box = parse_box_score(box_document)
    parsed_events = parse_play_by_play(pbp_document)
    parsed_player_boxes = tuple(
        sorted(
            (
                LineupPlayerBox(
                    game_id=row.game_id,
                    team_id=row.team_id,
                    player_id=row.player.player_id,
                    position=row.position,
                    starter=row.starter,
                    played=row.played,
                    comment=row.comment,
                )
                for row in parsed_box.player_rows
            ),
            key=lambda row: (row.team_id, row.player_id),
        )
    )
    player_boxes = parsed_player_boxes if canonical_player_boxes is None else canonical_player_boxes
    events: tuple[PlayByPlayEvent, ...] = (
        parsed_events if canonical_events is None else canonical_events
    )
    game = GameContext(
        game_id=GAME_ID,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 1),
        status=GameStatus.FINAL,
        home_team_id=10,
        away_team_id=20,
        home_score=120,
        away_score=110,
    )
    return GameLineupInputSnapshot(
        game=game,
        applied_box_score_document_id=uuid4(),
        applied_box_score_sha256=box_document.checksum_sha256,
        applied_pbp_document_id=uuid4(),
        applied_pbp_sha256=pbp_document.checksum_sha256,
        canonical_player_boxes=player_boxes,
        canonical_events=events,
        canonical_player_box_sha256=canonical_player_boxes_checksum(player_boxes),
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        box_score_source_document=box_document,
        pbp_source_document=pbp_document,
    )
