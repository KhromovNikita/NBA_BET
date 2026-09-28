"""Live PostgreSQL contract checks for the Phase 5 feature control plane."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_snapshot_attempts,
)
from tests import runtime_config

pytestmark = pytest.mark.integration

FEATURE_TABLES = {
    "feature_build_runs",
    "feature_build_resources",
    "feature_snapshot_attempts",
    "feature_snapshot_dependencies",
}
SHA = "a" * 64
OTHER_SHA = "b" * 64
NOW = datetime(2030, 1, 3, tzinfo=UTC)
ISSUE = "invalid_lineup"


def _alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )


def _column_names(sync_connection: Connection, *, table_name: str) -> tuple[str, ...]:
    return tuple(column["name"] for column in inspect(sync_connection).get_columns(table_name))


def _unconstrained_indexes(
    sync_connection: Connection, *, table_name: str
) -> dict[str, tuple[str, ...]]:
    return {
        cast("str", item["name"]): tuple(cast("list[str]", item["column_names"]))
        for item in inspect(sync_connection).get_indexes(table_name)
        if not item.get("duplicates_constraint")
    }


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = _alembic("upgrade", "head")
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


async def _seed_sources(
    connection: AsyncConnection, *, suffix: int = 0
) -> tuple[str, UUID, UUID, UUID]:
    game_id = f"002251{suffix:04d}"
    box_document_id, pbp_document_id = uuid4(), uuid4()
    possession_run_id, lineup_run_id, feature_run_id = uuid4(), uuid4(), uuid4()
    possession_snapshot_id, lineup_snapshot_id = uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO seasons (season, season_type, season_start_year) "
            "VALUES ('2025-26', 'regular_season', 2025) ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO teams (team_id, city, name, tricode, slug) VALUES "
            "(930001, 'Home', 'Home', 'HOM', 'home'), "
            "(930002, 'Away', 'Away', 'AWY', 'away') ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO games (game_id, season, season_type, game_date, started_at, "
            "home_team_id, away_team_id, home_score, away_score, status) VALUES "
            "(:game_id, '2025-26', 'regular_season', '2026-01-03', "
            "'2026-01-03T00:00:00+00:00', 930001, 930002, 101, 99, 'final')"
        ),
        {"game_id": game_id},
    )
    for document_id, endpoint in (
        (box_document_id, "box_score"),
        (pbp_document_id, "play_by_play"),
    ):
        await connection.execute(
            text(
                "INSERT INTO source_documents (document_id, source, endpoint, resource_key, "
                "request_parameters, payload, checksum_sha256, requested_at) VALUES "
                "(:id, 'nba', :endpoint, :key, '{}'::jsonb, '{}'::jsonb, :sha, :at)"
            ),
            {
                "id": document_id,
                "endpoint": endpoint,
                "key": f"{endpoint}:{game_id}",
                "sha": SHA,
                "at": NOW,
            },
        )
    await connection.execute(
        text(
            "INSERT INTO possession_rebuild_runs "
            "(run_id, selectors, mode, forced_rebuild, status) "
            "VALUES (:id, '{}'::jsonb, 'materialize', false, 'running')"
        ),
        {"id": possession_run_id},
    )
    await connection.execute(
        text(
            "INSERT INTO possession_snapshot_attempts (snapshot_id, game_id, generation, "
            "parse_revision_id, input_token_sha256, input_pbp_document_id, input_pbp_sha256, "
            "input_event_sha256, game_context_sha256, normalizer_version, parser_version, "
            "rules_version, output_schema_version, state, created_by_run_id, "
            "created_by_claim_version) VALUES (:snapshot, :game, 1, :sha, :sha, :document, "
            ":sha, :sha, :sha, 'nba-pbp-v1', 'analytical-v1', 'nba-v1', "
            "'possessions-v1', 'allocated', :run, 1)"
        ),
        {
            "snapshot": possession_snapshot_id,
            "game": game_id,
            "sha": SHA,
            "document": pbp_document_id,
            "run": possession_run_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO lineup_rebuild_runs "
            "(run_id, selectors, mode, forced_rebuild, status) "
            "VALUES (:id, '{}'::jsonb, 'materialize', false, 'running')"
        ),
        {"id": lineup_run_id},
    )
    await connection.execute(
        text(
            "INSERT INTO lineup_snapshot_attempts (snapshot_id, game_id, generation, "
            "lineup_revision_id, input_token_sha256, input_box_score_document_id, "
            "input_box_score_sha256, input_pbp_document_id, input_pbp_sha256, "
            "canonical_player_box_sha256, canonical_event_sha256, game_context_sha256, "
            "normalizer_version, reconstructor_version, rules_version, output_schema_version, "
            "state, created_by_run_id, created_by_claim_version) VALUES "
            "(:snapshot, :game, 1, :sha, :sha, :box_document, :sha, :pbp_document, :sha, "
            ":sha, :sha, :sha, 'nba-v1', 'lineup-v1', 'nba-v1', 'lineups-v1', "
            "'allocated', :run, 1)"
        ),
        {
            "snapshot": lineup_snapshot_id,
            "game": game_id,
            "sha": SHA,
            "box_document": box_document_id,
            "pbp_document": pbp_document_id,
            "run": lineup_run_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO feature_build_runs (run_id, selectors, mode, forced_rebuild, "
            "feature_join_version, feature_formula_version, "
            "feature_event_normalizer_version, output_schema_version, "
            "player_output_schema_version, status) VALUES "
            "(:id, '{}'::jsonb, 'materialize', false, 'feature-join-v1', "
            "'feature-formulas-v1', 'nba-feature-events-v1', "
            "'game-state-features-v1', 'player-pregame-features-v1', 'running')"
        ),
        {"id": feature_run_id},
    )
    return game_id, possession_snapshot_id, lineup_snapshot_id, feature_run_id


async def _insert_attempt(
    connection: AsyncConnection,
    inputs: tuple[str, UUID, UUID, UUID],
    *,
    snapshot_id: UUID | None = None,
    generation: int = 1,
) -> UUID:
    game_id, possession_snapshot_id, lineup_snapshot_id, run_id = inputs
    result = snapshot_id or uuid4()
    await connection.execute(
        text(
            "INSERT INTO feature_snapshot_attempts (snapshot_id, game_id, generation, "
            "feature_revision_id, feature_input_token_sha256, possession_snapshot_id, "
            "parse_revision_id, possession_input_token_sha256, possession_output_sha256, "
            "lineup_snapshot_id, lineup_revision_id, lineup_input_token_sha256, "
            "lineup_output_sha256, lineup_result_status, dependency_input_sha256, "
            "dependency_revision_sha256, feature_join_version, feature_formula_version, "
            "feature_event_normalizer_version, output_schema_version, "
            "player_output_schema_version, state, created_by_run_id, created_by_claim_version) "
            "VALUES (:snapshot, :game, :generation, :sha, :sha, :possession, :sha, :sha, :sha, "
            ":lineup, :sha, :sha, :sha, 'valid', :sha, :sha, 'feature-join-v1', "
            "'feature-formulas-v1', 'nba-feature-events-v1', 'game-state-features-v1', "
            "'player-pregame-features-v1', 'allocated', :run, 1)"
        ),
        {
            "snapshot": result,
            "game": game_id,
            "generation": generation,
            "sha": SHA,
            "possession": possession_snapshot_id,
            "lineup": lineup_snapshot_id,
            "run": run_id,
        },
    )
    return result


async def _reject_attempt_change(
    connection: AsyncConnection,
    snapshot_id: UUID,
    changes: Mapping[str, object],
) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                update(feature_snapshot_attempts)
                .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
                .values(**changes)
            )


async def test_real_schema_has_exact_columns_keys_and_indexes(
    connection: AsyncConnection,
) -> None:
    names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    assert FEATURE_TABLES <= names
    expected_columns = {
        "feature_build_runs": (
            "run_id",
            "selectors",
            "mode",
            "forced_rebuild",
            "feature_join_version",
            "feature_formula_version",
            "feature_event_normalizer_version",
            "output_schema_version",
            "player_output_schema_version",
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
        ),
        "feature_build_resources": (
            "game_id",
            "publication_state",
            "attempt_count",
            "claim_version",
            "last_run_id",
            "next_generation",
            "active_run_id",
            "active_claim_version",
            "active_snapshot_id",
            "active_feature_revision_id",
            "active_feature_input_token_sha256",
            "claimed_until",
            "published_snapshot_id",
            "published_feature_revision_id",
            "published_feature_input_token_sha256",
            "published_at",
            "last_issue_codes",
            "last_error_category",
            "last_error_type",
            "created_at",
            "updated_at",
            "completed_at",
        ),
        "feature_snapshot_attempts": (
            "snapshot_id",
            "game_id",
            "generation",
            "feature_revision_id",
            "feature_input_token_sha256",
            "possession_snapshot_id",
            "parse_revision_id",
            "possession_input_token_sha256",
            "possession_output_sha256",
            "lineup_snapshot_id",
            "lineup_revision_id",
            "lineup_input_token_sha256",
            "lineup_output_sha256",
            "lineup_result_status",
            "dependency_input_sha256",
            "dependency_revision_sha256",
            "feature_join_version",
            "feature_formula_version",
            "feature_event_normalizer_version",
            "output_schema_version",
            "player_output_schema_version",
            "expected_state_row_count",
            "expected_state_output_sha256",
            "expected_player_row_count",
            "expected_player_output_sha256",
            "expected_feature_output_sha256",
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
        ),
        "feature_snapshot_dependencies": (
            "snapshot_id",
            "dependency_game_id",
            "started_at",
            "game_context_sha256",
            "applied_box_score_document_id",
            "applied_box_score_sha256",
            "canonical_team_box_sha256",
            "canonical_player_box_sha256",
            "possession_snapshot_id",
            "parse_revision_id",
            "possession_input_token_sha256",
            "possession_output_sha256",
            "ordinal",
            "inclusion_reasons",
        ),
    }
    for table_name, columns in expected_columns.items():
        actual = await connection.run_sync(partial(_column_names, table_name=table_name))
        assert actual == columns

    attempts_uqs = await connection.run_sync(
        lambda sync: inspect(sync).get_unique_constraints("feature_snapshot_attempts")
    )
    assert {tuple(item["column_names"]) for item in attempts_uqs} == {
        ("game_id", "snapshot_id"),
        ("game_id", "generation"),
    }
    dependency_pk = await connection.run_sync(
        lambda sync: inspect(sync).get_pk_constraint("feature_snapshot_dependencies")
    )
    dependency_uqs = await connection.run_sync(
        lambda sync: inspect(sync).get_unique_constraints("feature_snapshot_dependencies")
    )
    assert tuple(dependency_pk["constrained_columns"]) == (
        "snapshot_id",
        "dependency_game_id",
    )
    assert {tuple(item["column_names"]) for item in dependency_uqs} == {("snapshot_id", "ordinal")}

    resource_fks = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("feature_build_resources")
    )
    by_name = {item["name"]: item for item in resource_fks}
    for name in (
        "fk_feature_build_resources_active_snapshot",
        "fk_feature_build_resources_published_snapshot",
    ):
        assert by_name[name]["options"] == {"deferrable": True, "initially": "DEFERRED"}
    assert set(by_name) == {
        "fk_feature_build_resources_game",
        "fk_feature_build_resources_last_run",
        "fk_feature_build_resources_active_run",
        "fk_feature_build_resources_active_snapshot",
        "fk_feature_build_resources_published_snapshot",
    }

    attempt_fks = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("feature_snapshot_attempts")
    )
    assert {item["name"] for item in attempt_fks} == {
        "fk_feature_snapshot_attempts_game",
        "fk_feature_snapshot_attempts_possession_snapshot",
        "fk_feature_snapshot_attempts_lineup_snapshot",
        "fk_feature_snapshot_attempts_created_by_run",
    }
    dependency_fks = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("feature_snapshot_dependencies")
    )
    assert {item["name"] for item in dependency_fks} == {
        "fk_feature_snapshot_dependencies_snapshot",
        "fk_feature_snapshot_dependencies_game",
        "fk_feature_snapshot_dependencies_box_document",
        "fk_feature_snapshot_dependencies_possession_snapshot",
    }
    indexes = {
        table_name: await connection.run_sync(
            partial(_unconstrained_indexes, table_name=table_name)
        )
        for table_name in FEATURE_TABLES
    }
    assert indexes == {
        "feature_build_runs": {},
        "feature_build_resources": {},
        "feature_snapshot_attempts": {
            "ix_feature_snapshot_attempts_game_state": ("game_id", "state")
        },
        "feature_snapshot_dependencies": {
            "ix_feature_snapshot_dependencies_dependency_game_id": ("dependency_game_id",)
        },
    }


async def test_run_counters_versions_status_and_completion_are_constrained(
    connection: AsyncConnection,
) -> None:
    _, _, _, run_id = await _seed_sources(connection)
    rejected = (
        "discovered = -1",
        "feature_join_version = 'Bad Version'",
        "status = 'unknown'",
        "status = 'succeeded'",
        "discovered = 1, materialized = 2",
        "discovered = 1, materialized = 1, invalid = 2",
    )
    for assignment in rejected:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(f"UPDATE feature_build_runs SET {assignment} WHERE run_id = :id"),
                    {"id": run_id},
                )
    await connection.execute(
        text(
            "UPDATE feature_build_runs SET discovered = 2, materialized = 1, failed = 1, "
            "status = 'partial', finished_at = :now WHERE run_id = :id"
        ),
        {"id": run_id, "now": NOW},
    )


async def test_final_run_with_invalid_output_must_be_partial(
    connection: AsyncConnection,
) -> None:
    _, _, _, run_id = await _seed_sources(connection)
    await connection.execute(
        text(
            "UPDATE feature_build_runs SET discovered = 1, materialized = 1, invalid = 1 "
            "WHERE run_id = :id"
        ),
        {"id": run_id},
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE feature_build_runs SET status = 'interrupted', finished_at = :now "
                    "WHERE run_id = :id"
                ),
                {"id": run_id, "now": NOW},
            )


async def test_interrupted_run_without_invalid_output_remains_valid(
    connection: AsyncConnection,
) -> None:
    _, _, _, run_id = await _seed_sources(connection)
    await connection.execute(
        text(
            "UPDATE feature_build_runs SET discovered = 1, failed = 1, "
            "status = 'interrupted', finished_at = :now WHERE run_id = :id"
        ),
        {"id": run_id, "now": NOW},
    )


async def test_resource_requires_atomic_active_and_published_tuples(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    game_id, _, _, run_id = inputs
    await connection.execute(feature_build_resources.insert().values(game_id=game_id))
    rejected = (
        {"active_run_id": run_id},
        {
            "claim_version": 2,
            "active_run_id": run_id,
            "active_claim_version": 1,
            "active_snapshot_id": snapshot_id,
            "active_feature_revision_id": SHA,
            "active_feature_input_token_sha256": SHA,
            "claimed_until": NOW,
        },
        {"publication_state": "current"},
        {"published_snapshot_id": snapshot_id},
        {"last_error_category": "unsafe-value", "last_error_type": "validation"},
        {"last_error_category": "safe_code"},
    )
    for changes in rejected:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    update(feature_build_resources)
                    .where(feature_build_resources.c.game_id == game_id)
                    .values(**changes)
                )


async def test_resource_persists_mapping_ready_error_category_and_type(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    await connection.execute(feature_build_resources.insert().values(game_id=inputs[0]))
    await connection.execute(
        text(
            "UPDATE feature_build_resources SET last_error_category = 'bad_input', "
            "last_error_type = 'validation' WHERE game_id = :game_id"
        ),
        {"game_id": inputs[0]},
    )


VALID_METADATA = {
    "expected_state_row_count": 1,
    "expected_state_output_sha256": SHA,
    "expected_player_row_count": 0,
    "expected_player_output_sha256": SHA,
    "expected_feature_output_sha256": SHA,
    "expected_issue_codes": [],
    "result_status": "valid",
}


@pytest.mark.parametrize(
    ("state", "fields"),
    [
        ("allocated", {}),
        ("writing", VALID_METADATA | {"insert_started_at": NOW}),
        (
            "verified",
            VALID_METADATA | {"insert_started_at": NOW, "verified_at": NOW},
        ),
        (
            "published",
            VALID_METADATA | {"insert_started_at": NOW, "verified_at": NOW, "published_at": NOW},
        ),
        ("poisoned", {"safe_error_code": "bad_input", "safe_error_type": "validation"}),
        ("superseded", {}),
    ],
)
async def test_attempt_accepts_each_complete_lifecycle_shape(
    connection: AsyncConnection, state: str, fields: dict[str, object]
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    await connection.execute(
        update(feature_snapshot_attempts)
        .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
        .values(state=state, **fields)
    )


async def test_attempt_rejects_incomplete_metadata_counts_issues_and_timestamps(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    rejected = (
        {"state": "writing", "insert_started_at": NOW},
        {"state": "verified", **VALID_METADATA, "insert_started_at": NOW},
        {"state": "allocated", "published_at": NOW},
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "expected_state_row_count": 0,
        },
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "expected_player_row_count": -1,
        },
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "expected_issue_codes": [ISSUE],
        },
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "result_status": "invalid",
        },
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "result_status": "invalid",
            "expected_issue_codes": [],
        },
        {
            "state": "writing",
            **VALID_METADATA,
            "insert_started_at": NOW,
            "result_status": "invalid",
            "expected_issue_codes": ["Unsafe-Issue"],
        },
    )
    for changes in rejected:
        await _reject_attempt_change(connection, snapshot_id, changes)


async def test_attempt_rejects_insert_started_before_created_at(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE feature_snapshot_attempts SET state = 'writing', "
                    "expected_state_row_count = 1, expected_state_output_sha256 = :sha, "
                    "expected_player_row_count = 0, expected_player_output_sha256 = :sha, "
                    "expected_feature_output_sha256 = :sha, expected_issue_codes = '[]'::jsonb, "
                    "result_status = 'valid', "
                    "insert_started_at = created_at - INTERVAL '1 second' "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"sha": SHA, "snapshot_id": snapshot_id},
            )


async def test_invalid_attempt_may_have_zero_player_rows(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    await connection.execute(
        update(feature_snapshot_attempts)
        .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
        .values(
            state="writing",
            **(
                VALID_METADATA
                | {
                    "insert_started_at": NOW,
                    "expected_issue_codes": [ISSUE],
                    "result_status": "invalid",
                }
            ),
        )
    )


async def test_attempt_enforces_generation_uniqueness_and_source_uuid_ownership(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    await _insert_attempt(connection, inputs)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(connection, inputs, generation=1)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(
                connection,
                inputs,
                snapshot_id=UUID("00000000-0000-1000-8000-000000000001"),
                generation=2,
            )

    other_inputs = await _seed_sources(connection, suffix=1)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE feature_snapshot_attempts SET possession_snapshot_id = :foreign "
                    "WHERE game_id = :game"
                ),
                {"foreign": other_inputs[1], "game": inputs[0]},
            )


async def test_dependency_identity_ordinal_and_reason_bitmask_are_constrained(
    connection: AsyncConnection,
) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    values = {
        "snapshot": snapshot_id,
        "game": inputs[0],
        "started": NOW,
        "sha": SHA,
        "document": uuid4(),
        "possession": inputs[1],
    }
    # The applied document is an owned identity, not an arbitrary UUID.
    await connection.execute(
        text(
            "INSERT INTO source_documents (document_id, source, endpoint, resource_key, "
            "request_parameters, payload, checksum_sha256, requested_at) VALUES "
            "(:document, 'nba', 'box_score', :key, '{}'::jsonb, '{}'::jsonb, :sha, :started)"
        ),
        values | {"key": f"dependency:{inputs[0]}"},
    )
    insert_sql = text(
        "INSERT INTO feature_snapshot_dependencies (snapshot_id, dependency_game_id, "
        "started_at, game_context_sha256, applied_box_score_document_id, "
        "applied_box_score_sha256, canonical_team_box_sha256, canonical_player_box_sha256, "
        "possession_snapshot_id, parse_revision_id, possession_input_token_sha256, "
        "possession_output_sha256, ordinal, inclusion_reasons) VALUES "
        "(:snapshot, :game, :started, :sha, :document, :sha, :sha, :sha, :possession, "
        ":sha, :sha, :sha, :ordinal, :reasons)"
    )
    await connection.execute(insert_sql, values | {"ordinal": 0, "reasons": 7})
    for changes in (
        {"ordinal": -1},
        {"inclusion_reasons": 0},
        {"inclusion_reasons": 8},
        {"game_context_sha256": "A" * 64},
    ):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "UPDATE feature_snapshot_dependencies SET "
                        + ", ".join(f"{key} = :{key}" for key in changes)
                        + " WHERE snapshot_id = :snapshot"
                    ),
                    dict(changes) | {"snapshot": snapshot_id},
                )


async def test_direct_sql_rejects_invalid_enums(connection: AsyncConnection) -> None:
    inputs = await _seed_sources(connection)
    snapshot_id = await _insert_attempt(connection, inputs)
    for table_name, key_name, key, assignment in (
        ("feature_build_runs", "run_id", inputs[3], "status = 'done'"),
        ("feature_build_resources", "game_id", inputs[0], "publication_state = 'done'"),
        ("feature_snapshot_attempts", "snapshot_id", snapshot_id, "state = 'done'"),
        (
            "feature_snapshot_attempts",
            "snapshot_id",
            snapshot_id,
            "lineup_result_status = 'partial'",
        ),
    ):
        if table_name == "feature_build_resources":
            await connection.execute(feature_build_resources.insert().values(game_id=inputs[0]))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(f"UPDATE {table_name} SET {assignment} WHERE {key_name} = :key"),
                    {"key": key},
                )


async def test_z_migration_replay_downgrade_preserves_earlier_phase_sentinels() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    team_id = 9_399_999
    lineup_run_id, ledger_id = uuid4(), f"sentinel_{uuid4().hex}"
    before: tuple[object, ...]
    try:
        async with engine.begin() as database_connection:
            await database_connection.execute(
                text(
                    "INSERT INTO teams (team_id, city, name, tricode, slug) "
                    "VALUES (:id, 'Sentinel', 'Phase Two', 'SNT', :slug)"
                ),
                {"id": team_id, "slug": f"sentinel-{team_id}"},
            )
            await database_connection.execute(
                text(
                    "INSERT INTO clickhouse_schema_migrations (migration_id, checksum_sha256, "
                    "expected_schema_fingerprint_sha256, state, applied_at) "
                    "VALUES (:id, :sha, :sha, 'applied', :at)"
                ),
                {"id": ledger_id, "sha": SHA, "at": NOW},
            )
            await database_connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_runs "
                    "(run_id, selectors, mode, forced_rebuild, status) "
                    'VALUES (:id, \'{"sentinel":"phase_four"}\'::jsonb, '
                    "'verify', false, 'running')"
                ),
                {"id": lineup_run_id},
            )
            row = (
                await database_connection.execute(
                    text(
                        "SELECT team_id, city, name, tricode, slug FROM teams WHERE team_id = :id"
                    ),
                    {"id": team_id},
                )
            ).one()
            before = tuple(row)

        down = _alembic("downgrade", "0004_lineup_reconstruction")
        assert down.returncode == 0, down.stderr
        async with engine.connect() as database_connection:
            names = set(
                await database_connection.run_sync(lambda sync: inspect(sync).get_table_names())
            )
            assert not (FEATURE_TABLES & names)
            assert {
                "teams",
                "possession_snapshot_attempts",
                "lineup_snapshot_attempts",
                "clickhouse_schema_migrations",
            } <= names
            assert (
                tuple(
                    (
                        await database_connection.execute(
                            text(
                                "SELECT team_id, city, name, tricode, slug FROM teams "
                                "WHERE team_id = :id"
                            ),
                            {"id": team_id},
                        )
                    ).one()
                )
                == before
            )
            assert (
                await database_connection.scalar(
                    text(
                        "SELECT count(*) FROM clickhouse_schema_migrations "
                        "WHERE migration_id = :id AND checksum_sha256 = :sha"
                    ),
                    {"id": ledger_id, "sha": SHA},
                )
                == 1
            )
            assert (
                await database_connection.scalar(
                    text("SELECT count(*) FROM lineup_rebuild_runs WHERE run_id = :id"),
                    {"id": lineup_run_id},
                )
                == 1
            )

        for _ in range(2):
            up = _alembic("upgrade", "head")
            assert up.returncode == 0, up.stderr
    finally:
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr
        async with engine.begin() as database_connection:
            await database_connection.execute(
                text("DELETE FROM lineup_rebuild_runs WHERE run_id = :id"),
                {"id": lineup_run_id},
            )
            await database_connection.execute(
                text("DELETE FROM clickhouse_schema_migrations WHERE migration_id = :id"),
                {"id": ledger_id},
            )
            await database_connection.execute(
                text("DELETE FROM teams WHERE team_id = :id"),
                {"id": team_id},
            )
        await engine.dispose()
