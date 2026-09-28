from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nba_live_totals.infrastructure.clickhouse.migrations import (
    GAME_STATE_FEATURES_SCHEMA_ID,
    PLAYER_PREGAME_FEATURES_SCHEMA_ID,
    load_migrations,
    schema_fingerprint,
    split_migration_statements,
)

PROJECT_ROOT = Path(__file__).parents[4]
MIGRATIONS_DIR = PROJECT_ROOT / "clickhouse" / "migrations"

GAME_STATE_MIGRATION_PATH = MIGRATIONS_DIR / "0003_game_state_features.sql"
GAME_STATE_SCHEMA_PATH = MIGRATIONS_DIR / "0003_game_state_features.schema.json"
PLAYER_MIGRATION_PATH = MIGRATIONS_DIR / "0004_player_pregame_features.sql"
PLAYER_SCHEMA_PATH = MIGRATIONS_DIR / "0004_player_pregame_features.schema.json"

SEASON_TYPE = "Enum8('regular_season' = 1, 'playoffs' = 2)"
BOUNDARY_SIDE = "Enum8('before' = 1, 'after' = 2)"
INVALID_BOUNDARY_SIDE = "Enum8('none' = 0, 'before' = 1, 'after' = 2)"
LINEUP_STATUS = "Enum8('valid' = 1, 'invalid' = 2)"

GAME_STATE_COLUMNS = [
    ("snapshot_id", "UUID"),
    ("insert_version", "UInt64"),
    ("feature_revision_id", "FixedString(64)"),
    ("feature_input_token_sha256", "FixedString(64)"),
    ("dependency_input_sha256", "FixedString(64)"),
    ("dependency_revision_sha256", "FixedString(64)"),
    ("feature_join_version", "LowCardinality(String)"),
    ("feature_formula_version", "LowCardinality(String)"),
    ("feature_event_normalizer_version", "LowCardinality(String)"),
    ("output_schema_version", "LowCardinality(String)"),
    ("state_output_sha256", "FixedString(64)"),
    ("feature_output_sha256", "FixedString(64)"),
    ("possession_snapshot_id", "UUID"),
    ("parse_revision_id", "FixedString(64)"),
    ("possession_output_sha256", "FixedString(64)"),
    ("lineup_snapshot_id", "UUID"),
    ("lineup_revision_id", "FixedString(64)"),
    ("lineup_output_sha256", "FixedString(64)"),
    ("game_id", "FixedString(10)"),
    ("season", "FixedString(7)"),
    ("season_start_year", "UInt16"),
    ("season_type", SEASON_TYPE),
    ("game_date", "Date"),
    ("started_at", "DateTime64(3, 'UTC')"),
    ("home_team_id", "UInt64"),
    ("away_team_id", "UInt64"),
    ("state_number", "UInt16"),
    ("state_kind", "Enum8('pregame' = 1, 'after_possession' = 2)"),
    ("possession_number", "UInt16"),
    ("boundary_sequence_number", "UInt32"),
    ("boundary_side", BOUNDARY_SIDE),
    ("period", "UInt16"),
    ("seconds_remaining_game_ms", "UInt32"),
    ("seconds_remaining_period_ms", "UInt32"),
    ("home_score", "UInt16"),
    ("away_score", "UInt16"),
    ("score_diff", "Int16"),
    ("home_possessions", "UInt16"),
    ("away_possessions", "UInt16"),
    ("home_lineup_status", LINEUP_STATUS),
    ("home_player_ids", "Array(UInt64)"),
    ("home_invalid_reason", "LowCardinality(String)"),
    ("home_invalid_since_sequence_number", "UInt32"),
    ("home_invalid_since_boundary_side", INVALID_BOUNDARY_SIDE),
    ("away_lineup_status", LINEUP_STATUS),
    ("away_player_ids", "Array(UInt64)"),
    ("away_invalid_reason", "LowCardinality(String)"),
    ("away_invalid_since_sequence_number", "UInt32"),
    ("away_invalid_since_boundary_side", INVALID_BOUNDARY_SIDE),
    ("home_games_used_5", "UInt8"),
    ("home_games_used_10", "UInt8"),
    ("home_games_used_20", "UInt8"),
    ("away_games_used_5", "UInt8"),
    ("away_games_used_10", "UInt8"),
    ("away_games_used_20", "UInt8"),
    ("home_off_rating_5", "Nullable(Int64)"),
    ("home_off_rating_10", "Nullable(Int64)"),
    ("home_off_rating_20", "Nullable(Int64)"),
    ("home_def_rating_5", "Nullable(Int64)"),
    ("home_def_rating_10", "Nullable(Int64)"),
    ("home_def_rating_20", "Nullable(Int64)"),
    ("home_pace_5", "Nullable(Int64)"),
    ("home_pace_10", "Nullable(Int64)"),
    ("home_pace_20", "Nullable(Int64)"),
    ("away_off_rating_5", "Nullable(Int64)"),
    ("away_off_rating_10", "Nullable(Int64)"),
    ("away_off_rating_20", "Nullable(Int64)"),
    ("away_def_rating_5", "Nullable(Int64)"),
    ("away_def_rating_10", "Nullable(Int64)"),
    ("away_def_rating_20", "Nullable(Int64)"),
    ("away_pace_5", "Nullable(Int64)"),
    ("away_pace_10", "Nullable(Int64)"),
    ("away_pace_20", "Nullable(Int64)"),
    ("home_rest_days", "Nullable(Int64)"),
    ("away_rest_days", "Nullable(Int64)"),
    ("home_back_to_back", "UInt8"),
    ("away_back_to_back", "UInt8"),
    ("current_pace", "Nullable(Int64)"),
    ("pace_last_5", "Nullable(Int64)"),
    ("pace_last_10", "Nullable(Int64)"),
    ("pace_last_20", "Nullable(Int64)"),
    ("pace_last_5_possessions_used", "UInt8"),
    ("pace_last_10_possessions_used", "UInt8"),
    ("pace_last_20_possessions_used", "UInt8"),
    ("home_ppp", "Nullable(Int64)"),
    ("away_ppp", "Nullable(Int64)"),
    ("home_ppp_last_5", "Nullable(Int64)"),
    ("home_ppp_last_10", "Nullable(Int64)"),
    ("home_ppp_last_20", "Nullable(Int64)"),
    ("away_ppp_last_5", "Nullable(Int64)"),
    ("away_ppp_last_10", "Nullable(Int64)"),
    ("away_ppp_last_20", "Nullable(Int64)"),
    ("home_ppp_last_5_possessions_used", "UInt8"),
    ("home_ppp_last_10_possessions_used", "UInt8"),
    ("home_ppp_last_20_possessions_used", "UInt8"),
    ("away_ppp_last_5_possessions_used", "UInt8"),
    ("away_ppp_last_10_possessions_used", "UInt8"),
    ("away_ppp_last_20_possessions_used", "UInt8"),
    ("home_three_point_attempt_rate", "Nullable(Int64)"),
    ("away_three_point_attempt_rate", "Nullable(Int64)"),
    ("home_free_throw_rate", "Nullable(Int64)"),
    ("away_free_throw_rate", "Nullable(Int64)"),
    ("home_turnover_rate", "Nullable(Int64)"),
    ("away_turnover_rate", "Nullable(Int64)"),
    ("home_offensive_rebound_rate", "Nullable(Int64)"),
    ("away_offensive_rebound_rate", "Nullable(Int64)"),
    ("home_profiled_player_count", "UInt8"),
    ("away_profiled_player_count", "UInt8"),
    ("home_lineup_avg_off_rating", "Nullable(Int64)"),
    ("away_lineup_avg_off_rating", "Nullable(Int64)"),
    ("home_lineup_avg_def_rating", "Nullable(Int64)"),
    ("away_lineup_avg_def_rating", "Nullable(Int64)"),
    ("home_lineup_avg_usage_rate", "Nullable(Int64)"),
    ("away_lineup_avg_usage_rate", "Nullable(Int64)"),
    ("home_lineup_avg_three_point_attempt_rate", "Nullable(Int64)"),
    ("away_lineup_avg_three_point_attempt_rate", "Nullable(Int64)"),
    ("home_lineup_avg_free_throw_rate", "Nullable(Int64)"),
    ("away_lineup_avg_free_throw_rate", "Nullable(Int64)"),
    ("home_lineup_avg_minutes", "Nullable(Int64)"),
    ("away_lineup_avg_minutes", "Nullable(Int64)"),
    ("home_lineup_avg_pace_context", "Nullable(Int64)"),
    ("away_lineup_avg_pace_context", "Nullable(Int64)"),
    ("offense_difference", "Nullable(Int64)"),
    ("defense_difference", "Nullable(Int64)"),
    ("pace_difference", "Nullable(Int64)"),
    ("is_valid", "UInt8"),
]

PLAYER_COLUMNS = [
    ("snapshot_id", "UUID"),
    ("insert_version", "UInt64"),
    ("feature_revision_id", "FixedString(64)"),
    ("feature_input_token_sha256", "FixedString(64)"),
    ("dependency_input_sha256", "FixedString(64)"),
    ("dependency_revision_sha256", "FixedString(64)"),
    ("feature_formula_version", "LowCardinality(String)"),
    ("output_schema_version", "LowCardinality(String)"),
    ("player_output_sha256", "FixedString(64)"),
    ("feature_output_sha256", "FixedString(64)"),
    ("game_id", "FixedString(10)"),
    ("season", "FixedString(7)"),
    ("season_start_year", "UInt16"),
    ("season_type", SEASON_TYPE),
    ("game_date", "Date"),
    ("started_at", "DateTime64(3, 'UTC')"),
    ("player_id", "UInt64"),
    ("team_id", "UInt64"),
    ("is_home", "UInt8"),
    ("history_cutoff_started_at", "DateTime64(3, 'UTC')"),
    ("games_used", "UInt8"),
    ("seconds_played", "UInt32"),
    ("estimated_possessions", "Nullable(Int64)"),
    ("minutes_avg", "Nullable(Int64)"),
    ("points_per_100", "Nullable(Int64)"),
    ("off_rating", "Nullable(Int64)"),
    ("def_rating", "Nullable(Int64)"),
    ("usage_rate", "Nullable(Int64)"),
    ("assist_rate", "Nullable(Int64)"),
    ("turnover_rate", "Nullable(Int64)"),
    ("three_point_attempt_rate", "Nullable(Int64)"),
    ("three_point_percentage", "Nullable(Int64)"),
    ("free_throw_rate", "Nullable(Int64)"),
    ("rebound_rate", "Nullable(Int64)"),
    ("pace_context", "Nullable(Int64)"),
]


def _expected_projection(
    *, schema_id: str, table: str, columns: list[tuple[str, str]], order: tuple[str, ...]
) -> dict[str, Any]:
    sorting = set(order)
    sorting_key = ", ".join(order)
    return {
        "schema": schema_id,
        "database": "nba",
        "table": table,
        "table_contract": {
            "engine": "ReplacingMergeTree",
            "engine_full": (
                "ReplacingMergeTree(insert_version) PARTITION BY season_start_year "
                f"ORDER BY ({sorting_key}) SETTINGS index_granularity = 8192"
            ),
            "partition_key": "season_start_year",
            "sorting_key": sorting_key,
            "primary_key": sorting_key,
            "sampling_key": "",
        },
        "columns": [
            {
                "position": position,
                "name": name,
                "type": column_type,
                "default_kind": "",
                "default_expression": "",
                "is_in_partition_key": name == "season_start_year",
                "is_in_sorting_key": name in sorting,
                "is_in_primary_key": name in sorting,
                "is_in_sampling_key": False,
            }
            for position, (name, column_type) in enumerate(columns, start=1)
        ],
    }


@pytest.mark.parametrize(
    ("schema_path", "schema_id", "table", "columns", "order"),
    [
        (
            GAME_STATE_SCHEMA_PATH,
            GAME_STATE_FEATURES_SCHEMA_ID,
            "game_state_features_v1",
            GAME_STATE_COLUMNS,
            ("game_id", "snapshot_id", "state_number"),
        ),
        (
            PLAYER_SCHEMA_PATH,
            PLAYER_PREGAME_FEATURES_SCHEMA_ID,
            "player_pregame_features_v1",
            PLAYER_COLUMNS,
            ("game_id", "snapshot_id", "player_id"),
        ),
    ],
)
def test_feature_schema_is_exact_pinned_clickhouse_projection(
    schema_path: Path,
    schema_id: str,
    table: str,
    columns: list[tuple[str, str]],
    order: tuple[str, ...],
) -> None:
    projection = json.loads(schema_path.read_text(encoding="utf-8"))

    assert projection == _expected_projection(
        schema_id=schema_id,
        table=table,
        columns=columns,
        order=order,
    )


@pytest.mark.parametrize(
    ("migration_id", "schema_id", "table", "columns", "order"),
    [
        (
            "0003_game_state_features",
            GAME_STATE_FEATURES_SCHEMA_ID,
            "game_state_features_v1",
            GAME_STATE_COLUMNS,
            ("game_id", "snapshot_id", "state_number"),
        ),
        (
            "0004_player_pregame_features",
            PLAYER_PREGAME_FEATURES_SCHEMA_ID,
            "player_pregame_features_v1",
            PLAYER_COLUMNS,
            ("game_id", "snapshot_id", "player_id"),
        ),
    ],
)
def test_feature_migration_registers_stable_schema_contract(
    migration_id: str,
    schema_id: str,
    table: str,
    columns: list[tuple[str, str]],
    order: tuple[str, ...],
) -> None:
    migration = {item.migration_id: item for item in load_migrations(MIGRATIONS_DIR)}[migration_id]
    expected = _expected_projection(
        schema_id=schema_id,
        table=table,
        columns=columns,
        order=order,
    )

    assert migration.schema_id == schema_id
    assert (migration.database, migration.table) == ("nba", table)
    assert migration.expected_schema == expected
    assert migration.expected_schema_fingerprint_sha256 == schema_fingerprint(expected)


@pytest.mark.parametrize(
    ("migration_path", "table", "order"),
    [
        (
            GAME_STATE_MIGRATION_PATH,
            "game_state_features_v1",
            "game_id, snapshot_id, state_number",
        ),
        (
            PLAYER_MIGRATION_PATH,
            "player_pregame_features_v1",
            "game_id, snapshot_id, player_id",
        ),
    ],
)
def test_feature_ddl_has_pinned_engine_partition_and_sort_key(
    migration_path: Path, table: str, order: str
) -> None:
    statements = split_migration_statements(migration_path.read_text(encoding="utf-8"))

    assert len(statements) == 1
    assert statements[0].startswith(f"CREATE TABLE IF NOT EXISTS nba.{table}")
    assert "ENGINE = ReplacingMergeTree(insert_version)" in statements[0]
    assert "PARTITION BY season_start_year" in statements[0]
    assert f"ORDER BY ({order})" in statements[0]


def test_feature_columns_preserve_fixed_point_count_array_and_boolean_contracts() -> None:
    state_types = dict(GAME_STATE_COLUMNS)
    player_types = dict(PLAYER_COLUMNS)

    metric_names = {
        name
        for name, _ in GAME_STATE_COLUMNS + PLAYER_COLUMNS
        if any(
            marker in name
            for marker in (
                "rating",
                "pace",
                "ppp",
                "rate",
                "percentage",
                "minutes_avg",
                "points_per_100",
                "estimated_possessions",
                "difference",
            )
        )
        and not name.endswith("possessions_used")
    }
    assert {state_types.get(name, player_types.get(name)) for name in metric_names} == {
        "Nullable(Int64)"
    }
    assert state_types["home_player_ids"] == state_types["away_player_ids"] == "Array(UInt64)"
    assert state_types["home_rest_days"] == state_types["away_rest_days"] == "Nullable(Int64)"
    assert state_types["home_back_to_back"] == state_types["away_back_to_back"] == "UInt8"
    assert state_types["is_valid"] == player_types["is_home"] == "UInt8"
    assert state_types["seconds_remaining_game_ms"] == "UInt32"
    assert state_types["seconds_remaining_period_ms"] == "UInt32"
    assert all(
        column_type.startswith("UInt")
        for name, column_type in GAME_STATE_COLUMNS + PLAYER_COLUMNS
        if name.endswith("_used") or name in {"games_used", "seconds_played"}
    )
