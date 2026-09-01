"""Protocol-faithful in-memory collaborators for ingestion service tests."""

from datetime import UTC, datetime
from uuid import UUID

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    ClaimAction,
    ClaimDecision,
    ResourceClaim,
    ResourceKey,
    StoredSourceDocument,
)
from nba_live_totals.domain import (
    BoxScore,
    GameId,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonSchedule,
    SourceDocument,
    canonical_json_checksum,
)


class FakeHistoricalDataSource:
    """Return deterministic documents and aggregates while recording boundary order."""

    def __init__(
        self,
        *,
        schedules: dict[SeasonId, SeasonSchedule],
        box_scores: dict[GameId, BoxScore],
        play_by_play: dict[GameId, tuple[PlayByPlayEvent, ...]],
        events: list[str] | None = None,
    ) -> None:
        self.schedules = schedules
        self.box_scores = box_scores
        self.play_by_play = play_by_play
        self.events = events if events is not None else []
        self.schedule_fetches: list[SeasonId] = []
        self.box_score_fetches: list[GameId] = []
        self.play_by_play_fetches: list[GameId] = []
        self.parsed_keys: list[str] = []

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument:
        key = ResourceKey.schedule(season)
        self.schedule_fetches.append(season)
        self.events.append(f"fetch:{key}")
        return source_document(key)

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.box_score(game_id)
        self.box_score_fetches.append(game_id)
        self.events.append(f"fetch:{key}")
        return source_document(key)

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.play_by_play(game_id)
        self.play_by_play_fetches.append(game_id)
        self.events.append(f"fetch:{key}")
        return source_document(key)

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule:
        self.parsed_keys.append(document.resource_key)
        self.events.append(f"parse:{document.resource_key}")
        season = SeasonId(document.resource_key.removeprefix("schedule:"))
        return self.schedules[season]

    def parse_box_score(self, document: SourceDocument) -> BoxScore:
        self.parsed_keys.append(document.resource_key)
        self.events.append(f"parse:{document.resource_key}")
        game_id = GameId(document.resource_key.removeprefix("box_score:"))
        return self.box_scores[game_id]

    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
        self.parsed_keys.append(document.resource_key)
        self.events.append(f"parse:{document.resource_key}")
        game_id = GameId(document.resource_key.removeprefix("play_by_play:"))
        return self.play_by_play[game_id]


class FakeHistoricalIngestionStore:
    """Apply claim decisions in memory and retain every use-case-level write."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        decisions: dict[ResourceKey, ClaimDecision | ClaimAction] | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.decisions = decisions if decisions is not None else {}
        self.run_id = UUID("00000000-0000-0000-0000-000000000010")
        self.started: list[BackfillRequest] = []
        self.finished: list[tuple[UUID, BackfillResult]] = []
        self.claimed: list[tuple[UUID, ResourceKey, bool]] = []
        self.latest: dict[ResourceKey, StoredSourceDocument] = {}
        self.saved: list[tuple[ResourceClaim, SourceDocument, UUID]] = []
        self.applied_schedules: list[tuple[ResourceClaim, UUID, SeasonSchedule]] = []
        self.applied_box_scores: list[tuple[ResourceClaim, UUID, BoxScore]] = []
        self.applied_play_by_play: list[
            tuple[ResourceClaim, UUID, tuple[PlayByPlayEvent, ...]]
        ] = []
        self.failures: list[tuple[ResourceClaim, str, str]] = []

    async def start_run(self, request: BackfillRequest) -> UUID:
        self.events.append("start_run")
        self.started.append(request)
        return self.run_id

    async def finish_run(self, run_id: UUID, result: BackfillResult) -> None:
        self.events.append("finish_run")
        self.finished.append((run_id, result))

    async def claim_resource(
        self, run_id: UUID, key: ResourceKey, *, refresh: bool
    ) -> ClaimDecision:
        self.events.append(f"claim:{key}")
        self.claimed.append((run_id, key, refresh))
        configured = self.decisions.get(key, ClaimAction.FETCH)
        if isinstance(configured, ClaimDecision):
            return configured
        if configured is ClaimAction.SKIP:
            return ClaimDecision(configured, stored_document=self.latest[key])
        if configured is ClaimAction.BUSY:
            return ClaimDecision(configured)
        claim = ResourceClaim(key=key, run_id=run_id, version=1)
        if configured is ClaimAction.REPARSE:
            return ClaimDecision(
                configured,
                claim=claim,
                stored_document=self.latest[key],
            )
        return ClaimDecision(configured, claim=claim)

    async def latest_document(self, key: ResourceKey) -> StoredSourceDocument | None:
        self.events.append(f"latest:{key}")
        return self.latest.get(key)

    async def save_document(self, claim: ResourceClaim, document: SourceDocument) -> UUID:
        document_id = UUID(int=100 + len(self.saved))
        self.events.append(f"save:{claim.key}")
        stored = StoredSourceDocument(document_id=document_id, document=document)
        self.saved.append((claim, document, document_id))
        self.latest[claim.key] = stored
        return document_id

    async def apply_schedule(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        schedule: SeasonSchedule,
    ) -> None:
        self.events.append(f"apply:{claim.key}")
        self.applied_schedules.append((claim, document_id, schedule))

    async def replace_box_score(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        box_score: BoxScore,
    ) -> None:
        self.events.append(f"apply:{claim.key}")
        self.applied_box_scores.append((claim, document_id, box_score))

    async def replace_play_by_play(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        events: tuple[PlayByPlayEvent, ...],
    ) -> None:
        self.events.append(f"apply:{claim.key}")
        self.applied_play_by_play.append((claim, document_id, events))

    async def fail_resource(
        self,
        claim: ResourceClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        self.events.append(f"fail:{claim.key}")
        self.failures.append((claim, error_code, error_type))


def source_document(key: ResourceKey) -> SourceDocument:
    """Build a complete raw document for one stable resource key."""
    payload: JsonObject = {"resource": str(key)}
    endpoint = {
        "schedule": "scheduleleaguev2",
        "box_score": "boxscoretraditionalv3",
        "play_by_play": "playbyplayv3",
    }[key.resource_type.value]
    return SourceDocument(
        source="fake.nba",
        endpoint=endpoint,
        resource_key=str(key),
        requested_at=datetime(2026, 9, 1, tzinfo=UTC),
        request_parameters={"resource": str(key)},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
