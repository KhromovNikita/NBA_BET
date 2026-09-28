from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic import AnyUrl, HttpUrl, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.infrastructure.clickhouse.client import (
    ClickHouseClient,
    ClickHouseError,
)
from nba_live_totals.infrastructure.clickhouse.migrations import (
    GAME_STATE_FEATURES_SCHEMA_ID,
    LINEUP_SEGMENTS_SCHEMA_ID,
    MIGRATION_ADVISORY_LOCK_KEY,
    PLAYER_PREGAME_FEATURES_SCHEMA_ID,
    POSSESSIONS_SCHEMA_ID,
    ClickHouseMigrationError,
    fetch_schema_projection,
    load_migrations,
    migrate_clickhouse,
)
from nba_live_totals.lifecycle import close_resources, create_resources
from nba_live_totals.settings import Settings
from tests import runtime_config

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "clickhouse" / "migrations"
MIGRATION_ID = "0001_possessions"
LINEUP_SEGMENTS_MIGRATION_ID = "0002_lineup_segments"
GAME_STATE_FEATURES_MIGRATION_ID = "0003_game_state_features"
PLAYER_PREGAME_FEATURES_MIGRATION_ID = "0004_player_pregame_features"
AUXILIARY_MIGRATION_ID = "0002_migration_test_aux"
POSSESSION_UPGRADE_MIGRATION_ID = "0003_possessions_upgrade"
AUXILIARY_TABLE = "migration_test_aux_v1"
ANALYTICAL_TABLES = (
    "possessions_v1",
    "lineup_segments_v1",
    "game_state_features_v1",
    "player_pregame_features_v1",
)
DESTRUCTIVE_TABLES = (*ANALYTICAL_TABLES, AUXILIARY_TABLE)


@dataclass(frozen=True, slots=True)
class MigrationResources:
    engine: AsyncEngine
    client: ClickHouseClient


@pytest.fixture
async def migration_resources() -> AsyncIterator[MigrationResources]:
    if not runtime_config.destructive_clickhouse_migrations_enabled():
        pytest.skip(
            "destructive ClickHouse migration tests require "
            "TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1 and an empty isolated store"
        )
    engine = create_async_engine(runtime_config.postgres_url(), pool_pre_ping=True)
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(),
        user="nba",
        password="nba",
        timeout_seconds=30.0,
    )
    async with _migration_resource_context(MigrationResources(engine, client)) as resources:
        yield resources


@asynccontextmanager
async def _migration_resource_context(
    resources: MigrationResources,
) -> AsyncIterator[MigrationResources]:
    destructive_run_started = False
    try:
        await _require_empty_migration_stores(resources.client)
        destructive_run_started = True
        await _reset(resources)
        yield resources
    finally:
        try:
            if destructive_run_started:
                await _restore_default(resources)
        finally:
            try:
                await resources.client.aclose()
            finally:
                await resources.engine.dispose()


async def _require_empty_migration_stores(client: ClickHouseClient) -> None:
    for table_name in DESTRUCTIVE_TABLES:
        table = await client.query(
            "SELECT name FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String}",
            parameters={"database": "nba", "table": table_name},
        )
        if not table:
            continue
        rows = await client.query(f"SELECT count() AS count FROM nba.{table_name}")
        if rows != [{"count": 0}]:
            pytest.fail(
                f"refusing destructive ClickHouse migration tests: nba.{table_name} is not empty"
            )


class _NonEmptyClickHouse:
    def __init__(self) -> None:
        self.closed = False

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        del parameters
        if "system.tables" in statement:
            return [{"name": "possessions_v1"}]
        return [{"count": 1}]

    async def execute(self, statement: str) -> None:
        raise AssertionError(f"destructive statement executed after guard rejection: {statement}")

    async def aclose(self) -> None:
        self.closed = True


class _NonEmptyAuxiliaryClickHouse:
    def __init__(self) -> None:
        self.closed = False
        self.executed: list[str] = []

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        if "system.tables" in statement:
            assert parameters is not None
            return [{"name": AUXILIARY_TABLE}] if parameters.get("table") == AUXILIARY_TABLE else []
        if AUXILIARY_TABLE in statement:
            return [{"count": 1}]
        raise AssertionError(f"unexpected guard query: {statement}")

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)

    async def aclose(self) -> None:
        self.closed = True


class _NonEmptyAnalyticalClickHouse:
    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        self.closed = False
        self.executed: list[str] = []

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        if "system.tables" in statement:
            assert parameters is not None
            return [{"name": self.table_name}] if parameters.get("table") == self.table_name else []
        if statement == f"SELECT count() AS count FROM nba.{self.table_name}":
            return [{"count": 1}]
        raise AssertionError(f"unexpected guard query: {statement}")

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)

    async def aclose(self) -> None:
        self.closed = True


class _GuardEngine:
    def __init__(self) -> None:
        self.disposed = False
        self.begin_calls = 0

    def begin(self) -> object:
        self.begin_calls += 1
        raise AssertionError("database reset started after guard rejection")

    async def dispose(self) -> None:
        self.disposed = True


async def test_destructive_migration_guard_refuses_nonempty_store() -> None:
    client = cast(ClickHouseClient, _NonEmptyClickHouse())

    with pytest.raises(pytest.fail.Exception, match="not empty"):
        await _require_empty_migration_stores(client)


async def test_migration_resource_context_does_not_restore_after_guard_rejection() -> None:
    client = _NonEmptyClickHouse()
    engine = _GuardEngine()
    resources = MigrationResources(
        cast(AsyncEngine, engine),
        cast(ClickHouseClient, client),
    )

    with pytest.raises(pytest.fail.Exception, match="not empty"):
        async with _migration_resource_context(resources):
            pytest.fail("guard unexpectedly allowed destructive migration test")

    assert client.closed is True
    assert engine.disposed is True


async def test_migration_resource_context_guards_every_cleanup_table() -> None:
    client = _NonEmptyAuxiliaryClickHouse()
    engine = _GuardEngine()
    resources = MigrationResources(
        cast(AsyncEngine, engine),
        cast(ClickHouseClient, client),
    )

    with pytest.raises(pytest.fail.Exception, match=AUXILIARY_TABLE):
        async with _migration_resource_context(resources):
            pytest.fail("guard unexpectedly allowed destructive migration test")

    assert client.executed == []
    assert client.closed is True
    assert engine.disposed is True


@pytest.mark.parametrize("table_name", ANALYTICAL_TABLES)
async def test_migration_context_refuses_each_nonempty_analytical_table_before_reset(
    table_name: str,
) -> None:
    client = _NonEmptyAnalyticalClickHouse(table_name)
    engine = _GuardEngine()
    resources = MigrationResources(
        cast(AsyncEngine, engine),
        cast(ClickHouseClient, client),
    )

    with pytest.raises(pytest.fail.Exception, match=rf"nba\.{table_name} is not empty"):
        async with _migration_resource_context(resources):
            pytest.fail("guard unexpectedly allowed destructive migration test")

    assert engine.begin_calls == 0
    assert client.executed == []
    assert client.closed is True
    assert engine.disposed is True


async def _reset(resources: MigrationResources) -> None:
    for table_name in DESTRUCTIVE_TABLES:
        await resources.client.execute(f"DROP TABLE IF EXISTS nba.{table_name}")
    async with resources.engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM clickhouse_schema_migrations "
                "WHERE migration_id IN "
                "(:current, :lineup_segments, :game_state_features, "
                ":player_pregame_features, :future, :upgrade, :auxiliary, "
                ":possession_upgrade)"
            ),
            {
                "current": MIGRATION_ID,
                "lineup_segments": LINEUP_SEGMENTS_MIGRATION_ID,
                "game_state_features": GAME_STATE_FEATURES_MIGRATION_ID,
                "player_pregame_features": PLAYER_PREGAME_FEATURES_MIGRATION_ID,
                "future": "9999_future",
                "upgrade": "0002_additive_upgrade",
                "auxiliary": AUXILIARY_MIGRATION_ID,
                "possession_upgrade": POSSESSION_UPGRADE_MIGRATION_ID,
            },
        )


async def _restore_default(resources: MigrationResources) -> None:
    await _reset(resources)
    await migrate_clickhouse(resources.engine, resources.client)


async def _ledger(engine: AsyncEngine, migration_id: str = MIGRATION_ID) -> Mapping[str, object]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT migration_id, checksum_sha256, "
                "expected_schema_fingerprint_sha256, state, applied_at "
                "FROM clickhouse_schema_migrations WHERE migration_id = :migration_id"
            ),
            {"migration_id": migration_id},
        )
        row = result.mappings().one()
        return dict(row)


class DelegatingClient(ClickHouseClient):
    def __init__(self, delegate: ClickHouseClient) -> None:
        self.delegate = delegate

    async def execute(self, statement: str) -> None:
        await self.delegate.execute(statement)

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        return await self.delegate.query(statement, parameters=parameters)


class ObservingClient(DelegatingClient):
    def __init__(self, delegate: ClickHouseClient, engine: AsyncEngine) -> None:
        super().__init__(delegate)
        self.engine = engine
        self.lock_attempts: list[bool] = []
        self.ledger_states: list[str] = []

    async def execute(self, statement: str) -> None:
        async with self.engine.connect() as observer:
            acquired = bool(
                await observer.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
                )
            )
            self.lock_attempts.append(acquired)
            if acquired:
                await observer.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
                )
            state = await observer.scalar(
                text(
                    "SELECT state FROM clickhouse_schema_migrations "
                    "WHERE migration_id = :migration_id"
                ),
                {"migration_id": MIGRATION_ID},
            )
            self.ledger_states.append(str(state))
        await super().execute(statement)


async def test_migration_holds_session_lock_and_persists_applying_before_ddl(
    migration_resources: MigrationResources,
) -> None:
    observer = ObservingClient(migration_resources.client, migration_resources.engine)

    await migrate_clickhouse(migration_resources.engine, observer)

    assert observer.lock_attempts == [False, False, False, False, False]
    assert observer.ledger_states == ["applying", "applying", "applied", "applied", "applied"]
    ledger = await _ledger(migration_resources.engine)
    migration = load_migrations()[0]
    assert ledger == {
        "migration_id": MIGRATION_ID,
        "checksum_sha256": migration.checksum_sha256,
        "expected_schema_fingerprint_sha256": (migration.expected_schema_fingerprint_sha256),
        "state": "applied",
        "applied_at": ledger["applied_at"],
    }
    assert ledger["applied_at"] is not None


async def test_migration_creates_exact_committed_pinned_schema(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    actual = await fetch_schema_projection(
        migration_resources.client,
        schema_id=POSSESSIONS_SCHEMA_ID,
        database="nba",
        table="possessions_v1",
    )
    expected = json.loads(
        (MIGRATIONS_DIR / "0001_possessions.schema.json").read_text(encoding="utf-8")
    )
    assert actual == expected


async def test_migration_creates_all_four_exact_committed_pinned_schemas(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    expected_by_schema = {
        POSSESSIONS_SCHEMA_ID: ("possessions_v1", "0001_possessions.schema.json"),
        LINEUP_SEGMENTS_SCHEMA_ID: ("lineup_segments_v1", "0002_lineup_segments.schema.json"),
        GAME_STATE_FEATURES_SCHEMA_ID: (
            "game_state_features_v1",
            "0003_game_state_features.schema.json",
        ),
        PLAYER_PREGAME_FEATURES_SCHEMA_ID: (
            "player_pregame_features_v1",
            "0004_player_pregame_features.schema.json",
        ),
    }
    for schema_id, (table, filename) in expected_by_schema.items():
        actual = await fetch_schema_projection(
            migration_resources.client,
            schema_id=schema_id,
            database="nba",
            table=table,
        )
        expected = json.loads((MIGRATIONS_DIR / filename).read_text(encoding="utf-8"))
        assert actual == expected
    assert (await _ledger(migration_resources.engine, MIGRATION_ID))["state"] == "applied"
    assert (await _ledger(migration_resources.engine, LINEUP_SEGMENTS_MIGRATION_ID))[
        "state"
    ] == "applied"
    assert (await _ledger(migration_resources.engine, GAME_STATE_FEATURES_MIGRATION_ID))[
        "state"
    ] == "applied"
    assert (await _ledger(migration_resources.engine, PLAYER_PREGAME_FEATURES_MIGRATION_ID))[
        "state"
    ] == "applied"


async def test_applied_replay_skips_ddl_but_rechecks_actual_schema(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    class ReplayObserver(DelegatingClient):
        def __init__(self, delegate: ClickHouseClient) -> None:
            super().__init__(delegate)
            self.executed: list[str] = []
            self.catalog_queries = 0

        async def execute(self, statement: str) -> None:
            self.executed.append(statement)
            await super().execute(statement)

        async def query(
            self,
            statement: str,
            *,
            parameters: Mapping[str, str | int] | None = None,
        ) -> list[dict[str, object]]:
            self.catalog_queries += 1
            return await super().query(statement, parameters=parameters)

    observer = ReplayObserver(migration_resources.client)
    await migrate_clickhouse(migration_resources.engine, observer)

    assert observer.executed == []
    assert observer.catalog_queries == 8
    assert (await _ledger(migration_resources.engine))["state"] == "applied"
    assert (await _ledger(migration_resources.engine, LINEUP_SEGMENTS_MIGRATION_ID))[
        "state"
    ] == "applied"


async def test_adding_feature_tables_preserves_possession_and_lineup_sentinels_and_projections(
    migration_resources: MigrationResources,
    tmp_path: Path,
) -> None:
    before_feature_tables = tmp_path / "before_feature_tables"
    before_feature_tables.mkdir()
    for filename in (
        "0001_possessions.sql",
        "0001_possessions.schema.json",
        "0002_lineup_segments.sql",
        "0002_lineup_segments.schema.json",
    ):
        shutil.copy(MIGRATIONS_DIR / filename, before_feature_tables / filename)

    await migrate_clickhouse(
        migration_resources.engine,
        migration_resources.client,
        migrations_dir=before_feature_tables,
    )
    possession_projection_before = await fetch_schema_projection(
        migration_resources.client,
        schema_id=POSSESSIONS_SCHEMA_ID,
        database="nba",
        table="possessions_v1",
    )
    lineup_projection_before = await fetch_schema_projection(
        migration_resources.client,
        schema_id=LINEUP_SEGMENTS_SCHEMA_ID,
        database="nba",
        table="lineup_segments_v1",
    )
    possession_snapshot_id = "00000000-0000-0000-0000-000000000012"
    lineup_snapshot_id = "00000000-0000-0000-0000-000000000014"
    await migration_resources.client.execute(
        "INSERT INTO nba.possessions_v1 "
        "(snapshot_id, insert_version, parse_revision_id, normalizer_version, parser_version, "
        "rules_version, input_pbp_document_id, input_pbp_sha256, input_event_sha256, "
        "game_context_sha256, output_schema_version, output_sha256, game_id, season, "
        "season_start_year, season_type, game_date, home_team_id, away_team_id, "
        "possession_number, offense_possession_number, period, offense_team_id, defense_team_id, "
        "start_sequence_number, start_boundary_side, end_sequence_number, end_boundary_side, "
        "start_seconds_remaining_period_ms, end_seconds_remaining_period_ms, "
        "seconds_remaining_game_ms, home_score_before, away_score_before, home_score, away_score, "
        "score_diff, offense_points, defense_points, home_possessions, away_possessions, "
        "end_reason, source_event_count) VALUES "
        "('00000000-0000-0000-0000-000000000012', 1, repeat('a', 64), 'test', 'test', "
        "'test', '00000000-0000-0000-0000-000000000013', repeat('b', 64), repeat('c', 64), "
        "repeat('d', 64), 'test', repeat('e', 64), '0029899012', '2098-99', 2098, "
        "'regular_season', '2098-10-01', 1, 2, 1, 1, 1, 1, 2, 1, 'before', 2, 'after', "
        "720000, 719000, 2880000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 'turnover', 1)"
    )
    await migration_resources.client.execute(
        "INSERT INTO nba.lineup_segments_v1 "
        "(snapshot_id, game_id, season_start_year) VALUES "
        "('00000000-0000-0000-0000-000000000014', '0029899012', 2098)"
    )

    await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    possession_sentinel_rows = await migration_resources.client.query(
        "SELECT count() AS count FROM nba.possessions_v1 WHERE snapshot_id = {snapshot_id:UUID}",
        parameters={"snapshot_id": possession_snapshot_id},
    )
    lineup_sentinel_rows = await migration_resources.client.query(
        "SELECT count() AS count FROM nba.lineup_segments_v1 "
        "WHERE snapshot_id = {snapshot_id:UUID}",
        parameters={"snapshot_id": lineup_snapshot_id},
    )
    assert possession_sentinel_rows == [{"count": 1}]
    assert lineup_sentinel_rows == [{"count": 1}]
    assert (
        await fetch_schema_projection(
            migration_resources.client,
            schema_id=POSSESSIONS_SCHEMA_ID,
            database="nba",
            table="possessions_v1",
        )
        == possession_projection_before
    )
    assert (
        await fetch_schema_projection(
            migration_resources.client,
            schema_id=LINEUP_SEGMENTS_SCHEMA_ID,
            database="nba",
            table="lineup_segments_v1",
        )
        == lineup_projection_before
    )
    assert (await _ledger(migration_resources.engine, MIGRATION_ID))["state"] == "applied"
    assert (await _ledger(migration_resources.engine, LINEUP_SEGMENTS_MIGRATION_ID))[
        "state"
    ] == "applied"
    assert (await _ledger(migration_resources.engine, GAME_STATE_FEATURES_MIGRATION_ID))[
        "state"
    ] == "applied"
    assert (await _ledger(migration_resources.engine, PLAYER_PREGAME_FEATURES_MIGRATION_ID))[
        "state"
    ] == "applied"


async def test_additive_upgrade_checks_only_latest_projection_on_first_apply_and_replay(
    migration_resources: MigrationResources,
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    for filename in ("0001_possessions.sql", "0001_possessions.schema.json"):
        shutil.copy(MIGRATIONS_DIR / filename, migration_dir / filename)

    upgrade_sql = (
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS migration_upgrade_marker UInt8;\n"
    )
    (migration_dir / "0002_additive_upgrade.sql").write_text(
        upgrade_sql,
        encoding="utf-8",
        newline="\n",
    )
    previous_schema = json.loads(
        (MIGRATIONS_DIR / "0001_possessions.schema.json").read_text(encoding="utf-8")
    )
    upgraded_schema = {
        **previous_schema,
        "columns": [
            *previous_schema["columns"],
            {
                "position": len(previous_schema["columns"]) + 1,
                "name": "migration_upgrade_marker",
                "type": "UInt8",
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": False,
                "is_in_sorting_key": False,
                "is_in_primary_key": False,
                "is_in_sampling_key": False,
            },
        ],
    }
    (migration_dir / "0002_additive_upgrade.schema.json").write_text(
        json.dumps(upgraded_schema, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )

    migrations = load_migrations(migration_dir)
    await migrate_clickhouse(
        migration_resources.engine,
        migration_resources.client,
        migrations_dir=migration_dir,
    )

    for migration in migrations:
        ledger = await _ledger(migration_resources.engine, migration.migration_id)
        assert ledger["checksum_sha256"] == migration.checksum_sha256
        assert (
            ledger["expected_schema_fingerprint_sha256"]
            == migration.expected_schema_fingerprint_sha256
        )
        assert ledger["state"] == "applied"

    await migrate_clickhouse(
        migration_resources.engine,
        migration_resources.client,
        migrations_dir=migration_dir,
    )
    assert (await _ledger(migration_resources.engine, "0001_possessions"))["state"] == "applied"
    assert (await _ledger(migration_resources.engine, "0002_additive_upgrade"))[
        "state"
    ] == "applied"


async def test_replay_validates_latest_head_for_every_known_schema(
    migration_resources: MigrationResources,
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    for filename in ("0001_possessions.sql", "0001_possessions.schema.json"):
        shutil.copy(MIGRATIONS_DIR / filename, migration_dir / filename)

    auxiliary_sql = f"""CREATE TABLE IF NOT EXISTS nba.{AUXILIARY_TABLE}
(
    game_id FixedString(10),
    lineup_slot UInt8
)
ENGINE = MergeTree
PARTITION BY game_id
ORDER BY (game_id);
"""
    (migration_dir / f"{AUXILIARY_MIGRATION_ID}.sql").write_text(
        auxiliary_sql,
        encoding="utf-8",
        newline="\n",
    )
    auxiliary_schema = {
        "schema": "clickhouse-migration-test-aux-schema-v1",
        "database": "nba",
        "table": AUXILIARY_TABLE,
        "table_contract": {
            "engine": "MergeTree",
            "engine_full": (
                "MergeTree PARTITION BY game_id ORDER BY game_id SETTINGS index_granularity = 8192"
            ),
            "partition_key": "game_id",
            "sorting_key": "game_id",
            "primary_key": "game_id",
            "sampling_key": "",
        },
        "columns": [
            {
                "position": 1,
                "name": "game_id",
                "type": "FixedString(10)",
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": True,
                "is_in_sorting_key": True,
                "is_in_primary_key": True,
                "is_in_sampling_key": False,
            },
            {
                "position": 2,
                "name": "lineup_slot",
                "type": "UInt8",
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": False,
                "is_in_sorting_key": False,
                "is_in_primary_key": False,
                "is_in_sampling_key": False,
            },
        ],
    }
    (migration_dir / f"{AUXILIARY_MIGRATION_ID}.schema.json").write_text(
        json.dumps(auxiliary_schema, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )

    previous_schema = json.loads(
        (MIGRATIONS_DIR / "0001_possessions.schema.json").read_text(encoding="utf-8")
    )
    upgrade_sql = (
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS migration_upgrade_marker UInt8;\n"
    )
    (migration_dir / f"{POSSESSION_UPGRADE_MIGRATION_ID}.sql").write_text(
        upgrade_sql,
        encoding="utf-8",
        newline="\n",
    )
    upgraded_schema = {
        **previous_schema,
        "columns": [
            *previous_schema["columns"],
            {
                "position": len(previous_schema["columns"]) + 1,
                "name": "migration_upgrade_marker",
                "type": "UInt8",
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": False,
                "is_in_sorting_key": False,
                "is_in_primary_key": False,
                "is_in_sampling_key": False,
            },
        ],
    }
    (migration_dir / f"{POSSESSION_UPGRADE_MIGRATION_ID}.schema.json").write_text(
        json.dumps(upgraded_schema, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )

    await migrate_clickhouse(
        migration_resources.engine,
        migration_resources.client,
        migrations_dir=migration_dir,
    )

    class ReplayObserver(DelegatingClient):
        def __init__(self, delegate: ClickHouseClient) -> None:
            super().__init__(delegate)
            self.catalog_parameters: list[Mapping[str, str | int] | None] = []

        async def query(
            self,
            statement: str,
            *,
            parameters: Mapping[str, str | int] | None = None,
        ) -> list[dict[str, object]]:
            if "system.tables" in statement or "system.columns" in statement:
                self.catalog_parameters.append(parameters)
            return await super().query(statement, parameters=parameters)

    observer = ReplayObserver(migration_resources.client)
    await migrate_clickhouse(
        migration_resources.engine,
        observer,
        migrations_dir=migration_dir,
    )

    assert observer.catalog_parameters == [
        {"database": "nba", "table": AUXILIARY_TABLE},
        {"database": "nba", "table": AUXILIARY_TABLE},
        {"database": "nba", "table": "possessions_v1"},
        {"database": "nba", "table": "possessions_v1"},
    ]


async def test_crash_after_ddl_leaves_applying_and_replay_finishes_safely(
    migration_resources: MigrationResources,
) -> None:
    class CrashAfterDdlClient(DelegatingClient):
        async def query(
            self,
            statement: str,
            *,
            parameters: Mapping[str, str | int] | None = None,
        ) -> list[dict[str, object]]:
            raise ClickHouseError("clickhouse_unavailable")

    with pytest.raises(ClickHouseError, match=r"^clickhouse_unavailable$"):
        await migrate_clickhouse(
            migration_resources.engine,
            CrashAfterDdlClient(migration_resources.client),
        )

    assert (await _ledger(migration_resources.engine))["state"] == "applying"
    assert await migration_resources.client.query(
        "SELECT name FROM system.tables "
        "WHERE database = {database:String} AND name = {table:String}",
        parameters={"database": "nba", "table": "possessions_v1"},
    ) == [{"name": "possessions_v1"}]

    await migrate_clickhouse(migration_resources.engine, migration_resources.client)
    assert (await _ledger(migration_resources.engine))["state"] == "applied"


async def test_applied_ledger_with_missing_table_fails_closed(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)
    await migration_resources.client.execute("DROP TABLE nba.possessions_v1")

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    assert captured.value.code == "clickhouse_schema_missing"
    assert (await _ledger(migration_resources.engine))["state"] == "applied"


async def test_applied_ledger_with_missing_lineup_segments_table_fails_closed(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)
    await migration_resources.client.execute("DROP TABLE nba.lineup_segments_v1")

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    assert captured.value.code == "clickhouse_schema_missing"
    assert (await _ledger(migration_resources.engine, LINEUP_SEGMENTS_MIGRATION_ID))[
        "state"
    ] == "applied"


async def test_applied_ledger_with_actual_schema_drift_fails_closed(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)
    await migration_resources.client.execute(
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS drift UInt8"
    )

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    assert captured.value.code == "clickhouse_schema_drift"
    assert (await _ledger(migration_resources.engine))["state"] == "applied"


async def test_ledger_checksum_and_expected_fingerprint_drift_fail_before_ddl(
    migration_resources: MigrationResources,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    for column, expected_code in (
        ("checksum_sha256", "clickhouse_migration_checksum_drift"),
        (
            "expected_schema_fingerprint_sha256",
            "clickhouse_migration_fingerprint_drift",
        ),
    ):
        async with migration_resources.engine.begin() as connection:
            await connection.execute(
                text(
                    f"UPDATE clickhouse_schema_migrations SET {column} = :value "
                    "WHERE migration_id = :migration_id"
                ),
                {"value": "a" * 64, "migration_id": MIGRATION_ID},
            )
        with pytest.raises(ClickHouseMigrationError) as captured:
            await migrate_clickhouse(migration_resources.engine, migration_resources.client)
        assert captured.value.code == expected_code
        async with migration_resources.engine.begin() as connection:
            migration = load_migrations()[0]
            await connection.execute(
                text(
                    f"UPDATE clickhouse_schema_migrations SET {column} = :value "
                    "WHERE migration_id = :migration_id"
                ),
                {
                    "value": (
                        migration.checksum_sha256
                        if column == "checksum_sha256"
                        else migration.expected_schema_fingerprint_sha256
                    ),
                    "migration_id": MIGRATION_ID,
                },
            )


async def test_changed_migration_bytes_are_fatal_checksum_drift(
    migration_resources: MigrationResources,
    tmp_path: Path,
) -> None:
    await migrate_clickhouse(migration_resources.engine, migration_resources.client)
    changed_dir = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, changed_dir)
    original = (MIGRATIONS_DIR / "0001_possessions.sql").read_text(encoding="utf-8")
    (changed_dir / "0001_possessions.sql").write_text(
        f"{original}\n-- changed bytes\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(
            migration_resources.engine,
            migration_resources.client,
            migrations_dir=changed_dir,
        )

    assert captured.value.code == "clickhouse_migration_checksum_drift"


async def test_database_ahead_of_available_forward_only_migrations_fails_closed(
    migration_resources: MigrationResources,
) -> None:
    async with migration_resources.engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO clickhouse_schema_migrations "
                "(migration_id, checksum_sha256, expected_schema_fingerprint_sha256, "
                "state, applied_at) VALUES (:migration_id, :checksum, :fingerprint, "
                "'applied', now())"
            ),
            {
                "migration_id": "9999_future",
                "checksum": "a" * 64,
                "fingerprint": "b" * 64,
            },
        )

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(migration_resources.engine, migration_resources.client)

    assert captured.value.code == "clickhouse_migration_set_drift"


async def test_fastapi_resource_startup_does_not_apply_clickhouse_migrations(
    migration_resources: MigrationResources,
) -> None:
    resources = await create_resources(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=AnyUrl(runtime_config.postgres_url()),
            clickhouse_url=HttpUrl(runtime_config.clickhouse_url()),
            clickhouse_user="nba",
            clickhouse_password=SecretStr("nba"),
        )
    )
    await close_resources(resources)

    assert (
        await migration_resources.client.query(
            "SELECT name FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String}",
            parameters={"database": "nba", "table": "possessions_v1"},
        )
        == []
    )
