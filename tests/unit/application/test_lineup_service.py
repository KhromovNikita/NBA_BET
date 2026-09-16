"""Lineup build lifecycle and read-only verification contracts."""

from __future__ import annotations

import pytest

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    CurrentLineupSnapshotResolution,
    LineupAttemptState,
    LineupBuildMode,
    LineupBuildService,
    LineupBuildStatus,
    LineupClaimAction,
    LineupClaimDecision,
    LineupNormalizationError,
    LineupPublicationState,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import SeasonId
from nba_live_totals.domain.lineups import LineupStatus

from .lineup_fakes import (
    DEFAULT_GAME_ID,
    VERSIONS,
    FakeNormalizer,
    FakeReconstructor,
    FakeSnapshotStore,
    FakeWorkStore,
    make_claim,
    make_resolution,
)

SEASON = SeasonId("2025-26")


def make_service(
    work: FakeWorkStore,
    snapshots: FakeSnapshotStore,
    normalizer: FakeNormalizer,
    reconstructor: FakeReconstructor,
    *,
    max_concurrency: int = 1,
) -> LineupBuildService:
    return LineupBuildService(
        normalizer=normalizer,
        reconstructor=reconstructor,
        work_store=work,
        snapshot_store=snapshots,
        versions=VERSIONS,
        max_concurrency=max_concurrency,
    )


def collaborators(
    *, invalid: bool = False
) -> tuple[list[str], FakeWorkStore, FakeSnapshotStore, FakeNormalizer, FakeReconstructor]:
    events: list[str] = []
    work = FakeWorkStore((DEFAULT_GAME_ID,), events)
    snapshots = FakeSnapshotStore(events)
    normalizer = FakeNormalizer(events)
    reconstructor = FakeReconstructor(events, invalid=invalid)
    return events, work, snapshots, normalizer, reconstructor


@pytest.mark.asyncio
async def test_allocated_claim_publishes_after_one_write_and_exact_readback() -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators()
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.status is LineupBuildStatus.SUCCEEDED
    assert (result.materialized, result.invalid, result.failed) == (1, 0, 0)
    assert events == [
        "start_run",
        "select_games",
        "claim",
        "normalize",
        "reconstruct",
        "mark_writing",
        "write_snapshot",
        "verify_snapshot",
        "mark_verified",
        "publish_snapshot",
        "finish_run",
    ]
    assert len(snapshots.write_calls) == len(snapshots.verify_calls) == 1


@pytest.mark.asyncio
async def test_invalid_result_is_published_and_counted_as_quality_partial() -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators(invalid=True)
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert (result.status, result.materialized, result.invalid, result.failed) == (
        LineupBuildStatus.PARTIAL,
        1,
        1,
        0,
    )
    assert "publish_snapshot" in events


@pytest.mark.asyncio
async def test_structural_normalizer_failure_releases_claim_without_snapshot_write() -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators()
    normalizer.error = LineupNormalizationError("unsupported_document_schema")
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.failed == 1
    assert work.fail_calls == [("unsupported_document_schema", "LineupNormalizationError")]
    assert snapshots.write_calls == [] and snapshots.verify_calls == []
    assert "publish_snapshot" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_skip_current_counts_quality_without_claim_or_clickhouse(invalid: bool) -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(
        LineupClaimAction.SKIP_CURRENT,
        current=make_resolution(
            make_claim(state=LineupAttemptState.PUBLISHED, invalid=invalid)
        ).reference,
    )
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert (result.skipped_current, result.invalid, result.materialized) == (1, int(invalid), 0)
    assert result.status is (LineupBuildStatus.PARTIAL if invalid else LineupBuildStatus.SUCCEEDED)
    assert snapshots.verify_calls == [] and normalizer.calls == []


@pytest.mark.asyncio
async def test_busy_counts_operational_failure_without_snapshot_io() -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(LineupClaimAction.BUSY)
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert (result.status, result.busy, result.failed) == (LineupBuildStatus.FAILED, 1, 0)
    assert snapshots.write_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [LineupAttemptState.WRITING, LineupAttemptState.VERIFIED])
async def test_resume_never_normalizes_or_resends_snapshot(state: LineupAttemptState) -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(
        LineupClaimAction.RESUME, claim=make_claim(state=state)
    )
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.materialized == 1 and snapshots.write_calls == [] and normalizer.calls == []
    assert len(snapshots.verify_calls) == (1 if state is LineupAttemptState.WRITING else 0)


@pytest.mark.asyncio
async def test_lost_insert_response_uses_exact_readback_without_resend() -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    snapshots.write_error = TimeoutError("secret ClickHouse response")
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.materialized == 1
    assert len(snapshots.write_calls) == len(snapshots.verify_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        LineupVerificationStatus.ABSENT,
        LineupVerificationStatus.DIFFERENT,
        LineupVerificationStatus.UNAVAILABLE,
    ],
)
async def test_nonexact_readback_never_publishes_and_only_definite_failure_poisons(
    status: LineupVerificationStatus,
) -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators()
    claim = make_claim(state=LineupAttemptState.WRITING)
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(LineupClaimAction.RESUME, claim=claim)
    snapshots.verification = LineupSnapshotVerification(claim.attempt.snapshot_id, status)
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.failed == 1 and snapshots.write_calls == [] and "publish_snapshot" not in events
    assert len(work.poison_calls) == (0 if status is LineupVerificationStatus.UNAVAILABLE else 1)


@pytest.mark.asyncio
async def test_exact_readback_requires_status_issues_hash_count_and_snapshot_id() -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators()
    claim = make_claim(state=LineupAttemptState.WRITING)
    work.decisions[DEFAULT_GAME_ID] = LineupClaimDecision(LineupClaimAction.RESUME, claim=claim)
    snapshots.verification = LineupSnapshotVerification(
        claim.attempt.snapshot_id,
        LineupVerificationStatus.EXACT,
        1,
        "f" * 64,
        LineupStatus.VALID,
        (),
    )
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,))
    )
    assert result.failed == 1 and work.poison_calls and "publish_snapshot" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [LineupPublicationState.ABSENT, LineupPublicationState.STALE])
async def test_verify_missing_or_stale_does_not_claim_or_read_clickhouse(
    state: LineupPublicationState,
) -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    work.resolutions[DEFAULT_GAME_ID] = [CurrentLineupSnapshotResolution(state)]
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,), mode=LineupBuildMode.VERIFY)
    )
    assert result.failed == 1 and work.claim_calls == [] and snapshots.verify_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_verify_resolves_before_and_after_exact_readback_and_counts_invalid(
    invalid: bool,
) -> None:
    events, work, snapshots, normalizer, reconstructor = collaborators()
    resolution = make_resolution(make_claim(state=LineupAttemptState.PUBLISHED, invalid=invalid))
    work.resolutions[DEFAULT_GAME_ID] = [resolution, resolution]
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,), mode=LineupBuildMode.VERIFY)
    )
    assert (result.verified, result.invalid) == (1, int(invalid))
    assert events == [
        "start_run",
        "select_games",
        "resolve_current",
        "verify_snapshot",
        "resolve_current",
        "finish_run",
    ]
    assert work.claim_calls == []


@pytest.mark.asyncio
async def test_verify_pointer_changes_after_readback_is_stale() -> None:
    _, work, snapshots, normalizer, reconstructor = collaborators()
    resolution = make_resolution(make_claim(state=LineupAttemptState.PUBLISHED))
    work.resolutions[DEFAULT_GAME_ID] = [
        resolution,
        CurrentLineupSnapshotResolution(LineupPublicationState.STALE),
    ]
    result = await make_service(work, snapshots, normalizer, reconstructor).build(
        BuildLineupsRequest((SEASON,), mode=LineupBuildMode.VERIFY)
    )
    assert result.failed == 1 and result.verified == 0
