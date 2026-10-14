"""Published modeling data read exclusively through public Phase 5 boundaries."""

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    FeatureBuildMode,
    FeaturePublicationState,
    FeatureSnapshotStore,
    FeatureSourceReader,
    FeatureTargetRows,
    FeatureTargetSnapshot,
    FeatureWorkStore,
)
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.modeling.ports import HorizonNormalizer
from nba_live_totals.domain.analytical_lineage import PbpLineage
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import FeatureDataset, FeatureVersions
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_targets import HorizonEnd, TrainingGameInput
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, IssueSeverity, PeriodEnd


class PublishedModelFeatureSource:
    def __init__(
        self,
        work: FeatureWorkStore,
        reader: FeatureSourceReader,
        snapshots: FeatureSnapshotStore,
        *,
        versions: FeatureVersions,
        normalizer: HorizonNormalizer,
    ) -> None:
        if not isinstance(versions, FeatureVersions):
            raise ValueError("invalid model feature versions")
        self._work = work
        self._reader = reader
        self._snapshots = snapshots
        self._versions = versions
        self._normalizer = normalizer

    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]:
        return await self._work.select_games(
            BuildDatasetRequest(seasons, game_id=game_id, mode=FeatureBuildMode.VERIFY)
        )

    async def _resolve(self, game_id: GameId) -> tuple[ModelSourceReference, FeatureTargetRows]:
        try:
            target = await self._work.resolve_target(game_id)
            if not isinstance(target, FeatureTargetSnapshot) or target.game.game_id != game_id:
                raise ValueError("model target identity mismatch")
            possession = target.possession_reference
            lineup = target.lineup_reference
            if (
                self._normalizer.version != "nba-pbp-v2"
                or self._normalizer.version
                != possession.attempt.input_token.versions.normalizer_version
            ):
                raise ModelingError("model_normalizer_incompatible")
            rows = await self._reader.read_target(target)
            if not isinstance(rows, FeatureTargetRows) or rows.target != target:
                raise ValueError("model target rows mismatch")
            resolution = await self._work.resolve_current(
                target, rows.target_player_ids, self._versions
            )
            if resolution.status not in (
                FeaturePublicationState.CURRENT,
                FeaturePublicationState.INVALID,
            ):
                raise ModelingError(f"model_source_{resolution.status.value}")
            ref = resolution.reference
            if ref is None:
                raise ModelingError("model_source_absent")
            token = ref.attempt.input_token
            if (
                token.versions != self._versions
                or token.possession_snapshot_id != possession.attempt.snapshot_id
                or token.parse_revision_id != possession.attempt.parse_revision_id
                or token.possession_input_token_sha256 != possession.attempt.input_token.sha256
                or token.possession_output_sha256 != possession.output_sha256
                or token.lineup_snapshot_id != lineup.attempt.snapshot_id
                or token.lineup_revision_id != lineup.attempt.lineup_revision_id
                or token.lineup_input_token_sha256 != lineup.attempt.input_token.sha256
                or token.lineup_output_sha256 != lineup.output_sha256
                or token.lineup_result_status != lineup.result_status
            ):
                raise ValueError("feature source token mismatch")
            pbp = target.pbp_snapshot
            source = ModelSourceReference(
                target.game,
                ref,
                PbpLineage(
                    game_id,
                    pbp.applied_pbp_document_id,
                    pbp.applied_pbp_sha256,
                    pbp.canonical_event_sha256,
                    pbp.game_context_sha256,
                ),
                possession.attempt.input_token.versions,
                lineup.attempt.input_token.versions,
            )
            return source, rows
        except ModelingError:
            raise
        except RuntimeError, ValueError, TypeError:
            raise ModelingError("model_source_unavailable") from None

    async def resolve(self, game_id: GameId) -> ModelSourceReference:
        source, _ = await self._resolve(game_id)
        return source

    async def _features(self, source: ModelSourceReference) -> FeatureDataset:
        try:
            dataset = await self._snapshots.read_snapshot(source.reference)
            ref = source.reference
            if (
                not isinstance(dataset, FeatureDataset)
                or dataset.game_id != source.game.game_id
                or (
                    len(dataset.states),
                    state_output_checksum(dataset.states),
                    len(dataset.players),
                    player_output_checksum(dataset.players),
                    feature_output_checksum(dataset),
                )
                != (
                    ref.state_row_count,
                    ref.state_output_sha256,
                    ref.player_row_count,
                    ref.player_output_sha256,
                    ref.feature_output_sha256,
                )
            ):
                raise ValueError("model feature output mismatch")
            return dataset
        except RuntimeError, ValueError, TypeError:
            raise ModelingError("model_source_mismatch") from None

    async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
        await self.recheck(source)
        return await self._features(source)

    async def read_training(self, source: ModelSourceReference) -> TrainingGameInput:
        current, rows = await self._resolve(source.game.game_id)
        if current != source:
            raise ModelingError("model_source_changed")
        features = await self._features(source)
        try:
            normalized = self._normalizer.normalize(rows.target.pbp_snapshot)
            if normalized.game_id != source.game.game_id or any(
                issue.severity is IssueSeverity.ERROR for issue in normalized.issues
            ):
                raise ValueError("invalid horizon normalization")
            ends: list[HorizonEnd] = []
            maximum_period = max((action.period for action in normalized.actions), default=0)
            cursor = 0
            home_score = away_score = home_count = away_count = 0
            for action in normalized.actions:
                if action.period < 4 or not any(
                    isinstance(signal, PeriodEnd) for signal in action.signals
                ):
                    continue
                boundary = EventBoundary(action.sequence_number, BoundarySide.AFTER)
                if action.clock_ms != 0:
                    raise ValueError("period end must have zero clock")
                while (
                    cursor < len(rows.possessions)
                    and rows.possessions[cursor].end_boundary <= boundary
                ):
                    possession = rows.possessions[cursor]
                    home_score, away_score = possession.home_score, possession.away_score
                    home_count, away_count = (
                        possession.home_possessions,
                        possession.away_possessions,
                    )
                    cursor += 1
                if (
                    action.checkpoint_home_score != home_score
                    or action.checkpoint_away_score != away_score
                ):
                    raise ValueError("period end score mismatch")
                ends.append(
                    HorizonEnd(
                        source.game.game_id,
                        action.period,
                        boundary,
                        home_score,
                        away_score,
                        home_count,
                        away_count,
                    )
                )
            if not ends or ends[-1].period != maximum_period:
                raise ValueError("missing final known period endpoint")
            return TrainingGameInput(source.game, features, rows.possessions, tuple(ends))
        except RuntimeError, ValueError, TypeError:
            raise ModelingError("model_source_mismatch") from None

    async def recheck(self, source: ModelSourceReference) -> None:
        if await self.resolve(source.game.game_id) != source:
            raise ModelingError("model_source_changed")
