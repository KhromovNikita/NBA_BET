"""One-game, opt-in acceptance canary against NBA.com and PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillStatus,
    HistoricalIngestionService,
    ResourceKey,
)
from nba_live_totals.domain import GameId, JsonObject, SeasonId
from nba_live_totals.infrastructure.nba import (
    NBAApiHistoricalDataSource,
    NBAApiTransport,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import (
    PostgresHistoricalIngestionStore,
)
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)
from nba_live_totals.settings import Settings
from tests import runtime_config

SEASON = os.getenv("TEST_NBA_SEASON")
GAME_ID = os.getenv("TEST_NBA_GAME_ID")
pytestmark = [pytest.mark.live, pytest.mark.integration]

if not SEASON or not GAME_ID:
    pytest.skip(
        "set TEST_NBA_SEASON and TEST_NBA_GAME_ID for the opt-in live canary",
        allow_module_level=True,
    )

SELECTED_SEASON = SeasonId(SEASON)
SELECTED_GAME = GameId(GAME_ID)
RESOURCE_KEYS = (
    str(ResourceKey.schedule(SELECTED_SEASON)),
    str(ResourceKey.box_score(SELECTED_GAME)),
    str(ResourceKey.play_by_play(SELECTED_GAME)),
)


class RecordingLiveTransport:
    """Count requests while delegating every call to the real rate-limited transport."""

    def __init__(self, transport: NBAApiTransport) -> None:
        self.transport = transport
        self.requests: list[tuple[str, JsonObject]] = []

    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        self.requests.append((endpoint, parameters))
        return await self.transport.get(endpoint, parameters)


class LiveHarness:
    """Own the empty, isolated database state used by one live canary."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        settings = Settings(
            _env_file=None,
            nba_api_max_concurrency=1,
            nba_api_min_interval_seconds=0.75,
        )  # type: ignore[call-arg]
        self.transport = RecordingLiveTransport(NBAApiTransport(settings))
        self.source = NBAApiHistoricalDataSource(self.transport)
        self.store = PostgresHistoricalIngestionStore(
            engine,
            source="nba.com",
            lease_seconds=int(settings.nba_api_resource_lease_seconds),
        )
        self.service = HistoricalIngestionService(self.source, self.store, max_concurrency=1)
        self.run_ids: list[UUID] = []

    async def assert_empty(self) -> None:
        """Refuse to mutate a database containing any non-migration data."""
        tables = (
            seasons,
            teams,
            games,
            players,
            team_box_scores,
            player_box_scores,
            play_by_play_events,
            source_documents,
            ingestion_resources,
            ingestion_runs,
        )
        async with self.engine.connect() as connection:
            counts = {
                table.name: cast(
                    int,
                    await connection.scalar(select(func.count()).select_from(table)),
                )
                for table in tables
            }
        assert all(count == 0 for count in counts.values()), (
            "live canary requires an empty isolated PostgreSQL database",
            counts,
        )

    async def snapshot(self) -> dict[str, int]:
        async with self.engine.connect() as connection:
            return {
                "games": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(games)
                        .where(games.c.game_id == SELECTED_GAME.value)
                    ),
                ),
                "team_box_scores": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(team_box_scores)
                        .where(team_box_scores.c.game_id == SELECTED_GAME.value)
                    ),
                ),
                "player_box_scores": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(player_box_scores)
                        .where(player_box_scores.c.game_id == SELECTED_GAME.value)
                    ),
                ),
                "play_by_play_events": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(play_by_play_events)
                        .where(play_by_play_events.c.game_id == SELECTED_GAME.value)
                    ),
                ),
                "source_documents": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(source_documents)
                        .where(
                            source_documents.c.source == "nba.com",
                            source_documents.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    ),
                ),
                "ingestion_resources": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(ingestion_resources)
                        .where(
                            ingestion_resources.c.source == "nba.com",
                            ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    ),
                ),
            }

    async def safe_resource_states(self) -> dict[str, tuple[object, object, object]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(
                            ingestion_resources.c.resource_key,
                            ingestion_resources.c.status,
                            ingestion_resources.c.last_error_code,
                            ingestion_resources.c.last_error_type,
                        ).where(
                            ingestion_resources.c.source == "nba.com",
                            ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {
            cast(str, row["resource_key"]): (
                row["status"],
                row["last_error_code"],
                row["last_error_type"],
            )
            for row in rows
        }

    async def cleanup(self) -> None:
        """Delete only canonical identities persisted for the selected season."""
        async with self.engine.begin() as connection:
            season_games = (
                await connection.execute(
                    select(
                        games.c.game_id,
                        games.c.home_team_id,
                        games.c.away_team_id,
                    ).where(games.c.season == SELECTED_SEASON.value)
                )
            ).all()
            schedule_game_ids = tuple(row.game_id for row in season_games)
            schedule_team_ids = tuple(
                {
                    team_id
                    for row in season_games
                    for team_id in (row.home_team_id, row.away_team_id)
                }
            )
            player_ids = tuple(
                (
                    await connection.execute(
                        select(player_box_scores.c.player_id).where(
                            player_box_scores.c.game_id.in_(schedule_game_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            if schedule_game_ids:
                await connection.execute(
                    delete(play_by_play_events).where(
                        play_by_play_events.c.game_id.in_(schedule_game_ids)
                    )
                )
                await connection.execute(
                    delete(player_box_scores).where(
                        player_box_scores.c.game_id.in_(schedule_game_ids)
                    )
                )
                await connection.execute(
                    delete(team_box_scores).where(team_box_scores.c.game_id.in_(schedule_game_ids))
                )
                await connection.execute(
                    delete(games).where(games.c.game_id.in_(schedule_game_ids))
                )
            if player_ids:
                await connection.execute(delete(players).where(players.c.player_id.in_(player_ids)))
            if schedule_team_ids:
                await connection.execute(
                    delete(teams).where(teams.c.team_id.in_(schedule_team_ids))
                )
            await connection.execute(
                delete(seasons).where(seasons.c.season == SELECTED_SEASON.value)
            )
            await connection.execute(
                delete(ingestion_resources).where(
                    ingestion_resources.c.source == "nba.com",
                    ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            await connection.execute(
                delete(source_documents).where(
                    source_documents.c.source == "nba.com",
                    source_documents.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            if self.run_ids:
                await connection.execute(
                    delete(ingestion_runs).where(ingestion_runs.c.run_id.in_(self.run_ids))
                )

        await self.assert_empty()


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def harness() -> AsyncIterator[LiveHarness]:
    engine = create_async_engine(runtime_config.postgres_url())
    value = LiveHarness(engine)
    try:
        await value.assert_empty()
    except BaseException:
        await engine.dispose()
        raise
    try:
        yield value
    finally:
        try:
            await value.cleanup()
        finally:
            await engine.dispose()


async def test_selected_completed_game_ingests_and_reruns_idempotently(
    harness: LiveHarness,
) -> None:
    request = BackfillRequest(seasons=(SELECTED_SEASON,), game_id=SELECTED_GAME)

    first = await harness.service.backfill(request)
    harness.run_ids.append(first.run_id)

    assert first.status is BackfillStatus.SUCCEEDED, await harness.safe_resource_states()
    assert first.discovered == 1
    assert first.succeeded == 3
    assert first.skipped == 0
    assert first.failed == 0
    assert harness.transport.requests == [
        ("scheduleleaguev2", {"LeagueID": "00", "Season": SELECTED_SEASON.value}),
        (
            "boxscoretraditionalv3",
            {
                "GameID": SELECTED_GAME.value,
                "StartPeriod": 0,
                "EndPeriod": 0,
                "StartRange": 0,
                "EndRange": 0,
                "RangeType": 0,
            },
        ),
        (
            "playbyplayv3",
            {
                "GameID": SELECTED_GAME.value,
                "StartPeriod": 0,
                "EndPeriod": 0,
            },
        ),
    ]
    first_snapshot = await harness.snapshot()
    assert first_snapshot["games"] == 1
    assert first_snapshot["team_box_scores"] == 2
    assert first_snapshot["player_box_scores"] >= 2
    assert first_snapshot["play_by_play_events"] > 0
    assert first_snapshot["source_documents"] == 3
    assert first_snapshot["ingestion_resources"] == 3

    async with harness.engine.connect() as connection:
        selected_game = (
            (
                await connection.execute(
                    select(games.c.season, games.c.status).where(
                        games.c.game_id == SELECTED_GAME.value
                    )
                )
            )
            .mappings()
            .one()
        )
        resources = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == "nba.com",
                        ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                    )
                )
            )
            .mappings()
            .all()
        )
        endpoints = set(
            (
                await connection.execute(
                    select(source_documents.c.endpoint).where(
                        source_documents.c.source == "nba.com",
                        source_documents.c.resource_key.in_(RESOURCE_KEYS),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert selected_game == {"season": SELECTED_SEASON.value, "status": "final"}
    assert endpoints == {
        "scheduleleaguev2",
        "boxscoretraditionalv3",
        "playbyplayv3",
    }
    assert all(resource["status"] == "succeeded" for resource in resources)
    assert all(resource["latest_document_id"] is not None for resource in resources)
    assert all(
        resource["latest_document_id"] == resource["applied_document_id"] for resource in resources
    )
    assert all(resource["last_error_code"] is None for resource in resources)
    assert all(resource["last_error_type"] is None for resource in resources)

    requests_after_first = list(harness.transport.requests)
    second = await harness.service.backfill(request)
    harness.run_ids.append(second.run_id)

    assert second.status is BackfillStatus.SUCCEEDED
    assert second.discovered == 1
    assert second.succeeded == 0
    assert second.skipped == 3
    assert second.failed == 0
    assert harness.transport.requests == requests_after_first
    assert await harness.snapshot() == first_snapshot
