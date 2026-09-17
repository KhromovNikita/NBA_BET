"""Exact-ID immutable ClickHouse persistence for reconstructed lineup segments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from nba_live_totals.application.lineups import (
    CurrentLineupSnapshotReference,
    LineupAttemptState,
    LineupBuildClaim,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import BoundarySide, EventBoundary
from nba_live_totals.domain.lineup_hashing import lineup_output_checksum
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    TeamLineupState,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError

_TABLE = "nba.lineup_segments_v1"
_COLUMN_NAMES = (
    "snapshot_id",
    "insert_version",
    "lineup_revision_id",
    "normalizer_version",
    "reconstructor_version",
    "rules_version",
    "input_box_score_document_id",
    "input_box_score_sha256",
    "input_pbp_document_id",
    "input_pbp_sha256",
    "canonical_player_box_sha256",
    "canonical_event_sha256",
    "game_context_sha256",
    "output_schema_version",
    "output_sha256",
    "result_status",
    "game_id",
    "season",
    "season_start_year",
    "season_type",
    "game_date",
    "home_team_id",
    "away_team_id",
    "segment_number",
    "start_sequence_number",
    "start_boundary_side",
    "end_sequence_number",
    "end_boundary_side",
    "start_period",
    "end_period",
    "start_seconds_remaining_period_ms",
    "end_seconds_remaining_period_ms",
    "home_status",
    "home_player_ids",
    "home_invalid_reason",
    "home_invalid_since_sequence_number",
    "home_invalid_since_boundary_side",
    "away_status",
    "away_player_ids",
    "away_invalid_reason",
    "away_invalid_since_sequence_number",
    "away_invalid_since_boundary_side",
    "is_valid",
)
_READ_STATEMENT = (
    f"SELECT {', '.join(_COLUMN_NAMES)} FROM {_TABLE} FINAL "
    "WHERE season_start_year = {season_start_year:UInt16} "
    "AND game_id = {game_id:FixedString(10)} "
    "AND snapshot_id = {snapshot_id:UUID} "
    "ORDER BY segment_number"
)


class SnapshotClient(Protocol):
    async def insert_json_each_row(
        self, table: str, rows: Sequence[Mapping[str, object]]
    ) -> None: ...

    async def query(
        self, statement: str, *, parameters: Mapping[str, str | int] | None = None
    ) -> list[dict[str, object]]: ...


class LineupSnapshotStoreError(RuntimeError):
    """A bounded error for exact immutable lineup snapshot operations."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Readback:
    result: LineupReconstructionResult
    output_sha256: str


class ClickHouseLineupSnapshotStore:
    """Write and verify one lineup snapshot ID without latest-row semantics."""

    def __init__(self, client: SnapshotClient) -> None:
        self._client = client
        self._sent_snapshot_ids: set[UUID] = set()

    async def write_snapshot(
        self, claim: LineupBuildClaim, result: LineupReconstructionResult
    ) -> None:
        self._validate_write(claim, result)
        snapshot_id = claim.attempt.snapshot_id
        if snapshot_id in self._sent_snapshot_ids:
            raise LineupSnapshotStoreError("lineup_snapshot_already_sent")
        self._sent_snapshot_ids.add(snapshot_id)
        rows = tuple(
            self._serialize_row(claim, result, segment)
            for segment in sorted(result.segments, key=lambda item: item.segment_number)
        )
        try:
            await self._client.insert_json_each_row(_TABLE, rows)
        except ClickHouseError:
            raise LineupSnapshotStoreError("lineup_snapshot_unavailable") from None

    async def verify_snapshot(self, attempt: LineupSnapshotAttempt) -> LineupSnapshotVerification:
        if not _can_verify(attempt):
            return LineupSnapshotVerification(
                attempt.snapshot_id, LineupVerificationStatus.DIFFERENT
            )
        try:
            rows = await self._query_rows(attempt)
        except ClickHouseError:
            return LineupSnapshotVerification(
                attempt.snapshot_id, LineupVerificationStatus.UNAVAILABLE
            )
        if not rows:
            return LineupSnapshotVerification(attempt.snapshot_id, LineupVerificationStatus.ABSENT)
        try:
            readback = self._reconstruct(attempt, rows)
        except (KeyError, TypeError, ValueError):
            return LineupSnapshotVerification(
                attempt.snapshot_id, LineupVerificationStatus.DIFFERENT
            )
        if (
            len(readback.result.segments) != attempt.expected_row_count
            or readback.output_sha256 != attempt.expected_output_sha256
            or readback.result.status is not attempt.result_status
            or readback.result.issues != attempt.expected_issue_codes
        ):
            return LineupSnapshotVerification(
                attempt.snapshot_id, LineupVerificationStatus.DIFFERENT
            )
        return LineupSnapshotVerification(
            attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            row_count=len(readback.result.segments),
            output_sha256=readback.output_sha256,
            result_status=readback.result.status,
            issues=readback.result.issues,
        )

    async def read_snapshot(
        self, reference: CurrentLineupSnapshotReference
    ) -> tuple[LineupSegment, ...]:
        try:
            readback = self._reconstruct(
                reference.attempt, await self._query_rows(reference.attempt)
            )
        except ClickHouseError:
            raise LineupSnapshotStoreError("lineup_snapshot_unavailable") from None
        except (KeyError, TypeError, ValueError):
            raise LineupSnapshotStoreError("lineup_snapshot_mismatch") from None
        if (
            len(readback.result.segments) != reference.row_count
            or readback.output_sha256 != reference.output_sha256
            or readback.result.status is not reference.result_status
            or readback.result.issues != reference.issues
        ):
            raise LineupSnapshotStoreError("lineup_snapshot_mismatch")
        return readback.result.segments

    def _validate_write(self, claim: LineupBuildClaim, result: LineupReconstructionResult) -> None:
        attempt = claim.attempt
        if attempt.state is not LineupAttemptState.WRITING:
            raise LineupSnapshotStoreError("lineup_snapshot_write_not_writing")
        token = attempt.input_token
        context = result.context
        if (
            context.game != attempt.game
            or context.versions != token.versions
            or context.box_score_document_sha256 != token.applied_box_score_sha256
            or context.pbp_document_sha256 != token.applied_pbp_sha256
            or context.canonical_player_box_sha256 != token.canonical_player_box_sha256
            or context.canonical_event_sha256 != token.canonical_event_sha256
            or context.game_context_sha256 != token.game_context_sha256
            or context.lineup_revision_id != attempt.lineup_revision_id
        ):
            raise LineupSnapshotStoreError("lineup_snapshot_lineage_mismatch")
        output_sha256 = lineup_output_checksum(result)
        if (
            attempt.expected_row_count != len(result.segments)
            or attempt.expected_output_sha256 != output_sha256
            or attempt.expected_issue_codes != result.issues
            or attempt.result_status is not result.status
        ):
            raise LineupSnapshotStoreError("lineup_snapshot_expected_output_mismatch")
        try:
            _validate_segments(result.segments)
        except ValueError:
            raise LineupSnapshotStoreError("lineup_snapshot_invariants") from None

    def _serialize_row(
        self, claim: LineupBuildClaim, result: LineupReconstructionResult, segment: LineupSegment
    ) -> dict[str, object]:
        attempt = claim.attempt
        game = attempt.game
        token = attempt.input_token
        versions = token.versions
        return {
            "snapshot_id": str(attempt.snapshot_id),
            "insert_version": attempt.generation,
            "lineup_revision_id": attempt.lineup_revision_id,
            "normalizer_version": versions.normalizer_version,
            "reconstructor_version": versions.reconstructor_version,
            "rules_version": versions.rules_version,
            "input_box_score_document_id": str(token.applied_box_score_document_id),
            "input_box_score_sha256": token.applied_box_score_sha256,
            "input_pbp_document_id": str(token.applied_pbp_document_id),
            "input_pbp_sha256": token.applied_pbp_sha256,
            "canonical_player_box_sha256": token.canonical_player_box_sha256,
            "canonical_event_sha256": token.canonical_event_sha256,
            "game_context_sha256": token.game_context_sha256,
            "output_schema_version": versions.output_schema_version,
            "output_sha256": lineup_output_checksum(result),
            "result_status": result.status.value,
            "game_id": game.game_id.value,
            "season": game.season.value,
            "season_start_year": game.season.start_year,
            "season_type": game.season_type.value,
            "game_date": game.game_date.isoformat(),
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "segment_number": segment.segment_number,
            "start_sequence_number": segment.start_boundary.sequence_number,
            "start_boundary_side": segment.start_boundary.side.value,
            "end_sequence_number": segment.end_boundary.sequence_number,
            "end_boundary_side": segment.end_boundary.side.value,
            "start_period": segment.start_period,
            "end_period": segment.end_period,
            "start_seconds_remaining_period_ms": segment.start_seconds_remaining_period_ms,
            "end_seconds_remaining_period_ms": segment.end_seconds_remaining_period_ms,
            **_serialize_state("home", segment.home_state),
            **_serialize_state("away", segment.away_state),
            "is_valid": int(result.status is LineupStatus.VALID),
        }

    async def _query_rows(self, attempt: LineupSnapshotAttempt) -> list[dict[str, object]]:
        return await self._client.query(
            _READ_STATEMENT,
            parameters={
                "season_start_year": attempt.game.season.start_year,
                "game_id": attempt.game_id.value,
                "snapshot_id": str(attempt.snapshot_id),
            },
        )

    def _reconstruct(
        self, attempt: LineupSnapshotAttempt, rows: Sequence[Mapping[str, object]]
    ) -> _Readback:
        if not rows:
            raise ValueError("snapshot rows are missing")
        segments: list[LineupSegment] = []
        declared_hashes: set[str] = set()
        declared_statuses: set[LineupStatus] = set()
        for row in rows:
            if set(row) != set(_COLUMN_NAMES):
                raise ValueError("snapshot row projection is invalid")
            self._validate_shared_fields(attempt, row)
            declared_hashes.add(_string(row, "output_sha256"))
            status = LineupStatus(_string(row, "result_status"))
            declared_statuses.add(status)
            home = _read_state(row, "home")
            away = _read_state(row, "away")
            is_valid = _integer(row, "is_valid")
            if is_valid not in {0, 1} or is_valid != int(
                status is LineupStatus.VALID
                and home.status is LineupStatus.VALID
                and away.status is LineupStatus.VALID
            ):
                raise ValueError("snapshot validity flag is invalid")
            segments.append(
                LineupSegment(
                    segment_number=_integer(row, "segment_number"),
                    start_boundary=EventBoundary(
                        _integer(row, "start_sequence_number"),
                        BoundarySide(_string(row, "start_boundary_side")),
                    ),
                    end_boundary=EventBoundary(
                        _integer(row, "end_sequence_number"),
                        BoundarySide(_string(row, "end_boundary_side")),
                    ),
                    start_period=_integer(row, "start_period"),
                    end_period=_integer(row, "end_period"),
                    start_seconds_remaining_period_ms=_integer(
                        row, "start_seconds_remaining_period_ms"
                    ),
                    end_seconds_remaining_period_ms=_integer(
                        row, "end_seconds_remaining_period_ms"
                    ),
                    home_state=home,
                    away_state=away,
                )
            )
        if len(declared_hashes) != 1 or len(declared_statuses) != 1:
            raise ValueError("snapshot result fields disagree")
        context = _context_from_attempt(attempt)
        status = declared_statuses.pop()
        _validate_segments(segments)
        issues = _issues(context, tuple(segments))
        result = LineupReconstructionResult(context, status, tuple(segments), issues)
        output_sha256 = lineup_output_checksum(result)
        if output_sha256 != declared_hashes.pop():
            raise ValueError("snapshot hash does not match typed rows")
        return _Readback(result, output_sha256)

    def _validate_shared_fields(
        self, attempt: LineupSnapshotAttempt, row: Mapping[str, object]
    ) -> None:
        token = attempt.input_token
        game = attempt.game
        expected_strings = {
            "snapshot_id": str(attempt.snapshot_id),
            "lineup_revision_id": attempt.lineup_revision_id,
            "normalizer_version": token.versions.normalizer_version,
            "reconstructor_version": token.versions.reconstructor_version,
            "rules_version": token.versions.rules_version,
            "input_box_score_document_id": str(token.applied_box_score_document_id),
            "input_box_score_sha256": token.applied_box_score_sha256,
            "input_pbp_document_id": str(token.applied_pbp_document_id),
            "input_pbp_sha256": token.applied_pbp_sha256,
            "canonical_player_box_sha256": token.canonical_player_box_sha256,
            "canonical_event_sha256": token.canonical_event_sha256,
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


def _serialize_state(prefix: str, state: TeamLineupState) -> dict[str, object]:
    if state.status is LineupStatus.VALID:
        return {
            f"{prefix}_status": "valid",
            f"{prefix}_player_ids": sorted(state.player_ids),
            f"{prefix}_invalid_reason": "",
            f"{prefix}_invalid_since_sequence_number": 0,
            f"{prefix}_invalid_since_boundary_side": "none",
        }
    assert state.invalid_reason is not None and state.invalid_since is not None
    return {
        f"{prefix}_status": "invalid",
        f"{prefix}_player_ids": [],
        f"{prefix}_invalid_reason": state.invalid_reason,
        f"{prefix}_invalid_since_sequence_number": state.invalid_since.sequence_number,
        f"{prefix}_invalid_since_boundary_side": state.invalid_since.side.value,
    }


def _read_state(row: Mapping[str, object], prefix: str) -> TeamLineupState:
    status = LineupStatus(_string(row, f"{prefix}_status"))
    player_ids = _player_ids(row, f"{prefix}_player_ids")
    reason = _string(row, f"{prefix}_invalid_reason")
    since_number = _integer(row, f"{prefix}_invalid_since_sequence_number")
    since_side = _string(row, f"{prefix}_invalid_since_boundary_side")
    if status is LineupStatus.VALID:
        if reason != "" or since_number != 0 or since_side != "none":
            raise ValueError("valid lineup state has invalid metadata")
        return TeamLineupState(status, frozenset(player_ids), None, None)
    if player_ids or not reason or since_number <= 0:
        raise ValueError("invalid lineup state has invalid metadata")
    return TeamLineupState(
        status, frozenset(), reason, EventBoundary(since_number, BoundarySide(since_side))
    )


def _context_from_attempt(attempt: LineupSnapshotAttempt) -> LineupReconstructionContext:
    token = attempt.input_token
    return LineupReconstructionContext(
        game=attempt.game,
        versions=token.versions,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        lineup_revision_id=attempt.lineup_revision_id,
    )


def _issues(
    context: LineupReconstructionContext, segments: tuple[LineupSegment, ...]
) -> tuple[LineupIssue, ...]:
    issues: list[LineupIssue] = []
    seen: set[tuple[int, str, EventBoundary]] = set()
    for segment in segments:
        for team_id, state in (
            (context.game.home_team_id, segment.home_state),
            (context.game.away_team_id, segment.away_state),
        ):
            if state.status is LineupStatus.INVALID:
                assert state.invalid_reason is not None and state.invalid_since is not None
                issue = (team_id, state.invalid_reason, state.invalid_since)
                if issue not in seen:
                    seen.add(issue)
                    issues.append(
                        LineupIssue(
                            team_id,
                            state.invalid_reason,
                            state.invalid_since.sequence_number,
                            state.invalid_since.side,
                        )
                    )
                    # Match the domain's first 32 distinct issues in traversal order.
                    if len(issues) == 32:
                        return tuple(issues)
    return tuple(issues)


def _validate_segments(segments: Sequence[LineupSegment]) -> None:
    if not segments or segments[0].start_boundary != EventBoundary(1, BoundarySide.BEFORE):
        raise ValueError("snapshot has no initial segment")
    for number, segment in enumerate(segments, start=1):
        if segment.segment_number != number:
            raise ValueError("snapshot segment numbers are invalid")
        if number > 1:
            previous = segments[number - 2]
            if (
                previous.end_boundary.sequence_number != segment.start_boundary.sequence_number
                or previous.end_boundary.side is not BoundarySide.BEFORE
                or segment.start_boundary.side is not BoundarySide.AFTER
            ):
                raise ValueError("snapshot segment boundaries have a gap or overlap")


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


def _player_ids(row: Mapping[str, object], key: str) -> tuple[int, ...]:
    value = row[key]
    if type(value) is not list or any(type(item) is not int for item in value):
        raise TypeError("snapshot player IDs are not an integer array")
    values = tuple(value)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ValueError("snapshot player IDs are not sorted unique")
    return values


def _can_verify(attempt: LineupSnapshotAttempt) -> bool:
    return (
        attempt.state
        in {LineupAttemptState.WRITING, LineupAttemptState.VERIFIED, LineupAttemptState.PUBLISHED}
        and attempt.expected_row_count is not None
        and attempt.expected_output_sha256 is not None
        and attempt.expected_issue_codes is not None
        and attempt.result_status is not None
    )


__all__ = ["ClickHouseLineupSnapshotStore", "LineupSnapshotStoreError"]
