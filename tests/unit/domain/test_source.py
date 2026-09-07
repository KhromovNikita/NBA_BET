from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from nba_live_totals.domain.source import (
    JsonObject,
    JsonValue,
    SourceDocument,
    canonical_json_checksum,
)


def test_source_document_checksum_is_format_independent() -> None:
    left: JsonObject = {"b": [2, 1], "a": {"value": True}}
    right: JsonObject = {"a": {"value": True}, "b": [2, 1]}

    assert canonical_json_checksum(left) == canonical_json_checksum(right)
    assert len(canonical_json_checksum(left)) == 64


def test_source_document_requires_matching_checksum() -> None:
    payload: JsonObject = {"game": {"gameId": "0022500123"}}
    with pytest.raises(ValueError, match="checksum"):
        SourceDocument(
            source="nba.com",
            endpoint="playbyplayv3",
            resource_key="play_by_play:0022500123",
            requested_at=datetime.now(UTC),
            request_parameters={"GameID": "0022500123"},
            payload=payload,
            checksum_sha256="0" * 64,
        )


def test_source_document_rejects_naive_requested_at() -> None:
    payload: JsonObject = {"game": {"gameId": "0022500123"}}

    with pytest.raises(ValueError, match="timezone-aware"):
        SourceDocument(
            source="nba.com",
            endpoint="playbyplayv3",
            resource_key="play_by_play:0022500123",
            requested_at=datetime(2026, 1, 1, 1, 0),
            request_parameters={"GameID": "0022500123"},
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )


def test_source_document_normalizes_aware_requested_at_to_utc() -> None:
    payload: JsonObject = {"game": {"gameId": "0022500123"}}
    requested_at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=7)))

    document = SourceDocument(
        source="nba.com",
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
        requested_at=requested_at,
        request_parameters={"GameID": "0022500123"},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )

    assert document.requested_at == datetime(2025, 12, 31, 18, 0, tzinfo=UTC)
    assert document.requested_at.tzinfo is UTC


def test_source_document_snapshots_nested_payload_and_request_parameter_aliases() -> None:
    payload: JsonObject = {
        "game": {"actions": [{"tags": ["original"]}]},
    }
    request_parameters: JsonObject = {
        "filters": {"periods": [1, 2]},
    }
    expected_payload: JsonObject = {
        "game": {"actions": [{"tags": ["original"]}]},
    }
    expected_parameters: JsonObject = {
        "filters": {"periods": [1, 2]},
    }
    document = SourceDocument(
        source="nba.com",
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
        requested_at=datetime.now(UTC),
        request_parameters=request_parameters,
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )

    original_actions = cast(list[JsonValue], cast(JsonObject, payload["game"])["actions"])
    cast(JsonObject, original_actions[0])["tags"] = ["transport-mutated"]
    cast(list[JsonValue], cast(JsonObject, request_parameters["filters"])["periods"]).append(3)

    exposed_payload = document.payload
    exposed_actions = cast(
        list[JsonValue],
        cast(JsonObject, exposed_payload["game"])["actions"],
    )
    cast(list[JsonValue], cast(JsonObject, exposed_actions[0])["tags"]).append("consumer-mutated")
    exposed_parameters = document.request_parameters
    cast(list[JsonValue], cast(JsonObject, exposed_parameters["filters"])["periods"]).clear()

    assert document.payload == expected_payload
    assert document.request_parameters == expected_parameters
    assert document.serializable_payload() == expected_payload
    assert document.serializable_request_parameters() == expected_parameters
    assert document.checksum_sha256 == canonical_json_checksum(expected_payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        canonical_json_checksum({"value": value})


def test_canonical_json_never_stringifies_unsupported_values() -> None:
    unsupported = cast(JsonObject, {"value": object()})

    with pytest.raises(TypeError, match="JSON"):
        canonical_json_checksum(unsupported)


@pytest.mark.parametrize("value", [1.0, -12.5, 0.125])
def test_phase_2_canonical_json_still_accepts_finite_provider_floats(value: float) -> None:
    checksum = canonical_json_checksum({"provider_number": value})

    assert len(checksum) == 64
