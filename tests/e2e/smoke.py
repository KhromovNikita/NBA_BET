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


def has_ready_contract(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return False
    payload = response.json()
    if not isinstance(payload, dict) or list(payload) != ["status", "checks"]:
        return False
    checks = payload.get("checks")
    if payload.get("status") != "ready" or not isinstance(checks, dict):
        return False
    if list(checks) != ["postgres", "clickhouse", "redis"]:
        return False
    for name in ("postgres", "clickhouse", "redis"):
        check = checks.get(name)
        if not isinstance(check, dict) or list(check) != ["status", "latency_ms"]:
            return False
        latency_ms = check.get("latency_ms")
        if check.get("status") != "up":
            return False
        if not isinstance(latency_ms, int | float) or isinstance(latency_ms, bool):
            return False
        if latency_ms < 0:
            return False
    return True


async def wait_for_expected_response(
    client: httpx.AsyncClient,
    path: str,
    is_expected_response: ResponseMatcher,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.25,
) -> httpx.Response:
    deadline = monotonic() + timeout_seconds
    last_observation = "no response received"
    while monotonic() < deadline:
        try:
            response = await client.get(path)
            if is_expected_response(response):
                return response
            last_observation = f"status={response.status_code} body={response.text[:512]}"
        except (httpx.HTTPError, ValueError) as error:
            last_observation = f"{type(error).__name__}: {error}"
        await asyncio.sleep(poll_interval_seconds)
    raise AssertionError(f"API did not become ready: {last_observation}")
