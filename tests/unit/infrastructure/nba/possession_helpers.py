"""Shared real-fixture builders for NBA possession normalizer tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from nba_live_totals.application.possessions import GamePbpSnapshot
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
from nba_live_totals.infrastructure.nba.mappers import parse_play_by_play

_FIXTURE_ROOT = Path(__file__).parents[3] / "fixtures" / "nba" / "possessions"
GAME_ID = GameId("0022500123")
HOME_TEAM_ID = 1_610_612_736
AWAY_TEAM_ID = 1_610_612_744


def load_json_fixture(name: str) -> JsonObject:
    """Load a possession fixture as a new mutable JSON object."""
    return cast(JsonObject, json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8")))


def make_source_document(
    payload: JsonObject,
    *,
    source: str = "nba.com",
    endpoint: str = "playbyplayv3",
    resource_key: str = f"play_by_play:{GAME_ID.value}",
) -> SourceDocument:
    """Build the immutable applied provider document used by one snapshot."""
    return SourceDocument(
        source=source,
        endpoint=endpoint,
        resource_key=resource_key,
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_snapshot(
    payload: JsonObject | None = None,
    *,
    canonical_events: tuple[PlayByPlayEvent, ...] | None = None,
    source: str = "nba.com",
    endpoint: str = "playbyplayv3",
) -> GamePbpSnapshot:
    """Build a contract-valid snapshot, optionally retaining independent canonical rows."""
    payload = payload or load_json_fixture("pbp_v3_normalizer_v1.json")
    document = make_source_document(payload, source=source, endpoint=endpoint)
    events = canonical_events if canonical_events is not None else parse_play_by_play(document)
    game = GameContext(
        game_id=GAME_ID,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=HOME_TEAM_ID,
        away_team_id=AWAY_TEAM_ID,
        home_score=3,
        away_score=2,
    )
    return GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=UUID("12345678-1234-5678-9234-567812345678"),
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )
