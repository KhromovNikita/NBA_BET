"""Bounded, crash-safe lineup snapshot orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from uuid import UUID

from nba_live_totals.application.lineups.errors import LineupNormalizationError
from nba_live_totals.application.lineups.models import (
    BuildLineupsRequest,
    BuildLineupsResult,
    CurrentLineupSnapshotReference,
    LineupAttemptState,
    LineupBuildClaim,
    LineupBuildMode,
    LineupBuildStatus,
    LineupClaimAction,
    LineupPublicationState,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.application.lineups.ports import (
    LineupNormalizer,
    LineupReconstructor,
    LineupSnapshotStore,
    LineupWorkStore,
)
from nba_live_totals.domain import GameId
from nba_live_totals.domain.lineup_hashing import lineup_output_checksum
from nba_live_totals.domain.lineups import LineupReconstructionContext, LineupStatus, LineupVersions

logger = logging.getLogger(__name__)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class _Outcome(StrEnum):
    MATERIALIZED = "materialized"
    SKIPPED_CURRENT = "skipped_current"
    VERIFIED = "verified"
    FAILED = "failed"
    BUSY = "busy"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class _GameResult:
    outcome: _Outcome
    invalid: bool = False
    claim: LineupBuildClaim | None = None
    current: CurrentLineupSnapshotReference | None = None
    error_code: str | None = None
    error_type: str | None = None


class LineupBuildService:
    """Materialize or read-only verify deterministic lineup snapshots."""

    def __init__(
        self,
        *,
        normalizer: LineupNormalizer,
        reconstructor: LineupReconstructor,
        work_store: LineupWorkStore,
        snapshot_store: LineupSnapshotStore,
        versions: LineupVersions,
        max_concurrency: int,
    ) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
            raise ValueError("max concurrency must be an integer from 1 to 32")
        if not isinstance(versions, LineupVersions):
            raise ValueError("versions must be LineupVersions")
        self._normalizer = normalizer
        self._reconstructor = reconstructor
        self._work_store = work_store
        self._snapshot_store = snapshot_store
        self._versions = versions
        self._max_concurrency = max_concurrency

    async def build(self, request: BuildLineupsRequest) -> BuildLineupsResult:
        """Build one auditable run, preserving interruption as the primary error."""
        run_id = await self._work_store.start_run(request)
        games: tuple[GameId, ...] = ()
        outcomes: list[_GameResult] = []
        try:
            selected = await self._work_store.select_games(request)
            games = tuple(sorted(set(selected), key=lambda game_id: game_id.value))
            await self._process_games(run_id, games, request, outcomes)
        except BaseException:
            await self._best_effort_finish(
                run_id, self._result(run_id, len(games), outcomes, interrupted=True)
            )
            raise
        result = self._result(run_id, len(games), outcomes)
        try:
            await self._work_store.finish_run(run_id, result)
        except asyncio.CancelledError:
            await self._best_effort_finish(
                run_id, self._result(run_id, len(games), outcomes, interrupted=True)
            )
            raise
        return result

    async def _process_games(
        self,
        run_id: UUID,
        games: tuple[GameId, ...],
        request: BuildLineupsRequest,
        outcomes: list[_GameResult],
    ) -> None:
        iterator = iter(games)

        async def worker() -> None:
            for game_id in iterator:
                if request.mode is LineupBuildMode.VERIFY:
                    outcome = await self._verify_current(run_id, game_id)
                else:
                    outcome = await self._materialize(run_id, game_id, request.rebuild)
                outcomes.append(outcome)

        tasks = [
            asyncio.create_task(worker()) for _ in range(min(self._max_concurrency, len(games)))
        ]
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _materialize(self, run_id: UUID, game_id: GameId, rebuild: bool) -> _GameResult:
        started = monotonic()
        claim: LineupBuildClaim | None = None
        result = _GameResult(_Outcome.FAILED, error_code="persistence_error")
        stage = "claim"
        try:
            decision = await self._work_store.claim_game(
                run_id, game_id, self._versions, rebuild=rebuild
            )
            if decision.action is LineupClaimAction.SKIP_CURRENT:
                current = decision.current
                assert current is not None
                result = _GameResult(
                    _Outcome.SKIPPED_CURRENT,
                    invalid=current.result_status is LineupStatus.INVALID,
                    current=current,
                )
                return result
            if decision.action is LineupClaimAction.BUSY:
                result = _GameResult(
                    _Outcome.BUSY, error_code="resource_busy", error_type="ResourceBusy"
                )
                return result
            claim = decision.claim
            if claim is None:
                raise RuntimeError("claim decision lacks claim")
            if claim.attempt.state is LineupAttemptState.ALLOCATED:
                stage = "normalize"
                normalized = self._normalizer.normalize(claim.snapshot)
                stage = "reconstruct"
                reconstruction = self._reconstructor.reconstruct(self._context(claim), normalized)
                stage = "mark_writing"
                writing = await self._work_store.mark_writing(
                    claim,
                    expected_row_count=len(reconstruction.segments),
                    expected_output_sha256=lineup_output_checksum(reconstruction),
                    expected_issue_codes=reconstruction.issues,
                    result_status=reconstruction.status,
                )
                claim = replace(claim, attempt=writing)
                stage = "write"
                try:
                    await self._snapshot_store.write_snapshot(claim, reconstruction)
                except Exception:
                    # The INSERT may have succeeded despite a lost response. Never resend.
                    pass
                stage = "readback"
                result = await self._verify_and_publish(claim)
                return result
            if claim.attempt.state is LineupAttemptState.WRITING:
                stage = "readback"
                result = await self._verify_and_publish(claim)
                return result
            if claim.attempt.state is LineupAttemptState.VERIFIED:
                stage = "publish"
                current = await self._work_store.publish_snapshot(claim)
                result = _GameResult(
                    _Outcome.MATERIALIZED,
                    invalid=current.result_status is LineupStatus.INVALID,
                    claim=claim,
                    current=current,
                )
                return result
            raise RuntimeError("resumed attempt has invalid state")
        except asyncio.CancelledError:
            result = _GameResult(
                _Outcome.INTERRUPTED,
                claim=claim,
                error_code="interrupted",
                error_type="CancelledError",
            )
            raise
        except Exception as error:
            error_code = "persistence_error"
            if stage in {"normalize", "reconstruct"} and claim is not None:
                error_code = (
                    error.code
                    if isinstance(error, LineupNormalizationError)
                    else "structural_lineup_failure"
                )
                try:
                    await self._work_store.fail_game(
                        claim, error_code=error_code, error_type=self._safe_error_type(error)
                    )
                except Exception:
                    pass
            elif stage == "publish":
                error_code = "publication_failed"
            result = _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code=error_code,
                error_type=self._safe_error_type(error),
            )
            return result
        finally:
            self._log_terminal(run_id, game_id, result, started)

    async def _verify_and_publish(self, claim: LineupBuildClaim) -> _GameResult:
        verification = await self._readback(claim.attempt)
        if verification.status is LineupVerificationStatus.UNAVAILABLE:
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code="snapshot_unavailable",
                error_type="SnapshotUnavailable",
            )
        if not self._matches_expected(claim, verification):
            error_code = (
                "snapshot_absent"
                if verification.status is LineupVerificationStatus.ABSENT
                else "snapshot_mismatch"
            )
            await self._work_store.poison_attempt(
                claim, error_code=error_code, error_type="verification"
            )
            return _GameResult(
                _Outcome.FAILED, claim=claim, error_code=error_code, error_type="SnapshotMismatch"
            )
        verified = await self._work_store.mark_verified(claim, verification)
        claim = replace(claim, attempt=verified)
        try:
            current = await self._work_store.publish_snapshot(claim)
        except Exception as error:
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code="publication_failed",
                error_type=self._safe_error_type(error),
            )
        return _GameResult(
            _Outcome.MATERIALIZED,
            invalid=current.result_status is LineupStatus.INVALID,
            claim=claim,
            current=current,
        )

    async def _readback(self, attempt: LineupSnapshotAttempt) -> LineupSnapshotVerification:
        try:
            return await self._snapshot_store.verify_snapshot(attempt)
        except Exception:
            return LineupSnapshotVerification(
                attempt.snapshot_id, LineupVerificationStatus.UNAVAILABLE
            )

    async def _verify_current(self, run_id: UUID, game_id: GameId) -> _GameResult:
        started = monotonic()
        current: CurrentLineupSnapshotReference | None = None
        result = _GameResult(
            _Outcome.FAILED, error_code="snapshot_missing", error_type="SnapshotMissing"
        )
        try:
            resolution = await self._work_store.resolve_current(game_id, self._versions)
            if resolution.status in {LineupPublicationState.ABSENT, LineupPublicationState.STALE}:
                if resolution.status is LineupPublicationState.STALE:
                    result = _GameResult(
                        _Outcome.FAILED, error_code="stale_current", error_type="StaleCurrent"
                    )
                return result
            current = resolution.reference
            assert current is not None
            verification = await self._readback(current.attempt)
            resolved_again = await self._work_store.resolve_current(game_id, self._versions)
            if resolved_again != resolution:
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
                    error_code="stale_current",
                    error_type="StaleCurrent",
                )
                return result
            if not self._reference_matches(current, verification):
                error_code = {
                    LineupVerificationStatus.UNAVAILABLE: "snapshot_unavailable",
                    LineupVerificationStatus.ABSENT: "snapshot_missing",
                }.get(verification.status, "snapshot_mismatch")
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
                    error_code=error_code,
                    error_type="SnapshotVerificationFailed",
                )
                return result
            result = _GameResult(
                _Outcome.VERIFIED,
                invalid=current.result_status is LineupStatus.INVALID,
                current=current,
            )
            return result
        except asyncio.CancelledError:
            result = _GameResult(
                _Outcome.INTERRUPTED,
                current=current,
                error_code="interrupted",
                error_type="CancelledError",
            )
            raise
        except Exception as error:
            result = _GameResult(
                _Outcome.FAILED,
                current=current,
                error_code="persistence_error",
                error_type=self._safe_error_type(error),
            )
            return result
        finally:
            self._log_terminal(run_id, game_id, result, started)

    @staticmethod
    def _matches_expected(
        claim: LineupBuildClaim, verification: LineupSnapshotVerification
    ) -> bool:
        attempt = claim.attempt
        return (
            verification.status is LineupVerificationStatus.EXACT
            and verification.snapshot_id == attempt.snapshot_id
            and verification.row_count == attempt.expected_row_count
            and verification.output_sha256 == attempt.expected_output_sha256
            and verification.result_status == attempt.result_status
            and verification.issues == attempt.expected_issue_codes
        )

    @staticmethod
    def _reference_matches(
        current: CurrentLineupSnapshotReference, verification: LineupSnapshotVerification
    ) -> bool:
        attempt = current.attempt
        return (
            verification.status is LineupVerificationStatus.EXACT
            and verification.snapshot_id == attempt.snapshot_id
            and verification.row_count == current.row_count
            and verification.output_sha256 == current.output_sha256
            and verification.result_status == current.result_status
            and verification.issues == current.issues
        )

    def _context(self, claim: LineupBuildClaim) -> LineupReconstructionContext:
        token = claim.attempt.input_token
        return LineupReconstructionContext(
            game=claim.attempt.game,
            versions=self._versions,
            box_score_document_sha256=token.applied_box_score_sha256,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_player_box_sha256=token.canonical_player_box_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            lineup_revision_id=claim.attempt.lineup_revision_id,
        )

    async def _best_effort_finish(self, run_id: UUID, result: BuildLineupsResult) -> None:
        try:
            await self._work_store.finish_run(run_id, result)
        except BaseException as error:
            logger.error(
                "lineup_run_finish_failed",
                extra={
                    "event": "lineup_run_finish_failed",
                    "run_id": str(run_id),
                    "status": LineupBuildStatus.INTERRUPTED.value,
                    "error_code": "persistence_error",
                    "error_type": self._safe_error_type(error),
                },
            )

    def _log_terminal(
        self, run_id: UUID, game_id: GameId, result: _GameResult, started: float
    ) -> None:
        attempt = (
            result.claim.attempt
            if result.claim is not None
            else result.current.attempt
            if result.current is not None
            else None
        )
        values = {
            "event": "lineup_game_finished",
            "run_id": str(run_id),
            "game_id": game_id.value,
            "snapshot_id": str(attempt.snapshot_id) if attempt else None,
            "box_score_document_id": str(attempt.input_token.applied_box_score_document_id)
            if attempt
            else None,
            "pbp_document_id": str(attempt.input_token.applied_pbp_document_id)
            if attempt
            else None,
            "normalizer_version": self._versions.normalizer_version,
            "reconstructor_version": self._versions.reconstructor_version,
            "rules_version": self._versions.rules_version,
            "attempt": attempt.generation if attempt else None,
            "row_count": result.current.row_count if result.current else None,
            "duration_ms": round(max(0.0, monotonic() - started) * 1000, 3),
            "status": result.outcome.value,
            "error_code": result.error_code,
            "error_type": result.error_type,
        }
        extra = {key: value for key, value in values.items() if value is not None}
        log = logger.warning if result.outcome in {_Outcome.FAILED, _Outcome.BUSY} else logger.info
        log("lineup_game_finished", extra=extra)

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        name = type(error).__name__
        return name if _SAFE_IDENTIFIER.fullmatch(name) else "UnexpectedError"

    @staticmethod
    def _result(
        run_id: UUID, discovered: int, outcomes: list[_GameResult], *, interrupted: bool = False
    ) -> BuildLineupsResult:
        materialized = sum(item.outcome is _Outcome.MATERIALIZED for item in outcomes)
        skipped = sum(item.outcome is _Outcome.SKIPPED_CURRENT for item in outcomes)
        verified = sum(item.outcome is _Outcome.VERIFIED for item in outcomes)
        failed = sum(item.outcome is _Outcome.FAILED for item in outcomes)
        busy = sum(item.outcome is _Outcome.BUSY for item in outcomes)
        # The frozen result contract reserves nonzero invalid for PARTIAL only.
        # Preserve cancellation as primary; completed quality detail remains in
        # per-game audit events when the run is interrupted.
        invalid = 0 if interrupted else sum(item.invalid for item in outcomes)
        successful = materialized + skipped + verified
        if interrupted:
            status = LineupBuildStatus.INTERRUPTED
        elif not failed and not busy and not invalid:
            status = LineupBuildStatus.SUCCEEDED
        elif successful:
            status = LineupBuildStatus.PARTIAL
        else:
            status = LineupBuildStatus.FAILED
        return BuildLineupsResult(
            run_id,
            status,
            discovered,
            materialized,
            invalid,
            skipped,
            verified,
            discovered - successful - busy,
            busy,
        )


__all__ = ["LineupBuildService"]
