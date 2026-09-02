"""Immutable raw source-document contracts."""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type _FrozenJsonValue = JsonScalar | tuple[_FrozenJsonValue, ...] | Mapping[str, _FrozenJsonValue]
type _FrozenJsonObject = Mapping[str, _FrozenJsonValue]


def _normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "datetime must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _freeze_json_value(value: object) -> _FrozenJsonValue:
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            msg = "JSON numbers must be finite"
            raise ValueError(msg)
        return value
    if type(value) is list:
        return tuple(_freeze_json_value(item) for item in cast(list[object], value))
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if any(type(key) is not str for key in mapping):
            msg = "JSON object keys must be strings"
            raise TypeError(msg)
        return MappingProxyType(
            {cast(str, key): _freeze_json_value(item) for key, item in mapping.items()}
        )
    msg = "value is not a supported JSON type"
    raise TypeError(msg)


def _freeze_json_object(value: JsonObject) -> _FrozenJsonObject:
    frozen = _freeze_json_value(value)
    if not isinstance(frozen, Mapping):
        msg = "JSON document must be an object"
        raise TypeError(msg)
    return frozen


def _thaw_json_value(value: _FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _thaw_json_object(value: _FrozenJsonObject) -> JsonObject:
    return {key: _thaw_json_value(item) for key, item in value.items()}


def _snapshot_checksum(payload: _FrozenJsonObject) -> str:
    encoded = json.dumps(
        _thaw_json_object(payload),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_json_checksum(payload: JsonObject) -> str:
    """Return the SHA-256 digest of a validated canonical UTF-8 JSON snapshot."""
    return _snapshot_checksum(_freeze_json_object(payload))


@dataclass(frozen=True, slots=True, init=False)
class SourceDocument:
    """A raw response backed by private, deeply immutable JSON snapshots."""

    source: str
    endpoint: str
    resource_key: str
    requested_at: datetime
    _request_parameters_snapshot: _FrozenJsonObject = field(repr=False)
    _payload_snapshot: _FrozenJsonObject = field(repr=False)
    checksum_sha256: str

    def __init__(
        self,
        source: str,
        endpoint: str,
        resource_key: str,
        requested_at: datetime,
        request_parameters: JsonObject,
        payload: JsonObject,
        checksum_sha256: str,
    ) -> None:
        normalized_requested_at = _normalize_utc_datetime(requested_at)
        parameter_snapshot = _freeze_json_object(request_parameters)
        payload_snapshot = _freeze_json_object(payload)
        if checksum_sha256 != _snapshot_checksum(payload_snapshot):
            msg = "checksum must match the canonical payload SHA-256"
            raise ValueError(msg)

        object.__setattr__(self, "source", source)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "resource_key", resource_key)
        object.__setattr__(self, "requested_at", normalized_requested_at)
        object.__setattr__(self, "_request_parameters_snapshot", parameter_snapshot)
        object.__setattr__(self, "_payload_snapshot", payload_snapshot)
        object.__setattr__(self, "checksum_sha256", checksum_sha256)

    @property
    def request_parameters(self) -> JsonObject:
        """Return an independent mutable JSON value for ordinary consumers."""
        return self.serializable_request_parameters()

    @property
    def payload(self) -> JsonObject:
        """Return an independent mutable JSON value for ordinary consumers."""
        return self.serializable_payload()

    def serializable_request_parameters(self) -> JsonObject:
        """Return a fresh serializable copy of the validated parameter snapshot."""
        return _thaw_json_object(self._request_parameters_snapshot)

    def serializable_payload(self) -> JsonObject:
        """Return a fresh serializable copy of the checksum-validated payload snapshot."""
        return _thaw_json_object(self._payload_snapshot)
