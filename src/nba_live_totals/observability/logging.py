import json
import logging
from datetime import UTC, datetime
from typing import Any

from nba_live_totals.api.request_id import get_request_id
from nba_live_totals.settings import Settings


class JsonFormatter(logging.Formatter):
    def __init__(self, app_env: str) -> None:
        super().__init__()
        self._app_env = app_env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app_env": self._app_env,
        }
        record_request_id = getattr(record, "request_id", None)
        request_id = record_request_id if isinstance(record_request_id, str) else get_request_id()
        if request_id is not None:
            payload["request_id"] = request_id
        for field in (
            "dependency",
            "error_type",
            "latency_ms",
            "resource",
            "event",
            "run_id",
            "season",
            "game_id",
            "snapshot_id",
            "source_document_id",
            "normalizer_version",
            "parser_version",
            "rules_version",
            "resource_type",
            "attempt",
            "row_count",
            "status",
            "duration_ms",
            "error_code",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(settings.app_env))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.value)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.disabled = False
