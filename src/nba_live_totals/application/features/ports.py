"""Dependency-injected boundaries for Phase 5 feature materialization."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from nba_live_totals.application.features.models import (
    BuildDatasetRequest,
    BuildDatasetResult,
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeatureBuildClaim,
    FeatureClaimDecision,
    FeatureSnapshotAttempt,
    FeatureSnapshotVerification,
    FeatureTargetRows,
    FeatureTargetSnapshot,
)
from nba_live_totals.application.possessions.models import GamePbpSnapshot
from nba_live_totals.domain.features import (
    FeatureCalculationInput,
    FeatureDataset,
    FeatureResultStatus,
    FeatureVersions,
    NormalizedFeatureEvents,
)
from nba_live_totals.domain.identifiers import GameId


@runtime_checkable
class FeatureEventNormalizer(Protocol):
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedFeatureEvents: ...


@runtime_checkable
class FeatureSourceReader(Protocol):
    async def read_target(self, target: FeatureTargetSnapshot) -> FeatureTargetRows: ...

    async def read_inputs(self, claim: FeatureBuildClaim) -> FeatureCalculationInput: ...


@runtime_checkable
class FeatureCalculator(Protocol):
    def calculate(
        self, inputs: FeatureCalculationInput, events: NormalizedFeatureEvents
    ) -> FeatureDataset: ...


@runtime_checkable
class FeatureWorkStore(Protocol):
    async def start_run(self, request: BuildDatasetRequest) -> UUID: ...

    async def select_games(self, request: BuildDatasetRequest) -> tuple[GameId, ...]: ...

    async def resolve_target(self, game_id: GameId) -> FeatureTargetSnapshot: ...

    async def claim_game(
        self,
        run_id: UUID,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
        *,
        rebuild: bool,
    ) -> FeatureClaimDecision: ...

    async def mark_writing(
        self,
        claim: FeatureBuildClaim,
        *,
        expected_state_row_count: int,
        expected_state_output_sha256: str,
        expected_player_row_count: int,
        expected_player_output_sha256: str,
        expected_feature_output_sha256: str,
        expected_issue_codes: tuple[str, ...],
        result_status: FeatureResultStatus,
    ) -> FeatureSnapshotAttempt: ...

    async def mark_verified(
        self, claim: FeatureBuildClaim, verification: FeatureSnapshotVerification
    ) -> FeatureSnapshotAttempt: ...

    async def publish_snapshot(
        self, claim: FeatureBuildClaim
    ) -> CurrentFeatureSnapshotReference: ...

    async def poison_attempt(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None: ...

    async def fail_game(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None: ...

    async def record_unclaimed_failure(
        self, run_id: UUID, game_id: GameId, *, error_code: str, error_type: str
    ) -> None: ...

    async def resolve_current(
        self,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
    ) -> CurrentFeatureSnapshotResolution: ...

    async def finish_run(self, run_id: UUID, result: BuildDatasetResult) -> None: ...


@runtime_checkable
class FeatureSnapshotStore(Protocol):
    async def write_snapshot(self, claim: FeatureBuildClaim, dataset: FeatureDataset) -> None: ...

    async def verify_snapshot(
        self, attempt: FeatureSnapshotAttempt
    ) -> FeatureSnapshotVerification: ...

    async def read_snapshot(self, reference: CurrentFeatureSnapshotReference) -> FeatureDataset: ...


__all__ = [
    "FeatureCalculator",
    "FeatureEventNormalizer",
    "FeatureSnapshotStore",
    "FeatureSourceReader",
    "FeatureWorkStore",
]
