from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresChecker:
    """Check that PostgreSQL accepts a trivial query."""

    name = "postgres"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
