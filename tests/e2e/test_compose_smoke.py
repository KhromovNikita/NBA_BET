import httpx
import pytest

from tests import runtime_config
from tests.e2e.smoke import has_exact_json, has_ready_contract, wait_for_expected_response

pytestmark = pytest.mark.e2e


async def test_compose_api_is_alive_and_ready() -> None:
    async with httpx.AsyncClient(
        base_url=runtime_config.api_url(),
        timeout=2.0,
        trust_env=False,
    ) as client:
        live = await wait_for_expected_response(
            client,
            "/health",
            lambda response: has_exact_json(response, {"status": "alive"}),
            timeout_seconds=30.0,
        )
        ready = await wait_for_expected_response(
            client,
            "/health/ready",
            has_ready_contract,
            timeout_seconds=30.0,
        )

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    payload = ready.json()
    assert list(payload) == ["status", "checks"]
    assert payload["status"] == "ready"
    assert list(payload["checks"]) == ["postgres", "clickhouse", "redis"]
    for dependency in ("postgres", "clickhouse", "redis"):
        check = payload["checks"][dependency]
        assert list(check) == ["status", "latency_ms"]
        assert check["status"] == "up"
        assert isinstance(check["latency_ms"], int | float)
        assert not isinstance(check["latency_ms"], bool)
        assert check["latency_ms"] >= 0
