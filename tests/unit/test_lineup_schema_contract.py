"""Contract tests for the Phase 4 lineup reconstruction control plane."""
# ruff: noqa: E501

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql.base import PGDialect

from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.postgres import schema

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
POSTGRES_DIALECT = PGDialect()  # type: ignore[no-untyped-call]
TABLES = {
    "lineup_rebuild_runs",
    "lineup_snapshot_attempts",
    "lineup_rebuild_resources",
}


def _table(name: str):  # type: ignore[no-untyped-def]
    assert name in metadata.tables, f"shared metadata is missing {name}"
    return metadata.tables[name]


def _checks(name: str) -> dict[str, str]:
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in _table(name).constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_lineup_tables_have_exact_column_order_and_core_types() -> None:
    assert tuple(_table("lineup_rebuild_runs").c.keys()) == (
        "run_id",
        "selectors",
        "mode",
        "forced_rebuild",
        "status",
        "discovered",
        "materialized",
        "invalid",
        "skipped_current",
        "verified",
        "failed",
        "busy",
        "started_at",
        "finished_at",
    )
    assert tuple(_table("lineup_snapshot_attempts").c.keys()) == (
        "snapshot_id",
        "game_id",
        "generation",
        "lineup_revision_id",
        "input_token_sha256",
        "input_box_score_document_id",
        "input_box_score_sha256",
        "input_pbp_document_id",
        "input_pbp_sha256",
        "canonical_player_box_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
        "normalizer_version",
        "reconstructor_version",
        "rules_version",
        "output_schema_version",
        "expected_row_count",
        "expected_output_sha256",
        "expected_issue_codes",
        "result_status",
        "state",
        "created_by_run_id",
        "created_by_claim_version",
        "insert_started_at",
        "verified_at",
        "published_at",
        "safe_error_code",
        "safe_error_type",
        "created_at",
    )
    assert tuple(_table("lineup_rebuild_resources").c.keys()) == (
        "game_id",
        "publication_state",
        "attempt_count",
        "claim_version",
        "last_run_id",
        "next_generation",
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_lineup_revision_id",
        "active_input_token_sha256",
        "claimed_until",
        "published_snapshot_id",
        "published_lineup_revision_id",
        "published_input_token_sha256",
        "published_at",
        "last_issue_codes",
        "last_error_code",
        "last_error_type",
        "created_at",
        "updated_at",
        "completed_at",
    )
    runs, attempts, resources = (
        _table(name)
        for name in ("lineup_rebuild_runs", "lineup_snapshot_attempts", "lineup_rebuild_resources")
    )
    assert isinstance(runs.c.run_id.type, UUID) and isinstance(runs.c.selectors.type, JSONB)
    assert isinstance(runs.c.forced_rebuild.type, Boolean) and isinstance(
        runs.c.invalid.type, Integer
    )
    for column in (
        attempts.c.snapshot_id,
        attempts.c.input_box_score_document_id,
        attempts.c.input_pbp_document_id,
        attempts.c.created_by_run_id,
        resources.c.last_run_id,
        resources.c.active_run_id,
        resources.c.active_snapshot_id,
        resources.c.published_snapshot_id,
    ):
        assert isinstance(column.type, UUID)
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
    for column in (attempts.c.expected_issue_codes, resources.c.last_issue_codes):
        assert isinstance(column.type, JSONB)
    for table in (runs, attempts, resources):
        for column in table.c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone


def test_lineup_schema_enforces_lifecycle_lineage_and_fence_contracts() -> None:
    attempts = _checks("lineup_snapshot_attempts")
    resources = _checks("lineup_rebuild_resources")
    assert attempts["ck_lineup_snapshot_attempts_state_allowed"] == (
        "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')"
    )
    assert attempts["ck_lineup_snapshot_attempts_result_status_allowed"] == (
        "result_status IS NULL OR result_status IN ('valid', 'invalid')"
    )
    assert attempts["ck_lineup_snapshot_attempts_generation_bounds"] == (
        f"generation BETWEEN 1 AND {MAX_SIGNED_BIGINT}"
    )
    assert {
        "ck_lineup_snapshot_attempts_expected_metadata",
        "ck_lineup_snapshot_attempts_state_fields",
        "ck_lineup_snapshot_attempts_state_error_fields",
        "ck_lineup_snapshot_attempts_expected_issue_codes_contract",
    } <= set(attempts)
    assert resources["ck_lineup_rebuild_resources_publication_state_allowed"] == (
        "publication_state IN ('absent', 'current', 'stale', 'invalid')"
    )
    assert {
        "ck_lineup_rebuild_resources_active_claim_tuple",
        "ck_lineup_rebuild_resources_active_claim_fence",
        "ck_lineup_rebuild_resources_published_pointer_tuple",
        "ck_lineup_rebuild_resources_publication_pointer_state",
        "ck_lineup_rebuild_resources_last_issue_codes_contract",
    } <= set(resources)


def test_issue_json_projections_allow_only_canonical_safe_issues() -> None:
    attempts = _checks("lineup_snapshot_attempts")
    resources = _checks("lineup_rebuild_resources")
    expected = attempts["ck_lineup_snapshot_attempts_expected_issue_codes_contract"]
    last = resources["ck_lineup_rebuild_resources_last_issue_codes_contract"]
    for sql in (expected, last):
        assert "jsonb_typeof" in sql
        assert "jsonb_array_length" in sql
        assert "team_id" in sql
        assert "reason" in sql
        assert "sequence_number" in sql
        assert "boundary_side" in sql
        assert '@.key != "team_id"' in sql
        assert "lineup_issue_codes_unique" in sql
    assert "result_status = 'valid'" in attempts["ck_lineup_snapshot_attempts_expected_metadata"]
    assert "result_status = 'invalid'" in attempts["ck_lineup_snapshot_attempts_expected_metadata"]


def test_stale_and_invalid_resources_require_complete_published_pointer() -> None:
    pointer_state = _checks("lineup_rebuild_resources")[
        "ck_lineup_rebuild_resources_publication_pointer_state"
    ]
    assert (
        "publication_state IN ('stale', 'invalid') AND published_snapshot_id IS NOT NULL"
        in pointer_state
    )


def test_lineup_metadata_exports_real_control_plane_tables() -> None:
    for name in TABLES:
        assert getattr(schema, name) is _table(name)
        assert name in schema.__all__


def test_lineup_defaults_nullability_keys_foreign_keys_and_indexes_are_pinned() -> None:
    runs, attempts, resources = (
        _table(name)
        for name in ("lineup_rebuild_runs", "lineup_snapshot_attempts", "lineup_rebuild_resources")
    )
    assert {column.name for column in runs.c if column.nullable} == {"finished_at"}
    assert {column.name for column in attempts.c if column.nullable} == {
        "expected_row_count",
        "expected_output_sha256",
        "expected_issue_codes",
        "result_status",
        "insert_started_at",
        "verified_at",
        "published_at",
        "safe_error_code",
        "safe_error_type",
    }
    assert {column.name for column in resources.c if column.nullable} == {
        "last_run_id",
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_lineup_revision_id",
        "active_input_token_sha256",
        "claimed_until",
        "published_snapshot_id",
        "published_lineup_revision_id",
        "published_input_token_sha256",
        "published_at",
        "last_error_code",
        "last_error_type",
        "completed_at",
    }
    assert {
        column.name: str(column.server_default.arg) for column in runs.c if column.server_default
    } == {
        **{
            name: "0"
            for name in (
                "discovered",
                "materialized",
                "invalid",
                "skipped_current",
                "verified",
                "failed",
                "busy",
            )
        },
        "started_at": "now()",
    }
    assert not attempts.indexes and not resources.indexes
    assert {constraint.name for constraint in attempts.foreign_key_constraints} == {
        "fk_lineup_snapshot_attempts_game",
        "fk_lineup_snapshot_attempts_input_box_score_document",
        "fk_lineup_snapshot_attempts_input_pbp_document",
        "fk_lineup_snapshot_attempts_created_by_run",
    }
    assert {constraint.name for constraint in resources.foreign_key_constraints} == {
        "fk_lineup_rebuild_resources_game",
        "fk_lineup_rebuild_resources_last_run",
        "fk_lineup_rebuild_resources_active_run",
        "fk_lineup_rebuild_resources_active_snapshot",
        "fk_lineup_rebuild_resources_published_snapshot",
    }


def test_lineup_foreign_key_targets_uniques_and_critical_check_sql_are_exact() -> None:
    attempts, resources = _table("lineup_snapshot_attempts"), _table("lineup_rebuild_resources")
    assert {
        (
            fk.name,
            tuple(item.parent.name for item in fk.elements),
            fk.referred_table.name,
            tuple(item.column.name for item in fk.elements),
            fk.deferrable,
            fk.initially,
        )
        for fk in attempts.foreign_key_constraints
    } == {
        ("fk_lineup_snapshot_attempts_game", ("game_id",), "games", ("game_id",), None, None),
        (
            "fk_lineup_snapshot_attempts_input_box_score_document",
            ("input_box_score_document_id",),
            "source_documents",
            ("document_id",),
            None,
            None,
        ),
        (
            "fk_lineup_snapshot_attempts_input_pbp_document",
            ("input_pbp_document_id",),
            "source_documents",
            ("document_id",),
            None,
            None,
        ),
        (
            "fk_lineup_snapshot_attempts_created_by_run",
            ("created_by_run_id",),
            "lineup_rebuild_runs",
            ("run_id",),
            None,
            None,
        ),
    }
    assert {
        (
            fk.name,
            tuple(item.parent.name for item in fk.elements),
            fk.referred_table.name,
            tuple(item.column.name for item in fk.elements),
            fk.deferrable,
            fk.initially,
        )
        for fk in resources.foreign_key_constraints
    } == {
        ("fk_lineup_rebuild_resources_game", ("game_id",), "games", ("game_id",), None, None),
        (
            "fk_lineup_rebuild_resources_last_run",
            ("last_run_id",),
            "lineup_rebuild_runs",
            ("run_id",),
            None,
            None,
        ),
        (
            "fk_lineup_rebuild_resources_active_run",
            ("active_run_id",),
            "lineup_rebuild_runs",
            ("run_id",),
            None,
            None,
        ),
        (
            "fk_lineup_rebuild_resources_active_snapshot",
            ("game_id", "active_snapshot_id"),
            "lineup_snapshot_attempts",
            ("game_id", "snapshot_id"),
            True,
            "DEFERRED",
        ),
        (
            "fk_lineup_rebuild_resources_published_snapshot",
            ("game_id", "published_snapshot_id"),
            "lineup_snapshot_attempts",
            ("game_id", "snapshot_id"),
            True,
            "DEFERRED",
        ),
    }
    assert {
        tuple(item.columns.keys())
        for item in attempts.constraints
        if item.__class__.__name__ == "UniqueConstraint"
    } == {("game_id", "snapshot_id"), ("game_id", "generation")}
    checks = _checks("lineup_snapshot_attempts") | _checks("lineup_rebuild_resources")
    for protected in (
        "state = 'allocated'",
        "state = 'published'",
        "active_claim_version = claim_version",
        "published_snapshot_id IS NOT NULL",
        "lineup_issue_codes_unique",
    ):
        assert any(protected in sql for sql in checks.values())


def test_all_lineup_keys_defaults_and_indexes_are_exact() -> None:
    expected = {
        "lineup_rebuild_runs": (
            ("run_id",),
            {},
            {
                **{
                    name: "0"
                    for name in (
                        "discovered",
                        "materialized",
                        "invalid",
                        "skipped_current",
                        "verified",
                        "failed",
                        "busy",
                    )
                },
                "started_at": "now()",
            },
        ),
        "lineup_snapshot_attempts": (
            ("snapshot_id",),
            {
                "uq_game_snapshot": ("game_id", "snapshot_id"),
                "uq_game_generation": ("game_id", "generation"),
            },
            {"created_at": "now()"},
        ),
        "lineup_rebuild_resources": (
            ("game_id",),
            {},
            {
                "publication_state": "absent",
                "attempt_count": "0",
                "claim_version": "0",
                "next_generation": "1",
                "last_issue_codes": "'[]'::jsonb",
                "created_at": "now()",
                "updated_at": "now()",
            },
        ),
    }
    for name, (primary_key, uniques, defaults) in expected.items():
        table = _table(name)
        assert tuple(table.primary_key.columns.keys()) == primary_key
        assert table.primary_key.name == f"pk_{name}"
        assert {
            c.name: tuple(c.columns.keys())
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        } == uniques
        assert {
            c.name: str(c.server_default.arg) for c in table.c if c.server_default is not None
        } == defaults
        assert not table.indexes
        assert all(c.default is None for c in table.c)
        for foreign_key in table.foreign_key_constraints:
            assert foreign_key.ondelete is None
            assert foreign_key.onupdate is None
            assert foreign_key.match is None
    assert not _table("lineup_rebuild_runs").foreign_key_constraints


def _expected_issue_check(column: str, *, nullable: bool) -> str:
    # Independent literal SQL contract: do not import the production predicate builder.
    predicate = """jsonb_typeof(COLUMN) = 'array' AND jsonb_array_length(COLUMN) <= 32 AND lineup_issue_codes_unique(COLUMN) AND NOT COLUMN @? '$[*] ? (@.type() != "object")' AND NOT COLUMN @? '$[*].keyvalue() ? (@.key != "team_id" && @.key != "reason" && @.key != "sequence_number" && @.key != "boundary_side")' AND NOT COLUMN @? '$[*] ? (!exists(@.team_id) || @.team_id.type() != "number" || @.team_id < 1 || @.team_id > 9223372036854775807 || @.team_id != @.team_id.floor())' AND NOT COLUMN @? '$[*] ? (!exists(@.reason) || @.reason.type() != "string" || !(@.reason like_regex "^[a-z][a-z0-9_]{0,63}$"))' AND NOT COLUMN @? '$[*] ? (!exists(@.sequence_number) || @.sequence_number.type() != "number" || @.sequence_number < 1 || @.sequence_number > 9223372036854775807 || @.sequence_number != @.sequence_number.floor())' AND NOT COLUMN @? '$[*] ? (!exists(@.boundary_side) || @.boundary_side.type() != "string" || (@.boundary_side != "before" && @.boundary_side != "after"))'""".replace(
        "COLUMN", column
    )
    return f"{column} IS NULL OR ({predicate})" if nullable else predicate


def test_every_column_type_including_width_is_exact() -> None:
    expected = {
        "lineup_rebuild_runs": {
            "UUID": "run_id",
            "JSONB": "selectors",
            "VARCHAR(16)": "mode status",
            "BOOLEAN": "forced_rebuild",
            "INTEGER": "discovered materialized invalid skipped_current verified failed busy",
            "TIMESTAMP WITH TIME ZONE": "started_at finished_at",
        },
        "lineup_snapshot_attempts": {
            "UUID": "snapshot_id input_box_score_document_id input_pbp_document_id created_by_run_id",
            "VARCHAR(10)": "game_id",
            "VARCHAR(16)": "result_status state",
            "BIGINT": "generation expected_row_count created_by_claim_version",
            "CHAR(64)": "lineup_revision_id input_token_sha256 input_box_score_sha256 input_pbp_sha256 canonical_player_box_sha256 canonical_event_sha256 game_context_sha256 expected_output_sha256",
            "TEXT": "normalizer_version reconstructor_version rules_version output_schema_version safe_error_code safe_error_type",
            "JSONB": "expected_issue_codes",
            "TIMESTAMP WITH TIME ZONE": "insert_started_at verified_at published_at created_at",
        },
        "lineup_rebuild_resources": {
            "UUID": "last_run_id active_run_id active_snapshot_id published_snapshot_id",
            "VARCHAR(10)": "game_id",
            "VARCHAR(16)": "publication_state",
            "BIGINT": "attempt_count claim_version next_generation active_claim_version",
            "CHAR(64)": "active_lineup_revision_id active_input_token_sha256 published_lineup_revision_id published_input_token_sha256",
            "TEXT": "last_error_code last_error_type",
            "JSONB": "last_issue_codes",
            "TIMESTAMP WITH TIME ZONE": "claimed_until published_at created_at updated_at completed_at",
        },
    }
    for name, types in expected.items():
        assert {c.name: str(c.type.compile(dialect=POSTGRES_DIALECT)) for c in _table(name).c} == {
            column: sql_type for sql_type, columns in types.items() for column in columns.split()
        }


def test_every_lineup_check_has_exact_sql() -> None:
    runs = {
        "mode_allowed": "mode IN ('materialize', 'verify')",
        "status_allowed": "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
        **{
            f"nonnegative_{name}": f"{name} >= 0"
            for name in (
                "discovered",
                "materialized",
                "invalid",
                "skipped_current",
                "verified",
                "failed",
                "busy",
            )
        },
    }
    attempts = {
        "state_allowed": "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')",
        "result_status_allowed": "result_status IS NULL OR result_status IN ('valid', 'invalid')",
        "generation_bounds": "generation BETWEEN 1 AND 9223372036854775807",
        "expected_row_count_bounds": "expected_row_count IS NULL OR expected_row_count BETWEEN 1 AND 9223372036854775807",
        "created_by_claim_version_bounds": "created_by_claim_version BETWEEN 1 AND 9223372036854775807",
        **{
            f"{name}_format": f"{name} ~ '^[0-9a-f]{{64}}$'"
            for name in (
                "lineup_revision_id",
                "input_token_sha256",
                "input_box_score_sha256",
                "input_pbp_sha256",
                "canonical_player_box_sha256",
                "canonical_event_sha256",
                "game_context_sha256",
            )
        },
        "expected_output_sha256_format": "expected_output_sha256 IS NULL OR expected_output_sha256 ~ '^[0-9a-f]{64}$'",
        **{
            f"{name}_format": f"{name} ~ '^[a-z][a-z0-9._-]{{0,127}}$'"
            for name in (
                "normalizer_version",
                "reconstructor_version",
                "rules_version",
                "output_schema_version",
            )
        },
        **{
            f"{name}_format": f"{name} IS NULL OR {name} ~ '^[a-z][a-z0-9_]{{0,63}}$'"
            for name in ("safe_error_code", "safe_error_type")
        },
        "expected_metadata": "(expected_row_count IS NULL AND expected_output_sha256 IS NULL AND expected_issue_codes IS NULL AND result_status IS NULL) OR (expected_row_count IS NOT NULL AND expected_output_sha256 IS NOT NULL AND expected_issue_codes IS NOT NULL AND result_status IS NOT NULL AND ((result_status = 'valid' AND expected_issue_codes = '[]'::jsonb) OR (result_status = 'invalid' AND jsonb_array_length(expected_issue_codes) >= 1)))",
        "state_fields": "(state = 'allocated' AND expected_row_count IS NULL AND insert_started_at IS NULL AND verified_at IS NULL AND published_at IS NULL) OR (state = 'writing' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL AND verified_at IS NULL AND published_at IS NULL) OR (state = 'verified' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL AND verified_at IS NOT NULL AND published_at IS NULL) OR (state = 'published' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL AND verified_at IS NOT NULL AND published_at IS NOT NULL) OR (state IN ('poisoned', 'superseded') AND published_at IS NULL)",
        "state_error_fields": "(state = 'poisoned' AND safe_error_code IS NOT NULL AND safe_error_type IS NOT NULL) OR (state <> 'poisoned' AND safe_error_code IS NULL AND safe_error_type IS NULL)",
        "expected_issue_codes_contract": _expected_issue_check(
            "expected_issue_codes", nullable=True
        ),
    }
    resources = {
        "publication_state_allowed": "publication_state IN ('absent', 'current', 'stale', 'invalid')",
        **{
            f"{name}_bounds": f"{name} BETWEEN {low} AND 9223372036854775807"
            for name, low in (("attempt_count", 0), ("claim_version", 0), ("next_generation", 1))
        },
        **{
            f"{name}_format": f"{name} IS NULL OR {name} ~ '^[0-9a-f]{{64}}$'"
            for name in (
                "active_lineup_revision_id",
                "active_input_token_sha256",
                "published_lineup_revision_id",
                "published_input_token_sha256",
            )
        },
        **{
            f"{name}_format": f"{name} IS NULL OR {name} ~ '^[a-z][a-z0-9_]{{0,63}}$'"
            for name in ("last_error_code", "last_error_type")
        },
        "active_claim_tuple": "(active_run_id IS NULL AND active_claim_version IS NULL AND active_snapshot_id IS NULL AND active_lineup_revision_id IS NULL AND active_input_token_sha256 IS NULL AND claimed_until IS NULL) OR (active_run_id IS NOT NULL AND active_claim_version IS NOT NULL AND active_snapshot_id IS NOT NULL AND active_lineup_revision_id IS NOT NULL AND active_input_token_sha256 IS NOT NULL AND claimed_until IS NOT NULL)",
        "active_claim_fence": "active_claim_version IS NULL OR (active_claim_version BETWEEN 1 AND 9223372036854775807 AND active_claim_version = claim_version)",
        "published_pointer_tuple": "(published_snapshot_id IS NULL AND published_lineup_revision_id IS NULL AND published_input_token_sha256 IS NULL AND published_at IS NULL) OR (published_snapshot_id IS NOT NULL AND published_lineup_revision_id IS NOT NULL AND published_input_token_sha256 IS NOT NULL AND published_at IS NOT NULL)",
        "publication_pointer_state": "(publication_state = 'current' AND published_snapshot_id IS NOT NULL) OR (publication_state = 'absent' AND published_snapshot_id IS NULL) OR (publication_state IN ('stale', 'invalid') AND published_snapshot_id IS NOT NULL)",
        "last_issue_codes_contract": _expected_issue_check("last_issue_codes", nullable=False),
        "publication_issue_codes_state": "(publication_state = 'current' AND last_issue_codes = '[]'::jsonb) OR (publication_state = 'invalid' AND jsonb_array_length(last_issue_codes) >= 1) OR publication_state IN ('absent', 'stale')",
        "last_error_tuple": "(last_error_code IS NULL AND last_error_type IS NULL) OR (last_error_code IS NOT NULL AND last_error_type IS NOT NULL)",
    }
    for name, expected in (
        ("lineup_rebuild_runs", runs),
        ("lineup_snapshot_attempts", attempts),
        ("lineup_rebuild_resources", resources),
    ):
        assert _checks(name) == {f"ck_{name}_{key}": value for key, value in expected.items()}
