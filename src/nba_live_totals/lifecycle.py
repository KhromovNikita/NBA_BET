import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import Lifespan

from nba_live_totals.application.health import HealthService
from nba_live_totals.infrastructure.clickhouse import ClickHouseChecker
from nba_live_totals.infrastructure.postgres import PostgresChecker
from nba_live_totals.infrastructure.redis import RedisChecker
from nba_live_totals.settings import Settings


@dataclass(frozen=True, slots=True)
class AppResources:
    postgres_engine: AsyncEngine
    clickhouse_client: httpx.AsyncClient
    redis_client: Redis
    health_service: HealthService


def create_resources(settings: Settings) -> AppResources:
    postgres_engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    redis_client = Redis.from_url(str(settings.redis_url))
    clickhouse_client = httpx.AsyncClient(
        base_url=str(settings.clickhouse_url).rstrip("/"),
        timeout=settings.healthcheck_timeout_seconds,
    )
    health_service = HealthService(
        checkers=(
            PostgresChecker(postgres_engine),
            ClickHouseChecker(
                clickhouse_client,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password.get_secret_value(),
            ),
            RedisChecker(redis_client),
        ),
        timeout_seconds=settings.healthcheck_timeout_seconds,
    )
    return AppResources(postgres_engine, clickhouse_client, redis_client, health_service)


async def close_resources(resources: AppResources) -> None:
    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("clickhouse", resources.clickhouse_client.aclose),
        ("redis", resources.redis_client.aclose),
        ("postgres", resources.postgres_engine.dispose),
    )
    await close_in_order(closers)


async def close_in_order(
    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...],
) -> None:
    for resource_name, closer in closers:
        await _close_one(resource_name, closer)


async def _close_one(name: str, close: Callable[[], Awaitable[None]]) -> None:
    try:
        await close()
    except Exception as error:
        logging.getLogger(__name__).error(
            "resource_close_failed",
            extra={"resource": name, "error_type": type(error).__name__},
        )


def create_lifespan(settings: Settings) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resources = create_resources(settings)
        app.state.resources = resources
        try:
            yield
        finally:
            await close_resources(resources)

    return lifespan
