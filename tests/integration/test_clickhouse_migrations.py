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
    MIGRATION_ADVISORY_LOCK_KEY,
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
LINEUP_MIGRATION_ID = "0002_lineups"
POSSESSION_UPGRADE_MIGRATION_ID = "0003_possessions_upgrade"


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
        await _require_empty_possession_store(resources.client)
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


async def _require_empty_possession_store(client: ClickHouseClient) -> None:
    table = await client.query(
        "SELECT name FROM system.tables "
        "WHERE database = {database:String} AND name = {table:String}",
        parameters={"database": "nba", "table": "possessions_v1"},
    )
    if not table:
        return
    rows = await client.query("SELECT count() AS count FROM nba.possessions_v1")
    if rows != [{"count": 0}]:
        pytest.fail(
            "refusing destructive ClickHouse migration tests: nba.possessions_v1 is not empty"
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


class _GuardEngine:
    def __init__(self) -> None:
        self.disposed = False

    def begin(self) -> object:
        raise AssertionError("database reset started after guard rejection")

    async def dispose(self) -> None:
        self.disposed = True


async def test_destructive_migration_guard_refuses_nonempty_store() -> None:
    client = cast(ClickHouseClient, _NonEmptyClickHouse())

    with pytest.raises(pytest.fail.Exception, match="not empty"):
        await _require_empty_possession_store(client)


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


async def _reset(resources: MigrationResources) -> None:
    await resources.client.execute("DROP TABLE IF EXISTS nba.possessions_v1")
    await resources.client.execute("DROP TABLE IF EXISTS nba.lineups_v1")
    async with resources.engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM clickhouse_schema_migrations "
                "WHERE migration_id IN "
                "(:current, :future, :upgrade, :lineups, :possession_upgrade)"
            ),
            {
                "current": MIGRATION_ID,
                "future": "9999_future",
                "upgrade": "0002_additive_upgrade",
                "lineups": LINEUP_MIGRATION_ID,
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

    assert observer.lock_attempts == [False, False]
    assert observer.ledger_states == ["applying", "applying"]
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
    assert observer.catalog_queries == 2
    assert (await _ledger(migration_resources.engine))["state"] == "applied"


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

    lineup_sql = """CREATE TABLE IF NOT EXISTS nba.lineups_v1
(
    game_id FixedString(10),
    lineup_slot UInt8
)
ENGINE = MergeTree
PARTITION BY game_id
ORDER BY (game_id);
"""
    (migration_dir / f"{LINEUP_MIGRATION_ID}.sql").write_text(
        lineup_sql,
        encoding="utf-8",
        newline="\n",
    )
    lineup_schema = {
        "schema": "clickhouse-lineup-schema-v1",
        "database": "nba",
        "table": "lineups_v1",
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
    (migration_dir / f"{LINEUP_MIGRATION_ID}.schema.json").write_text(
        json.dumps(lineup_schema, separators=(",", ":")),
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
        {"database": "nba", "table": "lineups_v1"},
        {"database": "nba", "table": "lineups_v1"},
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
    changed_dir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_possessions.schema.json", changed_dir)
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
