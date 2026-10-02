"""Exact ClickHouse reads for manifest-bound Phase 5 feature inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureInputToken,
    FeatureSnapshotAttempt,
    FeatureSnapshotVerification,
    FeatureTargetRows,
    FeatureTargetSnapshot,
    FeatureVerificationStatus,
)
from nba_live_totals.domain.analytical_lineage import (
    PbpLineage,
    require_matching_pbp_lineage,
)
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import (
    FeatureCalculationInput,
    FeatureDataset,
    FeatureStateKind,
    GameStateFeature,
    LineupAggregateFeatures,
    LiveFeatures,
    PlayerPregameFeatures,
    TeamPregameFeatures,
)
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession
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
_STATE_TABLE = "nba.game_state_features_v1"
_PLAYER_TABLE = "nba.player_pregame_features_v1"

_STATE_COLUMNS = (
    "snapshot_id",
    "insert_version",
    "feature_revision_id",
    "feature_input_token_sha256",
    "dependency_input_sha256",
    "dependency_revision_sha256",
    "feature_join_version",
    "feature_formula_version",
    "feature_event_normalizer_version",
    "output_schema_version",
    "state_output_sha256",
    "feature_output_sha256",
    "possession_snapshot_id",
    "parse_revision_id",
    "possession_output_sha256",
    "lineup_snapshot_id",
    "lineup_revision_id",
    "lineup_output_sha256",
    "game_id",
    "season",
    "season_start_year",
    "season_type",
    "game_date",
    "started_at",
    "home_team_id",
    "away_team_id",
    "state_number",
    "state_kind",
    "possession_number",
    "boundary_sequence_number",
    "boundary_side",
    "period",
    "seconds_remaining_game_ms",
    "seconds_remaining_period_ms",
    "home_score",
    "away_score",
    "score_diff",
    "home_possessions",
    "away_possessions",
    "home_lineup_status",
    "home_player_ids",
    "home_invalid_reason",
    "home_invalid_since_sequence_number",
    "home_invalid_since_boundary_side",
    "away_lineup_status",
    "away_player_ids",
    "away_invalid_reason",
    "away_invalid_since_sequence_number",
    "away_invalid_since_boundary_side",
    "home_games_used_5",
    "home_games_used_10",
    "home_games_used_20",
    "away_games_used_5",
    "away_games_used_10",
    "away_games_used_20",
    "home_off_rating_5",
    "home_off_rating_10",
    "home_off_rating_20",
    "home_def_rating_5",
    "home_def_rating_10",
    "home_def_rating_20",
    "home_pace_5",
    "home_pace_10",
    "home_pace_20",
    "away_off_rating_5",
    "away_off_rating_10",
    "away_off_rating_20",
    "away_def_rating_5",
    "away_def_rating_10",
    "away_def_rating_20",
    "away_pace_5",
    "away_pace_10",
    "away_pace_20",
    "home_rest_days",
    "away_rest_days",
    "home_back_to_back",
    "away_back_to_back",
    "current_pace",
    "pace_last_5",
    "pace_last_10",
    "pace_last_20",
    "pace_last_5_possessions_used",
    "pace_last_10_possessions_used",
    "pace_last_20_possessions_used",
    "home_ppp",
    "away_ppp",
    "home_ppp_last_5",
    "home_ppp_last_10",
    "home_ppp_last_20",
    "away_ppp_last_5",
    "away_ppp_last_10",
    "away_ppp_last_20",
    "home_ppp_last_5_possessions_used",
    "home_ppp_last_10_possessions_used",
    "home_ppp_last_20_possessions_used",
    "away_ppp_last_5_possessions_used",
    "away_ppp_last_10_possessions_used",
    "away_ppp_last_20_possessions_used",
    "home_three_point_attempt_rate",
    "away_three_point_attempt_rate",
    "home_free_throw_rate",
    "away_free_throw_rate",
    "home_turnover_rate",
    "away_turnover_rate",
    "home_offensive_rebound_rate",
    "away_offensive_rebound_rate",
    "home_profiled_player_count",
    "away_profiled_player_count",
    "home_lineup_avg_off_rating",
    "away_lineup_avg_off_rating",
    "home_lineup_avg_def_rating",
    "away_lineup_avg_def_rating",
    "home_lineup_avg_usage_rate",
    "away_lineup_avg_usage_rate",
    "home_lineup_avg_three_point_attempt_rate",
    "away_lineup_avg_three_point_attempt_rate",
    "home_lineup_avg_free_throw_rate",
    "away_lineup_avg_free_throw_rate",
    "home_lineup_avg_minutes",
    "away_lineup_avg_minutes",
    "home_lineup_avg_pace_context",
    "away_lineup_avg_pace_context",
    "offense_difference",
    "defense_difference",
    "pace_difference",
    "is_valid",
)
_PLAYER_COLUMNS = (
    "snapshot_id",
    "insert_version",
    "feature_revision_id",
    "feature_input_token_sha256",
    "dependency_input_sha256",
    "dependency_revision_sha256",
    "feature_formula_version",
    "output_schema_version",
    "player_output_sha256",
    "feature_output_sha256",
    "game_id",
    "season",
    "season_start_year",
    "season_type",
    "game_date",
    "started_at",
    "player_id",
    "team_id",
    "is_home",
    "history_cutoff_started_at",
    "games_used",
    "seconds_played",
    "estimated_possessions",
    "minutes_avg",
    "points_per_100",
    "off_rating",
    "def_rating",
    "usage_rate",
    "assist_rate",
    "turnover_rate",
    "three_point_attempt_rate",
    "three_point_percentage",
    "free_throw_rate",
    "rebound_rate",
    "pace_context",
)
_STATE_READ = (
    f"SELECT {', '.join(_STATE_COLUMNS)} FROM {_STATE_TABLE} FINAL "
    "WHERE season_start_year = {season_start_year:UInt16} "
    "AND game_id = {game_id:FixedString(10)} AND snapshot_id = {snapshot_id:UUID} "
    "ORDER BY state_number"
)
_PLAYER_READ = (
    f"SELECT {', '.join(_PLAYER_COLUMNS)} FROM {_PLAYER_TABLE} FINAL "
    "WHERE season_start_year = {season_start_year:UInt16} "
    "AND game_id = {game_id:FixedString(10)} AND snapshot_id = {snapshot_id:UUID} "
    "ORDER BY player_id"
)


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


class FeatureSnapshotStoreError(RuntimeError):
    """A bounded error for immutable two-table feature snapshots."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClickHouseFeatureSnapshotStore:
    """Persist one feature snapshot UUID across both analytical tables once."""

    def __init__(self, client: SourceClient) -> None:
        self._client = client
        self._sent_snapshot_ids: set[UUID] = set()

    async def write_snapshot(self, claim: FeatureBuildClaim, dataset: FeatureDataset) -> None:
        self._validate_write(claim, dataset)
        snapshot_id = claim.attempt.snapshot_id
        if snapshot_id in self._sent_snapshot_ids:
            await self.verify_snapshot(claim.attempt)
            raise FeatureSnapshotStoreError("feature_snapshot_already_sent")
        self._sent_snapshot_ids.add(snapshot_id)
        state_hash = state_output_checksum(dataset.states)
        player_hash = player_output_checksum(dataset.players)
        feature_hash = feature_output_checksum(dataset)
        state_rows = tuple(
            _serialize_state_row(claim, row, state_hash, feature_hash)
            for row in sorted(dataset.states, key=lambda item: item.state_number)
        )
        player_rows = tuple(
            _serialize_player_row(claim, row, player_hash, feature_hash)
            for row in sorted(dataset.players, key=lambda item: item.player_id)
        )
        try:
            await self._client.insert_json_each_row(_STATE_TABLE, state_rows)
            if player_rows:
                await self._client.insert_json_each_row(_PLAYER_TABLE, player_rows)
        except ClickHouseError:
            verification = await self.verify_snapshot(claim.attempt)
            if verification.status is FeatureVerificationStatus.EXACT:
                return
            raise FeatureSnapshotStoreError("feature_snapshot_partial_write") from None

    async def verify_snapshot(self, attempt: FeatureSnapshotAttempt) -> FeatureSnapshotVerification:
        if not _can_verify(attempt):
            return FeatureSnapshotVerification(
                attempt.snapshot_id, FeatureVerificationStatus.DIFFERENT
            )
        try:
            state_rows, player_rows = await self._query_rows(attempt)
        except ClickHouseError:
            return FeatureSnapshotVerification(
                attempt.snapshot_id, FeatureVerificationStatus.UNAVAILABLE
            )
        if not state_rows and not player_rows:
            return FeatureSnapshotVerification(
                attempt.snapshot_id, FeatureVerificationStatus.ABSENT
            )
        try:
            dataset = _reconstruct(attempt, state_rows, player_rows)
            state_hash = state_output_checksum(dataset.states)
            player_hash = player_output_checksum(dataset.players)
            feature_hash = feature_output_checksum(dataset)
        except (KeyError, TypeError, ValueError):
            return FeatureSnapshotVerification(
                attempt.snapshot_id, FeatureVerificationStatus.DIFFERENT
            )
        if (
            len(dataset.states) != attempt.expected_state_row_count
            or state_hash != attempt.expected_state_output_sha256
            or len(dataset.players) != attempt.expected_player_row_count
            or player_hash != attempt.expected_player_output_sha256
            or feature_hash != attempt.expected_feature_output_sha256
            or dataset.status is not attempt.result_status
            or dataset.issues != attempt.expected_issue_codes
        ):
            return FeatureSnapshotVerification(
                attempt.snapshot_id, FeatureVerificationStatus.DIFFERENT
            )
        return FeatureSnapshotVerification(
            attempt.snapshot_id,
            FeatureVerificationStatus.EXACT,
            state_row_count=len(dataset.states),
            state_output_sha256=state_hash,
            player_row_count=len(dataset.players),
            player_output_sha256=player_hash,
            feature_output_sha256=feature_hash,
            result_status=dataset.status,
            issues=dataset.issues,
        )

    async def read_snapshot(self, reference: CurrentFeatureSnapshotReference) -> FeatureDataset:
        try:
            state_rows, player_rows = await self._query_rows(reference.attempt)
            dataset = _reconstruct(reference.attempt, state_rows, player_rows)
        except ClickHouseError:
            raise FeatureSnapshotStoreError("feature_snapshot_unavailable") from None
        except (KeyError, TypeError, ValueError):
            raise FeatureSnapshotStoreError("feature_snapshot_mismatch") from None
        if (
            len(dataset.states) != reference.state_row_count
            or state_output_checksum(dataset.states) != reference.state_output_sha256
            or len(dataset.players) != reference.player_row_count
            or player_output_checksum(dataset.players) != reference.player_output_sha256
            or feature_output_checksum(dataset) != reference.feature_output_sha256
            or dataset.status is not reference.result_status
            or dataset.issues != reference.issues
        ):
            raise FeatureSnapshotStoreError("feature_snapshot_mismatch")
        return dataset

    async def _query_rows(
        self, attempt: FeatureSnapshotAttempt
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        parameters: dict[str, str | int] = {
            "season_start_year": attempt.game.season.start_year,
            "game_id": attempt.game_id.value,
            "snapshot_id": str(attempt.snapshot_id),
        }
        state_rows = await self._client.query(_STATE_READ, parameters=parameters)
        player_rows = await self._client.query(_PLAYER_READ, parameters=parameters)
        return state_rows, player_rows

    @staticmethod
    def _validate_write(claim: FeatureBuildClaim, dataset: FeatureDataset) -> None:
        attempt = claim.attempt
        if attempt.state is not FeatureAttemptState.WRITING:
            raise FeatureSnapshotStoreError("feature_snapshot_write_not_writing")
        try:
            expected_token = FeatureInputToken.from_source(
                claim.source, attempt.input_token.versions
            )
        except (TypeError, ValueError):
            raise FeatureSnapshotStoreError("feature_snapshot_lineage_mismatch") from None
        if (
            expected_token != attempt.input_token
            or attempt.feature_revision_id != attempt.input_token.feature_revision_id
            or dataset.game_id != attempt.game_id
            or tuple(row.player_id for row in dataset.players) != claim.source.target_player_ids
        ):
            raise FeatureSnapshotStoreError("feature_snapshot_lineage_mismatch")
        if (
            len(dataset.states) != attempt.expected_state_row_count
            or state_output_checksum(dataset.states) != attempt.expected_state_output_sha256
            or len(dataset.players) != attempt.expected_player_row_count
            or player_output_checksum(dataset.players) != attempt.expected_player_output_sha256
            or feature_output_checksum(dataset) != attempt.expected_feature_output_sha256
            or dataset.status is not attempt.result_status
            or dataset.issues != attempt.expected_issue_codes
        ):
            raise FeatureSnapshotStoreError("feature_snapshot_expected_output_mismatch")


def _datetime_text(value: datetime) -> str:
    iso = value.isoformat(timespec="milliseconds")
    return iso.replace("T", " ").replace("+00:00", "")


def _shared_row(claim: FeatureBuildClaim) -> dict[str, object]:
    attempt = claim.attempt
    token = attempt.input_token
    game = attempt.game
    return {
        "snapshot_id": str(attempt.snapshot_id),
        "insert_version": attempt.generation,
        "feature_revision_id": attempt.feature_revision_id,
        "feature_input_token_sha256": token.sha256,
        "dependency_input_sha256": token.dependency_input_sha256,
        "dependency_revision_sha256": token.dependency_revision_sha256,
        "feature_formula_version": token.versions.formula_version,
        "feature_output_sha256": "",
        "game_id": game.game_id.value,
        "season": game.season.value,
        "season_start_year": game.season.start_year,
        "season_type": game.season_type.value,
        "game_date": game.game_date.isoformat(),
        "started_at": _datetime_text(game.started_at),
    }


def _serialize_lineup_state(prefix: str, row: GameStateFeature) -> dict[str, object]:
    status = getattr(row, f"{prefix}_lineup_status")
    player_ids = getattr(row, f"{prefix}_player_ids")
    reason = getattr(row, f"{prefix}_invalid_reason")
    since = getattr(row, f"{prefix}_invalid_since")
    if status is LineupStatus.VALID:
        return {
            f"{prefix}_lineup_status": status.value,
            f"{prefix}_player_ids": list(player_ids),
            f"{prefix}_invalid_reason": "",
            f"{prefix}_invalid_since_sequence_number": 0,
            f"{prefix}_invalid_since_boundary_side": "none",
        }
    assert reason is not None and since is not None
    return {
        f"{prefix}_lineup_status": status.value,
        f"{prefix}_player_ids": [],
        f"{prefix}_invalid_reason": reason,
        f"{prefix}_invalid_since_sequence_number": since.sequence_number,
        f"{prefix}_invalid_since_boundary_side": since.side.value,
    }


def _serialize_team_pregame(prefix: str, value: TeamPregameFeatures) -> dict[str, object]:
    return {
        f"{prefix}_{item.name}": int(getattr(value, item.name))
        if item.name == "back_to_back"
        else getattr(value, item.name)
        for item in fields(TeamPregameFeatures)
        if item.name not in {"team_id", "is_home"}
    }


def _serialize_lineup(prefix: str, value: LineupAggregateFeatures) -> dict[str, object]:
    return {
        f"{prefix}_{item.name}": getattr(value, item.name)
        for item in fields(LineupAggregateFeatures)
        if item.name not in {"team_id", "player_ids"}
    }


def _serialize_live(value: LiveFeatures) -> dict[str, object]:
    return {item.name: getattr(value, item.name) for item in fields(LiveFeatures)}


def _serialize_state_row(
    claim: FeatureBuildClaim,
    row: GameStateFeature,
    state_hash: str,
    feature_hash: str,
) -> dict[str, object]:
    attempt = claim.attempt
    token = attempt.input_token
    game = attempt.game
    if any(
        value is None
        for value in (
            row.home_pregame,
            row.away_pregame,
            row.home_lineup,
            row.away_lineup,
            row.live,
        )
    ):
        raise FeatureSnapshotStoreError("feature_snapshot_invariants")
    assert row.home_pregame is not None and row.away_pregame is not None
    assert row.home_lineup is not None and row.away_lineup is not None and row.live is not None
    result = _shared_row(claim)
    result.update(
        {
            "feature_join_version": token.versions.join_version,
            "feature_event_normalizer_version": token.versions.event_normalizer_version,
            "output_schema_version": token.versions.output_schema_version,
            "state_output_sha256": state_hash,
            "feature_output_sha256": feature_hash,
            "possession_snapshot_id": str(token.possession_snapshot_id),
            "parse_revision_id": token.parse_revision_id,
            "possession_output_sha256": token.possession_output_sha256,
            "lineup_snapshot_id": str(token.lineup_snapshot_id),
            "lineup_revision_id": token.lineup_revision_id,
            "lineup_output_sha256": token.lineup_output_sha256,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "state_number": row.state_number,
            "state_kind": row.state_kind.value,
            "possession_number": row.possession_number,
            "boundary_sequence_number": row.boundary.sequence_number,
            "boundary_side": row.boundary.side.value,
            "period": row.period,
            "seconds_remaining_game_ms": row.seconds_remaining_game_ms,
            "seconds_remaining_period_ms": row.seconds_remaining_period_ms,
            "home_score": row.home_score,
            "away_score": row.away_score,
            "score_diff": row.score_diff,
            "home_possessions": row.home_possessions,
            "away_possessions": row.away_possessions,
            "offense_difference": row.offense_difference,
            "defense_difference": row.defense_difference,
            "pace_difference": row.pace_difference,
            "is_valid": int(row.is_valid),
        }
    )
    result.update(_serialize_lineup_state("home", row))
    result.update(_serialize_lineup_state("away", row))
    result.update(_serialize_team_pregame("home", row.home_pregame))
    result.update(_serialize_team_pregame("away", row.away_pregame))
    result.update(_serialize_lineup("home", row.home_lineup))
    result.update(_serialize_lineup("away", row.away_lineup))
    result.update(_serialize_live(row.live))
    if set(result) != set(_STATE_COLUMNS):
        raise FeatureSnapshotStoreError("feature_snapshot_invariants")
    return result


def _serialize_player_row(
    claim: FeatureBuildClaim,
    row: PlayerPregameFeatures,
    player_hash: str,
    feature_hash: str,
) -> dict[str, object]:
    attempt = claim.attempt
    token = attempt.input_token
    result = _shared_row(claim)
    result.update(
        {
            "output_schema_version": token.versions.player_output_schema_version,
            "player_output_sha256": player_hash,
            "feature_output_sha256": feature_hash,
            "player_id": row.player_id,
            "team_id": row.team_id,
            "is_home": int(row.is_home),
            "history_cutoff_started_at": _datetime_text(attempt.game.started_at),
        }
    )
    result.update(
        {
            item.name: getattr(row, item.name)
            for item in fields(PlayerPregameFeatures)
            if item.name not in {"game_id", "player_id", "team_id", "is_home"}
        }
    )
    if set(result) != set(_PLAYER_COLUMNS):
        raise FeatureSnapshotStoreError("feature_snapshot_invariants")
    return result


def _integer(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"{key} is not an integer")
    return value


def _optional_integer(row: Mapping[str, object], key: str) -> int | None:
    value = row[key]
    if value is not None and type(value) is not int:
        raise TypeError(f"{key} is not a nullable integer")
    return value


def _string(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError(f"{key} is not a string")
    return value


def _player_ids(row: Mapping[str, object], key: str) -> tuple[int, ...]:
    value = row[key]
    if type(value) is not list or any(type(item) is not int for item in value):
        raise TypeError(f"{key} is not an integer array")
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{key} is not sorted unique")
    return result


def _validate_shared(
    attempt: FeatureSnapshotAttempt, row: Mapping[str, object], *, player: bool
) -> None:
    token = attempt.input_token
    game = attempt.game
    strings = {
        "snapshot_id": str(attempt.snapshot_id),
        "feature_revision_id": attempt.feature_revision_id,
        "feature_input_token_sha256": token.sha256,
        "dependency_input_sha256": token.dependency_input_sha256,
        "dependency_revision_sha256": token.dependency_revision_sha256,
        "feature_formula_version": token.versions.formula_version,
        "output_schema_version": (
            token.versions.player_output_schema_version
            if player
            else token.versions.output_schema_version
        ),
        "game_id": game.game_id.value,
        "season": game.season.value,
        "season_type": game.season_type.value,
        "game_date": game.game_date.isoformat(),
        "started_at": _datetime_text(game.started_at),
    }
    if any(_string(row, key) != value for key, value in strings.items()):
        raise ValueError("feature snapshot shared identity differs")
    if (
        _integer(row, "insert_version") != attempt.generation
        or _integer(row, "season_start_year") != game.season.start_year
    ):
        raise ValueError("feature snapshot shared identity differs")


def _read_lineup_state(
    row: Mapping[str, object], prefix: str
) -> tuple[LineupStatus, tuple[int, ...], str | None, EventBoundary | None]:
    status = LineupStatus(_string(row, f"{prefix}_lineup_status"))
    ids = _player_ids(row, f"{prefix}_player_ids")
    reason = _string(row, f"{prefix}_invalid_reason")
    number = _integer(row, f"{prefix}_invalid_since_sequence_number")
    side = _string(row, f"{prefix}_invalid_since_boundary_side")
    if status is LineupStatus.VALID:
        if reason or number != 0 or side != "none":
            raise ValueError("valid lineup carries invalid sentinels")
        return status, ids, None, None
    if ids or not reason or number <= 0 or side == "none":
        raise ValueError("invalid lineup carries invalid values")
    return status, (), reason, EventBoundary(number, BoundarySide(side))


def _read_team_pregame(
    row: Mapping[str, object], prefix: str, team_id: int, is_home: bool
) -> TeamPregameFeatures:
    values: dict[str, object] = {"team_id": team_id, "is_home": is_home}
    for item in fields(TeamPregameFeatures):
        if item.name in values:
            continue
        key = f"{prefix}_{item.name}"
        if item.name == "back_to_back":
            flag = _integer(row, key)
            if flag not in {0, 1}:
                raise ValueError("back-to-back flag is invalid")
            values[item.name] = bool(flag)
        elif item.name.startswith("games_used"):
            values[item.name] = _integer(row, key)
        else:
            values[item.name] = _optional_integer(row, key)
    return TeamPregameFeatures(**values)  # type: ignore[arg-type]


def _read_lineup(
    row: Mapping[str, object], prefix: str, team_id: int, player_ids: tuple[int, ...]
) -> LineupAggregateFeatures:
    values: dict[str, object] = {"team_id": team_id, "player_ids": player_ids}
    for item in fields(LineupAggregateFeatures):
        if item.name in values:
            continue
        key = f"{prefix}_{item.name}"
        values[item.name] = (
            _integer(row, key)
            if item.name == "profiled_player_count"
            else _optional_integer(row, key)
        )
    return LineupAggregateFeatures(**values)  # type: ignore[arg-type]


def _read_live(row: Mapping[str, object]) -> LiveFeatures:
    values: dict[str, object] = {}
    for item in fields(LiveFeatures):
        values[item.name] = (
            _integer(row, item.name)
            if item.name.endswith("possessions_used")
            else _optional_integer(row, item.name)
        )
    return LiveFeatures(**values)  # type: ignore[arg-type]


def _read_state(attempt: FeatureSnapshotAttempt, row: Mapping[str, object]) -> GameStateFeature:
    if set(row) != set(_STATE_COLUMNS):
        raise ValueError("state projection differs")
    _validate_shared(attempt, row, player=False)
    token = attempt.input_token
    expected = {
        "feature_join_version": token.versions.join_version,
        "feature_event_normalizer_version": token.versions.event_normalizer_version,
        "possession_snapshot_id": str(token.possession_snapshot_id),
        "parse_revision_id": token.parse_revision_id,
        "possession_output_sha256": token.possession_output_sha256,
        "lineup_snapshot_id": str(token.lineup_snapshot_id),
        "lineup_revision_id": token.lineup_revision_id,
        "lineup_output_sha256": token.lineup_output_sha256,
    }
    if any(_string(row, key) != value for key, value in expected.items()):
        raise ValueError("state source lineage differs")
    home_status, home_ids, home_reason, home_since = _read_lineup_state(row, "home")
    away_status, away_ids, away_reason, away_since = _read_lineup_state(row, "away")
    game = attempt.game
    if (
        _integer(row, "home_team_id") != game.home_team_id
        or _integer(row, "away_team_id") != game.away_team_id
    ):
        raise ValueError("state teams differ")
    state = GameStateFeature(
        game_id=attempt.game_id,
        state_number=_integer(row, "state_number"),
        state_kind=FeatureStateKind(_string(row, "state_kind")),
        possession_number=_integer(row, "possession_number"),
        boundary=EventBoundary(
            _integer(row, "boundary_sequence_number"),
            BoundarySide(_string(row, "boundary_side")),
        ),
        home_lineup_status=home_status,
        away_lineup_status=away_status,
        home_player_ids=home_ids,
        away_player_ids=away_ids,
        home_invalid_reason=home_reason,
        home_invalid_since=home_since,
        away_invalid_reason=away_reason,
        away_invalid_since=away_since,
        period=_integer(row, "period"),
        seconds_remaining_game_ms=_integer(row, "seconds_remaining_game_ms"),
        seconds_remaining_period_ms=_integer(row, "seconds_remaining_period_ms"),
        home_score=_integer(row, "home_score"),
        away_score=_integer(row, "away_score"),
        score_diff=_integer(row, "score_diff"),
        home_possessions=_integer(row, "home_possessions"),
        away_possessions=_integer(row, "away_possessions"),
        home_pregame=_read_team_pregame(row, "home", game.home_team_id, True),
        away_pregame=_read_team_pregame(row, "away", game.away_team_id, False),
        home_lineup=_read_lineup(row, "home", game.home_team_id, home_ids),
        away_lineup=_read_lineup(row, "away", game.away_team_id, away_ids),
        live=_read_live(row),
        offense_difference=_optional_integer(row, "offense_difference"),
        defense_difference=_optional_integer(row, "defense_difference"),
        pace_difference=_optional_integer(row, "pace_difference"),
    )
    flag = _integer(row, "is_valid")
    if flag not in {0, 1} or flag != int(state.is_valid):
        raise ValueError("state validity flag differs")
    return state


def _read_player(
    attempt: FeatureSnapshotAttempt, row: Mapping[str, object]
) -> PlayerPregameFeatures:
    if set(row) != set(_PLAYER_COLUMNS):
        raise ValueError("player projection differs")
    _validate_shared(attempt, row, player=True)
    if _string(row, "history_cutoff_started_at") != _datetime_text(attempt.game.started_at):
        raise ValueError("player history cutoff differs")
    flag = _integer(row, "is_home")
    if flag not in {0, 1}:
        raise ValueError("player home flag is invalid")
    values: dict[str, object] = {
        "game_id": attempt.game_id,
        "player_id": _integer(row, "player_id"),
        "team_id": _integer(row, "team_id"),
        "is_home": bool(flag),
    }
    for item in fields(PlayerPregameFeatures):
        if item.name in values:
            continue
        values[item.name] = (
            _integer(row, item.name)
            if item.name in {"games_used", "seconds_played"}
            else _optional_integer(row, item.name)
        )
    player = PlayerPregameFeatures(**values)  # type: ignore[arg-type]
    game = attempt.game
    expected_team = game.home_team_id if player.is_home else game.away_team_id
    if player.team_id != expected_team:
        raise ValueError("player team role differs")
    return player


def _reconstruct(
    attempt: FeatureSnapshotAttempt,
    state_rows: Sequence[Mapping[str, object]],
    player_rows: Sequence[Mapping[str, object]],
) -> FeatureDataset:
    if not state_rows or attempt.expected_issue_codes is None:
        raise ValueError("feature state rows are missing")
    if attempt.expected_player_row_count != len(player_rows):
        raise ValueError("player table is absent or partial")
    states = tuple(_read_state(attempt, row) for row in state_rows)
    players = tuple(_read_player(attempt, row) for row in player_rows)
    if tuple(row.state_number for row in states) != tuple(range(len(states))):
        raise ValueError("state rows are not ordered and contiguous")
    if tuple(row.player_id for row in players) != tuple(sorted(row.player_id for row in players)):
        raise ValueError("player rows are not ordered")
    dataset = FeatureDataset(attempt.game_id, states, players, attempt.expected_issue_codes)
    state_hash = state_output_checksum(states)
    player_hash = player_output_checksum(players)
    feature_hash = feature_output_checksum(dataset)
    if any(_string(row, "state_output_sha256") != state_hash for row in state_rows):
        raise ValueError("declared state hash differs")
    if player_rows and any(
        _string(row, "player_output_sha256") != player_hash for row in player_rows
    ):
        raise ValueError("declared player hash differs")
    if any(
        _string(row, "feature_output_sha256") != feature_hash for row in (*state_rows, *player_rows)
    ):
        raise ValueError("declared feature hash differs")
    return dataset


def _can_verify(attempt: FeatureSnapshotAttempt) -> bool:
    return (
        attempt.state
        in {
            FeatureAttemptState.WRITING,
            FeatureAttemptState.VERIFIED,
            FeatureAttemptState.PUBLISHED,
        }
        and attempt.expected_state_row_count is not None
        and attempt.expected_state_output_sha256 is not None
        and attempt.expected_player_row_count is not None
        and attempt.expected_player_output_sha256 is not None
        and attempt.expected_feature_output_sha256 is not None
        and attempt.expected_issue_codes is not None
        and attempt.result_status is not None
    )


__all__ = [
    "ClickHouseFeatureSnapshotStore",
    "ClickHouseFeatureSourceReader",
    "FeatureSnapshotStoreError",
    "FeatureSourceReaderError",
]
