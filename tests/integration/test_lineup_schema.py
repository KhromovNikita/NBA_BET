"""Live PostgreSQL checks for the Phase 4 control-plane tables."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

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
