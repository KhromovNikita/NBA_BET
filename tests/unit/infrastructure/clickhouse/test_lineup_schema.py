from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nba_live_totals.infrastructure.clickhouse.migrations import (
    LINEUP_SEGMENTS_SCHEMA_ID,
    load_migrations,
    schema_fingerprint,
    split_migration_statements,
)

PROJECT_ROOT = Path(__file__).parents[4]
MIGRATIONS_DIR = PROJECT_ROOT / "clickhouse" / "migrations"
MIGRATION_PATH = MIGRATIONS_DIR / "0002_lineup_segments.sql"
SCHEMA_PATH = MIGRATIONS_DIR / "0002_lineup_segments.schema.json"

EXPECTED_COLUMNS = [
    ("snapshot_id", "UUID"),
    ("insert_version", "UInt64"),
    ("lineup_revision_id", "FixedString(64)"),
    ("normalizer_version", "LowCardinality(String)"),
    ("reconstructor_version", "LowCardinality(String)"),
    ("rules_version", "LowCardinality(String)"),
    ("input_box_score_document_id", "UUID"),
    ("input_box_score_sha256", "FixedString(64)"),
    ("input_pbp_document_id", "UUID"),
    ("input_pbp_sha256", "FixedString(64)"),
    ("canonical_player_box_sha256", "FixedString(64)"),
    ("canonical_event_sha256", "FixedString(64)"),
    ("game_context_sha256", "FixedString(64)"),
    ("output_schema_version", "LowCardinality(String)"),
    ("output_sha256", "FixedString(64)"),
    ("result_status", "Enum8('valid' = 1, 'invalid' = 2)"),
    ("game_id", "FixedString(10)"),
    ("season", "FixedString(7)"),
    ("season_start_year", "UInt16"),
    ("season_type", "Enum8('regular_season' = 1, 'playoffs' = 2)"),
    ("game_date", "Date"),
    ("home_team_id", "UInt64"),
    ("away_team_id", "UInt64"),
    ("segment_number", "UInt32"),
    ("start_sequence_number", "UInt32"),
    ("start_boundary_side", "Enum8('before' = 1, 'after' = 2)"),
    ("end_sequence_number", "UInt32"),
    ("end_boundary_side", "Enum8('before' = 1, 'after' = 2)"),
    ("start_period", "UInt16"),
    ("end_period", "UInt16"),
    ("start_seconds_remaining_period_ms", "UInt32"),
    ("end_seconds_remaining_period_ms", "UInt32"),
    ("home_status", "Enum8('valid' = 1, 'invalid' = 2)"),
    ("home_player_ids", "Array(UInt64)"),
    ("home_invalid_reason", "LowCardinality(String)"),
    ("home_invalid_since_sequence_number", "UInt32"),
    ("home_invalid_since_boundary_side", "Enum8('none' = 0, 'before' = 1, 'after' = 2)"),
    ("away_status", "Enum8('valid' = 1, 'invalid' = 2)"),
    ("away_player_ids", "Array(UInt64)"),
    ("away_invalid_reason", "LowCardinality(String)"),
    ("away_invalid_since_sequence_number", "UInt32"),
    ("away_invalid_since_boundary_side", "Enum8('none' = 0, 'before' = 1, 'after' = 2)"),
    ("is_valid", "UInt8"),
]

EXPECTED_TABLE_CONTRACT = {
    "engine": "ReplacingMergeTree",
    "engine_full": (
        "ReplacingMergeTree(insert_version) PARTITION BY season_start_year "
        "ORDER BY (game_id, snapshot_id, segment_number) "
        "SETTINGS index_granularity = 8192"
    ),
    "partition_key": "season_start_year",
    "sorting_key": "game_id, snapshot_id, segment_number",
    "primary_key": "game_id, snapshot_id, segment_number",
    "sampling_key": "",
}


def expected_projection() -> dict[str, Any]:
    partition = {"season_start_year"}
    sorting = {"game_id", "snapshot_id", "segment_number"}
    return {
        "schema": "clickhouse-lineup-segments-schema-v1",
        "database": "nba",
        "table": "lineup_segments_v1",
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


def test_lineup_segments_schema_is_exact_pinned_clickhouse_projection() -> None:
    projection = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert projection == expected_projection()
    assert len(projection["columns"]) == 43


def test_lineup_segments_migration_registers_the_stable_schema_contract() -> None:
    migration = load_migrations(MIGRATIONS_DIR)[-1]

    assert migration.migration_id == "0002_lineup_segments"
    assert migration.schema_id == LINEUP_SEGMENTS_SCHEMA_ID
    assert migration.database == "nba"
    assert migration.table == "lineup_segments_v1"
    assert migration.expected_schema == expected_projection()
    assert migration.expected_schema_fingerprint_sha256 == schema_fingerprint(expected_projection())


def test_lineup_segments_ddl_has_the_pinned_engine_partition_and_sort_key() -> None:
    statements = split_migration_statements(MIGRATION_PATH.read_text(encoding="utf-8"))

    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS nba.lineup_segments_v1")
    assert "ENGINE = ReplacingMergeTree(insert_version)" in statements[0]
    assert "PARTITION BY season_start_year" in statements[0]
    assert "ORDER BY (game_id, snapshot_id, segment_number)" in statements[0]
