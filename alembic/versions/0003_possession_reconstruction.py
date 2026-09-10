"""Add PostgreSQL control tables for possession reconstruction."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_possession_reconstruction"
down_revision: str | None = "0002_historical_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
SHA256_PATTERN = "^[0-9a-f]{64}$"
SAFE_IDENTIFIER_PATTERN = "^[a-z][a-z0-9_]{0,63}$"
VERSION_PATTERN = "^[a-z][a-z0-9._-]{0,127}$"


def upgrade() -> None:
    op.create_table(
        "possession_rebuild_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selectors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("forced_rebuild", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("discovered", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("materialized", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_current", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("verified", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("busy", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "mode IN ('materialize', 'verify')",
            name=op.f("ck_possession_rebuild_runs_mode_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
            name=op.f("ck_possession_rebuild_runs_status_allowed"),
        ),
        *(
            sa.CheckConstraint(
                f"{name} >= 0",
                name=op.f(f"ck_possession_rebuild_runs_nonnegative_{name}"),
            )
            for name in (
                "discovered",
                "materialized",
                "skipped_current",
                "verified",
                "failed",
                "busy",
            )
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_possession_rebuild_runs")),
    )

    op.create_table(
        "possession_snapshot_attempts",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("parse_revision_id", sa.CHAR(length=64), nullable=False),
        sa.Column("input_token_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("input_pbp_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_pbp_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("input_event_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("game_context_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("normalizer_version", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("rules_version", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.Text(), nullable=False),
        sa.Column("expected_row_count", sa.BigInteger()),
        sa.Column("expected_output_sha256", sa.CHAR(length=64)),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_by_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_claim_version", sa.BigInteger(), nullable=False),
        sa.Column("insert_started_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("safe_error_code", sa.Text()),
        sa.Column("safe_error_type", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')",
            name=op.f("ck_possession_snapshot_attempts_state_allowed"),
        ),
        sa.CheckConstraint(
            f"generation BETWEEN 1 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_snapshot_attempts_generation_bounds"),
        ),
        sa.CheckConstraint(
            f"expected_row_count IS NULL OR expected_row_count BETWEEN 1 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_snapshot_attempts_expected_row_count_bounds"),
        ),
        sa.CheckConstraint(
            f"created_by_claim_version BETWEEN 1 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_snapshot_attempts_created_by_claim_version_bounds"),
        ),
        *(
            sa.CheckConstraint(
                f"{name} ~ '{SHA256_PATTERN}'",
                name=op.f(f"ck_possession_snapshot_attempts_{name}_format"),
            )
            for name in (
                "parse_revision_id",
                "input_token_sha256",
                "input_pbp_sha256",
                "input_event_sha256",
                "game_context_sha256",
            )
        ),
        sa.CheckConstraint(
            f"expected_output_sha256 IS NULL OR expected_output_sha256 ~ '{SHA256_PATTERN}'",
            name=op.f("ck_possession_snapshot_attempts_expected_output_sha256_format"),
        ),
        *(
            sa.CheckConstraint(
                f"{name} ~ '{VERSION_PATTERN}'",
                name=op.f(f"ck_possession_snapshot_attempts_{name}_format"),
            )
            for name in (
                "normalizer_version",
                "parser_version",
                "rules_version",
                "output_schema_version",
            )
        ),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR {name} ~ '{SAFE_IDENTIFIER_PATTERN}'",
                name=op.f(f"ck_possession_snapshot_attempts_{name}_format"),
            )
            for name in ("safe_error_code", "safe_error_type")
        ),
        sa.CheckConstraint(
            "(state = 'allocated' AND expected_row_count IS NULL "
            "AND expected_output_sha256 IS NULL AND insert_started_at IS NULL "
            "AND verified_at IS NULL AND published_at IS NULL) OR "
            "(state = 'writing' AND expected_row_count IS NOT NULL "
            "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
            "AND verified_at IS NULL AND published_at IS NULL) OR "
            "(state = 'verified' AND expected_row_count IS NOT NULL "
            "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
            "AND verified_at IS NOT NULL AND published_at IS NULL) OR "
            "(state = 'published' AND expected_row_count IS NOT NULL "
            "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
            "AND verified_at IS NOT NULL AND published_at IS NOT NULL) OR "
            "(state IN ('poisoned', 'superseded') AND published_at IS NULL)",
            name=op.f("ck_possession_snapshot_attempts_state_fields"),
        ),
        sa.CheckConstraint(
            "(state = 'poisoned' AND safe_error_code IS NOT NULL "
            "AND safe_error_type IS NOT NULL) OR "
            "(state <> 'poisoned' AND safe_error_code IS NULL AND safe_error_type IS NULL)",
            name=op.f("ck_possession_snapshot_attempts_state_error_fields"),
        ),
        sa.ForeignKeyConstraint(
            ("game_id",),
            ("games.game_id",),
            name="fk_possession_snapshot_attempts_game",
        ),
        sa.ForeignKeyConstraint(
            ("input_pbp_document_id",),
            ("source_documents.document_id",),
            name="fk_possession_snapshot_attempts_input_pbp_document",
        ),
        sa.ForeignKeyConstraint(
            ("created_by_run_id",),
            ("possession_rebuild_runs.run_id",),
            name="fk_possession_snapshot_attempts_created_by_run",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_possession_snapshot_attempts")),
        sa.UniqueConstraint(
            "game_id",
            "snapshot_id",
            name="uq_possession_snapshot_attempts_game_snapshot",
        ),
        sa.UniqueConstraint(
            "game_id",
            "generation",
            name="uq_possession_snapshot_attempts_game_generation",
        ),
    )

    op.create_table(
        "possession_rebuild_resources",
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column(
            "publication_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'absent'"),
        ),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("claim_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("next_generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("active_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("active_claim_version", sa.BigInteger()),
        sa.Column("active_snapshot_id", postgresql.UUID(as_uuid=True)),
        sa.Column("active_parse_revision_id", sa.CHAR(length=64)),
        sa.Column("active_input_token_sha256", sa.CHAR(length=64)),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        sa.Column("published_snapshot_id", postgresql.UUID(as_uuid=True)),
        sa.Column("published_parse_revision_id", sa.CHAR(length=64)),
        sa.Column("published_input_token_sha256", sa.CHAR(length=64)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_issue_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_error_type", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "publication_state IN ('absent', 'current', 'stale', 'invalid')",
            name=op.f("ck_possession_rebuild_resources_publication_state_allowed"),
        ),
        sa.CheckConstraint(
            f"attempt_count BETWEEN 0 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_rebuild_resources_attempt_count_bounds"),
        ),
        sa.CheckConstraint(
            f"claim_version BETWEEN 0 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_rebuild_resources_claim_version_bounds"),
        ),
        sa.CheckConstraint(
            f"next_generation BETWEEN 1 AND {MAX_SIGNED_BIGINT}",
            name=op.f("ck_possession_rebuild_resources_next_generation_bounds"),
        ),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR {name} ~ '{SHA256_PATTERN}'",
                name=op.f(f"ck_possession_rebuild_resources_{constraint_name}"),
            )
            for name, constraint_name in (
                ("active_parse_revision_id", "active_parse_sha256_format"),
                ("active_input_token_sha256", "active_input_sha256_format"),
                ("published_parse_revision_id", "published_parse_sha256_format"),
                ("published_input_token_sha256", "published_input_sha256_format"),
            )
        ),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR {name} ~ '{SAFE_IDENTIFIER_PATTERN}'",
                name=op.f(f"ck_possession_rebuild_resources_{name}_format"),
            )
            for name in ("last_error_code", "last_error_type")
        ),
        sa.CheckConstraint(
            "(active_run_id IS NULL AND active_claim_version IS NULL "
            "AND active_snapshot_id IS NULL AND active_parse_revision_id IS NULL "
            "AND active_input_token_sha256 IS NULL AND claimed_until IS NULL) OR "
            "(active_run_id IS NOT NULL AND active_claim_version IS NOT NULL "
            "AND active_snapshot_id IS NOT NULL AND active_parse_revision_id IS NOT NULL "
            "AND active_input_token_sha256 IS NOT NULL AND claimed_until IS NOT NULL)",
            name=op.f("ck_possession_rebuild_resources_active_claim_tuple"),
        ),
        sa.CheckConstraint(
            "active_claim_version IS NULL OR "
            f"(active_claim_version BETWEEN 1 AND {MAX_SIGNED_BIGINT} "
            "AND active_claim_version = claim_version)",
            name=op.f("ck_possession_rebuild_resources_active_claim_fence"),
        ),
        sa.CheckConstraint(
            "(published_snapshot_id IS NULL AND published_parse_revision_id IS NULL "
            "AND published_input_token_sha256 IS NULL AND published_at IS NULL) OR "
            "(published_snapshot_id IS NOT NULL AND published_parse_revision_id IS NOT NULL "
            "AND published_input_token_sha256 IS NOT NULL AND published_at IS NOT NULL)",
            name=op.f("ck_possession_rebuild_resources_published_pointer_tuple"),
        ),
        sa.CheckConstraint(
            "(publication_state = 'current' AND published_snapshot_id IS NOT NULL) OR "
            "(publication_state = 'absent' AND published_snapshot_id IS NULL) OR "
            "publication_state IN ('stale', 'invalid')",
            name=op.f("ck_possession_rebuild_resources_publication_pointer_state"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(last_issue_codes) = 'array' "
            "AND jsonb_array_length(last_issue_codes) <= 32 "
            "AND NOT last_issue_codes @? '$[*] ? (@.type() != \"object\")' "
            "AND NOT last_issue_codes @? '$[*] ? (!exists(@.code) || "
            '@.code.type() != "string" || '
            '!(@.code like_regex "^[a-z][a-z0-9_]{0,63}$"))\' '
            'AND NOT last_issue_codes @? \'$[*].keyvalue() ? (@.key != "code" '
            '&& @.key != "sequence_number" && @.key != "related_sequence_number")\' '
            "AND NOT last_issue_codes @? '$[*] ? (exists(@.sequence_number) && "
            '(@.sequence_number.type() != "number" || @.sequence_number < 1 || '
            f"@.sequence_number > {MAX_SIGNED_BIGINT} || "
            "@.sequence_number != @.sequence_number.floor()))' "
            "AND NOT last_issue_codes @? '$[*] ? (exists(@.related_sequence_number) && "
            '(@.related_sequence_number.type() != "number" || '
            "@.related_sequence_number < 1 || "
            f"@.related_sequence_number > {MAX_SIGNED_BIGINT} || "
            "@.related_sequence_number != @.related_sequence_number.floor()))'",
            name=op.f("ck_possession_rebuild_resources_last_issue_codes_contract"),
        ),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND last_error_type IS NULL) OR "
            "(last_error_code IS NOT NULL AND last_error_type IS NOT NULL)",
            name=op.f("ck_possession_rebuild_resources_last_error_tuple"),
        ),
        sa.ForeignKeyConstraint(
            ("game_id",),
            ("games.game_id",),
            name="fk_possession_rebuild_resources_game",
        ),
        sa.ForeignKeyConstraint(
            ("last_run_id",),
            ("possession_rebuild_runs.run_id",),
            name="fk_possession_rebuild_resources_last_run",
        ),
        sa.ForeignKeyConstraint(
            ("active_run_id",),
            ("possession_rebuild_runs.run_id",),
            name="fk_possession_rebuild_resources_active_run",
        ),
        sa.ForeignKeyConstraint(
            ("game_id", "active_snapshot_id"),
            ("possession_snapshot_attempts.game_id", "possession_snapshot_attempts.snapshot_id"),
            name="fk_possession_rebuild_resources_active_snapshot",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ("game_id", "published_snapshot_id"),
            ("possession_snapshot_attempts.game_id", "possession_snapshot_attempts.snapshot_id"),
            name="fk_possession_rebuild_resources_published_snapshot",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("game_id", name=op.f("pk_possession_rebuild_resources")),
    )

    op.create_table(
        "clickhouse_schema_migrations",
        sa.Column("migration_id", sa.Text(), nullable=False),
        sa.Column("checksum_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("expected_schema_fingerprint_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            f"checksum_sha256 ~ '{SHA256_PATTERN}'",
            name=op.f("ck_clickhouse_schema_migrations_checksum_sha256_format"),
        ),
        sa.CheckConstraint(
            f"expected_schema_fingerprint_sha256 ~ '{SHA256_PATTERN}'",
            name=op.f("ck_clickhouse_schema_migrations_fingerprint_sha256_format"),
        ),
        sa.CheckConstraint(
            "state IN ('applying', 'applied')",
            name=op.f("ck_clickhouse_schema_migrations_state_allowed"),
        ),
        sa.CheckConstraint(
            "(state = 'applying' AND applied_at IS NULL) OR "
            "(state = 'applied' AND applied_at IS NOT NULL)",
            name=op.f("ck_clickhouse_schema_migrations_applied_at_state"),
        ),
        sa.PrimaryKeyConstraint("migration_id", name=op.f("pk_clickhouse_schema_migrations")),
    )


def downgrade() -> None:
    op.drop_table("clickhouse_schema_migrations")
    op.drop_table("possession_rebuild_resources")
    op.drop_table("possession_snapshot_attempts")
    op.drop_table("possession_rebuild_runs")
