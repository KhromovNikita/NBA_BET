"""Live PostgreSQL checks for the Phase 4 control-plane tables."""
# ruff: noqa: E501

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests import runtime_config

pytestmark = pytest.mark.integration

TABLES = {"lineup_rebuild_runs", "lineup_snapshot_attempts", "lineup_rebuild_resources"}


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(runtime_config.postgres_url())
    async with engine.connect() as database_connection:
        transaction = await database_connection.begin()
        try:
            yield database_connection
        finally:
            await transaction.rollback()
    await engine.dispose()


async def _seed_lineup_inputs(connection: AsyncConnection) -> tuple[str, UUID, UUID, UUID]:
    game_id, box_document_id, pbp_document_id, run_id = "0022509999", uuid4(), uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO seasons (season, season_type, season_start_year) VALUES ('2025-26', 'regular_season', 2025) ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO teams (team_id, city, name, tricode, slug) VALUES (920001, 'Home', 'Home', 'HOM', 'home'), (920002, 'Away', 'Away', 'AWY', 'away') ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO games (game_id, season, season_type, game_date, started_at, home_team_id, away_team_id, home_score, away_score, status) VALUES (:game_id, '2025-26', 'regular_season', '2026-01-02', '2026-01-02T00:00:00+00:00', 920001, 920002, 1, 0, 'final')"
        ),
        {"game_id": game_id},
    )
    for document_id, endpoint in (
        (box_document_id, "box_score"),
        (pbp_document_id, "play_by_play"),
    ):
        await connection.execute(
            text(
                "INSERT INTO source_documents (document_id, source, endpoint, resource_key, request_parameters, payload, checksum_sha256, requested_at) VALUES (:id, 'nba', :endpoint, :key, '{}'::jsonb, '{}'::jsonb, :sha, '2026-01-02T01:00:00+00:00')"
            ),
            {
                "id": document_id,
                "endpoint": endpoint,
                "key": f"{endpoint}:{game_id}",
                "sha": "a" * 64,
            },
        )
    await connection.execute(
        text(
            "INSERT INTO lineup_rebuild_runs (run_id, selectors, mode, forced_rebuild, status) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running')"
        ),
        {"run_id": run_id},
    )
    return game_id, box_document_id, pbp_document_id, run_id


async def _insert_attempt(
    connection: AsyncConnection,
    game_id: str,
    box_document_id: UUID,
    pbp_document_id: UUID,
    run_id: UUID,
    *,
    snapshot_id: UUID | None = None,
    generation: int = 1,
    state: str = "allocated",
    issue_codes: str = "NULL",
    result_status: str = "NULL",
) -> UUID:
    snapshot_id = snapshot_id or uuid4()
    await connection.execute(
        text(
            f"""INSERT INTO lineup_snapshot_attempts (snapshot_id, game_id, generation, lineup_revision_id, input_token_sha256, input_box_score_document_id, input_box_score_sha256, input_pbp_document_id, input_pbp_sha256, canonical_player_box_sha256, canonical_event_sha256, game_context_sha256, normalizer_version, reconstructor_version, rules_version, output_schema_version, expected_row_count, expected_output_sha256, expected_issue_codes, result_status, state, created_by_run_id, created_by_claim_version, insert_started_at, verified_at, published_at) VALUES (:snapshot_id, :game_id, :generation, :sha, :sha, :box_id, :sha, :pbp_id, :sha, :sha, :sha, :sha, 'nba-v1', 'lineup-v1', 'nba-v1', 'lineups-v1', NULL, NULL, {issue_codes}, {result_status}, :state, :run_id, 1, NULL, NULL, NULL)"""
        ),
        {
            "snapshot_id": snapshot_id,
            "game_id": game_id,
            "generation": generation,
            "sha": "a" * 64,
            "box_id": box_document_id,
            "pbp_id": pbp_document_id,
            "state": state,
            "run_id": run_id,
        },
    )
    return snapshot_id


async def test_lineup_control_plane_has_all_tables_and_deferred_pointer_fks() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            foreign_keys = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys("lineup_rebuild_resources")
            )
    finally:
        await engine.dispose()
    assert TABLES <= set(names)
    pointers = {fk["name"]: fk for fk in foreign_keys}
    assert pointers["fk_lineup_rebuild_resources_active_snapshot"]["options"] == {
        "initially": "DEFERRED",
        "deferrable": True,
    }
    assert pointers["fk_lineup_rebuild_resources_published_snapshot"]["options"] == {
        "initially": "DEFERRED",
        "deferrable": True,
    }


async def test_lineup_database_rejects_bad_counters_generations_json_and_pointers(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_runs (run_id, selectors, mode, forced_rebuild, status, invalid) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running', -1)"
                ),
                {"run_id": uuid4()},
            )
    await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, publication_state) VALUES (:game_id, 'stale')"
                ),
                {"game_id": game_id},
            )
    attempt_id = await _insert_attempt(connection, game_id, box_id, pbp_id, run_id, generation=2)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_snapshot_attempts SET state = 'writing', expected_row_count = 1, expected_output_sha256 = :sha, expected_issue_codes = '[{}]'::jsonb, result_status = 'invalid', insert_started_at = CURRENT_TIMESTAMP WHERE snapshot_id = :snapshot_id"
                ),
                {"sha": "a" * 64, "snapshot_id": attempt_id},
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_snapshot_attempts SET state = 'writing', expected_row_count = 1, "
                    "expected_output_sha256 = :sha, expected_issue_codes = "
                    '\'[{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"},{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"}]\'::jsonb, '
                    "result_status = 'invalid', insert_started_at = CURRENT_TIMESTAMP "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"sha": "a" * 64, "snapshot_id": attempt_id},
            )


async def test_lineup_deferred_pointer_can_reference_attempt_created_later(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    snapshot_id = uuid4()
    async with connection.begin_nested():
        await connection.execute(
            text(
                "INSERT INTO lineup_rebuild_resources (game_id, claim_version, active_run_id, active_claim_version, active_snapshot_id, active_lineup_revision_id, active_input_token_sha256, claimed_until) VALUES (:game_id, 1, :run_id, 1, :snapshot_id, :sha, :sha, CURRENT_TIMESTAMP)"
            ),
            {"game_id": game_id, "run_id": run_id, "snapshot_id": snapshot_id, "sha": "a" * 64},
        )
        await _insert_attempt(connection, game_id, box_id, pbp_id, run_id, snapshot_id=snapshot_id)
        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_lineup_database_rejects_complete_control_plane_mutation_matrix(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    invalid_issue = (
        '[{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"}]'
    )
    rejected_updates: tuple[tuple[str, dict[str, object]], ...] = (
        ("lineup_revision_id = 'bad'", {}),
        ("input_pbp_document_id = :missing_document", {"missing_document": uuid4()}),
        ("safe_error_code = 'safe_error'", {}),
        ("state = 'verified'", {}),
        (
            "state = 'writing', expected_row_count = 1, expected_output_sha256 = :sha, expected_issue_codes = :issues::jsonb, result_status = 'valid', insert_started_at = CURRENT_TIMESTAMP",
            {"sha": "a" * 64, "issues": invalid_issue},
        ),
    )
    for assignment, parameters in rejected_updates:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        f"UPDATE lineup_snapshot_attempts SET {assignment} WHERE snapshot_id = :snapshot_id"
                    ),
                    parameters | {"snapshot_id": attempt_id},
                )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, active_run_id) VALUES (:game_id, :run_id)"
                ),
                {"game_id": game_id, "run_id": run_id},
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, claim_version, active_run_id, active_claim_version, active_snapshot_id, active_lineup_revision_id, active_input_token_sha256, claimed_until) VALUES (:game_id, 2, :run_id, 1, :snapshot_id, :sha, :sha, CURRENT_TIMESTAMP)"
                ),
                {"game_id": game_id, "run_id": run_id, "snapshot_id": attempt_id, "sha": "a" * 64},
            )
    await connection.execute(
        text("INSERT INTO lineup_rebuild_resources (game_id) VALUES (:game_id)"),
        {"game_id": game_id},
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_rebuild_resources SET last_issue_codes = '[{}]'::jsonb WHERE game_id = :game_id"
                ),
                {"game_id": game_id},
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_rebuild_resources SET publication_state = 'invalid' WHERE game_id = :game_id"
                ),
                {"game_id": game_id},
            )
