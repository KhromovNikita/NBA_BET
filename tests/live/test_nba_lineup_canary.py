"""Opt-in, three-request provider-drift check using exclusively disposable stores."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Table, and_, delete, event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql.dml import Insert

from nba_live_totals.application.ingestion import BackfillRequest, HistoricalIngestionService
from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    GameLineupInputSnapshot,
    LineupBuildMode,
    LineupBuildService,
)
from nba_live_totals.domain import GameId, JsonObject, SeasonId
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import LineupVersions, NormalizedGameLineups, Substitute
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.nba import (
    NBAApiHistoricalDataSource,
    NBAApiTransport,
    NBAPlayByPlayV3LineupNormalizer,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import PostgresHistoricalIngestionStore
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.schema import games
from nba_live_totals.settings import Settings
from tests import runtime_config

pytestmark = [pytest.mark.live, pytest.mark.integration]
REQUIRED = ("TEST_NBA_SEASON", "TEST_NBA_GAME_ID", "TEST_DATABASE_URL", "TEST_CLICKHOUSE_URL")
if not all(os.getenv(name) for name in REQUIRED) or os.getenv("TEST_LIVE_ISOLATED_STORES") != "1":
    pytest.skip(
        "set TEST_NBA_SEASON, TEST_NBA_GAME_ID, TEST_DATABASE_URL, TEST_CLICKHOUSE_URL, "
        "and TEST_LIVE_ISOLATED_STORES=1 for the opt-in lineup canary",
        allow_module_level=True,
    )

SEASON = SeasonId(os.environ["TEST_NBA_SEASON"])
GAME = GameId(os.environ["TEST_NBA_GAME_ID"])
REQUEST = BuildLineupsRequest((SEASON,), game_id=GAME)
VERSIONS = LineupVersions(
    "nba-pbp-v3-lineup-v1", "causal-lineup-v1", "nba-5x5-v1", "lineup-segments-v1"
)
DATA_TABLES = tuple(
    t for t in games.metadata.sorted_tables if t.name != "clickhouse_schema_migrations"
)


class OwnedIdentities:
    """Journal primary keys before INSERT, including failed/uncertain commits.

    The hook also sees bulk and upsert statements. An existing unowned key aborts
    before the write and is never adopted. Stores must have exclusive canary use.
    """

    def __init__(self) -> None:
        self.rows: dict[Table, set[tuple[object, ...]]] = {}
        self.lineup_ids: set[UUID] = set()

    def before_insert(
        self,
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        compiled = context.compiled
        if compiled is None or not isinstance(compiled.statement, Insert):
            return
        table = compiled.statement.table
        assert isinstance(table, Table)
        columns = tuple(table.primary_key.columns)
        assert columns, "cannot own an insert without a primary key"
        owned = self.rows.setdefault(table, set())
        for values in context.compiled_parameters:
            # SQLAlchemy inline multi-values use column_m0, column_m1, ...;
            # executemany and single-row inserts use plain column names.
            suffixes = {
                key[len(columns[0].name) :]
                for key in values
                if key == columns[0].name
                or re.fullmatch(re.escape(columns[0].name) + r"_m\d+", key)
            }
            assert suffixes, "insert primary key must be explicit"
            for suffix in sorted(suffixes):
                identity = tuple(values[column.name + suffix] for column in columns)
                if identity in owned:
                    continue
                predicate = and_(
                    *(column == value for column, value in zip(columns, identity, strict=True))
                )
                assert (
                    connection.scalar(select(func.count()).select_from(table).where(predicate)) == 0
                ), f"refusing unowned identity collision in {table.name}"
                owned.add(identity)
                if table.name == "lineup_snapshot_attempts":
                    assert isinstance(identity[0], UUID)
                    self.lineup_ids.add(identity[0])

    async def cleanup_clickhouse(self, client: ClickHouseClient) -> None:
        for snapshot in sorted(self.lineup_ids):
            await client.execute(
                "ALTER TABLE nba.lineup_segments_v1 DELETE "
                f"WHERE snapshot_id = '{UUID(str(snapshot))}' SETTINGS mutations_sync = 2"
            )

    async def cleanup_postgres(self, engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            for table in reversed(DATA_TABLES):
                for identity in self.rows.get(table, ()):
                    await connection.execute(
                        delete(table).where(
                            and_(
                                *(
                                    column == value
                                    for column, value in zip(
                                        table.primary_key.columns, identity, strict=True
                                    )
                                )
                            )
                        )
                    )


class BoundedTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.transport = NBAApiTransport(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                nba_api_max_concurrency=1,
                nba_api_max_attempts=1,
                nba_api_min_interval_seconds=0.75,
            )
        )

    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        expected = ("scheduleleaguev2", "boxscoretraditionalv3", "playbyplayv3")
        assert len(self.calls) < 3 and endpoint == expected[len(self.calls)]
        if endpoint == "scheduleleaguev2":
            assert parameters["Season"] == SEASON.value
        else:
            assert parameters["GameID"] == GAME.value
        self.calls.append(endpoint)
        return await self.transport.get(endpoint, parameters)


class CanaryNormalizer(NBAPlayByPlayV3LineupNormalizer):
    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        result = super().normalize(snapshot)
        assert len(result.home_roster.starter_player_ids) == 5
        assert len(result.away_roster.starter_player_ids) == 5
        assert any(isinstance(event.transition, Substitute) for event in result.events)
        return result


class LiveLineupHarness:
    def __init__(self, engine: AsyncEngine, client: ClickHouseClient) -> None:
        self.engine, self.client = engine, client
        self.owned = OwnedIdentities()
        self.transport = BoundedTransport()
        self.ingestion = HistoricalIngestionService(
            NBAApiHistoricalDataSource(self.transport),
            PostgresHistoricalIngestionStore(engine),
            max_concurrency=1,
        )
        self.store = PostgresLineupWorkStore(engine)
        self.snapshots = ClickHouseLineupSnapshotStore(client)
        self.service = LineupBuildService(
            normalizer=CanaryNormalizer(),
            reconstructor=DeterministicLineupReconstructor(),
            work_store=self.store,
            snapshot_store=self.snapshots,
            versions=VERSIONS,
            max_concurrency=1,
        )

    async def assert_empty(self) -> None:
        async with self.engine.connect() as connection:
            for table in DATA_TABLES:
                assert await connection.scalar(select(func.count()).select_from(table)) == 0, (
                    f"live canary requires empty isolated PostgreSQL: {table.name}"
                )
        for analytical_table in ("possessions_v1", "lineup_segments_v1"):
            assert await self.client.query(
                f"SELECT count() AS count FROM nba.{analytical_table}"
            ) == [{"count": 0}], (
                f"live canary requires empty isolated ClickHouse: {analytical_table}"
            )


@pytest.fixture
async def live_harness() -> AsyncIterator[LiveLineupHarness]:
    # Resolve both guards before opening either connection.
    pg_url, ch_url = runtime_config.postgres_url(), runtime_config.clickhouse_url()
    engine = create_async_engine(pg_url)
    client = ClickHouseClient(
        base_url=ch_url,
        user=os.getenv("CLICKHOUSE_USER", "nba"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "nba"),
        timeout_seconds=30,
    )
    harness = LiveLineupHarness(engine, client)
    try:
        await harness.assert_empty()
        event.listen(engine.sync_engine, "before_cursor_execute", harness.owned.before_insert)
        try:
            yield harness
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", harness.owned.before_insert)
            try:
                await harness.owned.cleanup_clickhouse(client)
            finally:
                await harness.owned.cleanup_postgres(engine)
            await harness.assert_empty()
    finally:
        await client.aclose()
        await engine.dispose()


async def test_one_final_game_starters_substitutions_rebuild_verify_and_offline_rerun(
    live_harness: LiveLineupHarness,
) -> None:
    h = live_harness
    ingestion = await h.ingestion.backfill(BackfillRequest((SEASON,), game_id=GAME))
    assert (ingestion.discovered, ingestion.succeeded, ingestion.failed) == (1, 3, 0)
    assert len(h.transport.calls) == 3
    first = await h.service.build(REQUEST)
    assert (first.materialized, first.invalid, first.failed, first.busy) == (1, 0, 0, 0)
    reference = (await h.store.resolve_current(GAME, VERSIONS)).reference
    assert reference is not None
    rows = await h.snapshots.read_snapshot(reference)
    assert len(rows) == reference.row_count > 1
    for row in rows:
        for side in (row.home_state, row.away_state):
            assert side.status.value == "valid" and len(side.player_ids) == 5
    rebuilt = await h.service.build(BuildLineupsRequest((SEASON,), game_id=GAME, rebuild=True))
    assert (rebuilt.materialized, rebuilt.invalid, rebuilt.failed) == (1, 0, 0)
    new = (await h.store.resolve_current(GAME, VERSIONS)).reference
    assert new is not None
    assert new.attempt.snapshot_id != reference.attempt.snapshot_id
    assert new.attempt.generation == reference.attempt.generation + 1
    assert new.output_sha256 == reference.output_sha256
    assert await h.snapshots.read_snapshot(new) == rows
    verified = await h.service.build(
        BuildLineupsRequest((SEASON,), game_id=GAME, mode=LineupBuildMode.VERIFY)
    )
    assert (verified.verified, verified.failed) == (1, 0)
    attempts = set(h.owned.lineup_ids)
    rerun = await h.service.build(REQUEST)
    assert (rerun.skipped_current, rerun.materialized, rerun.failed) == (1, 0, 0)
    assert h.owned.lineup_ids == attempts
    repeated = await h.ingestion.backfill(BackfillRequest((SEASON,), game_id=GAME))
    assert (repeated.skipped, repeated.failed) == (3, 0)
    assert len(h.transport.calls) == 3
    assert (await h.store.resolve_current(GAME, VERSIONS)).reference == new
