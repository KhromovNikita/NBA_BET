import asyncio
from time import monotonic

import httpx
import pytest

pytestmark = pytest.mark.e2e


async def wait_for_json(path: str, timeout_seconds: float) -> httpx.Response:
    deadline = monotonic() + timeout_seconds
    last_error: Exception | None = None
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=2.0) as client:
        while monotonic() < deadline:
            try:
                response = await client.get(path)
                if response.status_code < 500:
                    return response
            except httpx.HTTPError as error:
                last_error = error
            await asyncio.sleep(0.25)
    raise AssertionError(f"API did not become ready: {type(last_error).__name__}")


async def test_compose_api_is_alive_and_ready() -> None:
    live = await wait_for_json("/health", timeout_seconds=30.0)
    ready = await wait_for_json("/health/ready", timeout_seconds=30.0)

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
