import asyncio
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


@dataclass(frozen=True, slots=True)
class ResourceFactories:
    postgres: Callable[[Settings], AsyncEngine]
    redis: Callable[[Settings], Redis]
    clickhouse: Callable[[Settings], httpx.AsyncClient]
    health_service: Callable[
        [Settings, AsyncEngine, httpx.AsyncClient, Redis],
        HealthService,
    ]


def _create_postgres(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.database_url), pool_pre_ping=True)


def _create_redis(settings: Settings) -> Redis:
    return Redis.from_url(str(settings.redis_url))


def _create_clickhouse(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=str(settings.clickhouse_url).rstrip("/"),
        timeout=settings.healthcheck_timeout_seconds,
        trust_env=False,
    )


def _create_health_service(
    settings: Settings,
    postgres_engine: AsyncEngine,
    clickhouse_client: httpx.AsyncClient,
    redis_client: Redis,
) -> HealthService:
    return HealthService(
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


async def create_resources(
    settings: Settings,
    *,
    factories: ResourceFactories | None = None,
) -> AppResources:
    resolved = factories or ResourceFactories(
        postgres=_create_postgres,
        redis=_create_redis,
        clickhouse=_create_clickhouse,
        health_service=_create_health_service,
    )
    rollback: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    try:
        postgres_engine = resolved.postgres(settings)
        rollback.append(("postgres", postgres_engine.dispose))
        redis_client = resolved.redis(settings)
        rollback.append(("redis", redis_client.aclose))
        clickhouse_client = resolved.clickhouse(settings)
        rollback.append(("clickhouse", clickhouse_client.aclose))
        health_service = resolved.health_service(
            settings,
            postgres_engine,
            clickhouse_client,
            redis_client,
        )
    except BaseException:
        try:
            await close_in_order(tuple(reversed(rollback)))
        except asyncio.CancelledError as cleanup_cancellation:
            logging.getLogger(__name__).warning(
                "resource_rollback_cancelled",
                extra={"error_type": type(cleanup_cancellation).__name__},
            )
        raise
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
    cancellation: asyncio.CancelledError | None = None
    for resource_name, closer in closers:
        try:
            await _close_one(resource_name, closer)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            logging.getLogger(__name__).warning(
                "resource_close_cancelled",
                extra={"resource": resource_name, "error_type": type(error).__name__},
            )
    if cancellation is not None:
        raise cancellation


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
        resources = await create_resources(settings)
        app.state.resources = resources
        try:
            yield
        finally:
            await close_resources(resources)

    return lifespan
