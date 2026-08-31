"""Provider and persistence ports for historical ingestion."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from nba_live_totals.application.ingestion.models import (
    BackfillRequest,
    BackfillResult,
    ClaimDecision,
    ResourceClaim,
    ResourceKey,
    StoredSourceDocument,
)
from nba_live_totals.domain import (
    BoxScore,
    GameId,
    PlayByPlayEvent,
    SeasonId,
    SeasonSchedule,
    SourceDocument,
)


@runtime_checkable
class HistoricalDataSource(Protocol):
    """Fetch raw provider documents and parse them into domain objects."""

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument: ...

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument: ...

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument: ...

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule: ...

    def parse_box_score(self, document: SourceDocument) -> BoxScore: ...

    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]: ...


@runtime_checkable
class HistoricalIngestionStore(Protocol):
    """Atomic persistence operations required by the ingestion use case."""

    async def start_run(self, request: BackfillRequest) -> UUID: ...

    async def finish_run(self, run_id: UUID, result: BackfillResult) -> None: ...

    async def claim_resource(
        self, run_id: UUID, key: ResourceKey, *, refresh: bool
    ) -> ClaimDecision: ...

    async def latest_document(self, key: ResourceKey) -> StoredSourceDocument | None: ...

    async def save_document(self, claim: ResourceClaim, document: SourceDocument) -> UUID: ...

    async def apply_schedule(
        self, claim: ResourceClaim, document_id: UUID, schedule: SeasonSchedule
    ) -> None: ...

    async def replace_box_score(
        self, claim: ResourceClaim, document_id: UUID, box_score: BoxScore
    ) -> None: ...

    async def replace_play_by_play(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        events: tuple[PlayByPlayEvent, ...],
    ) -> None: ...

    async def fail_resource(
        self, claim: ResourceClaim, *, error_code: str, error_type: str
    ) -> None: ...
