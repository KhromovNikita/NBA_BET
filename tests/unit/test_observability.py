import json
import logging
import sys
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
from nba_live_totals.settings import LogLevel, Settings


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


def test_json_formatter_emits_only_non_null_ingestion_fields() -> None:
    formatter = JsonFormatter(app_env="test")
    record = logging.LogRecord(
        name="nba.ingestion",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="ingestion_resource_finished",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(
        event="ingestion_resource_finished",
        run_id="00000000-0000-0000-0000-000000000010",
        season="2025-26",
        game_id="0022500001",
        resource_type="box_score",
        attempt=3,
        status="failed",
        duration_ms=12.5,
        error_code="network_timeout",
        error_type="SensitiveNetworkError",
        payload={"token": "secret"},
        request_parameters={"proxy": "secret"},
        headers={"Authorization": "secret"},
        dsn="postgresql://secret",
        absent=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload == {
        "timestamp": payload["timestamp"],
        "level": "WARNING",
        "logger": "nba.ingestion",
        "message": "ingestion_resource_finished",
        "app_env": "test",
        "event": "ingestion_resource_finished",
        "run_id": "00000000-0000-0000-0000-000000000010",
        "season": "2025-26",
        "game_id": "0022500001",
        "resource_type": "box_score",
        "attempt": 3,
        "status": "failed",
        "duration_ms": 12.5,
        "error_code": "network_timeout",
        "error_type": "SensitiveNetworkError",
    }


def test_json_formatter_omits_null_allowlisted_fields() -> None:
    formatter = JsonFormatter(app_env="test")
    record = logging.LogRecord(
        name="nba.ingestion",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="ingestion_resource_finished",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(
        event="ingestion_resource_finished",
        run_id="00000000-0000-0000-0000-000000000010",
        season="2025-26",
        game_id=None,
        resource_type="schedule",
        attempt=None,
        status="skipped",
        duration_ms=0.0,
        error_code=None,
        error_type=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["duration_ms"] == 0.0
    for field in ("game_id", "attempt", "error_code", "error_type"):
        assert field not in payload


def test_json_formatter_emits_only_safe_phase_three_fields() -> None:
    formatter = JsonFormatter(app_env="test")
    record = logging.LogRecord(
        name="nba.possessions",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="possession_game_finished",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(
        event="possession_game_finished",
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        game_id="0022500001",
        snapshot_id="00000000-0000-4000-8000-000000001001",
        source_document_id="00000000-0000-4000-8000-000000000101",
        normalizer_version="nba-pbp-v1",
        parser_version="analytical-v1",
        rules_version="nba-2025-v1",
        attempt=1,
        row_count=1,
        duration_ms=2.5,
        status="failed",
        error_code="snapshot_mismatch",
        error_type="SnapshotMismatch",
        description="credential=secret",
        signals=["raw", "provider", "actions"],
        payload={"token": "secret"},
        sql="SELECT password FROM secrets",
        url="https://user:secret@private/clickhouse",
        response="raw upstream body",
    )

    payload = json.loads(formatter.format(record))

    assert payload == {
        "timestamp": payload["timestamp"],
        "level": "WARNING",
        "logger": "nba.possessions",
        "message": "possession_game_finished",
        "app_env": "test",
        "event": "possession_game_finished",
        "run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "game_id": "0022500001",
        "snapshot_id": "00000000-0000-4000-8000-000000001001",
        "source_document_id": "00000000-0000-4000-8000-000000000101",
        "normalizer_version": "nba-pbp-v1",
        "parser_version": "analytical-v1",
        "rules_version": "nba-2025-v1",
        "attempt": 1,
        "row_count": 1,
        "duration_ms": 2.5,
        "status": "failed",
        "error_code": "snapshot_mismatch",
        "error_type": "SnapshotMismatch",
    }


def test_json_formatter_omits_phase_three_exception_text() -> None:
    try:
        raise RuntimeError("credential=super-secret")
    except RuntimeError:
        record = logging.LogRecord(
            name="nba_live_totals.application.possessions.service",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="possession_game_finished",
            args=(),
            exc_info=sys.exc_info(),
        )
    record.__dict__.update(
        event="possession_game_finished",
        error_code="unexpected_error",
        error_type="RuntimeError",
    )

    formatted = JsonFormatter(app_env="test").format(record)
    payload = json.loads(formatted)

    assert "exception" not in payload
    assert "super-secret" not in formatted


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


def test_configure_logging_suppresses_external_http_client_details_below_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    http_loggers = tuple(
        logging.getLogger(name)
        for name in ("httpx", "httpx.client", "httpcore", "httpcore.connection")
    )
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    for logger in http_loggers:
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "propagate", True)
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "level", logging.DEBUG)

    configure_logging(
        Settings(log_level=LogLevel.DEBUG, _env_file=None)  # type: ignore[call-arg]
    )
    logging.getLogger("httpx.client").info("GET https://user:secret@example.test/path?token=secret")
    logging.getLogger("httpcore.connection").debug("request_headers Authorization=Bearer-secret")
    logging.getLogger("nba_live_totals.safe").debug("safe_application_event")

    output = capsys.readouterr().err
    assert "safe_application_event" in output
    assert "example.test" not in output
    assert "Bearer-secret" not in output
