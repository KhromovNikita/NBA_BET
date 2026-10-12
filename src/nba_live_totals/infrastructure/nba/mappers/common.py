"""Strict, payload-safe conversions shared by NBA V3 mappers."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from nba_live_totals.domain import JsonObject, JsonValue, SourceDocument
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError

_DURATION_PATTERN = re.compile(r"^PT(?P<minutes>\d+)M(?P<seconds>\d{1,2}(?:\.\d+)?)S$")


def schema_mismatch() -> ProviderSchemaError:
    """Construct a stable error that never includes provider values."""
    return ProviderSchemaError("schema_mismatch")


def required_value(mapping: JsonObject, key: str) -> JsonValue:
    """Return a present key without exposing its value on failure."""
    if key not in mapping:
        raise schema_mismatch()
    return mapping[key]


def require_mapping(value: object) -> JsonObject:
    """Require a JSON object with string keys."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise schema_mismatch()
    return cast(JsonObject, value)


def require_list(value: object) -> list[JsonValue]:
    """Require a JSON array."""
    if not isinstance(value, list):
        raise schema_mismatch()
    return cast(list[JsonValue], value)


def require_str(value: object) -> str:
    """Require a JSON string."""
    if not isinstance(value, str):
        raise schema_mismatch()
    return value


def require_int(value: object) -> int:
    """Require a JSON integer, excluding booleans."""
    if type(value) is not int:
        raise schema_mismatch()
    return value


def optional_str(value: object) -> str | None:
    """Require a nullable JSON string."""
    if value is None:
        return None
    return require_str(value)


def optional_int(value: object) -> int | None:
    """Require a nullable JSON integer, excluding booleans."""
    if value is None:
        return None
    return require_int(value)


def optional_bool(value: object) -> bool | None:
    """Require a nullable JSON boolean."""
    if value is None:
        return None
    if type(value) is not bool:
        raise schema_mismatch()
    return value


def provider_flag(value: object) -> bool:
    """Convert an NBA V3 boolean flag from its documented scalar forms."""
    if type(value) is bool:
        return value
    if type(value) is int and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value in {"0", "1"}:
        return value == "1"
    raise schema_mismatch()


def parse_utc_datetime(value: object) -> datetime:
    """Parse an aware ISO datetime and normalize it to UTC."""
    text = require_str(value)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        raise schema_mismatch()
    if parsed.tzinfo is None:
        raise schema_mismatch()
    try:
        normalized_datetime = parsed.astimezone(UTC)
    except OverflowError, ValueError:
        normalized_datetime = None
    if normalized_datetime is None:
        raise schema_mismatch()
    return normalized_datetime


def _parse_duration(value: object) -> tuple[int, Decimal]:
    text = require_str(value)
    match = _DURATION_PATTERN.fullmatch(text)
    if match is None:
        raise schema_mismatch()
    try:
        minutes = int(match.group("minutes"))
    except ValueError:
        minutes = None
    if minutes is None:
        raise schema_mismatch()
    try:
        seconds = Decimal(match.group("seconds"))
    except InvalidOperation:
        seconds = None
    if seconds is None:
        raise schema_mismatch()
    if not seconds.is_finite() or seconds < 0 or seconds >= 60:
        raise schema_mismatch()
    return minutes, seconds


def parse_iso_duration_seconds(value: object) -> int:
    """Convert an NBA ISO minutes value to whole seconds."""
    minutes, seconds = _parse_duration(value)
    total = Decimal(minutes * 60) + seconds
    if total != total.to_integral_value():
        raise schema_mismatch()
    return int(total)


def parse_clock_milliseconds(value: object) -> int:
    """Convert an NBA ISO game clock to exact nonnegative milliseconds."""
    minutes, seconds = _parse_duration(value)
    total = (Decimal(minutes * 60) + seconds) * 1000
    if total != total.to_integral_value():
        raise schema_mismatch()
    return int(total)


def decimal_or_none(value: object) -> Decimal | None:
    """Convert a nullable JSON number to Decimal without binary-float artifacts."""
    if value is None:
        return None
    if type(value) not in {int, float, str}:
        raise schema_mismatch()
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        result = None
    if result is None:
        raise schema_mismatch()
    if not result.is_finite():
        raise schema_mismatch()
    return result


def require_document_identity(document: SourceDocument, *, endpoint: str) -> None:
    """Require the source and endpoint identities owned by these mappers."""
    if document.source != "nba.com" or document.endpoint != endpoint:
        raise schema_mismatch()
