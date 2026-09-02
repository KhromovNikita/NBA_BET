from datetime import UTC, datetime, timedelta, timezone

import pytest

from nba_live_totals.application.ingestion import HistoricalDataSource
from nba_live_totals.domain import GameId, JsonObject, SeasonId, canonical_json_checksum
from nba_live_totals.infrastructure.nba.source import (
    NBAApiDocumentFetcher,
    NBAApiHistoricalDataSource,
)


class RecordingTransport:
    def __init__(self, payloads: list[JsonObject]) -> None:
        self._payloads = iter(payloads)
        self.requests: list[tuple[str, JsonObject]] = []

    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        self.requests.append((endpoint, parameters))
        return next(self._payloads)


def test_historical_source_facade_satisfies_the_complete_runtime_protocol() -> None:
    source = NBAApiHistoricalDataSource(RecordingTransport([]))

    assert isinstance(source, HistoricalDataSource)


@pytest.mark.asyncio
async def test_fetcher_uses_exact_nba_endpoint_requests_and_resource_keys() -> None:
    payloads: list[JsonObject] = [
        {"schedule": True},
        {"box": True},
        {"playByPlay": True},
    ]
    transport = RecordingTransport(payloads)
    requested_at = datetime(2026, 9, 1, 0, 0, tzinfo=timezone(timedelta(hours=7)))
    fetcher = NBAApiDocumentFetcher(transport, utc_now=lambda: requested_at)

    schedule = await fetcher.fetch_schedule(SeasonId("2025-26"))
    box_score = await fetcher.fetch_box_score(GameId("0022500123"))
    play_by_play = await fetcher.fetch_play_by_play(GameId("0022500123"))

    assert transport.requests == [
        ("scheduleleaguev2", {"LeagueID": "00", "Season": "2025-26"}),
        (
            "boxscoretraditionalv3",
            {
                "GameID": "0022500123",
                "StartPeriod": 0,
                "EndPeriod": 0,
                "StartRange": 0,
                "EndRange": 0,
                "RangeType": 0,
            },
        ),
        (
            "playbyplayv3",
            {"GameID": "0022500123", "StartPeriod": 0, "EndPeriod": 0},
        ),
    ]
    assert [document.source for document in (schedule, box_score, play_by_play)] == ["nba.com"] * 3
    assert [document.endpoint for document in (schedule, box_score, play_by_play)] == [
        "scheduleleaguev2",
        "boxscoretraditionalv3",
        "playbyplayv3",
    ]
    assert [document.resource_key for document in (schedule, box_score, play_by_play)] == [
        "schedule:2025-26",
        "box_score:0022500123",
        "play_by_play:0022500123",
    ]
    assert [document.requested_at for document in (schedule, box_score, play_by_play)] == [
        datetime(2026, 8, 31, 17, 0, tzinfo=UTC)
    ] * 3
    assert [document.request_parameters for document in (schedule, box_score, play_by_play)] == [
        request[1] for request in transport.requests
    ]


@pytest.mark.asyncio
async def test_fetcher_snapshots_payload_values_and_builds_canonical_checksums() -> None:
    payloads: list[JsonObject] = [
        {"z": [3, {"é": True}], "a": None},
        {"resultSets": {"rowSet": []}},
        {"game": {"actions": []}},
    ]
    transport = RecordingTransport(payloads)
    fetcher = NBAApiDocumentFetcher(
        transport,
        utc_now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )

    documents = (
        await fetcher.fetch_schedule(SeasonId("2025-26")),
        await fetcher.fetch_box_score(GameId("0022500123")),
        await fetcher.fetch_play_by_play(GameId("0022500123")),
    )

    for document, payload in zip(documents, payloads, strict=True):
        assert document.payload == payload
        assert document.payload is not payload
        assert document.checksum_sha256 == canonical_json_checksum(payload)

    payloads[0]["z"] = ["mutated after transport return"]
    assert documents[0].payload == {"z": [3, {"é": True}], "a": None}
