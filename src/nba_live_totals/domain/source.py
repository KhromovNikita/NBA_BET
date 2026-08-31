"""Immutable raw source-document contracts."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _require_utc_datetime(value: datetime) -> None:
    if value.tzinfo is not UTC:
        msg = "datetime must be timezone-aware and in UTC"
        raise ValueError(msg)


def canonical_json_checksum(payload: JsonObject) -> str:
    """Return the SHA-256 digest of canonical UTF-8 JSON."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """An immutable raw response with a content-derived checksum."""

    source: str
    endpoint: str
    resource_key: str
    requested_at: datetime
    request_parameters: JsonObject
    payload: JsonObject
    checksum_sha256: str

    def __post_init__(self) -> None:
        _require_utc_datetime(self.requested_at)
        if self.checksum_sha256 != canonical_json_checksum(self.payload):
            msg = "checksum must match the canonical payload SHA-256"
            raise ValueError(msg)
