import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests import runtime_config

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
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


async def test_alembic_head_contains_the_complete_historical_schema() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
    finally:
        await engine.dispose()

    assert set(table_names) == EXPECTED_TABLES


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
