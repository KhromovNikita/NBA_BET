from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI

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


async def test_lifespan_exposes_resources_and_closes_clients() -> None:
    app = FastAPI()
    lifespan = create_lifespan(Settings(_env_file=None))  # type: ignore[call-arg]

    async with lifespan(app):
        resources = cast(AppResources, app.state.resources)
        assert not resources.clickhouse_client.is_closed

    assert resources.clickhouse_client.is_closed
