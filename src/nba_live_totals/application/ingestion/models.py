"""Provider- and storage-neutral historical-ingestion contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from uuid import UUID

from nba_live_totals.domain import GameId, SeasonId, SourceDocument


class ResourceType(StrEnum):
    """The distinct resources tracked by historical ingestion."""

    SCHEDULE = "schedule"
    BOX_SCORE = "box_score"
    PLAY_BY_PLAY = "play_by_play"


@dataclass(frozen=True, slots=True)
class ResourceKey:
    """A stable identity for one schedule or game-scoped resource."""

    resource_type: ResourceType
    target: SeasonId | GameId

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, ResourceType):
            msg = "resource type must be a ResourceType"
            raise ValueError(msg)
        if self.resource_type is ResourceType.SCHEDULE and not isinstance(self.target, SeasonId):
            msg = "schedule resources require a season target"
            raise ValueError(msg)
        if self.resource_type is not ResourceType.SCHEDULE and not isinstance(self.target, GameId):
            msg = "game resources require a game target"
            raise ValueError(msg)

    @classmethod
    def schedule(cls, season: SeasonId) -> ResourceKey:
        """Create the stable key for a season schedule."""
        return cls(ResourceType.SCHEDULE, season)

    @classmethod
    def box_score(cls, game_id: GameId) -> ResourceKey:
        """Create the stable key for a game's box score."""
        return cls(ResourceType.BOX_SCORE, game_id)

    @classmethod
    def play_by_play(cls, game_id: GameId) -> ResourceKey:
        """Create the stable key for a game's play-by-play feed."""
        return cls(ResourceType.PLAY_BY_PLAY, game_id)

    def __str__(self) -> str:
        """Return the storage- and provider-independent resource identity."""
        return f"{self.resource_type.value}:{self.target.value}"


class ClaimAction(StrEnum):
    """The action an ingester may take after claiming a resource."""

    FETCH = "fetch"
    REPARSE = "reparse"
    SKIP = "skip"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """A fencing token proving ownership of a resource mutation."""

    key: ResourceKey
    run_id: UUID
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, ResourceKey):
            msg = "claim key must be a ResourceKey"
            raise ValueError(msg)
        if not isinstance(self.run_id, UUID):
            msg = "claim run ID must be a UUID"
            raise ValueError(msg)
        if type(self.version) is not int or self.version <= 0:
            msg = "claim version must be a positive integer"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StoredSourceDocument:
    """A persisted immutable source document and its identifier."""

    document_id: UUID
    document: SourceDocument

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, UUID):
            msg = "document ID must be a UUID"
            raise ValueError(msg)
        if not isinstance(self.document, SourceDocument):
            msg = "stored document must be a SourceDocument"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    """A resource-state decision with only the state-valid payloads."""

    action: ClaimAction
    claim: ResourceClaim | None = None
    stored_document: StoredSourceDocument | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ClaimAction):
            msg = "claim action must be a ClaimAction"
            raise ValueError(msg)
        if self.claim is not None and not isinstance(self.claim, ResourceClaim):
            msg = "claim must be a ResourceClaim"
            raise ValueError(msg)
        if self.stored_document is not None and not isinstance(
            self.stored_document, StoredSourceDocument
        ):
            msg = "stored document must be a StoredSourceDocument"
            raise ValueError(msg)

        match self.action:
            case ClaimAction.FETCH:
                if self.claim is None:
                    msg = "fetch decisions require a claim"
                    raise ValueError(msg)
                if self.stored_document is not None:
                    msg = "fetch decisions cannot include a stored document"
                    raise ValueError(msg)
            case ClaimAction.REPARSE:
                if self.claim is None:
                    msg = "reparse decisions require a claim"
                    raise ValueError(msg)
                if self.stored_document is None:
                    msg = "reparse decisions require a stored document"
                    raise ValueError(msg)
            case ClaimAction.SKIP | ClaimAction.BUSY:
                if self.claim is not None or self.stored_document is not None:
                    msg = "skip and busy decisions cannot include a claim or stored document"
                    raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class BackfillRequest:
    """An explicit contiguous season selection, optionally narrowed to one game."""

    seasons: tuple[SeasonId, ...]
    game_id: GameId | None = None
    refresh: bool = False

    def __post_init__(self) -> None:
        seasons = tuple(self.seasons)
        object.__setattr__(self, "seasons", seasons)
        if not seasons:
            msg = "at least one season is required"
            raise ValueError(msg)
        if any(not isinstance(season, SeasonId) for season in seasons):
            msg = "seasons must contain SeasonId values"
            raise ValueError(msg)
        if any(
            next_season.start_year != season.start_year + 1
            for season, next_season in pairwise(seasons)
        ):
            msg = "seasons must be a continuous ascending range"
            raise ValueError(msg)
        if self.game_id is not None and not isinstance(self.game_id, GameId):
            msg = "game ID must be a GameId"
            raise ValueError(msg)
        if self.game_id is not None and len(seasons) != 1:
            msg = "a targeted game requires exactly one season"
            raise ValueError(msg)
        if type(self.refresh) is not bool:
            msg = "refresh must be a boolean"
            raise ValueError(msg)

    @classmethod
    def for_range(
        cls, season_from: SeasonId, season_to: SeasonId, *, refresh: bool = False
    ) -> BackfillRequest:
        """Create a request containing every season in an inclusive range."""
        if season_from.start_year > season_to.start_year:
            msg = "season range must be ascending"
            raise ValueError(msg)
        seasons = tuple(
            SeasonId(f"{year:04d}-{(year + 1) % 100:02d}")
            for year in range(season_from.start_year, season_to.start_year + 1)
        )
        return cls(seasons=seasons, refresh=refresh)


class BackfillStatus(StrEnum):
    """The terminal status of a backfill run."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """The immutable summary of one historical-ingestion run."""

    run_id: UUID
    status: BackfillStatus
    discovered: int
    succeeded: int
    skipped: int
    failed: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            msg = "run ID must be a UUID"
            raise ValueError(msg)
        if not isinstance(self.status, BackfillStatus):
            msg = "backfill status must be a BackfillStatus"
            raise ValueError(msg)
        for field_name in ("discovered", "succeeded", "skipped", "failed"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                msg = f"{field_name} must be a nonnegative integer"
                raise ValueError(msg)


class IngestionResourceError(RuntimeError):
    """A safe provider-neutral resource error identified only by its code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClaimLostError(IngestionResourceError):
    """A fencing token no longer owns the resource it tried to mutate."""

    def __init__(self) -> None:
        super().__init__("claim_lost")
