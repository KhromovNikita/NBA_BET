import asyncio
from uuid import UUID

import httpx

from nba_live_totals.api.app import create_app
from nba_live_totals.api.dependencies import get_health_service
from nba_live_totals.application.health import HealthService
from nba_live_totals.settings import Settings


class SuccessfulChecker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self) -> None:
        return None


class FailingChecker:
    name = "postgres"

    async def check(self) -> None:
        raise RuntimeError("postgresql://user:secret@host/db")


def build_service(*checkers: SuccessfulChecker | FailingChecker) -> HealthService:
    return HealthService(checkers=checkers, timeout_seconds=0.1)


async def request(
    service: HealthService,
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    app.dependency_overrides[get_health_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


async def test_liveness_has_no_dependency_on_readiness() -> None:
    response = await request(build_service(FailingChecker()), "/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_returns_ordered_all_up_contract() -> None:
    response = await request(
        build_service(
            SuccessfulChecker("postgres"),
            SuccessfulChecker("clickhouse"),
            SuccessfulChecker("redis"),
        ),
        "/health/ready",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert list(response.json()["checks"]) == ["postgres", "clickhouse", "redis"]


async def test_readiness_returns_safe_503_contract() -> None:
    response = await request(
        build_service(
            FailingChecker(),
            SuccessfulChecker("clickhouse"),
            SuccessfulChecker("redis"),
        ),
        "/health/ready",
    )

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["postgres"]["status"] == "down"
    assert "secret" not in response.text


async def test_request_id_is_preserved_or_regenerated() -> None:
    service = build_service(SuccessfulChecker("postgres"))
    preserved = await request(service, "/health", {"X-Request-ID": "trace-123"})
    regenerated = await request(service, "/health", {"X-Request-ID": "bad value"})

    assert preserved.headers["X-Request-ID"] == "trace-123"
    assert UUID(regenerated.headers["X-Request-ID"]).version == 4


async def test_concurrent_request_ids_are_isolated() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.get("/health", headers={"X-Request-ID": "trace-first"}),
            client.get("/health", headers={"X-Request-ID": "trace-second"}),
        )

    assert first.headers["X-Request-ID"] == "trace-first"
    assert second.headers["X-Request-ID"] == "trace-second"


async def test_unhandled_exception_is_sanitized() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("credential=super-secret")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom", headers={"X-Request-ID": "trace-error"})

    assert response.status_code == 500
    assert response.json()["detail"] == "internal_server_error"
    assert "super-secret" not in response.text
    assert response.headers["X-Request-ID"] == "trace-error"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
