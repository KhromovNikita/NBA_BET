from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
)


class PostgresChecker:
    """Check that PostgreSQL accepts a trivial query."""

    name = "postgres"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


__all__ = [
    "PostgresChecker",
    "PostgresFeatureWorkStore",
    "PostgresLineupWorkStore",
    "PostgresPossessionWorkStore",
    "feature_build_resources",
    "feature_build_runs",
    "feature_snapshot_attempts",
    "feature_snapshot_dependencies",
]
