"""Pure-component and persistence ports for lineup reconstruction."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from nba_live_totals.application.lineups.models import (
    BuildLineupsRequest,
    BuildLineupsResult,
    CurrentLineupSnapshotReference,
    CurrentLineupSnapshotResolution,
    GameLineupInputSnapshot,
    LineupBuildClaim,
    LineupClaimDecision,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
)
from nba_live_totals.domain import GameId
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    NormalizedGameLineups,
)


@runtime_checkable
class LineupNormalizer(Protocol):
    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups: ...


@runtime_checkable
class LineupReconstructor(Protocol):
    def reconstruct(
        self,
        context: LineupReconstructionContext,
        normalized: NormalizedGameLineups,
    ) -> LineupReconstructionResult: ...


@runtime_checkable
class LineupWorkStore(Protocol):
    async def start_run(self, request: BuildLineupsRequest) -> UUID: ...

    async def select_games(self, request: BuildLineupsRequest) -> tuple[GameId, ...]: ...

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: LineupVersions,
        *,
        rebuild: bool,
    ) -> LineupClaimDecision: ...

    async def mark_writing(
        self,
        claim: LineupBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
        expected_issue_codes: tuple[LineupIssue, ...],
        result_status: LineupStatus,
    ) -> LineupSnapshotAttempt: ...

    async def mark_verified(
        self,
        claim: LineupBuildClaim,
        verification: LineupSnapshotVerification,
    ) -> LineupSnapshotAttempt: ...

    async def poison_attempt(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None: ...

    async def publish_snapshot(self, claim: LineupBuildClaim) -> CurrentLineupSnapshotReference: ...

    async def fail_game(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None: ...

    async def resolve_current(
        self, game_id: GameId, versions: LineupVersions
    ) -> CurrentLineupSnapshotResolution: ...

    async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None: ...


@runtime_checkable
class LineupSnapshotStore(Protocol):
    async def write_snapshot(
        self,
        claim: LineupBuildClaim,
        result: LineupReconstructionResult,
    ) -> None: ...

    async def verify_snapshot(
        self, attempt: LineupSnapshotAttempt
    ) -> LineupSnapshotVerification: ...

    async def read_snapshot(
        self, reference: CurrentLineupSnapshotReference
    ) -> tuple[LineupSegment, ...]: ...


__all__ = [
    "LineupNormalizer",
    "LineupReconstructor",
    "LineupSnapshotStore",
    "LineupWorkStore",
]
