import httpx
import pytest

from tests.e2e.smoke import has_exact_json, has_expected_status, wait_for_expected_response

pytestmark = pytest.mark.e2e


async def test_compose_api_is_alive_and_ready() -> None:
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=2.0) as client:
        live = await wait_for_expected_response(
            client,
            "/health",
            lambda response: has_exact_json(response, {"status": "alive"}),
            timeout_seconds=30.0,
        )
        ready = await wait_for_expected_response(
            client,
            "/health/ready",
            lambda response: has_expected_status(response, "ready"),
            timeout_seconds=30.0,
        )

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
