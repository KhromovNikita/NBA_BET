from time import monotonic

import httpx
import pytest

from tests.e2e.smoke import has_exact_json, wait_for_expected_response


async def test_polling_retries_transient_and_invalid_responses_until_exact_success() -> None:
    responses = iter(
        (
            httpx.Response(503, json={"status": "not_ready"}),
            httpx.Response(200, content=b"not json"),
            httpx.Response(404, json={"detail": "not found"}),
            httpx.Response(200, json={"status": "not_alive"}),
            httpx.Response(200, json={"status": "alive"}),
        )
    )
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return next(responses)

    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(respond),
    ) as client:
        response = await wait_for_expected_response(
            client,
            "/health",
            lambda item: has_exact_json(item, {"status": "alive"}),
            timeout_seconds=0.1,
            poll_interval_seconds=0,
        )

    assert requests == 5
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_polling_times_out_when_no_response_matches_expected_status() -> None:
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(404, json={"detail": "not found"})

    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(respond),
    ) as client:
        started = monotonic()
        with pytest.raises(AssertionError, match="API did not become ready"):
            await wait_for_expected_response(
                client,
                "/health",
                lambda item: has_exact_json(item, {"status": "alive"}),
                timeout_seconds=0.01,
                poll_interval_seconds=0,
            )

    assert requests > 0
    assert monotonic() - started < 0.2
