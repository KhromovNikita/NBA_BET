import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests import runtime_config

pytestmark = pytest.mark.integration

PHASE_TWO_TABLES = {
    "alembic_version",
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

PHASE_THREE_TABLES = {
    "possession_rebuild_runs",
    "possession_snapshot_attempts",
    "possession_rebuild_resources",
    "clickhouse_schema_migrations",
}

PHASE_FOUR_TABLES = {
    "lineup_rebuild_runs",
    "lineup_snapshot_attempts",
    "lineup_rebuild_resources",
}


def _upgrade_head() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = _upgrade_head()
    assert result.returncode == 0, result.stderr


def test_alembic_upgrade_head_is_idempotent() -> None:
    first = _upgrade_head()
    second = _upgrade_head()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr


async def test_alembic_head_contains_the_historical_possession_and_lineup_control_schema() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
    finally:
        await engine.dispose()

    assert set(table_names) == PHASE_TWO_TABLES | PHASE_THREE_TABLES | PHASE_FOUR_TABLES


async def test_ingestion_resource_document_links_match_shared_schema() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns("ingestion_resources")
            )
            foreign_keys = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_foreign_keys(
                    "ingestion_resources"
                )
            )
    finally:
        await engine.dispose()

    columns_by_name = {column["name"]: column for column in columns}
    assert columns_by_name["latest_document_id"]["nullable"] is True
    assert columns_by_name["applied_document_id"]["nullable"] is True
    assert {
        (
            foreign_key["name"],
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in foreign_keys
        if foreign_key["name"]
        in {
            "fk_ingestion_resources_latest_document",
            "fk_ingestion_resources_applied_document",
        }
    } == {
        (
            "fk_ingestion_resources_latest_document",
            ("latest_document_id",),
            "source_documents",
            ("document_id",),
        ),
        (
            "fk_ingestion_resources_applied_document",
            ("applied_document_id",),
            "source_documents",
            ("document_id",),
        ),
    }


async def test_play_by_play_migration_uses_lossless_sequence_identity() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            primary_key = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_pk_constraint(
                    "play_by_play_events"
                )
            )
            indexes = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_indexes("play_by_play_events")
            )
            checks = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_check_constraints(
                    "play_by_play_events"
                )
            )
    finally:
        await engine.dispose()

    assert primary_key["constrained_columns"] == ["game_id", "sequence_number"]
    assert {index["name"]: index["column_names"] for index in indexes}[
        "ix_play_by_play_events_game_id_period_sequence_number"
    ] == [
        "game_id",
        "period",
        "sequence_number",
    ]
    assert "ck_play_by_play_events_positive_sequence_number" in {check["name"] for check in checks}


async def test_migration_preserves_stable_check_constraint_names() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            constraints = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_check_constraints("games")
            )
    finally:
        await engine.dispose()

    assert {constraint["name"] for constraint in constraints} == {
        "ck_games_distinct_teams",
        "ck_games_final_scores_present",
        "ck_games_game_id_format",
        "ck_games_nonnegative_away_score",
        "ck_games_nonnegative_home_score",
        "ck_games_positive_away_team_id",
        "ck_games_positive_home_team_id",
        "ck_games_status_allowed",
    }


@pytest.mark.parametrize(
    ("table_name", "column_name"), [("teams", "team_id"), ("players", "player_id")]
)
async def test_provider_identity_columns_have_no_generated_default(
    table_name: str, column_name: str
) -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_columns(table_name)
            )
    finally:
        await engine.dispose()

    identity_column = next(column for column in columns if column["name"] == column_name)
    assert identity_column["default"] is None


async def test_possession_migration_downgrade_removes_only_phase_three_tables() -> None:
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "downgrade", "0002_historical_ingestion"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert downgrade.returncode == 0, downgrade.stderr

    try:
        engine = create_async_engine(runtime_config.postgres_url())
        try:
            async with engine.connect() as connection:
                table_names = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
        finally:
            await engine.dispose()

        assert set(table_names) == PHASE_TWO_TABLES
    finally:
        restore = await asyncio.to_thread(_upgrade_head)
        assert restore.returncode == 0, restore.stderr


async def test_lineup_migration_downgrade_removes_only_phase_four_tables() -> None:
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "downgrade", "0003_possession_reconstruction"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert downgrade.returncode == 0, downgrade.stderr
    try:
        engine = create_async_engine(runtime_config.postgres_url())
        try:
            async with engine.connect() as connection:
                table_names = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
        finally:
            await engine.dispose()
        assert set(table_names) == PHASE_TWO_TABLES | PHASE_THREE_TABLES
    finally:
        restore = await asyncio.to_thread(_upgrade_head)
        assert restore.returncode == 0, restore.stderr
