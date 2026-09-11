"""Async orchestration for deterministic, crash-safe possession builds."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from uuid import UUID

from nba_live_totals.application.possessions.models import (
    BuildMode,
    BuildPossessionsRequest,
    BuildPossessionsResult,
    BuildStatus,
    ClaimAction,
    CurrentSnapshotReference,
    PossessionAttemptState,
    PossessionBuildClaim,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.application.possessions.ports import (
    PossessionNormalizer,
    PossessionParser,
    PossessionSnapshotStore,
    PossessionWorkStore,
)
from nba_live_totals.domain import (
    GameId,
    ParseStatus,
    PossessionParseContext,
    PossessionVersions,
)

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
    claim: PossessionBuildClaim | None = None
    current: CurrentSnapshotReference | None = None
    row_count: int | None = None
    error_code: str | None = None
    error_type: str | None = None


class PossessionBuildService:
    """Coordinate bounded materialization or read-only current verification."""

    def __init__(
        self,
        *,
        normalizer: PossessionNormalizer,
        parser: PossessionParser,
        work_store: PossessionWorkStore,
        snapshot_store: PossessionSnapshotStore,
        versions: PossessionVersions,
        max_concurrency: int,
    ) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
            raise ValueError("max concurrency must be an integer from 1 to 32")
        if not isinstance(versions, PossessionVersions):
            raise ValueError("versions must be PossessionVersions")
        self._normalizer = normalizer
        self._parser = parser
        self._work_store = work_store
        self._snapshot_store = snapshot_store
        self._versions = versions
        self._max_concurrency = max_concurrency

    async def build(self, request: BuildPossessionsRequest) -> BuildPossessionsResult:
        """Run one auditable build/verify operation and durably finish its run."""
        run_id = await self._work_store.start_run(request)
        games: tuple[GameId, ...] = ()
        outcomes: list[_Outcome] = []
        try:
            selected = await self._work_store.select_games(request)
            games = tuple(sorted(set(selected), key=lambda game_id: game_id.value))
            await self._process_games(run_id, games, request, outcomes)
        except BaseException:
            interrupted = self._result(run_id, len(games), outcomes, interrupted=True)
            await self._best_effort_finish(run_id, interrupted)
            raise

        result = self._result(run_id, len(games), outcomes)
        await self._work_store.finish_run(run_id, result)
        return result

    async def _process_games(
        self,
        run_id: UUID,
        games: tuple[GameId, ...],
        request: BuildPossessionsRequest,
        outcomes: list[_Outcome],
    ) -> None:
        game_iterator = iter(games)

        async def worker() -> None:
            for game_id in game_iterator:
                if request.mode is BuildMode.VERIFY:
                    result = await self._verify_current(run_id, game_id)
                else:
                    result = await self._materialize(run_id, game_id, request.rebuild)
                outcomes.append(result.outcome)

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

    async def _materialize(
        self,
        run_id: UUID,
        game_id: GameId,
        rebuild: bool,
    ) -> _GameResult:
        started = monotonic()
        claim: PossessionBuildClaim | None = None
        result = _GameResult(_Outcome.FAILED, error_code="persistence_error")
        stage = "claim"
        try:
            decision = await self._work_store.claim_game(
                run_id,
                game_id,
                self._versions,
                rebuild=rebuild,
            )
            if decision.action is ClaimAction.SKIP_CURRENT:
                result = _GameResult(
                    _Outcome.SKIPPED_CURRENT,
                    current=decision.current,
                    row_count=decision.current.row_count if decision.current is not None else None,
                )
                return result
            if decision.action is ClaimAction.BUSY:
                result = _GameResult(
                    _Outcome.BUSY,
                    error_code="resource_busy",
                    error_type="ResourceBusy",
                )
                return result

            claim = decision.claim
            if claim is None:
                raise RuntimeError("build decision did not contain a claim")
            attempt_state = claim.attempt.state
            if attempt_state is PossessionAttemptState.ALLOCATED:
                stage = "normalize"
                normalized = self._normalizer.normalize(claim.snapshot)
                stage = "parse"
                parse_result = self._parser.parse(self._parse_context(claim), normalized)
                if parse_result.status is not ParseStatus.VALID:
                    stage = "fail_invalid"
                    await self._work_store.fail_game(
                        claim,
                        issues=parse_result.issues,
                        error_code="invalid_parse",
                        error_type="PossessionParseInvalid",
                    )
                    result = _GameResult(
                        _Outcome.FAILED,
                        claim=claim,
                        error_code="invalid_parse",
                        error_type="PossessionParseInvalid",
                    )
                    return result
                if parse_result.output_sha256 is None:
                    raise RuntimeError("valid parse did not contain output hash")

                stage = "mark_writing"
                writing = await self._work_store.mark_writing(
                    claim,
                    expected_row_count=len(parse_result.possessions),
                    expected_output_sha256=parse_result.output_sha256,
                )
                claim = replace(claim, attempt=writing)
                stage = "write"
                try:
                    await self._snapshot_store.write_snapshot(claim, parse_result)
                except Exception:
                    # Once the request might have been sent, only exact-ID readback may
                    # classify it. Retrying this immutable snapshot ID is forbidden.
                    pass
                stage = "verify"
                result = await self._verify_and_publish(claim)
                return result

            if attempt_state is PossessionAttemptState.WRITING:
                stage = "verify"
                result = await self._verify_and_publish(claim)
                return result
            if attempt_state is PossessionAttemptState.VERIFIED:
                stage = "publish"
                current = await self._work_store.publish_snapshot(claim)
                result = _GameResult(
                    _Outcome.MATERIALIZED,
                    claim=claim,
                    current=current,
                    row_count=current.row_count,
                )
                return result
            raise RuntimeError("resumed attempt has an invalid state")
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
            if stage in {"normalize", "parse"} and claim is not None:
                error_code = "invalid_parse"
                try:
                    await self._work_store.fail_game(
                        claim,
                        issues=(),
                        error_code=error_code,
                        error_type=self._safe_error_type(error),
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

    async def _verify_and_publish(self, claim: PossessionBuildClaim) -> _GameResult:
        verification = await self._readback(claim)
        if verification.status is VerificationStatus.UNAVAILABLE:
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code="snapshot_unavailable",
                error_type="SnapshotUnavailable",
            )
        if not self._matches_expected(claim, verification):
            await self._work_store.poison_attempt(
                claim,
                error_code="snapshot_mismatch",
                error_type="SnapshotMismatch",
            )
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                row_count=verification.row_count,
                error_code="snapshot_mismatch",
                error_type="SnapshotMismatch",
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
            claim=claim,
            current=current,
            row_count=current.row_count,
        )

    async def _readback(self, claim: PossessionBuildClaim) -> SnapshotVerification:
        try:
            return await self._snapshot_store.verify_snapshot(claim.attempt)
        except Exception:
            return SnapshotVerification(
                snapshot_id=claim.attempt.snapshot_id,
                status=VerificationStatus.UNAVAILABLE,
            )

    async def _verify_current(self, run_id: UUID, game_id: GameId) -> _GameResult:
        started = monotonic()
        current: CurrentSnapshotReference | None = None
        result = _GameResult(
            _Outcome.FAILED,
            error_code="snapshot_missing",
            error_type="SnapshotMissing",
        )
        try:
            current = await self._work_store.resolve_current(game_id, self._versions)
            if current is None:
                return result
            verification: SnapshotVerification | None = None
            verification_error: Exception | None = None
            try:
                verification = await self._snapshot_store.verify_snapshot(current.attempt)
            except Exception as error:
                verification_error = error
            resolved_again = await self._work_store.resolve_current(game_id, self._versions)
            if resolved_again != current:
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
                    row_count=verification.row_count if verification is not None else None,
                    error_code="stale_current",
                    error_type="StaleCurrent",
                )
                return result
            if verification_error is not None:
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
                    error_code="snapshot_unavailable",
                    error_type=self._safe_error_type(verification_error),
                )
                return result
            assert verification is not None
            if not self._reference_matches_verification(current, verification):
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
                    row_count=verification.row_count,
                    error_code=(
                        "snapshot_unavailable"
                        if verification.status is VerificationStatus.UNAVAILABLE
                        else "snapshot_mismatch"
                    ),
                    error_type=(
                        "SnapshotUnavailable"
                        if verification.status is VerificationStatus.UNAVAILABLE
                        else "SnapshotMismatch"
                    ),
                )
                return result
            result = _GameResult(
                _Outcome.VERIFIED,
                current=current,
                row_count=verification.row_count,
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
            self._log_terminal(run_id=run_id, game_id=game_id, result=result, started=started)

    @staticmethod
    def _matches_expected(
        claim: PossessionBuildClaim,
        verification: SnapshotVerification,
    ) -> bool:
        return (
            verification.status is VerificationStatus.EXACT
            and verification.snapshot_id == claim.attempt.snapshot_id
            and verification.row_count == claim.attempt.expected_row_count
            and verification.output_sha256 == claim.attempt.expected_output_sha256
        )

    @staticmethod
    def _reference_matches_verification(
        current: CurrentSnapshotReference,
        verification: SnapshotVerification,
    ) -> bool:
        return (
            verification.status is VerificationStatus.EXACT
            and verification.snapshot_id == current.attempt.snapshot_id
            and verification.row_count == current.row_count
            and verification.output_sha256 == current.output_sha256
        )

    def _parse_context(self, claim: PossessionBuildClaim) -> PossessionParseContext:
        token = claim.attempt.input_token
        return PossessionParseContext(
            game=claim.attempt.game,
            versions=self._versions,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            parse_revision_id=claim.attempt.parse_revision_id,
        )

    async def _best_effort_finish(
        self,
        run_id: UUID,
        result: BuildPossessionsResult,
    ) -> None:
        try:
            await self._work_store.finish_run(run_id, result)
        except Exception as error:
            logger.error(
                "possession_run_finish_failed",
                extra={
                    "event": "possession_run_finish_failed",
                    "run_id": str(run_id),
                    "status": BuildStatus.INTERRUPTED.value,
                    "error_code": "persistence_error",
                    "error_type": self._safe_error_type(error),
                },
            )

    def _log_terminal(
        self,
        run_id: UUID | None,
        game_id: GameId,
        result: _GameResult,
        started: float,
    ) -> None:
        claim = result.claim
        current = result.current
        attempt = claim.attempt if claim is not None else current.attempt if current else None
        optional_extra = {
            "event": "possession_game_finished",
            "run_id": str(run_id) if run_id is not None else None,
            "game_id": game_id.value,
            "snapshot_id": str(attempt.snapshot_id) if attempt is not None else None,
            "source_document_id": (
                str(attempt.input_token.applied_pbp_document_id) if attempt is not None else None
            ),
            "normalizer_version": self._versions.normalizer_version,
            "parser_version": self._versions.parser_version,
            "rules_version": self._versions.rules_version,
            "attempt": attempt.generation if attempt is not None else None,
            "row_count": result.row_count,
            "duration_ms": round(max(0.0, monotonic() - started) * 1000, 3),
            "status": result.outcome.value,
            "error_code": result.error_code,
            "error_type": result.error_type,
        }
        extra = {key: value for key, value in optional_extra.items() if value is not None}
        log = logger.warning if result.outcome in {_Outcome.FAILED, _Outcome.BUSY} else logger.info
        log("possession_game_finished", extra=extra)

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        name = type(error).__name__
        return name if _SAFE_IDENTIFIER.fullmatch(name) else "UnexpectedError"

    @staticmethod
    def _result(
        run_id: UUID,
        discovered: int,
        outcomes: list[_Outcome],
        *,
        interrupted: bool = False,
    ) -> BuildPossessionsResult:
        materialized = outcomes.count(_Outcome.MATERIALIZED)
        skipped = outcomes.count(_Outcome.SKIPPED_CURRENT)
        verified = outcomes.count(_Outcome.VERIFIED)
        failed = outcomes.count(_Outcome.FAILED)
        busy = outcomes.count(_Outcome.BUSY)
        successful = materialized + skipped + verified
        if interrupted:
            status = BuildStatus.INTERRUPTED
        elif failed == 0 and busy == 0:
            status = BuildStatus.SUCCEEDED
        elif successful:
            status = BuildStatus.PARTIAL
        else:
            status = BuildStatus.FAILED
        return BuildPossessionsResult(
            run_id=run_id,
            status=status,
            discovered=discovered,
            materialized=materialized,
            skipped_current=skipped,
            verified=verified,
            failed=failed,
            busy=busy,
        )


__all__ = ["PossessionBuildService"]
