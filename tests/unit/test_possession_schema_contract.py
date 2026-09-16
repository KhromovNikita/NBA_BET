from collections.abc import Iterable
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.postgres import schema

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
PHASE_THREE_TABLES = {
    "possession_rebuild_runs",
    "possession_snapshot_attempts",
    "possession_rebuild_resources",
    "clickhouse_schema_migrations",
}

EXPECTED_COLUMNS = {
    "possession_rebuild_runs": (
        "run_id",
        "selectors",
        "mode",
        "forced_rebuild",
        "status",
        "discovered",
        "materialized",
        "skipped_current",
        "verified",
        "failed",
        "busy",
        "started_at",
        "finished_at",
    ),
    "possession_snapshot_attempts": (
        "snapshot_id",
        "game_id",
        "generation",
        "parse_revision_id",
        "input_token_sha256",
        "input_pbp_document_id",
        "input_pbp_sha256",
        "input_event_sha256",
        "game_context_sha256",
        "normalizer_version",
        "parser_version",
        "rules_version",
        "output_schema_version",
        "expected_row_count",
        "expected_output_sha256",
        "state",
        "created_by_run_id",
        "created_by_claim_version",
        "insert_started_at",
        "verified_at",
        "published_at",
        "safe_error_code",
        "safe_error_type",
        "created_at",
    ),
    "possession_rebuild_resources": (
        "game_id",
        "publication_state",
        "attempt_count",
        "claim_version",
        "last_run_id",
        "next_generation",
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_parse_revision_id",
        "active_input_token_sha256",
        "claimed_until",
        "published_snapshot_id",
        "published_parse_revision_id",
        "published_input_token_sha256",
        "published_at",
        "last_issue_codes",
        "last_error_code",
        "last_error_type",
        "created_at",
        "updated_at",
        "completed_at",
    ),
    "clickhouse_schema_migrations": (
        "migration_id",
        "checksum_sha256",
        "expected_schema_fingerprint_sha256",
        "state",
        "applied_at",
    ),
}

EXPECTED_NULLABLE = {
    "possession_rebuild_runs": {"finished_at"},
    "possession_snapshot_attempts": {
        "expected_row_count",
        "expected_output_sha256",
        "insert_started_at",
        "verified_at",
        "published_at",
        "safe_error_code",
        "safe_error_type",
    },
    "possession_rebuild_resources": {
        "last_run_id",
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_parse_revision_id",
        "active_input_token_sha256",
        "claimed_until",
        "published_snapshot_id",
        "published_parse_revision_id",
        "published_input_token_sha256",
        "published_at",
        "last_error_code",
        "last_error_type",
        "completed_at",
    },
    "clickhouse_schema_migrations": {"applied_at"},
}

EXPECTED_DEFAULTS = {
    "possession_rebuild_runs": {
        "discovered": "0",
        "materialized": "0",
        "skipped_current": "0",
        "verified": "0",
        "failed": "0",
        "busy": "0",
        "started_at": "now()",
    },
    "possession_snapshot_attempts": {"created_at": "now()"},
    "possession_rebuild_resources": {
        "publication_state": "absent",
        "attempt_count": "0",
        "claim_version": "0",
        "next_generation": "1",
        "last_issue_codes": "'[]'::jsonb",
        "created_at": "now()",
        "updated_at": "now()",
    },
    "clickhouse_schema_migrations": {},
}


def _table(name: str):  # type: ignore[no-untyped-def]
    assert name in metadata.tables, f"shared metadata is missing {name}"
    return metadata.tables[name]


def _column_names(columns: Iterable[Column[Any]]) -> tuple[str, ...]:
    return tuple(column.name for column in columns)


def _check_sql(table_name: str) -> dict[str, str]:
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in _table(table_name).constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_shared_metadata_keeps_all_four_phase_three_tables() -> None:
    assert PHASE_THREE_TABLES <= (
        set(metadata.tables)
        - {
            "seasons",
            "teams",
            "players",
            "games",
            "team_box_scores",
            "player_box_scores",
            "play_by_play_events",
            "source_documents",
            "ingestion_runs",
            "ingestion_resources",
        }
    )


def test_columns_keys_nullability_and_defaults_match_the_contract() -> None:
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        table = _table(table_name)
        assert tuple(table.c.keys()) == expected_columns
        assert _column_names(table.primary_key.columns) == (expected_columns[0],)
        assert table.primary_key.name == f"pk_{table_name}"
        assert {column.name for column in table.c if column.nullable} == EXPECTED_NULLABLE[
            table_name
        ]
        assert {
            column.name: str(column.server_default.arg)
            for column in table.c
            if column.server_default is not None
        } == EXPECTED_DEFAULTS[table_name]


def test_postgresql_types_match_the_contract() -> None:
    runs = _table("possession_rebuild_runs")
    attempts = _table("possession_snapshot_attempts")
    resources = _table("possession_rebuild_resources")
    ledger = _table("clickhouse_schema_migrations")

    for column in (
        runs.c.run_id,
        attempts.c.snapshot_id,
        attempts.c.input_pbp_document_id,
        attempts.c.created_by_run_id,
        resources.c.last_run_id,
        resources.c.active_run_id,
        resources.c.active_snapshot_id,
        resources.c.published_snapshot_id,
    ):
        assert isinstance(column.type, UUID)
    for column in (runs.c.selectors, resources.c.last_issue_codes):
        assert isinstance(column.type, JSONB)
    assert isinstance(runs.c.forced_rebuild.type, Boolean)
    for column in (
        attempts.c.generation,
        attempts.c.expected_row_count,
        attempts.c.created_by_claim_version,
        resources.c.attempt_count,
        resources.c.claim_version,
        resources.c.next_generation,
        resources.c.active_claim_version,
    ):
        assert isinstance(column.type, BigInteger)
    for column in (
        runs.c.discovered,
        runs.c.materialized,
        runs.c.skipped_current,
        runs.c.verified,
        runs.c.failed,
        runs.c.busy,
    ):
        assert isinstance(column.type, Integer)
    for column in (
        attempts.c.parse_revision_id,
        attempts.c.input_token_sha256,
        attempts.c.input_pbp_sha256,
        attempts.c.input_event_sha256,
        attempts.c.game_context_sha256,
        attempts.c.expected_output_sha256,
        resources.c.active_parse_revision_id,
        resources.c.active_input_token_sha256,
        resources.c.published_parse_revision_id,
        resources.c.published_input_token_sha256,
        ledger.c.checksum_sha256,
        ledger.c.expected_schema_fingerprint_sha256,
    ):
        assert str(column.type) == "CHAR(64)"
    for column in (
        attempts.c.normalizer_version,
        attempts.c.parser_version,
        attempts.c.rules_version,
        attempts.c.output_schema_version,
        attempts.c.safe_error_code,
        attempts.c.safe_error_type,
        resources.c.last_error_code,
        resources.c.last_error_type,
        ledger.c.migration_id,
    ):
        assert isinstance(column.type, Text)
    for column in (runs.c.mode, runs.c.status, attempts.c.state, resources.c.publication_state):
        assert isinstance(column.type, String)
        assert column.type.length == 16
    for table_name in PHASE_THREE_TABLES:
        for column in _table(table_name).c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone


def test_run_checks_cover_modes_states_and_nonnegative_counters() -> None:
    assert _check_sql("possession_rebuild_runs") == {
        "ck_possession_rebuild_runs_mode_allowed": "mode IN ('materialize', 'verify')",
        "ck_possession_rebuild_runs_status_allowed": (
            "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')"
        ),
        **{
            f"ck_possession_rebuild_runs_nonnegative_{name}": f"{name} >= 0"
            for name in (
                "discovered",
                "materialized",
                "skipped_current",
                "verified",
                "failed",
                "busy",
            )
        },
    }


def test_attempt_checks_cover_bounds_hashes_versions_states_and_errors() -> None:
    checks = _check_sql("possession_snapshot_attempts")
    assert checks["ck_possession_snapshot_attempts_state_allowed"] == (
        "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')"
    )
    assert checks["ck_possession_snapshot_attempts_generation_bounds"] == (
        f"generation BETWEEN 1 AND {MAX_SIGNED_BIGINT}"
    )
    assert checks["ck_possession_snapshot_attempts_expected_row_count_bounds"] == (
        f"expected_row_count IS NULL OR expected_row_count BETWEEN 1 AND {MAX_SIGNED_BIGINT}"
    )
    assert checks["ck_possession_snapshot_attempts_created_by_claim_version_bounds"] == (
        f"created_by_claim_version BETWEEN 1 AND {MAX_SIGNED_BIGINT}"
    )
    for name in (
        "parse_revision_id",
        "input_token_sha256",
        "input_pbp_sha256",
        "input_event_sha256",
        "game_context_sha256",
    ):
        assert checks[f"ck_possession_snapshot_attempts_{name}_format"] == (
            f"{name} ~ '^[0-9a-f]{{64}}$'"
        )
    assert checks["ck_possession_snapshot_attempts_expected_output_sha256_format"] == (
        "expected_output_sha256 IS NULL OR expected_output_sha256 ~ '^[0-9a-f]{64}$'"
    )
    for name in (
        "normalizer_version",
        "parser_version",
        "rules_version",
        "output_schema_version",
    ):
        assert checks[f"ck_possession_snapshot_attempts_{name}_format"] == (
            f"{name} ~ '^[a-z][a-z0-9._-]{{0,127}}$'"
        )
    for name in ("safe_error_code", "safe_error_type"):
        assert checks[f"ck_possession_snapshot_attempts_{name}_format"] == (
            f"{name} IS NULL OR {name} ~ '^[a-z][a-z0-9_]{{0,63}}$'"
        )
    assert "ck_possession_snapshot_attempts_state_fields" in checks
    assert "ck_possession_snapshot_attempts_state_error_fields" in checks
    assert len(checks) == 18


def test_resource_checks_cover_fences_publication_and_bounded_issues() -> None:
    checks = _check_sql("possession_rebuild_resources")
    assert checks["ck_possession_rebuild_resources_publication_state_allowed"] == (
        "publication_state IN ('absent', 'current', 'stale', 'invalid')"
    )
    assert checks["ck_possession_rebuild_resources_attempt_count_bounds"] == (
        f"attempt_count BETWEEN 0 AND {MAX_SIGNED_BIGINT}"
    )
    assert checks["ck_possession_rebuild_resources_claim_version_bounds"] == (
        f"claim_version BETWEEN 0 AND {MAX_SIGNED_BIGINT}"
    )
    assert checks["ck_possession_rebuild_resources_next_generation_bounds"] == (
        f"next_generation BETWEEN 1 AND {MAX_SIGNED_BIGINT}"
    )
    for name, constraint_name in (
        ("active_parse_revision_id", "active_parse_sha256_format"),
        ("active_input_token_sha256", "active_input_sha256_format"),
        ("published_parse_revision_id", "published_parse_sha256_format"),
        ("published_input_token_sha256", "published_input_sha256_format"),
    ):
        assert checks[f"ck_possession_rebuild_resources_{constraint_name}"] == (
            f"{name} IS NULL OR {name} ~ '^[0-9a-f]{{64}}$'"
        )
    for name in ("last_error_code", "last_error_type"):
        assert checks[f"ck_possession_rebuild_resources_{name}_format"] == (
            f"{name} IS NULL OR {name} ~ '^[a-z][a-z0-9_]{{0,63}}$'"
        )
    assert {
        "ck_possession_rebuild_resources_active_claim_tuple",
        "ck_possession_rebuild_resources_active_claim_fence",
        "ck_possession_rebuild_resources_published_pointer_tuple",
        "ck_possession_rebuild_resources_publication_pointer_state",
        "ck_possession_rebuild_resources_last_issue_codes_contract",
        "ck_possession_rebuild_resources_last_error_tuple",
    } <= set(checks)
    assert len(checks) == 16


def test_ledger_checks_cover_hashes_and_state() -> None:
    assert _check_sql("clickhouse_schema_migrations") == {
        "ck_clickhouse_schema_migrations_checksum_sha256_format": (
            "checksum_sha256 ~ '^[0-9a-f]{64}$'"
        ),
        "ck_clickhouse_schema_migrations_fingerprint_sha256_format": (
            "expected_schema_fingerprint_sha256 ~ '^[0-9a-f]{64}$'"
        ),
        "ck_clickhouse_schema_migrations_state_allowed": "state IN ('applying', 'applied')",
        "ck_clickhouse_schema_migrations_applied_at_state": (
            "(state = 'applying' AND applied_at IS NULL) OR "
            "(state = 'applied' AND applied_at IS NOT NULL)"
        ),
    }


def test_unique_keys_and_real_foreign_keys_match_the_contract() -> None:
    attempts = _table("possession_snapshot_attempts")
    resources = _table("possession_rebuild_resources")
    assert {
        (constraint.name, _column_names(constraint.columns))
        for constraint in attempts.constraints
        if isinstance(constraint, UniqueConstraint)
    } == {
        ("uq_possession_snapshot_attempts_game_snapshot", ("game_id", "snapshot_id")),
        ("uq_possession_snapshot_attempts_game_generation", ("game_id", "generation")),
    }
    assert {
        (
            constraint.name,
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            constraint.deferrable,
            constraint.initially,
        )
        for constraint in resources.foreign_key_constraints
    } == {
        ("fk_possession_rebuild_resources_game", ("game_id",), "games", ("game_id",), None, None),
        (
            "fk_possession_rebuild_resources_last_run",
            ("last_run_id",),
            "possession_rebuild_runs",
            ("run_id",),
            None,
            None,
        ),
        (
            "fk_possession_rebuild_resources_active_run",
            ("active_run_id",),
            "possession_rebuild_runs",
            ("run_id",),
            None,
            None,
        ),
        (
            "fk_possession_rebuild_resources_active_snapshot",
            ("game_id", "active_snapshot_id"),
            "possession_snapshot_attempts",
            ("game_id", "snapshot_id"),
            True,
            "DEFERRED",
        ),
        (
            "fk_possession_rebuild_resources_published_snapshot",
            ("game_id", "published_snapshot_id"),
            "possession_snapshot_attempts",
            ("game_id", "snapshot_id"),
            True,
            "DEFERRED",
        ),
    }
    assert {
        (
            constraint.name,
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
        )
        for constraint in attempts.foreign_key_constraints
    } == {
        ("fk_possession_snapshot_attempts_game", ("game_id",), "games"),
        (
            "fk_possession_snapshot_attempts_input_pbp_document",
            ("input_pbp_document_id",),
            "source_documents",
        ),
        (
            "fk_possession_snapshot_attempts_created_by_run",
            ("created_by_run_id",),
            "possession_rebuild_runs",
        ),
    }


def test_phase_three_tables_are_exported_shared_metadata_objects() -> None:
    for name in PHASE_THREE_TABLES:
        assert getattr(schema, name, None) is _table(name)
