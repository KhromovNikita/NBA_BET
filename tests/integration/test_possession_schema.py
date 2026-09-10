import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests import runtime_config

pytestmark = pytest.mark.integration

PHASE_THREE_TABLES = {
    "possession_rebuild_runs",
    "possession_snapshot_attempts",
    "possession_rebuild_resources",
    "clickhouse_schema_migrations",
}
SHA = "a" * 64
OTHER_SHA = "b" * 64


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


async def _require_tables(connection: AsyncConnection) -> None:
    names = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_table_names()
    )
    assert set(names) >= PHASE_THREE_TABLES


def _get_columns(sync_connection: Connection, table_name: str) -> list[ReflectedColumn]:
    return inspect(sync_connection).get_columns(table_name)


async def _seed_game_input_run(
    connection: AsyncConnection, *, suffix: int = 0
) -> tuple[str, UUID, UUID]:
    game_id = f"002250{suffix:04d}"
    document_id = uuid4()
    run_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO seasons (season, season_type, season_start_year)
            VALUES ('2025-26', 'regular_season', 2025)
            ON CONFLICT DO NOTHING
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO teams (team_id, city, name, tricode, slug)
            VALUES
                (910001, 'Home', 'Home Team', 'HOM', 'home-team'),
                (910002, 'Away', 'Away Team', 'AWY', 'away-team')
            ON CONFLICT DO NOTHING
            """
        )
    )
    await connection.execute(
        text(
            """
            INSERT INTO games (
                game_id, season, season_type, game_date, started_at,
                home_team_id, away_team_id, home_score, away_score, status
            ) VALUES (
                :game_id, '2025-26', 'regular_season', '2026-01-02',
                '2026-01-02T00:00:00+00:00', 910001, 910002, 110, 108, 'final'
            )
            """
        ),
        {"game_id": game_id},
    )
    await connection.execute(
        text(
            """
            INSERT INTO source_documents (
                document_id, source, endpoint, resource_key, request_parameters,
                payload, checksum_sha256, requested_at
            ) VALUES (
                :document_id, 'nba', 'play_by_play', :resource_key,
                '{}'::jsonb, '{}'::jsonb, :sha, '2026-01-02T01:00:00+00:00'
            )
            """
        ),
        {"document_id": document_id, "resource_key": f"play_by_play:{game_id}", "sha": SHA},
    )
    await connection.execute(
        text(
            """
            INSERT INTO possession_rebuild_runs (
                run_id, selectors, mode, forced_rebuild, status
            ) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running')
            """
        ),
        {"run_id": run_id},
    )
    return game_id, document_id, run_id


async def _insert_attempt(
    connection: AsyncConnection,
    *,
    game_id: str,
    document_id: UUID,
    run_id: UUID,
    snapshot_id: UUID | None = None,
    generation: int = 1,
    state: str = "allocated",
    extra_columns: str = "",
    extra_values: str = "",
    parameters: dict[str, object] | None = None,
) -> UUID:
    resolved_snapshot_id = snapshot_id or uuid4()
    values: dict[str, object] = {
        "snapshot_id": resolved_snapshot_id,
        "game_id": game_id,
        "generation": generation,
        "document_id": document_id,
        "run_id": run_id,
        "state": state,
        "sha": SHA,
    }
    values.update(parameters or {})
    await connection.execute(
        text(
            f"""
            INSERT INTO possession_snapshot_attempts (
                snapshot_id, game_id, generation, parse_revision_id,
                input_token_sha256, input_pbp_document_id, input_pbp_sha256,
                input_event_sha256, game_context_sha256, normalizer_version,
                parser_version, rules_version, output_schema_version, state,
                created_by_run_id, created_by_claim_version {extra_columns}
            ) VALUES (
                :snapshot_id, :game_id, :generation, :sha, :sha,
                :document_id, :sha, :sha, :sha, 'nba-pbp-v1',
                'analytical-v1', 'nba-2025-v1', 'possessions-v1', :state,
                :run_id, 1 {extra_values}
            )
            """
        ),
        values,
    )
    return resolved_snapshot_id


async def test_real_schema_has_exact_columns_uniques_and_deferrable_foreign_keys(
    connection: AsyncConnection,
) -> None:
    await _require_tables(connection)
    expected_columns = {
        "possession_rebuild_runs": 13,
        "possession_snapshot_attempts": 24,
        "possession_rebuild_resources": 22,
        "clickhouse_schema_migrations": 5,
    }
    for table_name, column_count in expected_columns.items():
        columns = await connection.run_sync(_get_columns, table_name)
        assert len(columns) == column_count

    attempts_uniques = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_unique_constraints(
            "possession_snapshot_attempts"
        )
    )
    assert {item["name"]: item["column_names"] for item in attempts_uniques} == {
        "uq_possession_snapshot_attempts_game_generation": ["game_id", "generation"],
        "uq_possession_snapshot_attempts_game_snapshot": ["game_id", "snapshot_id"],
    }

    resource_foreign_keys = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_foreign_keys(
            "possession_rebuild_resources"
        )
    )
    by_name = {item["name"]: item for item in resource_foreign_keys}
    for name in (
        "fk_possession_rebuild_resources_active_snapshot",
        "fk_possession_rebuild_resources_published_snapshot",
    ):
        assert by_name[name]["constrained_columns"] in (
            ["game_id", "active_snapshot_id"],
            ["game_id", "published_snapshot_id"],
        )
        assert by_name[name]["referred_table"] == "possession_snapshot_attempts"
        assert by_name[name]["referred_columns"] == ["game_id", "snapshot_id"]
        assert by_name[name]["options"] == {"initially": "DEFERRED", "deferrable": True}

    attempt_foreign_keys = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_foreign_keys(
            "possession_snapshot_attempts"
        )
    )
    assert {item["referred_table"] for item in attempt_foreign_keys} == {
        "games",
        "source_documents",
        "possession_rebuild_runs",
    }


async def test_resource_initial_state_and_deferred_active_attempt_pointer(
    connection: AsyncConnection,
) -> None:
    await _require_tables(connection)
    game_id, document_id, run_id = await _seed_game_input_run(connection)
    snapshot_id = uuid4()
    await connection.execute(
        text("INSERT INTO possession_rebuild_resources (game_id) VALUES (:game_id)"),
        {"game_id": game_id},
    )
    initial = (
        await connection.execute(
            text(
                """
                SELECT publication_state, attempt_count, claim_version, next_generation,
                       last_issue_codes, last_run_id, active_snapshot_id,
                       published_snapshot_id, last_error_code, completed_at
                FROM possession_rebuild_resources WHERE game_id = :game_id
                """
            ),
            {"game_id": game_id},
        )
    ).one()
    assert tuple(initial) == ("absent", 0, 0, 1, [], None, None, None, None, None)

    await connection.execute(
        text(
            """
            UPDATE possession_rebuild_resources
            SET active_run_id = :run_id,
                active_claim_version = 1,
                active_snapshot_id = :snapshot_id,
                active_parse_revision_id = :sha,
                active_input_token_sha256 = :sha,
                claimed_until = '2026-01-02T02:00:00+00:00',
                claim_version = 1
            WHERE game_id = :game_id
            """
        ),
        {"game_id": game_id, "run_id": run_id, "snapshot_id": snapshot_id, "sha": SHA},
    )
    await _insert_attempt(
        connection,
        game_id=game_id,
        document_id=document_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
    )
    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    """
                    UPDATE possession_rebuild_resources SET claim_version = 2
                    WHERE game_id = :game_id
                    """
                ),
                {"game_id": game_id},
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("mode", "rebuild"),
        ("status", "complete"),
        ("discovered", -1),
        ("materialized", -1),
        ("skipped_current", -1),
        ("verified", -1),
        ("failed", -1),
        ("busy", -1),
    ],
)
async def test_run_rejects_invalid_states_and_negative_counters(
    connection: AsyncConnection, column: str, value: object
) -> None:
    await _require_tables(connection)
    run_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO possession_rebuild_runs (
                run_id, selectors, mode, forced_rebuild, status
            ) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running')
            """
        ),
        {"run_id": run_id},
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"UPDATE possession_rebuild_runs SET {column} = :value WHERE run_id = :run_id"
                ),
                {"run_id": run_id, "value": value},
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("generation", 0),
        ("parse_revision_id", "A" * 64),
        ("normalizer_version", "NBA PBP V1"),
        ("created_by_claim_version", 0),
    ],
)
async def test_attempt_rejects_invalid_bounds_hash_versions_and_claims(
    connection: AsyncConnection, column: str, value: object
) -> None:
    await _require_tables(connection)
    game_id, document_id, run_id = await _seed_game_input_run(connection)
    snapshot_id = await _insert_attempt(
        connection, game_id=game_id, document_id=document_id, run_id=run_id
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"UPDATE possession_snapshot_attempts SET {column} = :value "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id, "value": value},
            )


@pytest.mark.parametrize(
    ("state", "extra_columns", "extra_values"),
    [
        ("allocated", ", expected_row_count", ", 1"),
        ("writing", "", ""),
        (
            "verified",
            ", expected_row_count, expected_output_sha256, insert_started_at",
            ", 1, :sha, CURRENT_TIMESTAMP",
        ),
        (
            "published",
            ", expected_row_count, expected_output_sha256, insert_started_at, verified_at",
            ", 1, :sha, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP",
        ),
        ("poisoned", "", ""),
        ("superseded", ", safe_error_code, safe_error_type", ", 'bad_rows', 'schema_error'"),
    ],
)
async def test_attempt_state_requires_compatible_counts_timestamps_and_errors(
    connection: AsyncConnection, state: str, extra_columns: str, extra_values: str
) -> None:
    await _require_tables(connection)
    game_id, document_id, run_id = await _seed_game_input_run(connection)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(
                connection,
                game_id=game_id,
                document_id=document_id,
                run_id=run_id,
                state=state,
                extra_columns=extra_columns,
                extra_values=extra_values,
            )


@pytest.mark.parametrize(
    "overrides",
    [
        "attempt_count = -1",
        "claim_version = -1",
        "next_generation = 0",
        "active_run_id = '00000000-0000-0000-0000-000000000001'::uuid",
        "active_run_id = NULL, active_claim_version = 1",
        "publication_state = 'absent', "
        "published_snapshot_id = '00000000-0000-0000-0000-000000000001'::uuid, "
        f"published_parse_revision_id = '{SHA}', published_input_token_sha256 = '{SHA}', "
        "published_at = CURRENT_TIMESTAMP",
        "last_error_code = 'Bad Code'",
        "last_error_code = 'bad_code', last_error_type = NULL",
    ],
)
async def test_resource_rejects_invalid_bounds_partial_tuples_and_pointer_states(
    connection: AsyncConnection, overrides: str
) -> None:
    await _require_tables(connection)
    game_id, _, _ = await _seed_game_input_run(connection)
    await connection.execute(
        text("INSERT INTO possession_rebuild_resources (game_id) VALUES (:game_id)"),
        {"game_id": game_id},
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"UPDATE possession_rebuild_resources SET {overrides} WHERE game_id = :game_id"
                ),
                {"game_id": game_id},
            )


@pytest.mark.parametrize(
    "issues",
    [
        {"code": "offense_inferred"},
        [{"code": "Bad Code"}],
        [{"code": "offense_inferred", "description": "provider text"}],
        [{"code": "offense_inferred", "sequence_number": 0}],
        [{"code": "offense_inferred", "related_sequence_number": 1.5}],
        [{"sequence_number": 1}],
        [{"code": "offense_inferred"}] * 33,
    ],
)
async def test_resource_rejects_malformed_or_unbounded_issue_json(
    connection: AsyncConnection, issues: object
) -> None:
    await _require_tables(connection)
    game_id, _, _ = await _seed_game_input_run(connection)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    """
                    INSERT INTO possession_rebuild_resources (game_id, last_issue_codes)
                    VALUES (:game_id, CAST(:issues AS jsonb))
                    """
                ),
                {"game_id": game_id, "issues": json.dumps(issues)},
            )


async def test_attempt_unique_generation_and_real_foreign_keys(connection: AsyncConnection) -> None:
    await _require_tables(connection)
    game_id, document_id, run_id = await _seed_game_input_run(connection)
    await _insert_attempt(
        connection, game_id=game_id, document_id=document_id, run_id=run_id, generation=1
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(
                connection, game_id=game_id, document_id=document_id, run_id=run_id, generation=1
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(
                connection,
                game_id=game_id,
                document_id=uuid4(),
                run_id=run_id,
                generation=2,
            )


@pytest.mark.parametrize(
    ("checksum", "fingerprint", "state", "applied"),
    [
        ("A" * 64, SHA, "applying", None),
        (SHA, "B" * 64, "applying", None),
        (SHA, SHA, "unknown", None),
        (SHA, SHA, "applying", "CURRENT_TIMESTAMP"),
        (SHA, SHA, "applied", None),
    ],
)
async def test_clickhouse_ledger_rejects_invalid_hashes_and_state_timestamps(
    connection: AsyncConnection,
    checksum: str,
    fingerprint: str,
    state: str,
    applied: str | None,
) -> None:
    await _require_tables(connection)
    applied_sql = applied or "NULL"
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    f"""
                    INSERT INTO clickhouse_schema_migrations (
                        migration_id, checksum_sha256,
                        expected_schema_fingerprint_sha256, state, applied_at
                    ) VALUES ('0001_possessions', :checksum, :fingerprint, :state, {applied_sql})
                    """
                ),
                {"checksum": checksum, "fingerprint": fingerprint, "state": state},
            )
