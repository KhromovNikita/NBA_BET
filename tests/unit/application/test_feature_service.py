"""Sequential orchestration contracts for feature snapshot builds."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureBuildMode,
    FeatureBuildService,
    FeatureBuildStatus,
    FeatureClaimAction,
    FeatureClaimDecision,
    FeatureInputToken,
    FeaturePublicationState,
    FeatureSnapshotAttempt,
    FeatureSnapshotVerification,
    FeatureVerificationStatus,
)
from nba_live_totals.application.features.models import (
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeatureTargetRows,
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
    FeatureVersions,
    GameStateFeature,
    NormalizedFeatureEvents,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.application.feature_fakes import (
    HISTORY_GAME_ID,
    NOW,
    make_possession_reference,
    make_source,
    make_target,
    uuid4_value,
)


def make_dataset() -> FeatureDataset:
    source = make_source()
    ids = source.target_player_ids
    state = GameStateFeature(
        source.target.game.game_id,
        0,
        FeatureStateKind.PREGAME,
        0,
        EventBoundary(1, BoundarySide.BEFORE),
        home_player_ids=ids[:5],
        away_player_ids=ids[5:10],
        seconds_remaining_game_ms=2_880_000,
        seconds_remaining_period_ms=720_000,
    )
    return FeatureDataset(source.target.game.game_id, (state,), ())


def make_claim(state: FeatureAttemptState = FeatureAttemptState.ALLOCATED) -> FeatureBuildClaim:
    source = make_source()
    dataset = make_dataset()
    token = FeatureInputToken.from_source(source, FeatureVersions())
    populated = state is not FeatureAttemptState.ALLOCATED
    verified = state in {FeatureAttemptState.VERIFIED, FeatureAttemptState.PUBLISHED}
    attempt = FeatureSnapshotAttempt(
        source.target.game,
        uuid4_value(400),
        1,
        token.feature_revision_id,
        token,
        state,
        uuid4_value(401),
        1,
        NOW,
        expected_state_row_count=len(dataset.states) if populated else None,
        expected_state_output_sha256=state_output_checksum(dataset.states) if populated else None,
        expected_player_row_count=len(dataset.players) if populated else None,
        expected_player_output_sha256=player_output_checksum(dataset.players)
        if populated
        else None,
        expected_feature_output_sha256=feature_output_checksum(dataset) if populated else None,
        expected_issue_codes=dataset.issues if populated else None,
        result_status=dataset.status if populated else None,
        insert_started_at=NOW if populated else None,
        verified_at=NOW if verified else None,
        published_at=NOW if state is FeatureAttemptState.PUBLISHED else None,
    )
    return FeatureBuildClaim(attempt.created_by_run_id, 1, attempt, source)


def exact(attempt: FeatureSnapshotAttempt) -> FeatureSnapshotVerification:
    return FeatureSnapshotVerification(
        attempt.snapshot_id,
        FeatureVerificationStatus.EXACT,
        attempt.expected_state_row_count,
        attempt.expected_state_output_sha256,
        attempt.expected_player_row_count,
        attempt.expected_player_output_sha256,
        attempt.expected_feature_output_sha256,
        attempt.result_status,
        attempt.expected_issue_codes,
    )


class Normalizer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def normalize(self, snapshot: Any) -> NormalizedFeatureEvents:
        self.calls.append("normalize")
        return NormalizedFeatureEvents(snapshot.game.game_id, ())


class Calculator:
    def __init__(self, calls: list[str], dataset: FeatureDataset) -> None:
        self.calls = calls
        self.dataset = dataset

    def calculate(self, inputs: Any, events: Any) -> FeatureDataset:
        self.calls.append("calculate")
        return self.dataset


class SourceReader:
    def __init__(
        self,
        calls: list[str],
        *,
        cancel_inputs: bool = False,
        fail_target: bool = False,
    ) -> None:
        self.calls = calls
        self.cancel_inputs = cancel_inputs
        self.fail_target = fail_target

    async def read_target(self, target: Any) -> FeatureTargetRows:
        self.calls.append("read_target")
        if self.fail_target:
            raise ValueError("unsafe payload text")
        _, possessions, segments = make_target()
        return FeatureTargetRows(target, possessions, segments)

    async def read_inputs(self, claim: FeatureBuildClaim) -> FeatureCalculationInput:
        self.calls.append("read_inputs")
        if self.cancel_inputs:
            raise asyncio.CancelledError
        _, possessions, segments = make_target()
        _, _, history = make_possession_reference(HISTORY_GAME_ID)
        return FeatureCalculationInput(
            claim.attempt.game,
            possessions,
            segments,
            claim.source.historical_team_boxes,
            claim.source.historical_player_boxes,
            history,
        )


class SnapshotStore:
    def __init__(
        self,
        calls: list[str],
        status: FeatureVerificationStatus = FeatureVerificationStatus.EXACT,
        *,
        fail_write: bool = False,
    ) -> None:
        self.calls = calls
        self.status = status
        self.fail_write = fail_write

    async def write_snapshot(self, claim: FeatureBuildClaim, dataset: FeatureDataset) -> None:
        self.calls.append("write")
        if self.fail_write:
            raise RuntimeError("lost response")

    async def verify_snapshot(self, attempt: FeatureSnapshotAttempt) -> FeatureSnapshotVerification:
        self.calls.append("verify_snapshot")
        return (
            exact(attempt)
            if self.status is FeatureVerificationStatus.EXACT
            else FeatureSnapshotVerification(attempt.snapshot_id, self.status)
        )

    async def read_snapshot(self, reference: CurrentFeatureSnapshotReference) -> FeatureDataset:
        raise AssertionError("service verification must use verify_snapshot")


class WorkStore:
    def __init__(
        self,
        calls: list[str],
        claim: FeatureBuildClaim,
        *,
        action: FeatureClaimAction = FeatureClaimAction.BUILD,
        selected: tuple[Any, ...] | None = None,
    ) -> None:
        self.calls = calls
        self.claim = claim
        self.action = action
        self.selected = selected
        self.finished: list[Any] = []
        self.poisoned = 0
        self.failed = 0
        self.unclaimed = 0

    async def start_run(self, request: BuildDatasetRequest) -> UUID:
        self.calls.append("start_run")
        return self.claim.run_id

    async def select_games(self, request: BuildDatasetRequest) -> tuple[Any, ...]:
        self.calls.append("select_games")
        return self.selected or (self.claim.attempt.game_id,)

    async def resolve_target(self, game_id: Any) -> Any:
        self.calls.append("resolve_target")
        return self.claim.source.target

    async def claim_game(self, *args: Any, **kwargs: Any) -> FeatureClaimDecision:
        self.calls.append("claim_game")
        if self.action is FeatureClaimAction.SKIP_CURRENT:
            return FeatureClaimDecision(self.action, current=self.current())
        if self.action is FeatureClaimAction.BUSY:
            return FeatureClaimDecision(self.action)
        return FeatureClaimDecision(self.action, claim=self.claim)

    async def mark_writing(self, claim: FeatureBuildClaim, **values: Any) -> FeatureSnapshotAttempt:
        self.calls.append("mark_writing")
        self.claim = replace(
            claim,
            attempt=replace(
                claim.attempt,
                state=FeatureAttemptState.WRITING,
                insert_started_at=NOW,
                **values,
            ),
        )
        return self.claim.attempt

    async def mark_verified(
        self, claim: FeatureBuildClaim, verification: Any
    ) -> FeatureSnapshotAttempt:
        self.calls.append("mark_verified")
        self.claim = replace(
            claim,
            attempt=replace(claim.attempt, state=FeatureAttemptState.VERIFIED, verified_at=NOW),
        )
        return self.claim.attempt

    async def publish_snapshot(self, claim: FeatureBuildClaim) -> CurrentFeatureSnapshotReference:
        self.calls.append("publish_snapshot")
        self.claim = replace(
            claim,
            attempt=replace(claim.attempt, state=FeatureAttemptState.PUBLISHED, published_at=NOW),
        )
        return self.current()

    def current(self) -> CurrentFeatureSnapshotReference:
        attempt = self.claim.attempt
        if attempt.state is not FeatureAttemptState.PUBLISHED:
            attempt = replace(
                attempt,
                state=FeatureAttemptState.PUBLISHED,
                verified_at=attempt.verified_at or NOW,
                published_at=NOW,
            )
        assert attempt.expected_state_row_count is not None
        assert attempt.expected_state_output_sha256 is not None
        assert attempt.expected_player_row_count is not None
        assert attempt.expected_player_output_sha256 is not None
        assert attempt.expected_feature_output_sha256 is not None
        return CurrentFeatureSnapshotReference(
            attempt,
            attempt.expected_state_row_count,
            attempt.expected_state_output_sha256,
            attempt.expected_player_row_count,
            attempt.expected_player_output_sha256,
            attempt.expected_feature_output_sha256,
        )

    async def poison_attempt(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("poison_attempt")
        self.poisoned += 1

    async def fail_game(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("fail_game")
        self.failed += 1

    async def record_unclaimed_failure(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("record_unclaimed_failure")
        self.unclaimed += 1

    async def resolve_current(self, *args: Any, **kwargs: Any) -> CurrentFeatureSnapshotResolution:
        self.calls.append("resolve_current")
        current = self.current()
        return CurrentFeatureSnapshotResolution(
            FeaturePublicationState.INVALID
            if current.result_status.value == "invalid"
            else FeaturePublicationState.CURRENT,
            current,
        )

    async def finish_run(self, run_id: UUID, result: Any) -> None:
        self.calls.append("finish_run")
        self.finished.append(result)


def service(
    calls: list[str],
    work: WorkStore,
    snapshots: SnapshotStore,
    *,
    cancel_inputs: bool = False,
    fail_target: bool = False,
) -> FeatureBuildService:
    return FeatureBuildService(
        normalizer=Normalizer(calls),
        source_reader=SourceReader(calls, cancel_inputs=cancel_inputs, fail_target=fail_target),
        calculator=Calculator(calls, make_dataset()),
        work_store=work,
        snapshot_store=snapshots,
        versions=FeatureVersions(),
    )


async def test_fresh_build_has_exact_sequential_stage_order() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim())
    result = await service(calls, work, SnapshotStore(calls)).build(
        BuildDatasetRequest((work.claim.attempt.game.season,))
    )

    assert result.status is FeatureBuildStatus.SUCCEEDED
    assert result.materialized == 1
    assert calls == [
        "start_run",
        "select_games",
        "resolve_target",
        "read_target",
        "claim_game",
        "read_inputs",
        "normalize",
        "calculate",
        "mark_writing",
        "write",
        "verify_snapshot",
        "mark_verified",
        "publish_snapshot",
        "finish_run",
    ]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (FeatureAttemptState.WRITING, ["verify_snapshot", "mark_verified", "publish_snapshot"]),
        (FeatureAttemptState.VERIFIED, ["publish_snapshot"]),
    ],
)
async def test_resume_never_recalculates_or_rewrites(
    state: FeatureAttemptState, expected: list[str]
) -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim(state), action=FeatureClaimAction.RESUME)
    await service(calls, work, SnapshotStore(calls)).build(
        BuildDatasetRequest((work.claim.attempt.game.season,))
    )
    assert calls[5:-1] == expected


@pytest.mark.parametrize("action", [FeatureClaimAction.SKIP_CURRENT, FeatureClaimAction.BUSY])
async def test_skip_and_busy_stop_after_preliminary_target_read(action: FeatureClaimAction) -> None:
    calls: list[str] = []
    state = (
        FeatureAttemptState.PUBLISHED
        if action is FeatureClaimAction.SKIP_CURRENT
        else FeatureAttemptState.ALLOCATED
    )
    work = WorkStore(calls, make_claim(state), action=action)
    await service(calls, work, SnapshotStore(calls)).build(
        BuildDatasetRequest((work.claim.attempt.game.season,))
    )
    assert not {"read_inputs", "normalize", "calculate", "write"}.intersection(calls)


async def test_partial_readback_poisoned_and_never_rewritten() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim())
    result = await service(
        calls, work, SnapshotStore(calls, FeatureVerificationStatus.DIFFERENT)
    ).build(BuildDatasetRequest((work.claim.attempt.game.season,)))
    assert result.failed == 1
    assert work.poisoned == 1
    assert calls.count("write") == 1


async def test_lost_write_response_is_verified_without_second_write() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim())
    result = await service(calls, work, SnapshotStore(calls, fail_write=True)).build(
        BuildDatasetRequest((work.claim.attempt.game.season,))
    )
    assert result.materialized == 1
    assert calls.count("write") == 1


async def test_target_read_failure_is_recorded_without_allocation() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim())
    result = await service(calls, work, SnapshotStore(calls), fail_target=True).build(
        BuildDatasetRequest((work.claim.attempt.game.season,))
    )
    assert result.failed == 1
    assert work.unclaimed == 1
    assert "claim_game" not in calls


async def test_duplicate_selection_is_rejected_without_processing_a_game() -> None:
    calls: list[str] = []
    claim = make_claim()
    work = WorkStore(
        calls,
        claim,
        selected=(claim.attempt.game_id, claim.attempt.game_id),
    )
    with pytest.raises(ValueError, match="duplicates"):
        await service(calls, work, SnapshotStore(calls)).build(
            BuildDatasetRequest((claim.attempt.game.season,))
        )
    assert calls == ["start_run", "select_games", "finish_run"]
    assert work.finished[-1].status is FeatureBuildStatus.INTERRUPTED


async def test_verify_mode_is_read_only_and_rechecks_resolution() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim(FeatureAttemptState.PUBLISHED))
    result = await service(calls, work, SnapshotStore(calls)).build(
        BuildDatasetRequest((work.claim.attempt.game.season,), mode=FeatureBuildMode.VERIFY)
    )
    assert result.verified == 1
    assert calls.count("resolve_current") == 2
    assert not {"claim_game", "mark_writing", "write", "publish_snapshot"}.intersection(calls)


async def test_cancellation_is_primary_and_finishes_interrupted_without_next_game() -> None:
    calls: list[str] = []
    work = WorkStore(calls, make_claim())
    with pytest.raises(asyncio.CancelledError):
        await service(calls, work, SnapshotStore(calls), cancel_inputs=True).build(
            BuildDatasetRequest((work.claim.attempt.game.season,))
        )
    assert work.failed == 1
    assert work.finished[-1].status is FeatureBuildStatus.INTERRUPTED
    assert calls[-1] == "finish_run"
