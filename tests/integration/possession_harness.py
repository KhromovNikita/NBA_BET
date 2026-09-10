"""Real-PostgreSQL fixtures for possession work-store integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.domain import canonical_json_checksum
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    play_by_play_events,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
    seasons,
    source_documents,
    teams,
)

BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass
class PossessionHarness:
    """Own only test rows, leaving the shared compose volume intact."""

    engine: AsyncEngine
    source: str = field(default_factory=lambda: f"task-11-{uuid4()}")
    game_ids: set[str] = field(default_factory=set)
    run_ids: list[UUID] = field(default_factory=list)

    async def seed_game(
        self,
        *,
        game_id: str = "0022500111",
        season: str = "2025-26",
        season_type: str = "regular_season",
        status: str = "final",
        applied: bool = True,
        ingestion_status: str = "succeeded",
        event_count: int = 2,
        game_date: date = date(2026, 1, 2),
    ) -> UUID:
        """Seed a final-game PBP document, applied resource and canonical events."""
        self.game_ids.add(game_id)
        document_id = uuid4()
        checksum = canonical_json_checksum({"game_id": game_id, "raw": "v1"})
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(seasons)
                .values(
                    season=season,
                    season_type=season_type,
                    season_start_year=int(season[:4]),
                )
                .on_conflict_do_nothing()
            )
            await connection.execute(
                insert(teams)
                .values(
                    team_id=910001,
                    city="Home",
                    name="Home Team",
                    tricode="HOM",
                    slug="home-team",
                )
                .on_conflict_do_nothing()
            )
            await connection.execute(
                insert(teams)
                .values(
                    team_id=910002,
                    city="Away",
                    name="Away Team",
                    tricode="AWY",
                    slug="away-team",
                )
                .on_conflict_do_nothing()
            )
            await connection.execute(
                games.insert().values(
                    game_id=game_id,
                    season=season,
                    season_type=season_type,
                    game_date=game_date,
                    started_at=BASE_TIME,
                    home_team_id=910001,
                    away_team_id=910002,
                    home_score=110,
                    away_score=108,
                    status=status,
                )
            )
            await connection.execute(
                source_documents.insert().values(
                    document_id=document_id,
                    source=self.source,
                    endpoint="playbyplayv3",
                    resource_key=f"play_by_play:{game_id}",
                    request_parameters={"game_id": game_id},
                    payload={"game_id": game_id, "raw": "v1"},
                    checksum_sha256=checksum,
                    requested_at=BASE_TIME,
                )
            )
            await connection.execute(
                ingestion_resources.insert().values(
                    source=self.source,
                    resource_type="play_by_play",
                    resource_key=f"play_by_play:{game_id}",
                    status=ingestion_status,
                    applied_document_id=document_id if applied else None,
                )
            )
            for sequence_number in range(1, event_count + 1):
                await connection.execute(
                    play_by_play_events.insert().values(
                        game_id=game_id,
                        sequence_number=sequence_number,
                        action_number=sequence_number,
                        action_id=sequence_number,
                        period=1,
                        clock="PT11M00.00S",
                        seconds_remaining_period_ms=660_000,
                        team_id=910001,
                        person_id=None,
                        action_type="2pt",
                        sub_type="",
                        description="test event",
                        home_score=2 * sequence_number,
                        away_score=0,
                        shot_result="Made",
                        shot_distance=1,
                        legacy_x=None,
                        legacy_y=None,
                        location="h",
                        is_field_goal=True,
                        points_total=2 * sequence_number,
                        video_available=False,
                    )
                )
        return document_id

    async def cleanup(self) -> None:
        async with self.engine.begin() as connection:
            if self.game_ids:
                await connection.execute(
                    delete(possession_rebuild_resources).where(
                        possession_rebuild_resources.c.game_id.in_(self.game_ids)
                    )
                )
                await connection.execute(
                    delete(possession_snapshot_attempts).where(
                        possession_snapshot_attempts.c.game_id.in_(self.game_ids)
                    )
                )
                await connection.execute(
                    delete(ingestion_resources).where(ingestion_resources.c.source == self.source)
                )
                await connection.execute(
                    delete(play_by_play_events).where(
                        play_by_play_events.c.game_id.in_(self.game_ids)
                    )
                )
                await connection.execute(delete(games).where(games.c.game_id.in_(self.game_ids)))
            await connection.execute(
                delete(source_documents).where(source_documents.c.source == self.source)
            )
            if self.run_ids:
                await connection.execute(
                    delete(possession_rebuild_runs).where(
                        possession_rebuild_runs.c.run_id.in_(self.run_ids)
                    )
                )
