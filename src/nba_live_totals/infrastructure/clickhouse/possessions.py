"""Exact-ID immutable ClickHouse persistence for analytical possessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from nba_live_totals.application.possessions import (
    CurrentSnapshotReference,
    PossessionBuildClaim,
    PossessionSnapshotAttempt,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    ParseStatus,
    Possession,
    PossessionEndReason,
    PossessionParseResult,
    possession_output_checksum,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError

_TABLE = "nba.possessions_v1"
_COLUMN_NAMES = (
    "snapshot_id",
    "insert_version",
    "parse_revision_id",
    "normalizer_version",
    "parser_version",
    "rules_version",
    "input_pbp_document_id",
    "input_pbp_sha256",
    "input_event_sha256",
    "game_context_sha256",
    "output_schema_version",
    "output_sha256",
    "game_id",
    "season",
    "season_start_year",
    "season_type",
    "game_date",
    "home_team_id",
    "away_team_id",
    "possession_number",
    "offense_possession_number",
    "period",
    "offense_team_id",
    "defense_team_id",
    "start_sequence_number",
    "start_boundary_side",
    "end_sequence_number",
    "end_boundary_side",
    "start_seconds_remaining_period_ms",
    "end_seconds_remaining_period_ms",
    "seconds_remaining_game_ms",
    "home_score_before",
    "away_score_before",
    "home_score",
    "away_score",
    "score_diff",
    "offense_points",
    "defense_points",
    "home_possessions",
    "away_possessions",
    "end_reason",
    "source_event_count",
)
_READ_STATEMENT = (
    f"SELECT {', '.join(_COLUMN_NAMES)} FROM {_TABLE} FINAL "
    "WHERE season_start_year = {season_start_year:UInt16} "
    "AND game_id = {game_id:FixedString(10)} "
    "AND snapshot_id = {snapshot_id:UUID} "
    "ORDER BY possession_number"
)


class SnapshotClient(Protocol):
    async def insert_json_each_row(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
    ) -> None: ...

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]: ...


class PossessionSnapshotStoreError(RuntimeError):
    """A bounded error for an exact immutable snapshot operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Readback:
    possessions: tuple[Possession, ...]
    output_sha256: str


class PossessionSnapshotStore:
    """Write and read one immutable snapshot ID without latest-row semantics."""

    def __init__(self, client: SnapshotClient) -> None:
        self._client = client
        self._sent_snapshot_ids: set[UUID] = set()

    async def write_snapshot(
        self,
        claim: PossessionBuildClaim,
        result: PossessionParseResult,
    ) -> None:
        self._validate_write(claim, result)
        snapshot_id = claim.attempt.snapshot_id
        if snapshot_id in self._sent_snapshot_ids:
            raise PossessionSnapshotStoreError("possession_snapshot_already_sent")
        self._sent_snapshot_ids.add(snapshot_id)
        rows = tuple(
            self._serialize_row(claim, result, possession)
            for possession in sorted(result.possessions, key=lambda item: item.possession_number)
        )
        try:
            await self._client.insert_json_each_row(_TABLE, rows)
        except ClickHouseError:
            raise PossessionSnapshotStoreError("possession_snapshot_unavailable") from None

    async def verify_snapshot(
        self,
        attempt: PossessionSnapshotAttempt,
    ) -> SnapshotVerification:
        try:
            rows = await self._query_rows(attempt)
        except ClickHouseError:
            return SnapshotVerification(attempt.snapshot_id, VerificationStatus.UNAVAILABLE)
        if not rows:
            return SnapshotVerification(
                attempt.snapshot_id, VerificationStatus.MISSING, row_count=0
            )
        try:
            readback = self._reconstruct(attempt, rows)
        except (KeyError, TypeError, ValueError):
            return SnapshotVerification(attempt.snapshot_id, VerificationStatus.MISMATCH)
        if (
            attempt.expected_row_count is not None
            and len(readback.possessions) != attempt.expected_row_count
        ):
            return SnapshotVerification(attempt.snapshot_id, VerificationStatus.MISMATCH)
        if (
            attempt.expected_output_sha256 is not None
            and readback.output_sha256 != attempt.expected_output_sha256
        ):
            return SnapshotVerification(attempt.snapshot_id, VerificationStatus.MISMATCH)
        return SnapshotVerification(
            attempt.snapshot_id,
            VerificationStatus.EXACT,
            row_count=len(readback.possessions),
            output_sha256=readback.output_sha256,
        )

    async def read_snapshot(
        self,
        reference: CurrentSnapshotReference,
    ) -> tuple[Possession, ...]:
        try:
            rows = await self._query_rows(reference.attempt)
            readback = self._reconstruct(reference.attempt, rows)
        except ClickHouseError:
            raise PossessionSnapshotStoreError("possession_snapshot_unavailable") from None
        except (KeyError, TypeError, ValueError):
            raise PossessionSnapshotStoreError("possession_snapshot_mismatch") from None
        if (
            len(readback.possessions) != reference.row_count
            or readback.output_sha256 != reference.output_sha256
        ):
            raise PossessionSnapshotStoreError("possession_snapshot_mismatch")
        return readback.possessions

    def _validate_write(self, claim: PossessionBuildClaim, result: PossessionParseResult) -> None:
        attempt = claim.attempt
        if result.status is not ParseStatus.VALID or result.output_sha256 is None:
            raise PossessionSnapshotStoreError("possession_snapshot_invalid_result")
        context = result.context
        token = attempt.input_token
        if (
            context.game != attempt.game
            or context.versions != token.versions
            or context.pbp_document_sha256 != token.applied_pbp_sha256
            or context.canonical_event_sha256 != token.canonical_event_sha256
            or context.game_context_sha256 != token.game_context_sha256
            or context.parse_revision_id != attempt.parse_revision_id
        ):
            raise PossessionSnapshotStoreError("possession_snapshot_lineage_mismatch")
        computed = possession_output_checksum(
            parse_revision_id=attempt.parse_revision_id,
            game=attempt.game,
            possessions=result.possessions,
        )
        if computed != result.output_sha256:
            raise PossessionSnapshotStoreError("possession_snapshot_output_mismatch")

    def _serialize_row(
        self,
        claim: PossessionBuildClaim,
        result: PossessionParseResult,
        possession: Possession,
    ) -> dict[str, object]:
        attempt = claim.attempt
        game = attempt.game
        token = attempt.input_token
        versions = token.versions
        return {
            "snapshot_id": str(attempt.snapshot_id),
            "insert_version": attempt.generation,
            "parse_revision_id": attempt.parse_revision_id,
            "normalizer_version": versions.normalizer_version,
            "parser_version": versions.parser_version,
            "rules_version": versions.rules_version,
            "input_pbp_document_id": str(token.applied_pbp_document_id),
            "input_pbp_sha256": token.applied_pbp_sha256,
            "input_event_sha256": token.canonical_event_sha256,
            "game_context_sha256": token.game_context_sha256,
            "output_schema_version": versions.output_schema_version,
            "output_sha256": result.output_sha256,
            "game_id": game.game_id.value,
            "season": game.season.value,
            "season_start_year": game.season.start_year,
            "season_type": game.season_type.value,
            "game_date": game.game_date.isoformat(),
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "possession_number": possession.possession_number,
            "offense_possession_number": possession.offense_possession_number,
            "period": possession.period,
            "offense_team_id": possession.offense_team_id,
            "defense_team_id": possession.defense_team_id,
            "start_sequence_number": possession.start_boundary.sequence_number,
            "start_boundary_side": possession.start_boundary.side.value,
            "end_sequence_number": possession.end_boundary.sequence_number,
            "end_boundary_side": possession.end_boundary.side.value,
            "start_seconds_remaining_period_ms": possession.start_seconds_remaining_period_ms,
            "end_seconds_remaining_period_ms": possession.end_seconds_remaining_period_ms,
            "seconds_remaining_game_ms": possession.seconds_remaining_game_ms,
            "home_score_before": possession.home_score_before,
            "away_score_before": possession.away_score_before,
            "home_score": possession.home_score,
            "away_score": possession.away_score,
            "score_diff": possession.score_diff,
            "offense_points": possession.offense_points,
            "defense_points": possession.defense_points,
            "home_possessions": possession.home_possessions,
            "away_possessions": possession.away_possessions,
            "end_reason": possession.end_reason.value,
            "source_event_count": possession.source_event_count,
        }

    async def _query_rows(self, attempt: PossessionSnapshotAttempt) -> list[dict[str, object]]:
        return await self._client.query(
            _READ_STATEMENT,
            parameters={
                "season_start_year": attempt.game.season.start_year,
                "game_id": attempt.game_id.value,
                "snapshot_id": str(attempt.snapshot_id),
            },
        )

    def _reconstruct(
        self,
        attempt: PossessionSnapshotAttempt,
        rows: Sequence[Mapping[str, object]],
    ) -> _Readback:
        if not rows:
            raise ValueError("snapshot rows are missing")
        possessions: list[Possession] = []
        declared_hashes: set[str] = set()
        for row in rows:
            if set(row) != set(_COLUMN_NAMES):
                raise ValueError("snapshot row projection is invalid")
            self._validate_shared_fields(attempt, row)
            declared_hashes.add(_string(row, "output_sha256"))
            possessions.append(
                Possession(
                    game_id=attempt.game_id,
                    possession_number=_integer(row, "possession_number"),
                    offense_team_id=_integer(row, "offense_team_id"),
                    defense_team_id=_integer(row, "defense_team_id"),
                    offense_possession_number=_integer(row, "offense_possession_number"),
                    period=_integer(row, "period"),
                    start_boundary=EventBoundary(
                        _integer(row, "start_sequence_number"),
                        BoundarySide(_string(row, "start_boundary_side")),
                    ),
                    end_boundary=EventBoundary(
                        _integer(row, "end_sequence_number"),
                        BoundarySide(_string(row, "end_boundary_side")),
                    ),
                    start_seconds_remaining_period_ms=_integer(
                        row, "start_seconds_remaining_period_ms"
                    ),
                    end_seconds_remaining_period_ms=_integer(
                        row, "end_seconds_remaining_period_ms"
                    ),
                    seconds_remaining_game_ms=_integer(row, "seconds_remaining_game_ms"),
                    home_score_before=_integer(row, "home_score_before"),
                    away_score_before=_integer(row, "away_score_before"),
                    home_score=_integer(row, "home_score"),
                    away_score=_integer(row, "away_score"),
                    score_diff=_integer(row, "score_diff"),
                    offense_points=_integer(row, "offense_points"),
                    defense_points=_integer(row, "defense_points"),
                    home_possessions=_integer(row, "home_possessions"),
                    away_possessions=_integer(row, "away_possessions"),
                    end_reason=PossessionEndReason(_string(row, "end_reason")),
                    source_event_count=_integer(row, "source_event_count"),
                )
            )
        if len(declared_hashes) != 1:
            raise ValueError("snapshot hash fields disagree")
        output_sha256 = possession_output_checksum(
            parse_revision_id=attempt.parse_revision_id,
            game=attempt.game,
            possessions=possessions,
        )
        if output_sha256 != declared_hashes.pop():
            raise ValueError("snapshot hash does not match typed rows")
        return _Readback(
            tuple(sorted(possessions, key=lambda item: item.possession_number)), output_sha256
        )

    def _validate_shared_fields(
        self,
        attempt: PossessionSnapshotAttempt,
        row: Mapping[str, object],
    ) -> None:
        token = attempt.input_token
        game = attempt.game
        expected_strings = {
            "snapshot_id": str(attempt.snapshot_id),
            "parse_revision_id": attempt.parse_revision_id,
            "normalizer_version": token.versions.normalizer_version,
            "parser_version": token.versions.parser_version,
            "rules_version": token.versions.rules_version,
            "input_pbp_document_id": str(token.applied_pbp_document_id),
            "input_pbp_sha256": token.applied_pbp_sha256,
            "input_event_sha256": token.canonical_event_sha256,
            "game_context_sha256": token.game_context_sha256,
            "output_schema_version": token.versions.output_schema_version,
            "game_id": game.game_id.value,
            "season": game.season.value,
            "season_type": game.season_type.value,
            "game_date": game.game_date.isoformat(),
        }
        if any(_string(row, key) != value for key, value in expected_strings.items()):
            raise ValueError("snapshot shared identity does not match")
        expected_integers = {
            "insert_version": attempt.generation,
            "season_start_year": game.season.start_year,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
        }
        if any(_integer(row, key) != value for key, value in expected_integers.items()):
            raise ValueError("snapshot shared identity does not match")


def _string(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError("snapshot value is not a string")
    return value


def _integer(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError("snapshot value is not an integer")
    return value


__all__ = ["PossessionSnapshotStore", "PossessionSnapshotStoreError"]
