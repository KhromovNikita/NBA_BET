"""Crash recovery, fencing, cancellation and safe-log behavior."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableSequence
from uuid import UUID

import pytest

from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    BuildPossessionsResult,
    BuildStatus,
    ClaimAction,
    ClaimDecision,
    CurrentSnapshotReference,
    PossessionAttemptState,
    PossessionBuildClaim,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import (
    GameId,
    NormalizedGameActions,
    PossessionParseContext,
    PossessionParseResult,
    SeasonId,
)

from .possession_fakes import (
    RUN_ID,
    FakeNormalizer,
    FakeParser,
    FakeSnapshotStore,
    FakeWorkStore,
    make_claim,
    make_reference,
)
from .test_possession_service import make_service

SEASON = SeasonId("2025-26")
GAME_1 = GameId("0022500001")
GAME_2 = GameId("0022500002")


async def test_response_loss_after_insert_uses_only_exact_readback_and_publishes() -> None:
    events: list[str] = []
    claim = make_claim(GAME_1)
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.BUILD, claim=claim)},
    )
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.write_errors[claim.attempt.snapshot_id] = TimeoutError(
        "secret ClickHouse URL and response"
    )
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.SUCCEEDED
    assert len(snapshot_store.write_calls) == 1
    assert len(snapshot_store.verify_calls) == 1
    assert events.count(f"write_snapshot:{GAME_1.value}") == 1
    assert f"publish_snapshot:{GAME_1.value}" in events


async def test_resume_writing_never_inserts_and_exact_readback_publishes() -> None:
    events: list[str] = []
    claim = make_claim(GAME_1, state=PossessionAttemptState.WRITING)
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.RESUME, claim=claim)},
    )
    snapshot_store = FakeSnapshotStore(events)
    normalizer = FakeNormalizer(events)
    parser = FakeParser(events)
    service = make_service(work_store, snapshot_store, normalizer, parser)

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.materialized == 1
    assert snapshot_store.write_calls == []
    assert len(snapshot_store.verify_calls) == 1
    assert normalizer.calls == []
    assert parser.calls == []
    assert f"publish_snapshot:{GAME_1.value}" in events


async def test_resume_verified_uses_no_clickhouse_before_fenced_publish() -> None:
    events: list[str] = []
    claim = make_claim(GAME_1, state=PossessionAttemptState.VERIFIED)
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.RESUME, claim=claim)},
    )
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.materialized == 1
    assert snapshot_store.write_calls == []
    assert snapshot_store.verify_calls == []
    assert events == [
        "start_run",
        "select_games",
        f"claim:{GAME_1.value}",
        f"publish_snapshot:{GAME_1.value}",
        "finish_run",
    ]


@pytest.mark.parametrize("status", [VerificationStatus.MISSING, VerificationStatus.MISMATCH])
async def test_non_exact_readback_poisons_without_publication(
    status: VerificationStatus,
) -> None:
    events: list[str] = []
    claim = make_claim(GAME_1, state=PossessionAttemptState.WRITING)
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.RESUME, claim=claim)},
    )
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.verifications[claim.attempt.snapshot_id] = SnapshotVerification(
        claim.attempt.snapshot_id,
        status,
        0 if status is VerificationStatus.MISSING else 1,
        None if status is VerificationStatus.MISSING else "c" * 64,
    )
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.FAILED
    assert result.failed == 1
    assert len(work_store.poison_calls) == 1
    assert not any(item.startswith("publish_snapshot:") for item in events)


async def test_unavailable_readback_leaves_writing_attempt_for_recovery() -> None:
    events: list[str] = []
    claim = make_claim(GAME_1, state=PossessionAttemptState.WRITING)
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.RESUME, claim=claim)},
    )
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.verifications[claim.attempt.snapshot_id] = SnapshotVerification(
        claim.attempt.snapshot_id,
        VerificationStatus.UNAVAILABLE,
    )
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.FAILED
    assert work_store.poison_calls == []
    assert not any(item.startswith("publish_snapshot:") for item in events)


async def test_one_game_failure_does_not_prevent_remaining_game() -> None:
    events: list[str] = []

    class OneClaimFails(FakeWorkStore):
        async def claim_game(self, *args: object, **kwargs: object) -> ClaimDecision:
            game_id = args[1]
            assert isinstance(game_id, GameId)
            if game_id == GAME_1:
                self.events.append(f"claim:{game_id.value}")
                raise RuntimeError("postgresql://user:secret@host/db")
            return await super().claim_game(*args, **kwargs)  # type: ignore[arg-type]

    work_store = OneClaimFails((GAME_1, GAME_2), events)
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
        max_concurrency=1,
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.PARTIAL
    assert (result.materialized, result.failed) == (1, 1)
    assert f"publish_snapshot:{GAME_2.value}" in events


async def test_stale_publication_is_isolated_and_reported_without_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    class FirstPublishIsStale(FakeWorkStore):
        async def publish_snapshot(self, claim: PossessionBuildClaim) -> CurrentSnapshotReference:
            game_id = claim.attempt.game_id
            self.events.append(f"publish_snapshot:{game_id.value}")
            if game_id == GAME_1:
                raise RuntimeError("stale input token with postgresql://secret")
            return make_reference(claim)

    work_store = FirstPublishIsStale((GAME_1, GAME_2), events)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeParser(events),
        max_concurrency=1,
    )

    with caplog.at_level(logging.INFO, logger="nba_live_totals.application.possessions.service"):
        result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.PARTIAL
    assert (result.materialized, result.failed) == (1, 1)
    failed = next(record for record in caplog.records if record.game_id == GAME_1.value)  # type: ignore[attr-defined]
    assert failed.error_code == "publication_failed"  # type: ignore[attr-defined]
    assert "postgresql://secret" not in str(failed.__dict__)


async def test_parse_failure_cleanup_does_not_replace_primary_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    class PrimaryParseError(RuntimeError):
        pass

    class CleanupError(RuntimeError):
        pass

    class RaisingParser(FakeParser):
        def parse(
            self,
            context: PossessionParseContext,
            normalized: NormalizedGameActions,
        ) -> PossessionParseResult:
            del context, normalized
            raise PrimaryParseError("primary contains secret")

    class RaisingCleanup(FakeWorkStore):
        fail_attempts = 0

        async def fail_game(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.fail_attempts += 1
            raise CleanupError("cleanup contains another secret")

    work_store = RaisingCleanup((GAME_1,), events)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        RaisingParser(events),
    )

    with caplog.at_level(logging.INFO, logger="nba_live_totals.application.possessions.service"):
        result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.FAILED
    assert work_store.fail_attempts == 1
    terminal = next(record for record in caplog.records if record.msg == "possession_game_finished")
    assert terminal.error_type == "PrimaryParseError"  # type: ignore[attr-defined]
    assert "contains secret" not in str(terminal.__dict__)


async def test_invalid_result_cleanup_is_attempted_only_once() -> None:
    events: list[str] = []

    class RaisingCleanup(FakeWorkStore):
        fail_attempts = 0

        async def fail_game(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.fail_attempts += 1
            raise RuntimeError("cleanup failed")

    work_store = RaisingCleanup((GAME_1,), events)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeParser(events, invalid_games={GAME_1}),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.FAILED
    assert work_store.fail_attempts == 1


async def test_verify_terminal_event_keeps_run_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    claim = make_claim(
        GAME_1,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    current = make_reference(claim)
    work_store = FakeWorkStore((GAME_1,), events)
    work_store.resolve_sequences[GAME_1] = [current, current]
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    with caplog.at_level(logging.INFO, logger="nba_live_totals.application.possessions.service"):
        await service.build(  # type: ignore[attr-defined]
            BuildPossessionsRequest((SEASON,), mode=BuildMode.VERIFY)
        )

    terminal = next(record for record in caplog.records if record.msg == "possession_game_finished")
    assert terminal.run_id == str(RUN_ID)  # type: ignore[attr-defined]
    assert terminal.source_document_id == str(  # type: ignore[attr-defined]
        current.attempt.input_token.applied_pbp_document_id
    )


async def test_verify_readback_error_still_resolves_current_after_the_read() -> None:
    events: list[str] = []
    claim = make_claim(
        GAME_1,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    current = make_reference(claim)
    work_store = FakeWorkStore((GAME_1,), events)
    work_store.resolve_sequences[GAME_1] = [current, current]
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.verify_errors[current.attempt.snapshot_id] = TimeoutError(
        "raw ClickHouse response"
    )
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(  # type: ignore[attr-defined]
        BuildPossessionsRequest((SEASON,), mode=BuildMode.VERIFY)
    )

    assert result.status is BuildStatus.FAILED
    assert events == [
        "start_run",
        "select_games",
        f"resolve_current:{GAME_1.value}",
        f"verify_snapshot:{GAME_1.value}",
        f"resolve_current:{GAME_1.value}",
        "finish_run",
    ]


async def test_cancellation_stops_new_claims_and_cleanup_failure_cannot_mask_it() -> None:
    events: list[str] = []
    claim_started = asyncio.Event()

    class BlockingClaimAndFailingCleanup(FakeWorkStore):
        async def claim_game(self, *args: object, **kwargs: object) -> ClaimDecision:
            game_id = args[1]
            assert isinstance(game_id, GameId)
            self.events.append(f"claim:{game_id.value}")
            self.claim_calls.append((game_id, bool(kwargs["rebuild"])))
            claim_started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def finish_run(self, run_id: object, result: object) -> None:
            self.events.append("finish_run")
            self.finished.append(result)  # type: ignore[arg-type]
            raise RuntimeError("cleanup secret must not replace cancellation")

    work_store = BlockingClaimAndFailingCleanup((GAME_1, GAME_2), events)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeParser(events),
        max_concurrency=1,
    )
    task = asyncio.create_task(service.build(BuildPossessionsRequest((SEASON,))))  # type: ignore[attr-defined]
    await claim_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert work_store.claim_calls == [(GAME_1, False)]
    assert work_store.finished[0].status is BuildStatus.INTERRUPTED
    assert events[-1] == "finish_run"


@pytest.mark.parametrize(
    ("configured_actions", "expected_status"),
    [
        ((ClaimAction.SKIP_CURRENT,), BuildStatus.SUCCEEDED),
        ((ClaimAction.SKIP_CURRENT, ClaimAction.BUSY), BuildStatus.PARTIAL),
        ((ClaimAction.BUSY,), BuildStatus.FAILED),
    ],
)
async def test_cancellation_during_ordinary_finish_attempts_interrupted_finalization(
    configured_actions: tuple[ClaimAction, ...],
    expected_status: BuildStatus,
) -> None:
    events: list[str] = []
    game_ids = (GAME_1, GAME_2)[: len(configured_actions)]
    decisions: dict[GameId, ClaimDecision] = {}
    for game_id, action in zip(game_ids, configured_actions, strict=True):
        if action is ClaimAction.SKIP_CURRENT:
            published = make_claim(
                game_id,
                state=PossessionAttemptState.PUBLISHED,
                expected_row_count=1,
                expected_output_sha256="b" * 64,
            )
            decisions[game_id] = ClaimDecision(
                action,
                current=make_reference(published),
            )
        else:
            decisions[game_id] = ClaimDecision(action)
    ordinary_finish_started = asyncio.Event()

    class BlockingOrdinaryFinish(FakeWorkStore):
        async def finish_run(self, run_id: UUID, result: BuildPossessionsResult) -> None:
            del run_id
            self.events.append(f"finish_run:{result.status.value}")
            self.finished.append(result)
            if result.status is not BuildStatus.INTERRUPTED:
                ordinary_finish_started.set()
                await asyncio.Future()

    work_store = BlockingOrdinaryFinish(game_ids, events, decisions=decisions)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeParser(events),
        max_concurrency=1,
    )
    task = asyncio.create_task(service.build(BuildPossessionsRequest((SEASON,))))  # type: ignore[attr-defined]
    await ordinary_finish_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [result.status for result in work_store.finished] == [
        expected_status,
        BuildStatus.INTERRUPTED,
    ]


async def test_cancelled_cleanup_preserves_primary_error_and_logs_once_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    class PrimarySelectionError(RuntimeError):
        pass

    class CancelledCleanup(FakeWorkStore):
        finish_calls = 0

        async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]:
            del request
            raise PrimarySelectionError("primary DSN=postgresql://user:secret@host/db")

        async def finish_run(self, run_id: UUID, result: BuildPossessionsResult) -> None:
            del run_id, result
            self.finish_calls += 1
            raise asyncio.CancelledError("cleanup token=super-secret")

    work_store = CancelledCleanup((GAME_1,), events)
    service = make_service(
        work_store,
        FakeSnapshotStore(events),
        FakeNormalizer(events),
        FakeParser(events),
    )

    with (
        caplog.at_level(logging.INFO, logger="nba_live_totals.application.possessions.service"),
        pytest.raises(PrimarySelectionError),
    ):
        await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert work_store.finish_calls == 1
    cleanup_records = [
        record for record in caplog.records if record.msg == "possession_run_finish_failed"
    ]
    assert len(cleanup_records) == 1
    cleanup = cleanup_records[0]
    assert cleanup.error_type == "CancelledError"  # type: ignore[attr-defined]
    assert cleanup.error_code == "persistence_error"  # type: ignore[attr-defined]
    assert cleanup.exc_info is None
    rendered = str(cleanup.__dict__)
    assert "postgresql://user:secret" not in rendered
    assert "token=super-secret" not in rendered


async def test_service_emits_one_bounded_terminal_event_without_raw_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: MutableSequence[str] = []
    work_store = FakeWorkStore((GAME_1,), events)
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.write_errors[make_claim(GAME_1).attempt.snapshot_id] = RuntimeError(
        "token=super-secret https://private/clickhouse"
    )
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    with caplog.at_level(logging.INFO, logger="nba_live_totals.application.possessions.service"):
        await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    terminal = [record for record in caplog.records if record.msg == "possession_game_finished"]
    assert len(terminal) == 1
    record = terminal[0]
    assert record.game_id == GAME_1.value  # type: ignore[attr-defined]
    assert record.status == "materialized"  # type: ignore[attr-defined]
    assert "error_code" not in record.__dict__
    assert "error_type" not in record.__dict__
    rendered = str(record.__dict__)
    for secret in ("sensitive provider description", "super-secret", "https://private"):
        assert secret not in rendered
