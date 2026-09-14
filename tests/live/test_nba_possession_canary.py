"""Opt-in one-game Phase 2 -> Phase 3 canary against NBA.com."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    HistoricalIngestionService,
    ResourceKey,
)
from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    PossessionBuildService,
)
from nba_live_totals.domain import (
    AnalyticalPossessionParser,
    GameId,
    JsonObject,
    PossessionVersions,
    SeasonId,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.nba import (
    NBAApiHistoricalDataSource,
    NBAApiTransport,
    NBAPlayByPlayV3PossessionNormalizer,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import PostgresHistoricalIngestionStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)
from nba_live_totals.settings import Settings
from tests import runtime_config
from tests.integration.test_possession_snapshot_protocol import RecordingClickHouse

TEST_SEASON = os.getenv("TEST_NBA_SEASON")
TEST_GAME_ID = os.getenv("TEST_NBA_GAME_ID")
pytestmark = [pytest.mark.live, pytest.mark.integration]

if not TEST_SEASON or not TEST_GAME_ID:
    pytest.skip(
        "set TEST_NBA_SEASON and TEST_NBA_GAME_ID for the opt-in possession canary",
        allow_module_level=True,
    )

SELECTED_SEASON = SeasonId(TEST_SEASON)
SELECTED_GAME = GameId(TEST_GAME_ID)
# This validates season/game identity before fixture setup can issue an NBA request.
TARGET_REQUEST = BuildPossessionsRequest((SELECTED_SEASON,), game_id=SELECTED_GAME)
RESOURCE_KEYS = (
    str(ResourceKey.schedule(SELECTED_SEASON)),
    str(ResourceKey.box_score(SELECTED_GAME)),
    str(ResourceKey.play_by_play(SELECTED_GAME)),
)


class RecordingLiveTransport:
    def __init__(self, transport: NBAApiTransport) -> None:
        self.transport = transport
        self.requests: list[tuple[str, JsonObject]] = []

    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        self.requests.append((endpoint, parameters))
        return await self.transport.get(endpoint, parameters)


class LivePossessionHarness:
    """Own an initially empty isolated PG/CH pair for one live game."""

    def __init__(self, engine: AsyncEngine, client: ClickHouseClient) -> None:
        self.engine = engine
        self.client = client
        settings = Settings(
            _env_file=None,
            nba_api_max_concurrency=1,
            nba_api_min_interval_seconds=0.75,
        )  # type: ignore[call-arg]
        self.transport = RecordingLiveTransport(NBAApiTransport(settings))
        self.ingestion_store = PostgresHistoricalIngestionStore(engine, source="nba.com")
        self.ingestion = HistoricalIngestionService(
            NBAApiHistoricalDataSource(self.transport),
            self.ingestion_store,
            max_concurrency=1,
        )
        self.work_store = PostgresPossessionWorkStore(engine, source="nba.com")
        self.ingestion_run_ids: list[UUID] = []
        self.possession_run_ids: list[UUID] = []
        self.versions = PossessionVersions(
            settings.possession_normalizer_version,
            settings.possession_parser_version,
            settings.possession_rules_version,
            settings.possession_output_schema_version,
        )

    async def assert_empty(self) -> None:
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
            possession_rebuild_resources,
            possession_snapshot_attempts,
            possession_rebuild_runs,
        )
        async with self.engine.connect() as connection:
            counts = {
                table.name: cast(
                    int, await connection.scalar(select(func.count()).select_from(table))
                )
                for table in tables
            }
        clickhouse = await self.client.query("SELECT count() AS count FROM nba.possessions_v1")
        assert all(count == 0 for count in counts.values()), (
            "live possession canary requires empty isolated PostgreSQL",
            counts,
        )
        assert clickhouse == [{"count": 0}], (
            "live possession canary requires empty isolated ClickHouse",
            clickhouse,
        )

    def build_service(self, transport: RecordingClickHouse) -> PossessionBuildService:
        return PossessionBuildService(
            normalizer=NBAPlayByPlayV3PossessionNormalizer(),
            parser=AnalyticalPossessionParser(),
            work_store=self.work_store,
            snapshot_store=PossessionSnapshotStore(transport),
            versions=self.versions,
            max_concurrency=1,
        )

    async def cleanup(self) -> None:
        await self.client.execute(
            "ALTER TABLE nba.possessions_v1 DELETE "
            f"WHERE game_id = '{SELECTED_GAME.value}' SETTINGS mutations_sync = 2"
        )
        async with self.engine.begin() as connection:
            season_games = (
                await connection.execute(
                    select(games.c.game_id, games.c.home_team_id, games.c.away_team_id).where(
                        games.c.season == SELECTED_SEASON.value
                    )
                )
            ).all()
            game_ids = tuple(row.game_id for row in season_games)
            team_ids = tuple(
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
                            player_box_scores.c.game_id.in_(game_ids)
                        )
                    )
                ).scalars()
            )
            if game_ids:
                await connection.execute(
                    delete(possession_rebuild_resources).where(
                        possession_rebuild_resources.c.game_id.in_(game_ids)
                    )
                )
                await connection.execute(
                    delete(possession_snapshot_attempts).where(
                        possession_snapshot_attempts.c.game_id.in_(game_ids)
                    )
                )
                await connection.execute(
                    delete(play_by_play_events).where(play_by_play_events.c.game_id.in_(game_ids))
                )
                await connection.execute(
                    delete(player_box_scores).where(player_box_scores.c.game_id.in_(game_ids))
                )
                await connection.execute(
                    delete(team_box_scores).where(team_box_scores.c.game_id.in_(game_ids))
                )
                await connection.execute(delete(games).where(games.c.game_id.in_(game_ids)))
            if player_ids:
                await connection.execute(delete(players).where(players.c.player_id.in_(player_ids)))
            if team_ids:
                await connection.execute(delete(teams).where(teams.c.team_id.in_(team_ids)))
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
            if self.ingestion_run_ids:
                await connection.execute(
                    delete(ingestion_runs).where(
                        ingestion_runs.c.run_id.in_(self.ingestion_run_ids)
                    )
                )
            if self.possession_run_ids:
                await connection.execute(
                    delete(possession_rebuild_runs).where(
                        possession_rebuild_runs.c.run_id.in_(self.possession_run_ids)
                    )
                )


@pytest.fixture
async def live_harness() -> AsyncIterator[LivePossessionHarness]:
    engine = create_async_engine(runtime_config.postgres_url(), pool_pre_ping=True)
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(),
        user=os.getenv("CLICKHOUSE_USER", "nba"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "nba"),
        timeout_seconds=30,
    )
    value = LivePossessionHarness(engine, client)
    try:
        await value.assert_empty()
    except BaseException:
        await client.aclose()
        await engine.dispose()
        raise
    try:
        yield value
    finally:
        try:
            await value.cleanup()
            await value.assert_empty()
        finally:
            await client.aclose()
            await engine.dispose()


async def test_one_completed_live_game_builds_verifies_and_reruns_offline(
    live_harness: LivePossessionHarness,
) -> None:
    ingestion = await live_harness.ingestion.backfill(
        BackfillRequest(seasons=(SELECTED_SEASON,), game_id=SELECTED_GAME)
    )
    live_harness.ingestion_run_ids.append(ingestion.run_id)
    assert (ingestion.discovered, ingestion.succeeded, ingestion.failed) == (1, 3, 0)
    requests_after_ingestion = list(live_harness.transport.requests)
    assert len(requests_after_ingestion) == 3

    first_transport = RecordingClickHouse(live_harness.client)
    first = await live_harness.build_service(first_transport).build(TARGET_REQUEST)
    live_harness.possession_run_ids.append(first.run_id)
    assert (first.materialized, first.failed, first.busy) == (1, 0, 0)
    assert (first_transport.insert_calls, first_transport.query_calls) == (1, 1)

    verify_transport = RecordingClickHouse(live_harness.client)
    verified = await live_harness.build_service(verify_transport).build(
        BuildPossessionsRequest((SELECTED_SEASON,), game_id=SELECTED_GAME, mode=BuildMode.VERIFY)
    )
    live_harness.possession_run_ids.append(verified.run_id)
    assert (verified.verified, verified.failed) == (1, 0)
    assert (verify_transport.insert_calls, verify_transport.query_calls) == (0, 1)

    rerun_ingestion = await live_harness.ingestion.backfill(
        BackfillRequest(seasons=(SELECTED_SEASON,), game_id=SELECTED_GAME)
    )
    live_harness.ingestion_run_ids.append(rerun_ingestion.run_id)
    assert rerun_ingestion.skipped == 3
    assert live_harness.transport.requests == requests_after_ingestion

    rerun_transport = RecordingClickHouse(live_harness.client)
    rerun = await live_harness.build_service(rerun_transport).build(TARGET_REQUEST)
    live_harness.possession_run_ids.append(rerun.run_id)
    assert (rerun.skipped_current, rerun.materialized) == (1, 0)
    assert (rerun_transport.insert_calls, rerun_transport.query_calls) == (0, 0)

    first_reference = await live_harness.work_store.resolve_current(
        SELECTED_GAME, live_harness.versions
    )
    assert first_reference is not None
    rows = await PossessionSnapshotStore(live_harness.client).read_snapshot(first_reference)
    second_reference = await live_harness.work_store.resolve_current(
        SELECTED_GAME, live_harness.versions
    )
    assert second_reference == first_reference
    assert len(rows) == first_reference.row_count > 0
    assert rows[-1].home_score == first_reference.attempt.game.home_score
    assert rows[-1].away_score == first_reference.attempt.game.away_score
    print(
        "live-possession-canary "
        f"game_id={SELECTED_GAME.value} requests={len(requests_after_ingestion)} "
        f"possessions={len(rows)} output_sha256={first_reference.output_sha256}"
    )
