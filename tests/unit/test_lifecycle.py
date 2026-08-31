import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals import lifecycle
from nba_live_totals.application.health import HealthService
from nba_live_totals.lifecycle import AppResources, close_in_order, create_lifespan
from nba_live_totals.settings import Settings


async def test_close_in_order_continues_after_one_failure() -> None:
    calls: list[str] = []

    async def broken_clickhouse_close() -> None:
        calls.append("clickhouse")
        raise RuntimeError("close failed")

    async def redis_close() -> None:
        calls.append("redis")

    async def postgres_close() -> None:
        calls.append("postgres")

    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("clickhouse", broken_clickhouse_close),
        ("redis", redis_close),
        ("postgres", postgres_close),
    )

    await close_in_order(closers)

    assert calls == ["clickhouse", "redis", "postgres"]


async def test_close_in_order_finishes_after_cancellation_then_reraises_it() -> None:
    calls: list[str] = []
    cancellation = asyncio.CancelledError("shutdown cancelled")

    async def cancelled_clickhouse_close() -> None:
        calls.append("clickhouse")
        raise cancellation

    async def redis_close() -> None:
        calls.append("redis")

    async def postgres_close() -> None:
        calls.append("postgres")

    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("clickhouse", cancelled_clickhouse_close),
        ("redis", redis_close),
        ("postgres", postgres_close),
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        await close_in_order(closers)

    assert captured.value is cancellation
    assert calls == ["clickhouse", "redis", "postgres"]


@dataclass
class StubResource:
    name: str
    events: list[str]
    close_error: Exception | None = None

    async def aclose(self) -> None:
        await self._close()

    async def dispose(self) -> None:
        await self._close()

    async def _close(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        ("redis", ["create:postgres", "create:redis", "close:postgres"]),
        (
            "clickhouse",
            [
                "create:postgres",
                "create:redis",
                "create:clickhouse",
                "close:redis",
                "close:postgres",
            ],
        ),
        (
            "health",
            [
                "create:postgres",
                "create:redis",
                "create:clickhouse",
                "create:health",
                "close:clickhouse",
                "close:redis",
                "close:postgres",
            ],
        ),
    ],
)
async def test_partial_startup_closes_created_resources_in_reverse_order(
    failure_stage: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []
    construction_error = RuntimeError(f"failed at {failure_stage}")
    postgres = StubResource("postgres", events)
    redis = StubResource("redis", events)
    clickhouse = StubResource(
        "clickhouse",
        events,
        close_error=RuntimeError("cleanup failed") if failure_stage == "health" else None,
    )

    def create_postgres(settings: Settings) -> AsyncEngine:
        events.append("create:postgres")
        return cast(AsyncEngine, postgres)

    def create_redis(settings: Settings) -> Redis:
        events.append("create:redis")
        if failure_stage == "redis":
            raise construction_error
        return cast(Redis, redis)

    def create_clickhouse(settings: Settings) -> httpx.AsyncClient:
        events.append("create:clickhouse")
        if failure_stage == "clickhouse":
            raise construction_error
        return cast(httpx.AsyncClient, clickhouse)

    def create_health_service(
        settings: Settings,
        postgres_engine: AsyncEngine,
        clickhouse_client: httpx.AsyncClient,
        redis_client: Redis,
    ) -> HealthService:
        events.append("create:health")
        raise construction_error

    factories = lifecycle.ResourceFactories(
        postgres=create_postgres,
        redis=create_redis,
        clickhouse=create_clickhouse,
        health_service=create_health_service,
    )

    with pytest.raises(RuntimeError) as captured:
        await lifecycle.create_resources(
            Settings(_env_file=None),  # type: ignore[call-arg]
            factories=factories,
        )

    assert captured.value is construction_error
    assert events == expected_events


async def test_lifespan_exposes_resources_and_closes_clients() -> None:
    app = FastAPI()
    lifespan = create_lifespan(Settings(_env_file=None))  # type: ignore[call-arg]

    async with lifespan(app):
        resources = cast(AppResources, app.state.resources)
        assert not resources.clickhouse_client.is_closed

    assert resources.clickhouse_client.is_closed
