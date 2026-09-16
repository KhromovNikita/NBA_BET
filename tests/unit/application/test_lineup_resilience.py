"""Lineup build worker, recovery, and disclosure boundaries."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import pytest

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    BuildLineupsResult,
    CurrentLineupSnapshotReference,
    GameLineupInputSnapshot,
    LineupAttemptState,
    LineupBuildClaim,
    LineupBuildMode,
    LineupBuildStatus,
    LineupClaimAction,
    LineupClaimDecision,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.domain.lineups import (
    LineupReconstructionResult,
    LineupVersions,
    NormalizedGameLineups,
)

from .lineup_fakes import (
    DEFAULT_GAME_ID,
    FakeNormalizer,
    FakeReconstructor,
    FakeSnapshotStore,
    FakeWorkStore,
    make_claim,
    make_resolution,
)
from .test_lineup_service import make_service

SEASON = SeasonId("2025-26")
GAME_2 = GameId("0022500124")
GAME_3 = GameId("0022500125")


@pytest.mark.asyncio
async def test_bounded_workers_never_exceed_configured_concurrency() -> None:
    events: list[str] = []
    active = 0
    peak = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClaims(FakeWorkStore):
        async def claim_game(
            self, run_id: UUID, game_id: GameId, versions: LineupVersions, *, rebuild: bool
        ) -> LineupClaimDecision:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await release.wait()
            active -= 1
            return await super().claim_game(run_id, game_id, versions, rebuild=rebuild)

    work = BlockingClaims((DEFAULT_GAME_ID, GAME_2, GAME_3), events)
    service = make_service(
        work,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeReconstructor(events),
        max_concurrency=2,
    )
    task = asyncio.create_task(service.build(BuildLineupsRequest((SEASON,))))
    await both_started.wait()
    assert peak == 2 and work.claim_calls == []
    release.set()
    result = await task
    assert result.materialized == 3 and peak == 2


@pytest.mark.asyncio
async def test_cancellation_stops_new_claims_and_finishes_interrupted_best_effort() -> None:
    events: list[str] = []
    started = asyncio.Event()

    class BlockingClaim(FakeWorkStore):
        async def claim_game(
            self, run_id: UUID, game_id: GameId, versions: LineupVersions, *, rebuild: bool
        ) -> LineupClaimDecision:
            del run_id, versions, rebuild
            self.claim_calls.append(game_id)
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
            del run_id
            self.finished.append(result)
            raise RuntimeError("password=secret")

    work = BlockingClaim((DEFAULT_GAME_ID, GAME_2), events)
    service = make_service(
        work, FakeSnapshotStore(events), FakeNormalizer(events), FakeReconstructor(events)
    )
    task = asyncio.create_task(service.build(BuildLineupsRequest((SEASON,))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.claim_calls == [DEFAULT_GAME_ID]
    assert len(work.finished) == 1 and work.finished[0].status is LineupBuildStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_cancellation_during_finish_after_invalid_result_preserves_cancellation() -> None:
    events: list[str] = []
    ordinary_finish_started = asyncio.Event()

    class BlockingFinish(FakeWorkStore):
        async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
            del run_id
            self.finished.append(result)
            if result.status is not LineupBuildStatus.INTERRUPTED:
                ordinary_finish_started.set()
                await asyncio.Future()

    work = BlockingFinish((DEFAULT_GAME_ID,), events, invalid=True)
    service = make_service(
        work,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeReconstructor(events, invalid=True),
    )
    task = asyncio.create_task(service.build(BuildLineupsRequest((SEASON,))))
    await ordinary_finish_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [item.status for item in work.finished] == [
        LineupBuildStatus.PARTIAL,
        LineupBuildStatus.INTERRUPTED,
    ]
    assert [(item.materialized, item.invalid) for item in work.finished] == [(1, 1), (1, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_operation", ["write", "verify"])
async def test_cancellation_gathers_workers_before_finalization_without_retries(
    blocked_operation: str,
) -> None:
    events: list[str] = []
    both_started = asyncio.Event()
    active = 0

    class BlockingSnapshots(FakeSnapshotStore):
        async def _block(self) -> None:
            nonlocal active
            active += 1
            if active == 2:
                both_started.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1

        async def write_snapshot(
            self, claim: LineupBuildClaim, result: LineupReconstructionResult
        ) -> None:
            await super().write_snapshot(claim, result)
            if blocked_operation == "write":
                await self._block()

        async def verify_snapshot(
            self, attempt: LineupSnapshotAttempt
        ) -> LineupSnapshotVerification:
            verification = await super().verify_snapshot(attempt)
            if blocked_operation == "verify":
                await self._block()
            return verification

    class AssertGatheredFinish(FakeWorkStore):
        async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
            assert active == 0
            await super().finish_run(run_id, result)

    work = AssertGatheredFinish((DEFAULT_GAME_ID, GAME_2, GAME_3), events)
    snapshots = BlockingSnapshots(events)
    service = make_service(
        work,
        snapshots,
        FakeNormalizer(events),
        FakeReconstructor(events),
        max_concurrency=2,
    )
    task = asyncio.create_task(service.build(BuildLineupsRequest((SEASON,))))
    await both_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.claim_calls == [DEFAULT_GAME_ID, GAME_2]
    assert len(work.finished) == 1 and work.finished[0].status is LineupBuildStatus.INTERRUPTED
    assert len(snapshots.write_calls) == 2
    assert len(snapshots.verify_calls) == (0 if blocked_operation == "write" else 2)
    assert work.poison_calls == []
    assert "mark_verified" not in events and "publish_snapshot" not in events


@pytest.mark.asyncio
async def test_verify_different_or_unavailable_fails_without_mutating_pointer() -> None:
    for status in (LineupVerificationStatus.DIFFERENT, LineupVerificationStatus.UNAVAILABLE):
        events: list[str] = []
        work = FakeWorkStore((DEFAULT_GAME_ID,), events)
        claim = make_claim(state=LineupAttemptState.PUBLISHED)
        resolution = make_resolution(claim)
        work.resolutions[DEFAULT_GAME_ID] = [resolution, resolution]
        snapshots = FakeSnapshotStore(events)
        snapshots.verification = LineupSnapshotVerification(claim.attempt.snapshot_id, status)
        result = await make_service(
            work, snapshots, FakeNormalizer(events), FakeReconstructor(events)
        ).build(BuildLineupsRequest((SEASON,), mode=LineupBuildMode.VERIFY))
        assert result.failed == 1
        assert work.claim_calls == [] and work.poison_calls == []
        assert "resolve_current" == events[-2] and "finish_run" == events[-1]


@pytest.mark.asyncio
async def test_raised_readback_error_leaves_writing_attempt_unpoisoned() -> None:
    events: list[str] = []
    work = FakeWorkStore((DEFAULT_GAME_ID,), events)
    claim = make_claim(state=LineupAttemptState.WRITING)
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(LineupClaimAction.RESUME, claim)
    snapshots = FakeSnapshotStore(events)
    snapshots.verify_error = TimeoutError("clickhouse://user:secret@host")
    result = await make_service(
        work, snapshots, FakeNormalizer(events), FakeReconstructor(events)
    ).build(BuildLineupsRequest((SEASON,)))
    assert result.failed == 1 and snapshots.write_calls == []
    assert work.poison_calls == []
    assert "mark_verified" not in events and "publish_snapshot" not in events


@pytest.mark.asyncio
async def test_verify_readback_exception_still_double_resolves_without_mutation() -> None:
    events: list[str] = []
    work = FakeWorkStore((DEFAULT_GAME_ID,), events)
    claim = make_claim(state=LineupAttemptState.PUBLISHED)
    resolution = make_resolution(claim)
    work.resolutions[DEFAULT_GAME_ID] = [resolution, resolution]
    snapshots = FakeSnapshotStore(events)
    snapshots.verify_error = TimeoutError("clickhouse://user:secret@host")
    result = await make_service(
        work, snapshots, FakeNormalizer(events), FakeReconstructor(events)
    ).build(BuildLineupsRequest((SEASON,), mode=LineupBuildMode.VERIFY))
    assert result.failed == 1
    assert events == [
        "start_run",
        "select_games",
        "resolve_current",
        "verify_snapshot",
        "resolve_current",
        "finish_run",
    ]
    assert work.claim_calls == [] and work.poison_calls == [] and work.fail_calls == []


@pytest.mark.asyncio
async def test_stale_publish_fence_isolated_from_next_game() -> None:
    events: list[str] = []

    class StaleFirstPublish(FakeWorkStore):
        async def publish_snapshot(self, claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
            if claim.attempt.game_id == DEFAULT_GAME_ID:
                raise RuntimeError("postgresql://user:secret@host")
            return await super().publish_snapshot(claim)

    work = StaleFirstPublish((DEFAULT_GAME_ID, GAME_2), events)
    result = await make_service(
        work, FakeSnapshotStore(events), FakeNormalizer(events), FakeReconstructor(events)
    ).build(BuildLineupsRequest((SEASON,)))
    assert (result.status, result.materialized, result.failed) == (LineupBuildStatus.PARTIAL, 1, 1)
    assert work.finished == [result]


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_primary_safe_error_and_logs_no_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    class PrimaryError(RuntimeError):
        pass

    class FailingNormalizer(FakeNormalizer):
        def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
            del snapshot
            raise PrimaryError("player name and password=secret")

    class FailingCleanup(FakeWorkStore):
        async def fail_game(
            self, claim: LineupBuildClaim, *, error_code: str, error_type: str
        ) -> None:
            del claim
            self.fail_calls.append((error_code, error_type))
            raise RuntimeError("postgresql://user:secret@host")

    work = FailingCleanup((DEFAULT_GAME_ID,), events)
    service = make_service(
        work, FakeSnapshotStore(events), FailingNormalizer(events), FakeReconstructor(events)
    )
    with caplog.at_level(logging.INFO, logger="nba_live_totals.application.lineups.service"):
        result = await service.build(BuildLineupsRequest((SEASON,)))
    assert result.failed == 1 and work.fail_calls == [("structural_lineup_failure", "PrimaryError")]
    terminal = next(record for record in caplog.records if record.msg == "lineup_game_finished")
    assert terminal.error_code == "structural_lineup_failure"  # type: ignore[attr-defined]
    for secret in (
        "player name",
        "password=secret",
        "postgresql://",
        "provider text must not escape",
    ):
        assert secret not in str(terminal.__dict__)
