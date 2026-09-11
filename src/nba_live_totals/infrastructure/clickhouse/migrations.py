from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nba_live_totals.domain.canonical import canonical_json_sha256

STATEMENT_DELIMITER = "-- migration-statement-break"
DEFAULT_MIGRATIONS_DIR = Path(__file__).parents[4] / "clickhouse" / "migrations"
MIGRATION_ADVISORY_LOCK_KEY = 0x4E424143484D4947


class ClickHouseMigrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SchemaQueryClient(Protocol):
    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]: ...


class MigrationClient(SchemaQueryClient, Protocol):
    async def execute(self, statement: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ClickHouseMigration:
    migration_id: str
    statements: tuple[str, ...]
    checksum_sha256: str
    expected_schema: dict[str, Any]
    expected_schema_fingerprint_sha256: str


def load_migrations(migrations_dir: Path | None = None) -> tuple[ClickHouseMigration, ...]:
    directory = migrations_dir or DEFAULT_MIGRATIONS_DIR
    sql_paths = sorted(directory.glob("*.sql"))
    schema_paths = sorted(directory.glob("*.schema.json"))
    if not sql_paths or {path.stem for path in sql_paths} != {
        path.name.removesuffix(".schema.json") for path in schema_paths
    }:
        raise ClickHouseMigrationError("clickhouse_migration_set_invalid")

    migrations: list[ClickHouseMigration] = []
    for sql_path in sql_paths:
        migration_id = sql_path.stem
        if re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", migration_id) is None:
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        schema_path = directory / f"{migration_id}.schema.json"
        try:
            contents = sql_path.read_bytes()
            statement_text = contents.decode("utf-8")
            expected_schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid") from None
        if type(expected_schema_value) is not dict:
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        expected_schema = expected_schema_value
        migrations.append(
            ClickHouseMigration(
                migration_id=migration_id,
                statements=split_migration_statements(statement_text),
                checksum_sha256=hashlib.sha256(contents).hexdigest(),
                expected_schema=expected_schema,
                expected_schema_fingerprint_sha256=schema_fingerprint(expected_schema),
            )
        )
    return tuple(migrations)


def split_migration_statements(contents: str) -> tuple[str, ...]:
    pieces = contents.split(STATEMENT_DELIMITER)
    statements: list[str] = []
    for piece in pieces:
        statement = piece.strip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        if not statement or ";" in statement or re.search(r"--|/\*|\*/", statement):
            raise ClickHouseMigrationError("clickhouse_migration_invalid")
        if not _is_safe_migration_statement(statement):
            raise ClickHouseMigrationError("clickhouse_migration_invalid")
        statements.append(statement)
    return tuple(statements)


def _is_safe_migration_statement(statement: str) -> bool:
    """Accept only the reviewed, independently idempotent DDL forms."""

    if re.fullmatch(
        r"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+[A-Za-z_][A-Za-z0-9_]*",
        statement,
        re.IGNORECASE,
    ):
        return True
    if re.fullmatch(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
        r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\s*"
        r"\([\s\S]+\)\s*"
        r"ENGINE\s*=\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^;]*\))?\s*"
        r"PARTITION\s+BY\s+[A-Za-z_][A-Za-z0-9_]*\s*"
        r"ORDER\s+BY\s*\(\s*[A-Za-z_][A-Za-z0-9_]*"
        r"(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*\s*\)",
        statement,
        re.IGNORECASE,
    ):
        return True
    return bool(
        re.fullmatch(
            r"ALTER\s+TABLE\s+[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\s+"
            r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"[A-Za-z_][A-Za-z0-9_]*\s+"
            r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^;]*\))?",
            statement,
            re.IGNORECASE,
        )
    )


def schema_fingerprint(projection: dict[str, Any]) -> str:
    return canonical_json_sha256(projection)


async def fetch_schema_projection(
    client: SchemaQueryClient,
    *,
    database: str,
    table: str,
) -> dict[str, Any]:
    parameters = {"database": database, "table": table}
    table_rows = await client.query(
        "SELECT engine, engine_full, partition_key, sorting_key, primary_key, sampling_key "
        "FROM system.tables "
        "WHERE database = {database:String} AND name = {table:String}",
        parameters=parameters,
    )
    if not table_rows:
        raise ClickHouseMigrationError("clickhouse_schema_missing")
    if len(table_rows) != 1:
        raise ClickHouseMigrationError("clickhouse_schema_invalid")
    column_rows = await client.query(
        "SELECT position, name, type, default_kind, default_expression, "
        "is_in_partition_key, is_in_sorting_key, is_in_primary_key, is_in_sampling_key "
        "FROM system.columns "
        "WHERE database = {database:String} AND table = {table:String} "
        "ORDER BY position",
        parameters=parameters,
    )
    if not column_rows:
        raise ClickHouseMigrationError("clickhouse_schema_missing")
    try:
        table_contract = {
            key: _exact_string(table_rows[0], key)
            for key in (
                "engine",
                "engine_full",
                "partition_key",
                "sorting_key",
                "primary_key",
                "sampling_key",
            )
        }
        columns = [
            {
                "position": _exact_int(row, "position"),
                "name": _exact_string(row, "name"),
                "type": _exact_string(row, "type"),
                "default_kind": _exact_string(row, "default_kind"),
                "default_expression": _exact_string(row, "default_expression"),
                "is_in_partition_key": _exact_bool(row, "is_in_partition_key"),
                "is_in_sorting_key": _exact_bool(row, "is_in_sorting_key"),
                "is_in_primary_key": _exact_bool(row, "is_in_primary_key"),
                "is_in_sampling_key": _exact_bool(row, "is_in_sampling_key"),
            }
            for row in column_rows
        ]
    except (KeyError, TypeError, ValueError):
        raise ClickHouseMigrationError("clickhouse_schema_invalid") from None
    if [column["position"] for column in columns] != list(range(1, len(columns) + 1)):
        raise ClickHouseMigrationError("clickhouse_schema_invalid")
    return {
        "schema": "clickhouse-possession-schema-v1",
        "database": database,
        "table": table,
        "table_contract": table_contract,
        "columns": columns,
    }


async def migrate_clickhouse(
    engine: AsyncEngine,
    client: MigrationClient,
    *,
    migrations_dir: Path | None = None,
) -> None:
    migrations = load_migrations(migrations_dir)
    try:
        async with engine.connect() as connection:
            await _acquire_lock(connection)
            try:
                await _migrate_locked(connection, client, migrations)
            finally:
                active_error = sys.exception()
                try:
                    await _release_lock(connection)
                except SQLAlchemyError:
                    if active_error is None:
                        raise ClickHouseMigrationError(
                            "clickhouse_migration_ledger_unavailable"
                        ) from None
    except SQLAlchemyError:
        raise ClickHouseMigrationError("clickhouse_migration_ledger_unavailable") from None


def _exact_string(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError
    return value


def _exact_int(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError
    return value


def _exact_bool(row: dict[str, object], key: str) -> bool:
    value = row[key]
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    raise TypeError


async def _acquire_lock(connection: AsyncConnection) -> None:
    await connection.execute(
        text("SELECT pg_advisory_lock(:lock_key)"),
        {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
    )
    await connection.commit()


async def _release_lock(connection: AsyncConnection) -> None:
    if connection.in_transaction():
        await connection.rollback()
    await connection.execute(
        text("SELECT pg_advisory_unlock(:lock_key)"),
        {"lock_key": MIGRATION_ADVISORY_LOCK_KEY},
    )
    await connection.commit()


async def _migrate_locked(
    connection: AsyncConnection,
    client: MigrationClient,
    migrations: tuple[ClickHouseMigration, ...],
) -> None:
    result = await connection.execute(
        text(
            "SELECT migration_id, checksum_sha256, "
            "expected_schema_fingerprint_sha256, state "
            "FROM clickhouse_schema_migrations ORDER BY migration_id"
        )
    )
    ledger = {str(row["migration_id"]): dict(row) for row in result.mappings()}
    await connection.commit()
    known_ids = {migration.migration_id for migration in migrations}
    if set(ledger) - known_ids:
        raise ClickHouseMigrationError("clickhouse_migration_set_drift")

    latest_migration_id = migrations[-1].migration_id
    for migration in migrations:
        row = ledger.get(migration.migration_id)
        if row is not None:
            if row["checksum_sha256"] != migration.checksum_sha256:
                raise ClickHouseMigrationError("clickhouse_migration_checksum_drift")
            if (
                row["expected_schema_fingerprint_sha256"]
                != migration.expected_schema_fingerprint_sha256
            ):
                raise ClickHouseMigrationError("clickhouse_migration_fingerprint_drift")
        is_applied = row is not None and row["state"] == "applied"
        if not is_applied:
            async with connection.begin():
                await connection.execute(
                    text(
                        "INSERT INTO clickhouse_schema_migrations "
                        "(migration_id, checksum_sha256, "
                        "expected_schema_fingerprint_sha256, state, applied_at) "
                        "VALUES (:migration_id, :checksum, :fingerprint, 'applying', NULL) "
                        "ON CONFLICT (migration_id) DO UPDATE SET "
                        "checksum_sha256 = EXCLUDED.checksum_sha256, "
                        "expected_schema_fingerprint_sha256 = "
                        "EXCLUDED.expected_schema_fingerprint_sha256, "
                        "state = 'applying', applied_at = NULL"
                    ),
                    {
                        "migration_id": migration.migration_id,
                        "checksum": migration.checksum_sha256,
                        "fingerprint": migration.expected_schema_fingerprint_sha256,
                    },
                )
            for statement in migration.statements:
                await client.execute(statement)

        if migration.migration_id == latest_migration_id:
            expected_database = migration.expected_schema.get("database")
            expected_table = migration.expected_schema.get("table")
            if type(expected_database) is not str or type(expected_table) is not str:
                raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
            actual_schema = await fetch_schema_projection(
                client,
                database=expected_database,
                table=expected_table,
            )
            if schema_fingerprint(actual_schema) != migration.expected_schema_fingerprint_sha256:
                raise ClickHouseMigrationError("clickhouse_schema_drift")

        if not is_applied:
            async with connection.begin():
                result = await connection.execute(
                    text(
                        "UPDATE clickhouse_schema_migrations "
                        "SET state = 'applied', applied_at = now() "
                        "WHERE migration_id = :migration_id AND state = 'applying'"
                    ),
                    {"migration_id": migration.migration_id},
                )
                if result.rowcount != 1:
                    raise ClickHouseMigrationError("clickhouse_migration_ledger_invalid")


__all__ = [
    "DEFAULT_MIGRATIONS_DIR",
    "MIGRATION_ADVISORY_LOCK_KEY",
    "STATEMENT_DELIMITER",
    "ClickHouseMigration",
    "ClickHouseMigrationError",
    "fetch_schema_projection",
    "load_migrations",
    "migrate_clickhouse",
    "schema_fingerprint",
    "split_migration_statements",
]
