"""Strict canonical JSON bytes for Phase 3 content identities."""

from __future__ import annotations

import hashlib
import json
from typing import cast

type CanonicalScalar = str | int | bool | None
type CanonicalValue = (
    CanonicalScalar | list[CanonicalValue] | tuple[CanonicalValue, ...] | dict[str, CanonicalValue]
)
type CanonicalObject = dict[str, CanonicalValue]


def _validate_canonical_value(value: object) -> None:
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is list or type(value) is tuple:
        for item in cast(list[object] | tuple[object, ...], value):
            _validate_canonical_value(item)
        return
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if any(type(key) is not str for key in mapping):
            raise TypeError("canonical JSON object keys must be exact strings")
        for item in mapping.values():
            _validate_canonical_value(item)
        return
    raise TypeError("value is not supported by the strict canonical JSON contract")


def canonical_json_bytes(projection: object) -> bytes:
    """Encode an already-typed projection using ``nba-live-totals-cjson-v1``."""
    _validate_canonical_value(projection)
    return json.dumps(
        projection,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_sha256(projection: object) -> str:
    """Hash strict canonical UTF-8 JSON bytes with SHA-256."""
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


__all__ = [
    "CanonicalObject",
    "CanonicalScalar",
    "CanonicalValue",
    "canonical_json_bytes",
    "canonical_json_sha256",
]
