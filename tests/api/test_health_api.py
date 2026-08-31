import asyncio
import json
import logging
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from nba_live_totals.api.app import create_app
from nba_live_totals.api.dependencies import get_health_service
from nba_live_totals.application.health import HealthService
from nba_live_totals.observability.logging import JsonFormatter
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


class JsonLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


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


@pytest.mark.parametrize("header_value", ["trace-error", "invalid request id"])
async def test_unhandled_exception_is_sanitized_and_logged_with_request_id(
    header_value: str,
) -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("credential=super-secret")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    log_capture = JsonLogCapture()
    log_capture.setFormatter(JsonFormatter(app_env="test"))
    logger = logging.getLogger("nba_live_totals.api.app")
    logger.addHandler(log_capture)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom", headers={"X-Request-ID": header_value})
    finally:
        logger.removeHandler(log_capture)

    assert response.status_code == 500
    assert response.json()["detail"] == "internal_server_error"
    assert "super-secret" not in response.text
    request_id = response.headers["X-Request-ID"]
    assert response.json()["request_id"] == request_id
    if header_value == "trace-error":
        assert request_id == "trace-error"
    else:
        assert UUID(request_id).version == 4
    payloads = [
        json.loads(message)
        for message in log_capture.messages
        if json.loads(message)["message"] == "unhandled_request_error"
    ]
    assert len(payloads) == 1
    assert payloads[0]["request_id"] == request_id
    assert payloads[0]["error_type"] == "RuntimeError"
    assert "RuntimeError: credential=super-secret" in payloads[0]["exception"]


async def test_http_and_validation_exceptions_bypass_unhandled_error_boundary() -> None:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        http_error = await client.get("/teapot", headers={"X-Request-ID": "http-error"})
        validation_error = await client.get(
            "/items/not-an-integer",
            headers={"X-Request-ID": "validation-error"},
        )

    assert http_error.status_code == 418
    assert http_error.json() == {"detail": "short and stout"}
    assert http_error.headers["X-Request-ID"] == "http-error"
    assert validation_error.status_code == 422
    assert validation_error.headers["X-Request-ID"] == "validation-error"
    assert validation_error.json()["detail"][0]["type"] == "int_parsing"
