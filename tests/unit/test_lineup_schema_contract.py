"""Contract tests for the Phase 4 lineup reconstruction control plane."""

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID

from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.postgres import schema

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
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


def test_lineup_metadata_exports_real_control_plane_tables() -> None:
    for name in TABLES:
        assert getattr(schema, name) is _table(name)
