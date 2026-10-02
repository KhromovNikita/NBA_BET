"""Exact ClickHouse reads for manifest-bound Phase 5 feature inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from nba_live_totals.application.features import (
    FeatureBuildClaim,
    FeatureInputToken,
    FeatureTargetRows,
    FeatureTargetSnapshot,
)
from nba_live_totals.domain.analytical_lineage import (
    PbpLineage,
    require_matching_pbp_lineage,
)
from nba_live_totals.domain.features import FeatureCalculationInput
from nba_live_totals.domain.possessions import Possession
from nba_live_totals.infrastructure.clickhouse import possessions as possession_adapter
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError
from nba_live_totals.infrastructure.clickhouse.lineups import (
    ClickHouseLineupSnapshotStore,
    LineupSnapshotStoreError,
)
from nba_live_totals.infrastructure.clickhouse.possessions import (
    PossessionSnapshotStore,
    PossessionSnapshotStoreError,
)

_HISTORY_CHUNK_SIZE = 32


class SourceClient(Protocol):
    async def insert_json_each_row(
        self, table: str, rows: Sequence[Mapping[str, object]]
    ) -> None: ...

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]: ...


class FeatureSourceReaderError(RuntimeError):
    """A bounded error for an exact Phase 5 source read."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClickHouseFeatureSourceReader:
    """Read target and historical rows only through PostgreSQL-owned IDs."""

    def __init__(self, client: SourceClient) -> None:
        self._client = client
        self._possessions = PossessionSnapshotStore(client)
        self._lineups = ClickHouseLineupSnapshotStore(client)

    async def read_target(self, target: FeatureTargetSnapshot) -> FeatureTargetRows:
        if not isinstance(target, FeatureTargetSnapshot):
            raise ValueError("target must be a FeatureTargetSnapshot")
        self._require_target_lineage(target)
        try:
            possessions = await self._possessions.read_snapshot(target.possession_reference)
            segments = await self._lineups.read_snapshot(target.lineup_reference)
            return FeatureTargetRows(target, possessions, segments)
        except (PossessionSnapshotStoreError, LineupSnapshotStoreError):
            raise FeatureSourceReaderError("feature_target_source_mismatch") from None
        except (KeyError, TypeError, ValueError):
            raise FeatureSourceReaderError("feature_target_source_mismatch") from None

    async def read_inputs(self, claim: FeatureBuildClaim) -> FeatureCalculationInput:
        if not isinstance(claim, FeatureBuildClaim):
            raise ValueError("claim must be a FeatureBuildClaim")
        self._require_claim_identity(claim)
        target_rows = await self.read_target(claim.source.target)
        history = await self._read_history(claim)
        try:
            return FeatureCalculationInput(
                target_game=claim.source.target.game,
                possessions=target_rows.possessions,
                lineup_segments=target_rows.lineup_segments,
                historical_team_boxes=claim.source.historical_team_boxes,
                historical_player_boxes=claim.source.historical_player_boxes,
                history_possessions=history,
            )
        except ValueError:
            raise FeatureSourceReaderError("feature_history_source_mismatch") from None

    async def _read_history(self, claim: FeatureBuildClaim) -> tuple[Possession, ...]:
        sources = claim.source.historical_possession_sources
        if not sources:
            return ()
        rows_by_snapshot: dict[str, list[dict[str, object]]] = {
            str(source.possession_reference.attempt.snapshot_id): [] for source in sources
        }
        source_ids = tuple(rows_by_snapshot)
        try:
            for offset in range(0, len(source_ids), _HISTORY_CHUNK_SIZE):
                chunk = source_ids[offset : offset + _HISTORY_CHUNK_SIZE]
                placeholders = ", ".join(
                    f"{{snapshot_id_{index}:UUID}}" for index in range(len(chunk))
                )
                statement = (
                    f"SELECT {', '.join(possession_adapter._COLUMN_NAMES)} "
                    f"FROM {possession_adapter._TABLE} FINAL "
                    f"WHERE snapshot_id IN ({placeholders}) "
                    "ORDER BY snapshot_id, possession_number"
                )
                parameters = {
                    f"snapshot_id_{index}": snapshot_id for index, snapshot_id in enumerate(chunk)
                }
                for row in await self._client.query(statement, parameters=parameters):
                    snapshot_id = row.get("snapshot_id")
                    if type(snapshot_id) is not str or snapshot_id not in rows_by_snapshot:
                        raise ValueError("history query returned an unrequested snapshot")
                    rows_by_snapshot[snapshot_id].append(row)
        except ClickHouseError:
            raise FeatureSourceReaderError("feature_history_source_unavailable") from None
        except (KeyError, TypeError, ValueError):
            raise FeatureSourceReaderError("feature_history_source_mismatch") from None

        possessions: list[Possession] = []
        try:
            for source in sources:
                reference = source.possession_reference
                snapshot_id = str(reference.attempt.snapshot_id)
                readback = self._possessions._reconstruct(
                    reference.attempt, rows_by_snapshot[snapshot_id]
                )
                if (
                    len(readback.possessions) != reference.row_count
                    or readback.output_sha256 != reference.output_sha256
                ):
                    raise ValueError("historical possession output does not match")
                possessions.extend(readback.possessions)
        except (KeyError, TypeError, ValueError):
            raise FeatureSourceReaderError("feature_history_source_mismatch") from None
        return tuple(possessions)

    @staticmethod
    def _require_target_lineage(target: FeatureTargetSnapshot) -> None:
        snapshot = target.pbp_snapshot
        lineage = PbpLineage(
            game_id=snapshot.game.game_id,
            input_pbp_document_id=snapshot.applied_pbp_document_id,
            input_pbp_sha256=snapshot.applied_pbp_sha256,
            canonical_event_sha256=snapshot.canonical_event_sha256,
            game_context_sha256=snapshot.game_context_sha256,
        )
        try:
            require_matching_pbp_lineage(
                lineage, target.possession_reference.attempt.input_token.pbp_lineage
            )
            require_matching_pbp_lineage(
                lineage, target.lineup_reference.attempt.input_token.pbp_lineage
            )
        except (TypeError, ValueError):
            raise FeatureSourceReaderError("feature_target_lineage_mismatch") from None

    @classmethod
    def _require_claim_identity(cls, claim: FeatureBuildClaim) -> None:
        cls._require_target_lineage(claim.source.target)
        try:
            token = FeatureInputToken.from_source(claim.source, claim.attempt.input_token.versions)
            if token != claim.attempt.input_token:
                raise ValueError("claim token does not match source")
            dependencies = claim.source.manifest.dependencies
            if (
                tuple(source.dependency for source in claim.source.historical_possession_sources)
                != dependencies
            ):
                raise ValueError("historical sources do not match manifest")
        except (TypeError, ValueError):
            raise FeatureSourceReaderError("feature_claim_source_mismatch") from None


__all__ = ["ClickHouseFeatureSourceReader", "FeatureSourceReaderError"]
