from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.migrations import (
    STATEMENT_DELIMITER,
    ClickHouseMigrationError,
    fetch_schema_projection,
    load_migrations,
    migrate_clickhouse,
    schema_fingerprint,
    split_migration_statements,
)

PROJECT_ROOT = Path(__file__).parents[4]
MIGRATIONS_DIR = PROJECT_ROOT / "clickhouse" / "migrations"
MIGRATION_PATH = MIGRATIONS_DIR / "0001_possessions.sql"
SCHEMA_PATH = MIGRATIONS_DIR / "0001_possessions.schema.json"
EXPECTED_MIGRATION_SHA256 = "696cf578c69b8aaf779db7ffab8eaf0be7f6a97b6694f653a1727805e3ef3496"
EXPECTED_SCHEMA_FINGERPRINT_SHA256 = (
    "4e15c5b6a2b6aa061df3d6660673940f8ba1fbe1c26669e9f07eb141a2ff15f4"
)

EXPECTED_MIGRATION_TEXT = """CREATE DATABASE IF NOT EXISTS nba;

-- migration-statement-break
CREATE TABLE IF NOT EXISTS nba.possessions_v1
(
    snapshot_id UUID,
    insert_version UInt64,
    parse_revision_id FixedString(64),
    normalizer_version LowCardinality(String),
    parser_version LowCardinality(String),
    rules_version LowCardinality(String),
    input_pbp_document_id UUID,
    input_pbp_sha256 FixedString(64),
    input_event_sha256 FixedString(64),
    game_context_sha256 FixedString(64),
    output_schema_version LowCardinality(String),
    output_sha256 FixedString(64),

    game_id FixedString(10),
    season FixedString(7),
    season_start_year UInt16,
    season_type Enum8('regular_season' = 1, 'playoffs' = 2),
    game_date Date,
    home_team_id UInt64,
    away_team_id UInt64,

    possession_number UInt16,
    offense_possession_number UInt16,
    period UInt16,
    offense_team_id UInt64,
    defense_team_id UInt64,

    start_sequence_number UInt32,
    start_boundary_side Enum8('before' = 1, 'after' = 2),
    end_sequence_number UInt32,
    end_boundary_side Enum8('before' = 1, 'after' = 2),
    start_seconds_remaining_period_ms UInt32,
    end_seconds_remaining_period_ms UInt32,
    seconds_remaining_game_ms UInt32,

    home_score_before UInt16,
    away_score_before UInt16,
    home_score UInt16,
    away_score UInt16,
    score_diff Int16,
    offense_points UInt16,
    defense_points UInt16,
    home_possessions UInt16,
    away_possessions UInt16,

    end_reason Enum8(
        'made_field_goal' = 1,
        'defensive_rebound' = 2,
        'turnover' = 3,
        'final_free_throw' = 4,
        'period_end' = 5,
        'jump_ball_change' = 6,
        'control_award' = 7
    ),
    source_event_count UInt16
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, possession_number);
"""

EXPECTED_COLUMNS = [
    ("snapshot_id", "UUID"),
    ("insert_version", "UInt64"),
    ("parse_revision_id", "FixedString(64)"),
    ("normalizer_version", "LowCardinality(String)"),
    ("parser_version", "LowCardinality(String)"),
    ("rules_version", "LowCardinality(String)"),
    ("input_pbp_document_id", "UUID"),
    ("input_pbp_sha256", "FixedString(64)"),
    ("input_event_sha256", "FixedString(64)"),
    ("game_context_sha256", "FixedString(64)"),
    ("output_schema_version", "LowCardinality(String)"),
    ("output_sha256", "FixedString(64)"),
    ("game_id", "FixedString(10)"),
    ("season", "FixedString(7)"),
    ("season_start_year", "UInt16"),
    ("season_type", "Enum8('regular_season' = 1, 'playoffs' = 2)"),
    ("game_date", "Date"),
    ("home_team_id", "UInt64"),
    ("away_team_id", "UInt64"),
    ("possession_number", "UInt16"),
    ("offense_possession_number", "UInt16"),
    ("period", "UInt16"),
    ("offense_team_id", "UInt64"),
    ("defense_team_id", "UInt64"),
    ("start_sequence_number", "UInt32"),
    ("start_boundary_side", "Enum8('before' = 1, 'after' = 2)"),
    ("end_sequence_number", "UInt32"),
    ("end_boundary_side", "Enum8('before' = 1, 'after' = 2)"),
    ("start_seconds_remaining_period_ms", "UInt32"),
    ("end_seconds_remaining_period_ms", "UInt32"),
    ("seconds_remaining_game_ms", "UInt32"),
    ("home_score_before", "UInt16"),
    ("away_score_before", "UInt16"),
    ("home_score", "UInt16"),
    ("away_score", "UInt16"),
    ("score_diff", "Int16"),
    ("offense_points", "UInt16"),
    ("defense_points", "UInt16"),
    ("home_possessions", "UInt16"),
    ("away_possessions", "UInt16"),
    (
        "end_reason",
        "Enum8('made_field_goal' = 1, 'defensive_rebound' = 2, 'turnover' = 3, "
        "'final_free_throw' = 4, 'period_end' = 5, 'jump_ball_change' = 6, "
        "'control_award' = 7)",
    ),
    ("source_event_count", "UInt16"),
]

EXPECTED_TABLE_CONTRACT = {
    "engine": "ReplacingMergeTree",
    "engine_full": (
        "ReplacingMergeTree(insert_version) PARTITION BY season_start_year "
        "ORDER BY (game_id, snapshot_id, possession_number) "
        "SETTINGS index_granularity = 8192"
    ),
    "partition_key": "season_start_year",
    "sorting_key": "game_id, snapshot_id, possession_number",
    "primary_key": "game_id, snapshot_id, possession_number",
    "sampling_key": "",
}


def _expected_projection() -> dict[str, Any]:
    partition = {"season_start_year"}
    sorting = {"game_id", "snapshot_id", "possession_number"}
    return {
        "schema": "clickhouse-possession-schema-v1",
        "database": "nba",
        "table": "possessions_v1",
        "table_contract": EXPECTED_TABLE_CONTRACT,
        "columns": [
            {
                "position": position,
                "name": name,
                "type": column_type,
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": name in partition,
                "is_in_sorting_key": name in sorting,
                "is_in_primary_key": name in sorting,
                "is_in_sampling_key": False,
            }
            for position, (name, column_type) in enumerate(EXPECTED_COLUMNS, start=1)
        ],
    }


def test_committed_projection_is_the_exact_pinned_possession_schema() -> None:
    projection = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert projection == _expected_projection()
    assert all(not column["type"].startswith("Nullable(") for column in projection["columns"])


def test_committed_projection_has_exact_canonical_fingerprint() -> None:
    projection = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema_fingerprint(projection) == EXPECTED_SCHEMA_FINGERPRINT_SHA256


def test_migration_bytes_and_loaded_metadata_have_fixed_checksums() -> None:
    migrations = load_migrations(MIGRATIONS_DIR)

    assert len(migrations) == 1
    migration = migrations[0]
    assert migration.migration_id == "0001_possessions"
    assert migration.checksum_sha256 == EXPECTED_MIGRATION_SHA256
    assert migration.expected_schema == _expected_projection()
    assert migration.expected_schema_fingerprint_sha256 == EXPECTED_SCHEMA_FINGERPRINT_SHA256


def test_migration_uses_explicit_delimiter_and_independently_idempotent_statements() -> None:
    contents = MIGRATION_PATH.read_text(encoding="utf-8")

    assert contents == EXPECTED_MIGRATION_TEXT
    assert contents.count(STATEMENT_DELIMITER) == 1
    statements = split_migration_statements(contents)
    assert len(statements) == 2
    assert statements[0] == "CREATE DATABASE IF NOT EXISTS nba"
    assert statements[1].startswith("CREATE TABLE IF NOT EXISTS nba.possessions_v1")
    assert "ENGINE = ReplacingMergeTree(insert_version)" in statements[1]
    assert "PARTITION BY season_start_year" in statements[1]
    assert "ORDER BY (game_id, snapshot_id, possession_number)" in statements[1]


def test_split_rejects_empty_or_non_idempotent_statements() -> None:
    invalid_migrations = (
        "",
        "CREATE DATABASE nba",
        f"CREATE DATABASE IF NOT EXISTS nba\n{STATEMENT_DELIMITER}\n",
        f"DROP TABLE nba.old\n{STATEMENT_DELIMITER}\nCREATE DATABASE IF NOT EXISTS nba",
    )

    for contents in invalid_migrations:
        with pytest.raises(ClickHouseMigrationError) as captured:
            split_migration_statements(contents)
        assert captured.value.code == "clickhouse_migration_invalid"


@pytest.mark.parametrize(
    "contents",
    (
        "CREATE DATABASE IF NOT EXISTS nba; DROP DATABASE nba",
        "CREATE DATABASE IF NOT EXISTS nba; -- trailing command\nDROP DATABASE nba",
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS marker UInt8, DROP COLUMN source",
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS marker UInt8 DROP COLUMN source",
        "ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS marker UInt8; "
        "DROP TABLE nba.possessions_v1",
    ),
)
def test_split_rejects_statement_smuggling_and_compound_alter(contents: str) -> None:
    with pytest.raises(ClickHouseMigrationError) as captured:
        split_migration_statements(contents)

    assert captured.value.code == "clickhouse_migration_invalid"


class RecordingSchemaClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str | int] | None]] = []

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append((statement, parameters))
        if "system.tables" in statement:
            return [cast(dict[str, object], EXPECTED_TABLE_CONTRACT.copy())]
        return [
            {
                "position": position,
                "name": name,
                "type": column_type,
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": int(name == "season_start_year"),
                "is_in_sorting_key": int(name in {"game_id", "snapshot_id", "possession_number"}),
                "is_in_primary_key": int(name in {"game_id", "snapshot_id", "possession_number"}),
                "is_in_sampling_key": 0,
            }
            for position, (name, column_type) in enumerate(EXPECTED_COLUMNS, start=1)
        ]


async def test_schema_projection_queries_system_catalogs_with_parameters_and_types_bools() -> None:
    client = RecordingSchemaClient()

    projection = await fetch_schema_projection(
        client,
        database="nba'; DROP DATABASE nba; --",
        table="possessions_v1",
    )

    assert projection["database"] == "nba'; DROP DATABASE nba; --"
    assert projection["table"] == "possessions_v1"
    assert projection["table_contract"] == EXPECTED_TABLE_CONTRACT
    assert projection["columns"][0]["is_in_sorting_key"] is True
    assert projection["columns"][1]["is_in_sorting_key"] is False
    assert len(client.calls) == 2
    assert all(
        parameters == {"database": "nba'; DROP DATABASE nba; --", "table": "possessions_v1"}
        for _, parameters in client.calls
    )
    assert all("DROP" not in statement for statement, _ in client.calls)


async def test_missing_table_fails_closed_with_safe_schema_code() -> None:
    class MissingSchemaClient(RecordingSchemaClient):
        async def query(
            self,
            statement: str,
            *,
            parameters: Mapping[str, str | int] | None = None,
        ) -> list[dict[str, object]]:
            return []

    with pytest.raises(ClickHouseMigrationError) as captured:
        await fetch_schema_projection(
            MissingSchemaClient(),
            database="nba",
            table="possessions_v1",
        )

    assert captured.value.code == "clickhouse_schema_missing"


async def test_postgres_failures_map_without_connection_or_sql_leakage() -> None:
    secret = "postgresql+asyncpg://secret-user:secret-password@database/nba"

    class BrokenConnectionContext:
        async def __aenter__(self) -> None:
            raise SQLAlchemyError(f"could not connect to {secret}; SELECT secret_sql")

        async def __aexit__(self, *args: object) -> None:
            return None

    class BrokenEngine:
        def connect(self) -> BrokenConnectionContext:
            return BrokenConnectionContext()

    unused_client = cast(ClickHouseClient, object())
    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse(cast(AsyncEngine, BrokenEngine()), unused_client)

    assert captured.value.code == "clickhouse_migration_ledger_unavailable"
    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in formatted
    assert "secret-password" not in formatted
    assert "secret_sql" not in formatted
