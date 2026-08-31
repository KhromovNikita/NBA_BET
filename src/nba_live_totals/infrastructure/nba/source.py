"""Raw NBA source-document facade."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from nba_live_totals.domain import (
    GameId,
    JsonObject,
    SeasonId,
    SourceDocument,
    canonical_json_checksum,
)


class _RawTransport(Protocol):
    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject: ...


class NBAApiDocumentFetcher:
    """Fetch immutable raw documents for the three approved NBA endpoints."""

    def __init__(
        self,
        transport: _RawTransport,
        *,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transport = transport
        self._utc_now = utc_now

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument:
        parameters: JsonObject = {"LeagueID": "00", "Season": season.value}
        return await self._fetch(
            endpoint="scheduleleaguev2",
            resource_key=f"schedule:{season.value}",
            parameters=parameters,
        )

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        parameters: JsonObject = {
            "GameID": game_id.value,
            "StartPeriod": 0,
            "EndPeriod": 0,
            "StartRange": 0,
            "EndRange": 0,
            "RangeType": 0,
        }
        return await self._fetch(
            endpoint="boxscoretraditionalv3",
            resource_key=f"box_score:{game_id.value}",
            parameters=parameters,
        )

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        parameters: JsonObject = {
            "GameID": game_id.value,
            "StartPeriod": 0,
            "EndPeriod": 0,
        }
        return await self._fetch(
            endpoint="playbyplayv3",
            resource_key=f"play_by_play:{game_id.value}",
            parameters=parameters,
        )

    async def _fetch(
        self,
        *,
        endpoint: str,
        resource_key: str,
        parameters: JsonObject,
    ) -> SourceDocument:
        requested_at = self._utc_now()
        payload = await self._transport.get(endpoint, parameters)
        return SourceDocument(
            source="nba.com",
            endpoint=endpoint,
            resource_key=resource_key,
            requested_at=requested_at,
            request_parameters=parameters,
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )
