from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.infrastructure.clickhouse import migrations as migrations_module
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient, ClickHouseError
from nba_live_totals.infrastructure.clickhouse.migrations import (
    GAME_STATE_FEATURES_SCHEMA_ID,
    LINEUP_SEGMENTS_SCHEMA_ID,
    PLAYER_PREGAME_FEATURES_SCHEMA_ID,
    STATEMENT_DELIMITER,
    ClickHouseMigration,
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
EXPECTED_EXISTING_FILE_SHA256 = {
    "0001_possessions.sql": "696cf578c69b8aaf779db7ffab8eaf0be7f6a97b6694f653a1727805e3ef3496",
    "0001_possessions.schema.json": (
        "937b5a031d5c509f5509e20b54da427d4107b4099cad97c2f43cce1fd13472c9"
    ),
    "0002_lineup_segments.sql": (
        "65e9a65f80ab419867aec5de77925aea247f8b02eda13b47f0f672c6b1bb7e74"
    ),
    "0002_lineup_segments.schema.json": (
        "cc2c0d066d600bd494f916b49b14150fdc79be475c15c57dd9428f6e75a93096"
    ),
}

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


def _write_migration(
    directory: Path,
    migration_id: str,
    *,
    schema_id: str,
    database: str,
    table: str,
    statement: str | None = None,
    marker: str = "base",
) -> dict[str, Any]:
    projection = {
        "schema": schema_id,
        "database": database,
        "table": table,
        "table_contract": {"marker": marker},
        "columns": [],
    }
    if statement is None:
        statement = (
            f"CREATE TABLE IF NOT EXISTS {database}.{table} "
            "(value UInt8) ENGINE = MergeTree PARTITION BY value ORDER BY (value)"
        )
    (directory / f"{migration_id}.sql").write_text(statement, encoding="utf-8")
    (directory / f"{migration_id}.schema.json").write_text(json.dumps(projection), encoding="utf-8")
    return projection


def test_committed_projection_is_the_exact_pinned_possession_schema() -> None:
    projection = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert projection == _expected_projection()
    assert all(not column["type"].startswith("Nullable(") for column in projection["columns"])


def test_committed_projection_has_exact_canonical_fingerprint() -> None:
    projection = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema_fingerprint(projection) == EXPECTED_SCHEMA_FINGERPRINT_SHA256


def test_migration_bytes_and_loaded_metadata_have_fixed_checksums() -> None:
    migrations = load_migrations(MIGRATIONS_DIR)

    assert len(migrations) == 4
    migration = migrations[0]
    assert migration.migration_id == "0001_possessions"
    assert migration.schema_id == "clickhouse-possession-schema-v1"
    assert migration.database == "nba"
    assert migration.table == "possessions_v1"
    assert migration.checksum_sha256 == EXPECTED_MIGRATION_SHA256
    assert migration.expected_schema == _expected_projection()
    assert migration.expected_schema_fingerprint_sha256 == EXPECTED_SCHEMA_FINGERPRINT_SHA256
    lineup_segments = migrations[1]
    assert lineup_segments.migration_id == "0002_lineup_segments"
    assert lineup_segments.schema_id == LINEUP_SEGMENTS_SCHEMA_ID
    assert (lineup_segments.database, lineup_segments.table) == ("nba", "lineup_segments_v1")
    game_state_features = migrations[2]
    assert game_state_features.migration_id == "0003_game_state_features"
    assert game_state_features.schema_id == GAME_STATE_FEATURES_SCHEMA_ID
    assert (game_state_features.database, game_state_features.table) == (
        "nba",
        "game_state_features_v1",
    )
    player_features = migrations[3]
    assert player_features.migration_id == "0004_player_pregame_features"
    assert player_features.schema_id == PLAYER_PREGAME_FEATURES_SCHEMA_ID
    assert (player_features.database, player_features.table) == (
        "nba",
        "player_pregame_features_v1",
    )


def test_existing_migration_and_manifest_bytes_remain_frozen() -> None:
    import hashlib

    actual = {
        filename: hashlib.sha256((MIGRATIONS_DIR / filename).read_bytes()).hexdigest()
        for filename in EXPECTED_EXISTING_FILE_SHA256
    }

    assert actual == EXPECTED_EXISTING_FILE_SHA256


def test_load_migrations_orders_multiple_tables_and_preserves_stable_schema_mapping(
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    possessions_v1 = _write_migration(
        migration_dir,
        "0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
    )
    lineups = _write_migration(
        migration_dir,
        "0002_lineups",
        schema_id="clickhouse-lineup-schema-v1",
        database="nba",
        table="lineups_v1",
    )
    possessions_v2 = _write_migration(
        migration_dir,
        "0003_possessions_upgrade",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        statement=("ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS upgrade_marker UInt8"),
        marker="upgrade",
    )

    migrations = load_migrations(migration_dir)

    assert [migration.migration_id for migration in migrations] == [
        "0001_possessions",
        "0002_lineups",
        "0003_possessions_upgrade",
    ]
    assert [
        (migration.schema_id, migration.database, migration.table) for migration in migrations
    ] == [
        ("clickhouse-possession-schema-v1", "nba", "possessions_v1"),
        ("clickhouse-lineup-schema-v1", "nba", "lineups_v1"),
        ("clickhouse-possession-schema-v1", "nba", "possessions_v1"),
    ]
    assert [migration.expected_schema for migration in migrations] == [
        possessions_v1,
        lineups,
        possessions_v2,
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            ("clickhouse-shared-schema-v1", "nba", "possessions_v1"),
            ("clickhouse-shared-schema-v1", "nba", "lineups_v1"),
        ),
        (
            ("clickhouse-possession-schema-v1", "nba", "possessions_v1"),
            ("clickhouse-other-schema-v1", "nba", "possessions_v1"),
        ),
    ],
)
def test_load_migrations_rejects_non_bijective_schema_table_mapping(
    tmp_path: Path,
    first: tuple[str, str, str],
    second: tuple[str, str, str],
) -> None:
    _write_migration(
        tmp_path,
        "0001_first",
        schema_id=first[0],
        database=first[1],
        table=first[2],
    )
    _write_migration(
        tmp_path,
        "0002_second",
        schema_id=second[0],
        database=second[1],
        table=second[2],
    )

    with pytest.raises(ClickHouseMigrationError) as captured:
        load_migrations(tmp_path)

    assert captured.value.code == "clickhouse_migration_set_invalid"


@pytest.mark.parametrize(
    ("schema_id", "database", "table"),
    [
        ("UPPERCASE", "nba", "possessions_v1"),
        ("clickhouse-possession-schema-v1", "nba;drop", "possessions_v1"),
        ("clickhouse-possession-schema-v1", "nba", "possessions-v1"),
        ("s" * 65, "nba", "possessions_v1"),
        ("clickhouse-possession-schema-v1", "d" * 65, "possessions_v1"),
        ("clickhouse-possession-schema-v1", "nba", "t" * 65),
    ],
)
def test_load_migrations_rejects_unbounded_or_unsafe_manifest_names(
    tmp_path: Path,
    schema_id: str,
    database: str,
    table: str,
) -> None:
    _write_migration(
        tmp_path,
        "0001_invalid",
        schema_id=schema_id,
        database=database,
        table=table,
    )

    with pytest.raises(ClickHouseMigrationError) as captured:
        load_migrations(tmp_path)

    assert captured.value.code == "clickhouse_migration_set_invalid"


def test_load_migrations_accepts_maximum_bounded_manifest_names(tmp_path: Path) -> None:
    schema_id = "s" * 64
    database = "d" * 64
    table = "t" * 64
    _write_migration(
        tmp_path,
        "0001_boundary",
        schema_id=schema_id,
        database=database,
        table=table,
    )

    migration = load_migrations(tmp_path)[0]

    assert (migration.schema_id, migration.database, migration.table) == (
        schema_id,
        database,
        table,
    )


def test_load_migrations_rejects_sql_target_that_disagrees_with_manifest(tmp_path: Path) -> None:
    _write_migration(
        tmp_path,
        "0001_mismatch",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        statement=(
            "CREATE TABLE IF NOT EXISTS nba.lineups_v1 "
            "(value UInt8) ENGINE = MergeTree PARTITION BY value ORDER BY (value)"
        ),
    )

    with pytest.raises(ClickHouseMigrationError) as captured:
        load_migrations(tmp_path)

    assert captured.value.code == "clickhouse_migration_set_invalid"


def test_load_migrations_rejects_unpaired_sql_and_manifest_files(tmp_path: Path) -> None:
    (tmp_path / "0001_orphan.sql").write_text("CREATE DATABASE IF NOT EXISTS nba", encoding="utf-8")

    with pytest.raises(ClickHouseMigrationError) as captured:
        load_migrations(tmp_path)

    assert captured.value.code == "clickhouse_migration_set_invalid"


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
        "CREATE DATABASE IF NOT EXISTS nba -- trailing command\nDROP DATABASE nba",
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
        schema_id="clickhouse-lineup-schema-v1",
        database="nba'; DROP DATABASE nba; --",
        table="possessions_v1",
    )

    assert projection["schema"] == "clickhouse-lineup-schema-v1"
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
            schema_id="clickhouse-possession-schema-v1",
            database="nba",
            table="possessions_v1",
        )

    assert captured.value.code == "clickhouse_schema_missing"


class _SchemaMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _SchemaResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _SchemaMappings:
        return _SchemaMappings(self._rows)


class _SchemaConnection:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self._rows = rows
        self._execute_error = execute_error

    async def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> _SchemaResult:
        del statement
        if self._execute_error is not None:
            raise self._execute_error
        if parameters is None:
            return _SchemaResult(self._rows)
        migration_id = parameters.get("migration_id")
        return _SchemaResult([row for row in self._rows if row.get("migration_id") == migration_id])


class _SchemaConnectionContext:
    def __init__(self, connection: _SchemaConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _SchemaConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        del args


class _SchemaEngine:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self._connection = _SchemaConnection(rows, execute_error=execute_error)

    def connect(self) -> _SchemaConnectionContext:
        return _SchemaConnectionContext(self._connection)


def _write_selected_schema_migrations(
    directory: Path,
) -> tuple[ClickHouseMigration, ClickHouseMigration, ClickHouseMigration]:
    _write_migration(
        directory,
        "0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
    )
    _write_migration(
        directory,
        "0002_lineups",
        schema_id="clickhouse-lineup-schema-v1",
        database="nba",
        table="lineups_v1",
    )
    _write_migration(
        directory,
        "0003_possessions_upgrade",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        statement=("ALTER TABLE nba.possessions_v1 ADD COLUMN IF NOT EXISTS upgrade_marker UInt8"),
        marker="upgrade",
    )
    _write_migration(
        directory,
        "0004_boxscores",
        schema_id="clickhouse-boxscore-schema-v1",
        database="nba",
        table="boxscores_v1",
    )
    migrations = load_migrations(directory)
    return migrations[1], migrations[2], migrations[3]


async def test_validate_schema_selects_latest_schema_head_and_ignores_other_pending_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineup, possessions_head, later_pending = _write_selected_schema_migrations(tmp_path)
    rows: list[dict[str, object]] = [
        {
            "migration_id": "0001_possessions",
            "checksum_sha256": "obsolete-drift-is-ignored",
            "expected_schema_fingerprint_sha256": "obsolete-drift-is-ignored",
            "state": "applying",
        },
        {
            "migration_id": lineup.migration_id,
            "checksum_sha256": lineup.checksum_sha256,
            "expected_schema_fingerprint_sha256": (lineup.expected_schema_fingerprint_sha256),
            "state": "applied",
        },
        {
            "migration_id": possessions_head.migration_id,
            "checksum_sha256": possessions_head.checksum_sha256,
            "expected_schema_fingerprint_sha256": (
                possessions_head.expected_schema_fingerprint_sha256
            ),
            "state": "applied",
        },
        {
            "migration_id": later_pending.migration_id,
            "checksum_sha256": later_pending.checksum_sha256,
            "expected_schema_fingerprint_sha256": (
                later_pending.expected_schema_fingerprint_sha256
            ),
            "state": "applying",
        },
    ]
    calls: list[tuple[object, str, str, str]] = []

    async def projection(
        client: object, *, schema_id: str, database: str, table: str
    ) -> dict[str, Any]:
        calls.append((client, schema_id, database, table))
        selected_projections: dict[str, dict[str, Any]] = {
            possessions_head.schema_id: possessions_head.expected_schema,
            lineup.schema_id: lineup.expected_schema,
        }
        return selected_projections[schema_id]

    monkeypatch.setattr(migrations_module, "fetch_schema_projection", projection)
    client = object()

    await migrations_module.validate_schema(
        cast(AsyncEngine, _SchemaEngine(rows)),
        cast(ClickHouseClient, client),
        schema_id="clickhouse-possession-schema-v1",
        migrations_dir=tmp_path,
    )
    await migrations_module.validate_schema(
        cast(AsyncEngine, _SchemaEngine(rows)),
        cast(ClickHouseClient, client),
        schema_id="clickhouse-lineup-schema-v1",
        migrations_dir=tmp_path,
    )

    assert calls == [
        (client, "clickhouse-possession-schema-v1", "nba", "possessions_v1"),
        (client, "clickhouse-lineup-schema-v1", "nba", "lineups_v1"),
    ]


@pytest.mark.parametrize(
    ("row_changes", "expected_code"),
    [
        (None, "clickhouse_migration_set_drift"),
        ({"state": "applying"}, "clickhouse_schema_not_applied"),
        ({"checksum_sha256": "0" * 64}, "clickhouse_migration_checksum_drift"),
        (
            {"expected_schema_fingerprint_sha256": "0" * 64},
            "clickhouse_migration_fingerprint_drift",
        ),
    ],
)
async def test_validate_schema_fails_closed_for_selected_head_ledger(
    tmp_path: Path,
    row_changes: dict[str, object] | None,
    expected_code: str,
) -> None:
    _, selected, _ = _write_selected_schema_migrations(tmp_path)
    rows: list[dict[str, object]] = []
    if row_changes is not None:
        row: dict[str, object] = {
            "migration_id": selected.migration_id,
            "checksum_sha256": selected.checksum_sha256,
            "expected_schema_fingerprint_sha256": (selected.expected_schema_fingerprint_sha256),
            "state": "applied",
        }
        row.update(row_changes)
        rows.append(row)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, _SchemaEngine(rows)),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
            migrations_dir=tmp_path,
        )

    assert captured.value.code == expected_code


async def test_validate_schema_fails_closed_when_selected_catalog_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, selected, _ = _write_selected_schema_migrations(tmp_path)
    row: dict[str, object] = {
        "migration_id": selected.migration_id,
        "checksum_sha256": selected.checksum_sha256,
        "expected_schema_fingerprint_sha256": selected.expected_schema_fingerprint_sha256,
        "state": "applied",
    }

    async def unavailable(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        raise ClickHouseError("credentialed unavailable detail")

    monkeypatch.setattr(migrations_module, "fetch_schema_projection", unavailable)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, _SchemaEngine([row])),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
            migrations_dir=tmp_path,
        )

    assert captured.value.code == "clickhouse_schema_unavailable"
    assert captured.value.__cause__ is None
    assert "credentialed" not in str(captured.value)


async def test_validate_schema_rejects_unknown_schema_id(tmp_path: Path) -> None:
    _write_selected_schema_migrations(tmp_path)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, _SchemaEngine([])),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-unknown-schema-v1",
            migrations_dir=tmp_path,
        )

    assert captured.value.code == "clickhouse_migration_set_invalid"


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
