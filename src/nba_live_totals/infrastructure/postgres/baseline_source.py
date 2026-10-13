"""Read-only baseline access through authoritative Phase 5 published references."""

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.application.features import (
    BuildDatasetRequest,
    FeatureBuildMode,
    FeaturePublicationState,
    FeatureSnapshotStore,
    FeatureSourceReader,
    FeatureWorkStore,
)
from nba_live_totals.domain.features import FeatureDataset, FeatureVersions
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.infrastructure.clickhouse.features import (
    FeatureSnapshotStoreError,
    FeatureSourceReaderError,
)


class PublishedFeatureBaselineSource:
    def __init__(
        self,
        work_store: FeatureWorkStore,
        source_reader: FeatureSourceReader,
        snapshot_store: FeatureSnapshotStore,
        *,
        versions: FeatureVersions,
    ) -> None:
        self._work = work_store
        self._reader = source_reader
        self._snapshots = snapshot_store
        self._versions = versions

    async def select_games(self, request: BuildBaselinesRequest) -> tuple[GameId, ...]:
        return await self._work.select_games(
            BuildDatasetRequest(
                request.seasons, game_id=request.game_id, mode=FeatureBuildMode.VERIFY
            )
        )

    async def resolve(self, game_id: GameId) -> BaselineSourceReference:
        try:
            target = await self._work.resolve_target(game_id)
            rows = await self._reader.read_target(target)
            resolution = await self._work.resolve_current(
                target, rows.target_player_ids, self._versions
            )
            if resolution.status not in {
                FeaturePublicationState.CURRENT,
                FeaturePublicationState.INVALID,
            }:
                raise BaselineExportError(f"baseline_source_{resolution.status.value}")
            if resolution.reference is None:
                raise BaselineExportError("baseline_source_absent")
            return BaselineSourceReference(target.game, resolution.reference)
        except (FeatureSourceReaderError, RuntimeError, ValueError) as error:
            if isinstance(error, BaselineExportError):
                raise
            raise BaselineExportError("baseline_source_unavailable") from None

    async def read(self, source: BaselineSourceReference) -> FeatureDataset:
        try:
            dataset = await self._snapshots.read_snapshot(source.reference)
            if dataset.game_id != source.game.game_id:
                raise ValueError("baseline game mismatch")
            return dataset
        except FeatureSnapshotStoreError, ValueError:
            raise BaselineExportError("baseline_source_mismatch") from None

    async def recheck(self, source: BaselineSourceReference) -> None:
        if await self.resolve(source.game.game_id) != source:
            raise BaselineExportError("baseline_source_changed")
