import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.infrastructure.clickhouse import ClickHouseChecker
from nba_live_totals.infrastructure.postgres import PostgresChecker
from nba_live_totals.infrastructure.redis import RedisChecker
from tests import runtime_config

pytestmark = pytest.mark.integration


async def test_postgres_checker_executes_real_query() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        await PostgresChecker(engine).check()
    finally:
        await engine.dispose()


async def test_clickhouse_checker_uses_authenticated_ping() -> None:
    async with httpx.AsyncClient(
        base_url=runtime_config.clickhouse_url(),
        trust_env=False,
    ) as client:
        await ClickHouseChecker(client, user="nba", password="nba").check()


async def test_redis_checker_uses_real_ping() -> None:
    client = Redis.from_url(runtime_config.redis_url())
    try:
        await RedisChecker(client).check()
    finally:
        await client.aclose()
