import json
import logging
from datetime import datetime
from uuid import UUID

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from nba_live_totals.api.request_id import (
    RequestIdMiddleware,
    get_request_id,
    normalize_request_id,
)
from nba_live_totals.observability.logging import JsonFormatter, configure_logging
from nba_live_totals.settings import Settings


def test_json_formatter_emits_required_fields_and_whitelisted_extras() -> None:
    formatter = JsonFormatter(app_env="test")
    record = logging.LogRecord(
        name="nba.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="dependency_check_failed",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(
        dependency="postgres",
        error_type="TimeoutError",
        unapproved_extra="must_not_be_logged",
    )

    payload = json.loads(formatter.format(record))

    assert datetime.fromisoformat(payload["timestamp"]).tzinfo is not None
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "nba.test"
    assert payload["message"] == "dependency_check_failed"
    assert payload["app_env"] == "test"
    assert payload["dependency"] == "postgres"
    assert payload["error_type"] == "TimeoutError"
    assert "unapproved_extra" not in payload


def test_valid_request_id_is_preserved() -> None:
    assert normalize_request_id("game-0022500123.trace_1") == "game-0022500123.trace_1"


@pytest.mark.parametrize("value", ["", "contains spaces", "non-ascii-и", "a" * 129])
def test_invalid_request_id_is_replaced_with_uuid(value: str) -> None:
    generated = normalize_request_id(value)

    assert UUID(generated).version == 4


async def _report_request_id(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"request_id": get_request_id()})
    await response(scope, receive, send)


async def _report_request_id_with_existing_header(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    response = JSONResponse(
        {"request_id": get_request_id()},
        headers={"X-Request-ID": "downstream-value"},
    )
    await response(scope, receive, send)


async def _report_explicit_log_record_request_id(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    record = logging.LogRecord(
        name="nba.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "explicit-trace"
    payload = json.loads(JsonFormatter(app_env="test").format(record))
    await JSONResponse(payload)(scope, receive, send)


async def test_middleware_preserves_valid_id_and_resets_context() -> None:
    app = RequestIdMiddleware(_report_request_id)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"X-Request-ID": "trace-123"})

    assert response.headers["X-Request-ID"] == "trace-123"
    assert response.json()["request_id"] == "trace-123"
    assert get_request_id() is None


async def test_middleware_generates_uuid_for_invalid_id() -> None:
    app = RequestIdMiddleware(_report_request_id)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"X-Request-ID": "not valid"})

    assert UUID(response.headers["X-Request-ID"]).version == 4
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_middleware_replaces_existing_response_request_id_header() -> None:
    app = RequestIdMiddleware(_report_request_id_with_existing_header)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"X-Request-ID": "trace-single"})

    assert response.headers.get_list("X-Request-ID") == ["trace-single"]


async def test_json_formatter_prefers_explicit_request_id_to_context() -> None:
    app = RequestIdMiddleware(_report_explicit_log_record_request_id)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"X-Request-ID": "context-trace"})

    assert response.headers["X-Request-ID"] == "context-trace"
    assert response.json()["request_id"] == "explicit-trace"


def test_configure_logging_routes_uvicorn_loggers_once_through_root_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    uvicorn_loggers = tuple(
        logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    )
    for logger in uvicorn_loggers:
        monkeypatch.setattr(logger, "handlers", [logging.StreamHandler()])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "disabled", True)
        monkeypatch.setattr(logger, "level", logging.CRITICAL)

    configure_logging(Settings(_env_file=None))  # type: ignore[call-arg]

    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    for logger in uvicorn_loggers:
        assert logger.handlers == []
        assert logger.propagate is True
        assert logger.disabled is False
        assert logger.level == logging.NOTSET
