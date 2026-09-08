"""Pure-component and persistence ports for possession reconstruction."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from nba_live_totals.application.possessions.models import (
    BuildPossessionsRequest,
    BuildPossessionsResult,
    ClaimDecision,
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionBuildClaim,
    PossessionSnapshotAttempt,
    SnapshotVerification,
)
from nba_live_totals.domain import (
    GameId,
    NormalizedGameActions,
    Possession,
    PossessionIssue,
    PossessionParseContext,
    PossessionParseResult,
    PossessionVersions,
)


@runtime_checkable
class PossessionNormalizer(Protocol):
    """Synchronous provider adapter producing only normalized domain signals."""

    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions: ...


@runtime_checkable
class PossessionParser(Protocol):
    """Synchronous deterministic parser without transport or persistence identity."""

    def parse(
        self,
        context: PossessionParseContext,
        normalized: NormalizedGameActions,
    ) -> PossessionParseResult: ...


@runtime_checkable
class PossessionWorkStore(Protocol):
    """Atomic PostgreSQL operations for run, claim and publication lifecycle."""

    async def start_run(self, request: BuildPossessionsRequest) -> UUID: ...

    async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]: ...

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: PossessionVersions,
        *,
        rebuild: bool,
    ) -> ClaimDecision: ...

    async def mark_writing(
        self,
        claim: PossessionBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
    ) -> PossessionSnapshotAttempt: ...

    async def mark_verified(
        self,
        claim: PossessionBuildClaim,
        verification: SnapshotVerification,
    ) -> PossessionSnapshotAttempt: ...

    async def poison_attempt(
        self,
        claim: PossessionBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None: ...

    async def publish_snapshot(self, claim: PossessionBuildClaim) -> CurrentSnapshotReference: ...

    async def fail_game(
        self,
        claim: PossessionBuildClaim,
        *,
        issues: tuple[PossessionIssue, ...],
        error_code: str,
        error_type: str,
    ) -> None: ...

    async def resolve_current(
        self, game_id: GameId, versions: PossessionVersions
    ) -> CurrentSnapshotReference | None: ...

    async def finish_run(
        self,
        run_id: UUID,
        result: BuildPossessionsResult,
    ) -> None: ...


@runtime_checkable
class PossessionSnapshotStore(Protocol):
    """Exact-attempt immutable ClickHouse operations."""

    async def write_snapshot(
        self,
        claim: PossessionBuildClaim,
        result: PossessionParseResult,
    ) -> None: ...

    async def verify_snapshot(
        self,
        attempt: PossessionSnapshotAttempt,
    ) -> SnapshotVerification: ...

    async def read_snapshot(
        self,
        reference: CurrentSnapshotReference,
    ) -> tuple[Possession, ...]: ...


__all__ = [
    "PossessionNormalizer",
    "PossessionParser",
    "PossessionSnapshotStore",
    "PossessionWorkStore",
]
