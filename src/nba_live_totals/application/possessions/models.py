"""Immutable application contracts for possession reconstruction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from uuid import UUID

from nba_live_totals.domain import (
    GameContext,
    GameId,
    PlayByPlayEvent,
    PossessionVersions,
    SeasonId,
    SourceDocument,
    canonical_event_checksum,
    game_context_checksum,
    possession_input_token_checksum,
)
from nba_live_totals.domain import (
    parse_revision_id as compute_parse_revision_id,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


def _require_sha256(value: str, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_positive_bigint(value: int, *, field_name: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_BIGINT:
        raise ValueError(f"{field_name} must be a positive signed 64-bit integer")


def _require_nonnegative_integer(value: int, *, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class GamePbpSnapshot:
    """One immutable, transactionally consistent final-game PBP input."""

    game: GameContext
    applied_pbp_document_id: UUID
    applied_pbp_sha256: str
    canonical_events: tuple[PlayByPlayEvent, ...]
    canonical_event_sha256: str
    game_context_sha256: str
    source_document: SourceDocument

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameContext):
            raise ValueError("game must be a GameContext")
        if type(self.applied_pbp_document_id) is not UUID:
            raise ValueError("applied PBP document ID must be a UUID")
        if not isinstance(self.source_document, SourceDocument):
            raise ValueError("source document must be a SourceDocument")
        _require_sha256(self.applied_pbp_sha256, field_name="applied PBP SHA-256")
        _require_sha256(self.canonical_event_sha256, field_name="canonical event SHA-256")
        _require_sha256(self.game_context_sha256, field_name="game context SHA-256")
        events = tuple(self.canonical_events)
        if not events:
            raise ValueError("canonical events must not be empty")
        if any(not isinstance(event, PlayByPlayEvent) for event in events):
            raise ValueError("canonical events must contain PlayByPlayEvent values")
        if any(event.game_id != self.game.game_id for event in events):
            raise ValueError("canonical events and game context must belong to one game")
        if tuple(event.sequence_number for event in events) != tuple(range(1, len(events) + 1)):
            raise ValueError("canonical events must be contiguous and in sequence order")
        if self.source_document.resource_key != f"play_by_play:{self.game.game_id.value}":
            raise ValueError("source document resource must match the snapshot game")
        if self.source_document.checksum_sha256 != self.applied_pbp_sha256:
            raise ValueError("source document checksum must match applied PBP checksum")
        if canonical_event_checksum(events) != self.canonical_event_sha256:
            raise ValueError("canonical event checksum does not match canonical events")
        if game_context_checksum(self.game) != self.game_context_sha256:
            raise ValueError("game context checksum does not match game context")
        object.__setattr__(self, "canonical_events", events)


@dataclass(frozen=True, slots=True)
class PossessionInputToken:
    """Freshness identity combining PBP lineage, game context and code versions."""

    game_id: GameId
    applied_pbp_document_id: UUID
    applied_pbp_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str
    versions: PossessionVersions
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if type(self.applied_pbp_document_id) is not UUID:
            raise ValueError("applied PBP document ID must be a UUID")
        if not isinstance(self.versions, PossessionVersions):
            raise ValueError("versions must be PossessionVersions")
        _require_sha256(self.applied_pbp_sha256, field_name="applied PBP SHA-256")
        _require_sha256(self.canonical_event_sha256, field_name="canonical event SHA-256")
        _require_sha256(self.game_context_sha256, field_name="game context SHA-256")
        object.__setattr__(
            self,
            "sha256",
            possession_input_token_checksum(
                game_id=self.game_id,
                applied_pbp_document_id=self.applied_pbp_document_id,
                applied_pbp_sha256=self.applied_pbp_sha256,
                canonical_event_sha256=self.canonical_event_sha256,
                game_context_sha256=self.game_context_sha256,
                versions=self.versions,
            ),
        )

    @classmethod
    def from_snapshot(
        cls, snapshot: GamePbpSnapshot, versions: PossessionVersions
    ) -> PossessionInputToken:
        """Derive the exact input token from a validated immutable snapshot."""
        if not isinstance(snapshot, GamePbpSnapshot):
            raise ValueError("snapshot must be a GamePbpSnapshot")
        return cls(
            game_id=snapshot.game.game_id,
            applied_pbp_document_id=snapshot.applied_pbp_document_id,
            applied_pbp_sha256=snapshot.applied_pbp_sha256,
            canonical_event_sha256=snapshot.canonical_event_sha256,
            game_context_sha256=snapshot.game_context_sha256,
            versions=versions,
        )


class BuildMode(StrEnum):
    """Whether a run materializes snapshots or verifies current snapshots."""

    MATERIALIZE = "materialize"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class BuildPossessionsRequest:
    """An explicit contiguous season selection for a Phase 3 run."""

    seasons: tuple[SeasonId, ...]
    game_id: GameId | None = None
    mode: BuildMode = BuildMode.MATERIALIZE
    rebuild: bool = False

    def __post_init__(self) -> None:
        seasons = tuple(self.seasons)
        object.__setattr__(self, "seasons", seasons)
        if not seasons:
            raise ValueError("at least one season is required")
        if any(not isinstance(season, SeasonId) for season in seasons):
            raise ValueError("seasons must contain SeasonId values")
        if any(
            following.start_year != preceding.start_year + 1
            for preceding, following in pairwise(seasons)
        ):
            raise ValueError("seasons must be a continuous ascending range")
        if self.game_id is not None and not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if self.game_id is not None and len(seasons) != 1:
            raise ValueError("a targeted game requires exactly one season")
        if (
            self.game_id is not None
            and self.game_id.value[3:5] != f"{seasons[0].start_year % 100:02d}"
        ):
            raise ValueError("a targeted game must belong to the selected season")
        if not isinstance(self.mode, BuildMode):
            raise ValueError("mode must be a BuildMode")
        if type(self.rebuild) is not bool:
            raise ValueError("rebuild must be a boolean")
        if self.mode is BuildMode.VERIFY and self.rebuild:
            raise ValueError("verify mode and rebuild are mutually exclusive")

    @classmethod
    def for_range(
        cls,
        season_from: SeasonId,
        season_to: SeasonId,
        *,
        mode: BuildMode = BuildMode.MATERIALIZE,
        rebuild: bool = False,
    ) -> BuildPossessionsRequest:
        """Create an inclusive contiguous season-range request."""
        if season_from.start_year > season_to.start_year:
            raise ValueError("season range must be ascending")
        seasons = tuple(
            SeasonId(f"{year:04d}-{(year + 1) % 100:02d}")
            for year in range(season_from.start_year, season_to.start_year + 1)
        )
        return cls(seasons=seasons, mode=mode, rebuild=rebuild)


class BuildStatus(StrEnum):
    """Terminal status of one possession reconstruction run."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PossessionAttemptState(StrEnum):
    """Durable lifecycle state of one immutable materialization attempt."""

    ALLOCATED = "allocated"
    WRITING = "writing"
    VERIFIED = "verified"
    PUBLISHED = "published"
    POISONED = "poisoned"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class BuildPossessionsResult:
    """Bounded counters for a completed or interrupted Phase 3 run."""

    run_id: UUID
    status: BuildStatus
    discovered: int
    materialized: int
    skipped_current: int
    verified: int
    failed: int
    busy: int

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        if not isinstance(self.status, BuildStatus):
            raise ValueError("status must be a BuildStatus")
        for field_name in (
            "discovered",
            "materialized",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        ):
            _require_nonnegative_integer(getattr(self, field_name), field_name=field_name)
        accounted = (
            self.materialized + self.skipped_current + self.verified + self.failed + self.busy
        )
        if accounted > self.discovered or (
            self.status is not BuildStatus.INTERRUPTED and accounted != self.discovered
        ):
            raise ValueError("terminal run counters must account for discovered games")
        if self.status is BuildStatus.SUCCEEDED and (self.failed or self.busy):
            raise ValueError("succeeded runs cannot contain failed or busy games")
        successful = self.materialized + self.skipped_current + self.verified
        unsuccessful = self.failed + self.busy
        if self.status is BuildStatus.PARTIAL and (not successful or not unsuccessful):
            raise ValueError("partial runs require successful and unsuccessful outcomes")
        if self.status is BuildStatus.FAILED and successful:
            raise ValueError("failed runs cannot contain successful outcomes")


@dataclass(frozen=True, slots=True)
class PossessionSnapshotAttempt:
    """Immutable identity of one ClickHouse materialization attempt."""

    game: GameContext
    snapshot_id: UUID
    generation: int
    parse_revision_id: str
    input_token: PossessionInputToken
    state: PossessionAttemptState
    expected_row_count: int | None = None
    expected_output_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameContext):
            raise ValueError("game must be a GameContext")
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        _require_positive_bigint(self.generation, field_name="generation")
        _require_sha256(self.parse_revision_id, field_name="parse revision ID")
        if not isinstance(self.input_token, PossessionInputToken):
            raise ValueError("input token must be a PossessionInputToken")
        if self.input_token.game_id != self.game.game_id:
            raise ValueError("attempt and input token must belong to the same game")
        if self.input_token.game_context_sha256 != game_context_checksum(self.game):
            raise ValueError("attempt input token must match the game context")
        expected_revision = compute_parse_revision_id(
            game_id=self.game.game_id,
            pbp_document_sha256=self.input_token.applied_pbp_sha256,
            canonical_event_sha256=self.input_token.canonical_event_sha256,
            game_context_sha256=self.input_token.game_context_sha256,
            versions=self.input_token.versions,
        )
        if self.parse_revision_id != expected_revision:
            raise ValueError("parse revision ID must match the input token")
        if not isinstance(self.state, PossessionAttemptState):
            raise ValueError("state must be a PossessionAttemptState")
        if self.expected_row_count is not None:
            _require_positive_bigint(self.expected_row_count, field_name="expected row count")
        if self.expected_output_sha256 is not None:
            _require_sha256(self.expected_output_sha256, field_name="expected output SHA-256")
        has_expected = (
            self.expected_row_count is not None and self.expected_output_sha256 is not None
        )
        if (self.expected_row_count is None) != (self.expected_output_sha256 is None):
            raise ValueError("expected row count and hash must be set together")
        if self.state is PossessionAttemptState.ALLOCATED and has_expected:
            raise ValueError("allocated attempt cannot contain expected output")
        if (
            self.state
            in {
                PossessionAttemptState.WRITING,
                PossessionAttemptState.VERIFIED,
                PossessionAttemptState.PUBLISHED,
            }
            and not has_expected
        ):
            raise ValueError(f"{self.state.value} attempt requires expected output")

    @property
    def game_id(self) -> GameId:
        """Return the game identity used by persistence fences."""
        return self.game.game_id


@dataclass(frozen=True, slots=True)
class PossessionBuildClaim:
    """A fenced build claim with its exact immutable input snapshot."""

    run_id: UUID
    claim_version: int
    attempt: PossessionSnapshotAttempt
    snapshot: GamePbpSnapshot

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        _require_positive_bigint(self.claim_version, field_name="claim version")
        if not isinstance(self.attempt, PossessionSnapshotAttempt):
            raise ValueError("attempt must be a PossessionSnapshotAttempt")
        if not isinstance(self.snapshot, GamePbpSnapshot):
            raise ValueError("snapshot must be a GamePbpSnapshot")
        expected_token = PossessionInputToken.from_snapshot(
            self.snapshot, self.attempt.input_token.versions
        )
        if self.attempt.game != self.snapshot.game:
            raise ValueError("claim attempt and snapshot must belong to the same game")
        if self.attempt.input_token != expected_token:
            raise ValueError("claim attempt token must match the input snapshot")


@dataclass(frozen=True, slots=True)
class CurrentSnapshotReference:
    """A PostgreSQL-resolved exact published snapshot reference."""

    attempt: PossessionSnapshotAttempt
    row_count: int
    output_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, PossessionSnapshotAttempt):
            raise ValueError("attempt must be a PossessionSnapshotAttempt")
        if self.attempt.state is not PossessionAttemptState.PUBLISHED:
            raise ValueError("current reference requires a published attempt")
        _require_positive_bigint(self.row_count, field_name="row count")
        _require_sha256(self.output_sha256, field_name="output SHA-256")
        if (
            self.row_count != self.attempt.expected_row_count
            or self.output_sha256 != self.attempt.expected_output_sha256
        ):
            raise ValueError("current reference output must match the published attempt")


class ClaimAction(StrEnum):
    """Action selected by the fenced PostgreSQL work store."""

    BUILD = "build"
    RESUME = "resume"
    SKIP_CURRENT = "skip_current"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    """A claim decision whose payload is valid for exactly one action."""

    action: ClaimAction
    claim: PossessionBuildClaim | None = None
    current: CurrentSnapshotReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ClaimAction):
            raise ValueError("action must be a ClaimAction")
        if self.claim is not None and not isinstance(self.claim, PossessionBuildClaim):
            raise ValueError("claim must be a PossessionBuildClaim")
        if self.current is not None and not isinstance(self.current, CurrentSnapshotReference):
            raise ValueError("current must be a CurrentSnapshotReference")
        if self.action in {ClaimAction.BUILD, ClaimAction.RESUME}:
            if self.claim is None:
                raise ValueError(f"{self.action.value} decisions require a claim")
            if self.current is not None:
                raise ValueError(f"{self.action.value} decisions cannot include current")
            assert self.claim is not None
            if self.action is ClaimAction.BUILD and (
                self.claim.attempt.state is not PossessionAttemptState.ALLOCATED
            ):
                raise ValueError("build decisions require an allocated attempt")
            if self.action is ClaimAction.RESUME and self.claim.attempt.state not in {
                PossessionAttemptState.ALLOCATED,
                PossessionAttemptState.WRITING,
                PossessionAttemptState.VERIFIED,
            }:
                raise ValueError(
                    "resume decisions require an allocated, writing or verified attempt"
                )
        elif self.action is ClaimAction.SKIP_CURRENT:
            if self.current is None:
                raise ValueError("skip_current decisions require a current reference")
            if self.claim is not None:
                raise ValueError("skip_current decisions cannot include a claim")
        elif self.claim is not None or self.current is not None:
            raise ValueError("busy decisions cannot include a claim or current reference")


class VerificationStatus(StrEnum):
    """Bounded outcome of exact-ID ClickHouse snapshot verification."""

    EXACT = "exact"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """Safe verification metadata without raw analytical rows or transport details."""

    snapshot_id: UUID
    status: VerificationStatus
    row_count: int | None = None
    output_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        if not isinstance(self.status, VerificationStatus):
            raise ValueError("status must be a VerificationStatus")
        if self.row_count is not None:
            _require_nonnegative_integer(self.row_count, field_name="row count")
        if self.output_sha256 is not None:
            _require_sha256(self.output_sha256, field_name="output SHA-256")
        if self.status is VerificationStatus.EXACT:
            if self.row_count is None or self.row_count <= 0 or self.output_sha256 is None:
                raise ValueError("exact verification requires positive count and output hash")
        elif self.status is VerificationStatus.MISSING:
            if self.row_count not in {None, 0} or self.output_sha256 is not None:
                raise ValueError("missing verification cannot contain snapshot output")
        elif self.status is VerificationStatus.UNAVAILABLE:
            if self.row_count is not None or self.output_sha256 is not None:
                raise ValueError("unavailable verification cannot contain snapshot output")

    @property
    def is_exact(self) -> bool:
        """Whether the exact expected snapshot content was observed."""
        return self.status is VerificationStatus.EXACT


__all__ = [
    "BuildMode",
    "BuildPossessionsRequest",
    "BuildPossessionsResult",
    "BuildStatus",
    "ClaimAction",
    "ClaimDecision",
    "CurrentSnapshotReference",
    "GamePbpSnapshot",
    "PossessionAttemptState",
    "PossessionBuildClaim",
    "PossessionInputToken",
    "PossessionSnapshotAttempt",
    "SnapshotVerification",
    "VerificationStatus",
]
