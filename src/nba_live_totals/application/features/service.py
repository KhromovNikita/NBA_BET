"""Sequential, crash-safe feature snapshot orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from uuid import UUID

from nba_live_totals.application.features.errors import FeatureNormalizationError
from nba_live_totals.application.features.models import (
    BuildDatasetRequest,
    BuildDatasetResult,
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureBuildMode,
    FeatureBuildStatus,
    FeatureClaimAction,
    FeaturePublicationState,
    FeatureSnapshotVerification,
    FeatureTargetSnapshot,
    FeatureVerificationStatus,
)
from nba_live_totals.application.features.ports import (
    FeatureCalculator,
    FeatureEventNormalizer,
    FeatureSnapshotStore,
    FeatureSourceReader,
    FeatureWorkStore,
)
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import FeatureResultStatus, FeatureVersions
from nba_live_totals.domain.identifiers import GameId

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
    claim: FeatureBuildClaim | None = None
    current: CurrentFeatureSnapshotReference | None = None
    error_code: str | None = None
    error_type: str | None = None


class FeatureBuildService:
    """Materialize or verify feature snapshots one selected game at a time."""

    def __init__(
        self,
        *,
        normalizer: FeatureEventNormalizer,
        source_reader: FeatureSourceReader,
        calculator: FeatureCalculator,
        work_store: FeatureWorkStore,
        snapshot_store: FeatureSnapshotStore,
        versions: FeatureVersions,
    ) -> None:
        if not isinstance(versions, FeatureVersions):
            raise ValueError("versions must be FeatureVersions")
        self._normalizer = normalizer
        self._source_reader = source_reader
        self._calculator = calculator
        self._work_store = work_store
        self._snapshot_store = snapshot_store
        self._versions = versions

    async def build(self, request: BuildDatasetRequest) -> BuildDatasetResult:
        run_id = await self._work_store.start_run(request)
        games: tuple[GameId, ...] = ()
        outcomes: list[_GameResult] = []
        try:
            games = await self._work_store.select_games(request)
            if len(set(games)) != len(games):
                raise ValueError("selected games contain duplicates")
            previous_key: tuple[object, str] | None = None
            for game_id in games:
                target = await self._resolve_target(run_id, game_id, outcomes)
                if target is None:
                    continue
                key = (target.game.started_at, target.game.game_id.value)
                if target.game.game_id != game_id or (
                    previous_key is not None and key <= previous_key
                ):
                    raise ValueError("selected games are not in started-at/game-ID order")
                previous_key = key
                if request.mode is FeatureBuildMode.VERIFY:
                    outcome = await self._verify_current(run_id, target)
                else:
                    outcome = await self._materialize(run_id, target, request.rebuild)
                outcomes.append(outcome)
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

    async def _resolve_target(
        self, run_id: UUID, game_id: GameId, outcomes: list[_GameResult]
    ) -> FeatureTargetSnapshot | None:
        try:
            return await self._work_store.resolve_target(game_id)
        except asyncio.CancelledError:
            await self._best_effort_unclaimed(run_id, game_id, "interrupted", "CancelledError")
            raise
        except Exception as error:
            await self._best_effort_unclaimed(
                run_id, game_id, "source_resolution_failed", self._safe_error_type(error)
            )
            outcomes.append(
                _GameResult(
                    _Outcome.FAILED,
                    error_code="source_resolution_failed",
                    error_type=self._safe_error_type(error),
                )
            )
            return None

    async def _materialize(
        self, run_id: UUID, target: FeatureTargetSnapshot, rebuild: bool
    ) -> _GameResult:
        started = monotonic()
        game_id = target.game.game_id
        claim: FeatureBuildClaim | None = None
        result = _GameResult(_Outcome.FAILED, error_code="persistence_error")
        stage = "read_target"
        try:
            target_rows = await self._source_reader.read_target(target)
            stage = "claim"
            decision = await self._work_store.claim_game(
                run_id,
                target,
                target_rows.target_player_ids,
                self._versions,
                rebuild=rebuild,
            )
            if decision.action is FeatureClaimAction.SKIP_CURRENT:
                current = decision.current
                assert current is not None
                result = _GameResult(
                    _Outcome.SKIPPED_CURRENT,
                    invalid=current.result_status is FeatureResultStatus.INVALID,
                    current=current,
                )
                return result
            if decision.action is FeatureClaimAction.BUSY:
                result = _GameResult(
                    _Outcome.BUSY, error_code="resource_busy", error_type="ResourceBusy"
                )
                return result
            claim = decision.claim
            if claim is None:
                raise RuntimeError("claim decision lacks a claim")
            if claim.attempt.state is FeatureAttemptState.ALLOCATED:
                stage = "read_inputs"
                inputs = await self._source_reader.read_inputs(claim)
                stage = "normalize"
                events = self._normalizer.normalize(claim.source.target.pbp_snapshot)
                stage = "calculate"
                dataset = self._calculator.calculate(inputs, events)
                stage = "mark_writing"
                writing = await self._work_store.mark_writing(
                    claim,
                    expected_state_row_count=len(dataset.states),
                    expected_state_output_sha256=state_output_checksum(dataset.states),
                    expected_player_row_count=len(dataset.players),
                    expected_player_output_sha256=player_output_checksum(dataset.players),
                    expected_feature_output_sha256=feature_output_checksum(dataset),
                    expected_issue_codes=dataset.issues,
                    result_status=dataset.status,
                )
                claim = replace(claim, attempt=writing)
                stage = "write"
                try:
                    await self._snapshot_store.write_snapshot(claim, dataset)
                except Exception:
                    # Any INSERT intent is followed only by exact-ID readback.
                    pass
                stage = "readback"
                result = await self._verify_and_publish(claim)
                return result
            if claim.attempt.state is FeatureAttemptState.WRITING:
                stage = "readback"
                result = await self._verify_and_publish(claim)
                return result
            if claim.attempt.state is FeatureAttemptState.VERIFIED:
                stage = "publish"
                current = await self._work_store.publish_snapshot(claim)
                result = _GameResult(
                    _Outcome.MATERIALIZED,
                    invalid=current.result_status is FeatureResultStatus.INVALID,
                    claim=claim,
                    current=current,
                )
                return result
            raise RuntimeError("resumed attempt has an invalid state")
        except asyncio.CancelledError:
            if claim is None:
                await self._best_effort_unclaimed(run_id, game_id, "interrupted", "CancelledError")
            else:
                await self._best_effort_fail(claim, "interrupted", "CancelledError")
            result = _GameResult(
                _Outcome.INTERRUPTED,
                claim=claim,
                error_code="interrupted",
                error_type="CancelledError",
            )
            raise
        except Exception as error:
            error_code = self._stage_error_code(stage, error)
            if claim is None:
                await self._best_effort_unclaimed(
                    run_id, game_id, error_code, self._safe_error_type(error)
                )
            elif stage in {"read_inputs", "normalize", "calculate"}:
                await self._best_effort_fail(claim, error_code, stage)
            result = _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code=error_code,
                error_type=self._safe_error_type(error),
            )
            return result
        finally:
            self._log_terminal(run_id, game_id, result, started)

    async def _verify_and_publish(self, claim: FeatureBuildClaim) -> _GameResult:
        verification = await self._readback(claim)
        if verification.status is FeatureVerificationStatus.UNAVAILABLE:
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code="snapshot_unavailable",
                error_type="SnapshotUnavailable",
            )
        if not self._matches_attempt(claim, verification):
            error_code = (
                "snapshot_absent"
                if verification.status is FeatureVerificationStatus.ABSENT
                else "snapshot_mismatch"
            )
            await self._work_store.poison_attempt(
                claim, error_code=error_code, error_type="verification"
            )
            return _GameResult(
                _Outcome.FAILED,
                claim=claim,
                error_code=error_code,
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
            invalid=current.result_status is FeatureResultStatus.INVALID,
            claim=claim,
            current=current,
        )

    async def _readback(self, claim: FeatureBuildClaim) -> FeatureSnapshotVerification:
        try:
            return await self._snapshot_store.verify_snapshot(claim.attempt)
        except Exception:
            return FeatureSnapshotVerification(
                claim.attempt.snapshot_id, FeatureVerificationStatus.UNAVAILABLE
            )

    async def _verify_current(self, run_id: UUID, target: FeatureTargetSnapshot) -> _GameResult:
        started = monotonic()
        game_id = target.game.game_id
        current: CurrentFeatureSnapshotReference | None = None
        result = _GameResult(
            _Outcome.FAILED, error_code="snapshot_missing", error_type="SnapshotMissing"
        )
        try:
            target_rows = await self._source_reader.read_target(target)
            resolution = await self._work_store.resolve_current(
                target, target_rows.target_player_ids, self._versions
            )
            if resolution.status in {
                FeaturePublicationState.ABSENT,
                FeaturePublicationState.STALE,
            }:
                if resolution.status is FeaturePublicationState.STALE:
                    result = _GameResult(
                        _Outcome.FAILED,
                        error_code="stale_current",
                        error_type="StaleCurrent",
                    )
                return result
            current = resolution.reference
            assert current is not None
            verification: FeatureSnapshotVerification | None = None
            verification_error: Exception | None = None
            try:
                verification = await self._snapshot_store.verify_snapshot(current.attempt)
            except Exception as error:
                verification_error = error
            resolved_again = await self._work_store.resolve_current(
                target, target_rows.target_player_ids, self._versions
            )
            if resolved_again != resolution:
                result = _GameResult(
                    _Outcome.FAILED,
                    current=current,
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
            if not self._matches_reference(current, verification):
                error_code = {
                    FeatureVerificationStatus.UNAVAILABLE: "snapshot_unavailable",
                    FeatureVerificationStatus.ABSENT: "snapshot_missing",
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
                invalid=current.result_status is FeatureResultStatus.INVALID,
                current=current,
            )
            return result
        except asyncio.CancelledError:
            await self._best_effort_unclaimed(run_id, game_id, "interrupted", "CancelledError")
            result = _GameResult(
                _Outcome.INTERRUPTED,
                current=current,
                error_code="interrupted",
                error_type="CancelledError",
            )
            raise
        except Exception as error:
            await self._best_effort_unclaimed(
                run_id,
                game_id,
                "verification_failed",
                self._safe_error_type(error),
            )
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
    def _matches_attempt(
        claim: FeatureBuildClaim, verification: FeatureSnapshotVerification
    ) -> bool:
        attempt = claim.attempt
        return (
            verification.status is FeatureVerificationStatus.EXACT
            and verification.snapshot_id == attempt.snapshot_id
            and verification.state_row_count == attempt.expected_state_row_count
            and verification.state_output_sha256 == attempt.expected_state_output_sha256
            and verification.player_row_count == attempt.expected_player_row_count
            and verification.player_output_sha256 == attempt.expected_player_output_sha256
            and verification.feature_output_sha256 == attempt.expected_feature_output_sha256
            and verification.result_status is attempt.result_status
            and verification.issues == attempt.expected_issue_codes
        )

    @staticmethod
    def _matches_reference(
        current: CurrentFeatureSnapshotReference,
        verification: FeatureSnapshotVerification,
    ) -> bool:
        return (
            verification.status is FeatureVerificationStatus.EXACT
            and verification.snapshot_id == current.attempt.snapshot_id
            and verification.state_row_count == current.state_row_count
            and verification.state_output_sha256 == current.state_output_sha256
            and verification.player_row_count == current.player_row_count
            and verification.player_output_sha256 == current.player_output_sha256
            and verification.feature_output_sha256 == current.feature_output_sha256
            and verification.result_status is current.result_status
            and verification.issues == current.issues
        )

    async def _best_effort_finish(self, run_id: UUID, result: BuildDatasetResult) -> None:
        try:
            await self._work_store.finish_run(run_id, result)
        except BaseException as error:
            logger.error(
                "feature_run_finish_failed",
                extra={
                    "event": "feature_run_finish_failed",
                    "run_id": str(run_id),
                    "status": FeatureBuildStatus.INTERRUPTED.value,
                    "error_code": "persistence_error",
                    "error_type": self._safe_error_type(error),
                },
            )

    async def _best_effort_fail(
        self, claim: FeatureBuildClaim, error_code: str, error_type: str
    ) -> None:
        try:
            await self._work_store.fail_game(claim, error_code=error_code, error_type=error_type)
        except BaseException:
            pass

    async def _best_effort_unclaimed(
        self, run_id: UUID, game_id: GameId, error_code: str, error_type: str
    ) -> None:
        try:
            await self._work_store.record_unclaimed_failure(
                run_id, game_id, error_code=error_code, error_type=error_type
            )
        except BaseException:
            pass

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
            "event": "feature_game_finished",
            "run_id": str(run_id),
            "game_id": game_id.value,
            "snapshot_id": str(attempt.snapshot_id) if attempt else None,
            "generation": attempt.generation if attempt else None,
            "feature_revision_id": attempt.feature_revision_id if attempt else None,
            "possession_snapshot_id": str(attempt.input_token.possession_snapshot_id)
            if attempt
            else None,
            "lineup_snapshot_id": str(attempt.input_token.lineup_snapshot_id) if attempt else None,
            "state_row_count": attempt.expected_state_row_count if attempt else None,
            "player_row_count": attempt.expected_player_row_count if attempt else None,
            "result_status": attempt.result_status.value
            if attempt and attempt.result_status
            else None,
            "issue_codes": attempt.expected_issue_codes if attempt else None,
            "elapsed_ms": round(max(0.0, monotonic() - started) * 1000, 3),
            "status": result.outcome.value,
            "error_code": result.error_code,
            "error_type": result.error_type,
        }
        extra = {key: value for key, value in values.items() if value is not None}
        log = logger.warning if result.outcome in {_Outcome.FAILED, _Outcome.BUSY} else logger.info
        log("feature_game_finished", extra=extra)

    @staticmethod
    def _stage_error_code(stage: str, error: Exception) -> str:
        if isinstance(error, FeatureNormalizationError):
            return error.code
        if stage in {"read_target", "read_inputs", "normalize", "calculate"}:
            return "structural_feature_failure"
        if stage == "publish":
            return "publication_failed"
        return "persistence_error"

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        name = type(error).__name__
        return name if _SAFE_IDENTIFIER.fullmatch(name) else "UnexpectedError"

    @staticmethod
    def _result(
        run_id: UUID,
        discovered: int,
        outcomes: list[_GameResult],
        *,
        interrupted: bool = False,
    ) -> BuildDatasetResult:
        materialized = sum(item.outcome is _Outcome.MATERIALIZED for item in outcomes)
        skipped = sum(item.outcome is _Outcome.SKIPPED_CURRENT for item in outcomes)
        verified = sum(item.outcome is _Outcome.VERIFIED for item in outcomes)
        busy = sum(item.outcome is _Outcome.BUSY for item in outcomes)
        invalid = sum(item.invalid for item in outcomes)
        successful = materialized + skipped + verified
        failed = discovered - successful - busy
        if interrupted:
            # Frozen Task 8 accounting requires any invalid output to remain PARTIAL;
            # cancellation is still preserved as the primary raised exception.
            status = FeatureBuildStatus.PARTIAL if invalid else FeatureBuildStatus.INTERRUPTED
        elif not failed and not busy and not invalid:
            status = FeatureBuildStatus.SUCCEEDED
        elif successful:
            status = FeatureBuildStatus.PARTIAL
        else:
            status = FeatureBuildStatus.FAILED
        return BuildDatasetResult(
            run_id,
            status,
            discovered,
            materialized,
            invalid,
            skipped,
            verified,
            failed,
            busy,
        )


__all__ = ["FeatureBuildService"]
