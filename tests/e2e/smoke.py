import asyncio
from collections.abc import Callable
from time import monotonic

import httpx

ResponseMatcher = Callable[[httpx.Response], bool]


def has_exact_json(response: httpx.Response, expected_json: dict[str, str]) -> bool:
    return response.status_code == 200 and response.json() == expected_json


def has_expected_status(response: httpx.Response, expected_status: str) -> bool:
    payload = response.json()
    return (
        response.status_code == 200
        and isinstance(payload, dict)
        and payload.get("status") == expected_status
    )


async def wait_for_expected_response(
    client: httpx.AsyncClient,
    path: str,
    is_expected_response: ResponseMatcher,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.25,
) -> httpx.Response:
    deadline = monotonic() + timeout_seconds
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            response = await client.get(path)
            if is_expected_response(response):
                return response
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
        await asyncio.sleep(poll_interval_seconds)
    raise AssertionError(f"API did not become ready: {type(last_error).__name__}")
