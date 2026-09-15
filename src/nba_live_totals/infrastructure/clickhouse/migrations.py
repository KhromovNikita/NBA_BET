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
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError

STATEMENT_DELIMITER = "-- migration-statement-break"
DEFAULT_MIGRATIONS_DIR = Path(__file__).parents[4] / "clickhouse" / "migrations"
MIGRATION_ADVISORY_LOCK_KEY = 0x4E424143484D4947
POSSESSIONS_SCHEMA_ID = "clickhouse-possession-schema-v1"
_SCHEMA_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}")
_CLICKHOUSE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


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
    schema_id: str
    database: str
    table: str
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
    tables_by_schema_id: dict[str, tuple[str, str]] = {}
    schema_ids_by_table: dict[tuple[str, str], str] = {}
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
        schema_id = expected_schema.get("schema")
        database = expected_schema.get("database")
        table = expected_schema.get("table")
        if (
            type(schema_id) is not str
            or _SCHEMA_ID_PATTERN.fullmatch(schema_id) is None
            or type(database) is not str
            or _CLICKHOUSE_IDENTIFIER_PATTERN.fullmatch(database) is None
            or type(table) is not str
            or _CLICKHOUSE_IDENTIFIER_PATTERN.fullmatch(table) is None
        ):
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        statements = split_migration_statements(statement_text)
        _validate_migration_targets(statements, database=database, table=table)
        table_key = (database, table)
        if tables_by_schema_id.setdefault(schema_id, table_key) != table_key:
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        if schema_ids_by_table.setdefault(table_key, schema_id) != schema_id:
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        migrations.append(
            ClickHouseMigration(
                migration_id=migration_id,
                schema_id=schema_id,
                database=database,
                table=table,
                statements=statements,
                checksum_sha256=hashlib.sha256(contents).hexdigest(),
                expected_schema=expected_schema,
                expected_schema_fingerprint_sha256=schema_fingerprint(expected_schema),
            )
        )
    return tuple(migrations)


def _validate_migration_targets(
    statements: tuple[str, ...],
    *,
    database: str,
    table: str,
) -> None:
    has_table_statement = False
    for statement in statements:
        database_match = re.fullmatch(
            r"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
            statement,
            re.IGNORECASE,
        )
        if database_match is not None:
            if database_match.group(1) != database:
                raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
            continue
        table_match = re.match(
            r"(?:CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|ALTER\s+TABLE)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            statement,
            re.IGNORECASE,
        )
        if table_match is None or table_match.groups() != (database, table):
            raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
        has_table_statement = True
    if not has_table_statement:
        raise ClickHouseMigrationError("clickhouse_migration_set_invalid")


def split_migration_statements(contents: str) -> tuple[str, ...]:
    pieces = contents.split(STATEMENT_DELIMITER)
    statements: list[str] = []
    for piece in pieces:
        statement = piece.strip()
        comment = re.search(r"(?m)(?:^|\s)--", statement)
        if comment is not None:
            suffix = statement[comment.end() - 2 :]
            if any(
                line.strip() and not line.lstrip().startswith("--") for line in suffix.splitlines()
            ):
                raise ClickHouseMigrationError("clickhouse_migration_invalid")
            statement = statement[: comment.start()].rstrip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        if not statement or ";" in statement or re.search(r"/\*|\*/", statement):
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
    schema_id: str,
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
        "schema": schema_id,
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


async def validate_schema(
    engine: AsyncEngine,
    client: SchemaQueryClient,
    *,
    schema_id: str,
    migrations_dir: Path | None = None,
) -> None:
    migrations = load_migrations(migrations_dir)
    selected = [migration for migration in migrations if migration.schema_id == schema_id]
    if not selected:
        raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
    head = selected[-1]
    if (
        type(head.schema_id) is not str
        or _SCHEMA_ID_PATTERN.fullmatch(head.schema_id) is None
        or type(head.database) is not str
        or _CLICKHOUSE_IDENTIFIER_PATTERN.fullmatch(head.database) is None
        or type(head.table) is not str
        or _CLICKHOUSE_IDENTIFIER_PATTERN.fullmatch(head.table) is None
    ):
        raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT migration_id, checksum_sha256, "
                    "expected_schema_fingerprint_sha256, state "
                    "FROM clickhouse_schema_migrations "
                    "WHERE migration_id = :migration_id"
                ),
                {"migration_id": head.migration_id},
            )
            rows = [dict(row) for row in result.mappings().all()]
    except SQLAlchemyError:
        raise ClickHouseMigrationError("clickhouse_migration_ledger_unavailable") from None
    if len(rows) != 1 or rows[0].get("migration_id") != head.migration_id:
        raise ClickHouseMigrationError("clickhouse_migration_set_drift")
    row = rows[0]
    if row.get("checksum_sha256") != head.checksum_sha256:
        raise ClickHouseMigrationError("clickhouse_migration_checksum_drift")
    if row.get("expected_schema_fingerprint_sha256") != head.expected_schema_fingerprint_sha256:
        raise ClickHouseMigrationError("clickhouse_migration_fingerprint_drift")
    if row.get("state") != "applied":
        raise ClickHouseMigrationError("clickhouse_schema_not_applied")
    try:
        actual = await fetch_schema_projection(
            client,
            schema_id=head.schema_id,
            database=head.database,
            table=head.table,
        )
    except ClickHouseError:
        raise ClickHouseMigrationError("clickhouse_schema_unavailable") from None
    if schema_fingerprint(actual) != head.expected_schema_fingerprint_sha256:
        raise ClickHouseMigrationError("clickhouse_schema_drift")


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

    latest_by_schema_id = {migration.schema_id: migration for migration in migrations}
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

        if latest_by_schema_id[migration.schema_id] is migration:
            actual_schema = await fetch_schema_projection(
                client,
                schema_id=migration.schema_id,
                database=migration.database,
                table=migration.table,
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
    "POSSESSIONS_SCHEMA_ID",
    "STATEMENT_DELIMITER",
    "ClickHouseMigration",
    "ClickHouseMigrationError",
    "fetch_schema_projection",
    "load_migrations",
    "migrate_clickhouse",
    "schema_fingerprint",
    "split_migration_statements",
    "validate_schema",
]
