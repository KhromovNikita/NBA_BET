from datetime import UTC, datetime, timedelta, timezone

import pytest

from nba_live_totals.domain.source import (
    JsonObject,
    SourceDocument,
    canonical_json_checksum,
)


def test_source_document_checksum_is_format_independent() -> None:
    left: JsonObject = {"b": [2, 1], "a": {"value": True}}
    right: JsonObject = {"a": {"value": True}, "b": [2, 1]}

    assert canonical_json_checksum(left) == canonical_json_checksum(right)
    assert len(canonical_json_checksum(left)) == 64


def test_source_document_requires_utc_and_matching_checksum() -> None:
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

    with pytest.raises(ValueError, match="timezone-aware"):
        SourceDocument(
            source="nba.com",
            endpoint="playbyplayv3",
            resource_key="play_by_play:0022500123",
            requested_at=datetime.now(timezone(timedelta(hours=7))),
            request_parameters={"GameID": "0022500123"},
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        SourceDocument(
            source="nba.com",
            endpoint="playbyplayv3",
            resource_key="play_by_play:0022500123",
            requested_at=datetime.now(timezone(timedelta(0), name="Zulu")),
            request_parameters={"GameID": "0022500123"},
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )
