"""Happy-path and mode behavior for possession build orchestration."""

from __future__ import annotations

import nba_live_totals.application.possessions as possession_application
from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    BuildStatus,
    ClaimAction,
    ClaimDecision,
    PossessionAttemptState,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import GameId, SeasonId

from .possession_fakes import (
    VERSIONS,
    FakeNormalizer,
    FakeParser,
    FakeSnapshotStore,
    FakeWorkStore,
    make_claim,
    make_reference,
)

SEASON = SeasonId("2025-26")
GAME_1 = GameId("0022500001")
GAME_2 = GameId("0022500002")
GAME_3 = GameId("0022500003")


def make_service(
    work_store: FakeWorkStore,
    snapshot_store: FakeSnapshotStore,
    normalizer: FakeNormalizer,
    parser: FakeParser,
    *,
    max_concurrency: int = 2,
) -> object:
    service_type = getattr(possession_application, "PossessionBuildService", None)
    assert service_type is not None, "Task 15 service has not been implemented"
    return service_type(
        normalizer=normalizer,
        parser=parser,
        work_store=work_store,
        snapshot_store=snapshot_store,
        versions=VERSIONS,
        max_concurrency=max_concurrency,
    )


async def test_materialize_preserves_exact_per_game_lifecycle_and_counters() -> None:
    events: list[str] = []
    work_store = FakeWorkStore((GAME_1,), events)
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert events == [
        "start_run",
        "select_games",
        f"claim:{GAME_1.value}",
        f"normalize:{GAME_1.value}",
        f"parse:{GAME_1.value}",
        f"mark_writing:{GAME_1.value}",
        f"write_snapshot:{GAME_1.value}",
        f"verify_snapshot:{GAME_1.value}",
        f"mark_verified:{GAME_1.value}",
        f"publish_snapshot:{GAME_1.value}",
        "finish_run",
    ]
    assert result.status is BuildStatus.SUCCEEDED
    assert (
        result.discovered,
        result.materialized,
        result.skipped_current,
        result.verified,
        result.failed,
        result.busy,
    ) == (1, 1, 0, 0, 0, 0)
    written = snapshot_store.write_calls[0].attempt
    assert written.state is PossessionAttemptState.WRITING
    assert written.expected_row_count == 1
    assert written.expected_output_sha256 is not None


async def test_materialize_sorts_selection_and_never_exceeds_concurrency_bound() -> None:
    events: list[str] = []
    entered: list[GameId] = []
    active = 0
    maximum = 0

    class BlockingClaims(FakeWorkStore):
        async def claim_game(self, *args: object, **kwargs: object) -> ClaimDecision:
            nonlocal active, maximum
            decision = await super().claim_game(*args, **kwargs)  # type: ignore[arg-type]
            game_id = args[1]
            assert isinstance(game_id, GameId)
            entered.append(game_id)
            active += 1
            maximum = max(maximum, active)
            while len(entered) < 2:
                await __import__("asyncio").sleep(0)
            active -= 1
            return decision

    work_store = BlockingClaims((GAME_3, GAME_1, GAME_2), events)
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
        max_concurrency=2,
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert entered == [GAME_1, GAME_2, GAME_3]
    assert maximum == 2
    assert result.materialized == 3


async def test_skip_busy_and_invalid_parse_are_accounted_without_clickhouse() -> None:
    events: list[str] = []
    current_claim = make_claim(
        GAME_1,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    decisions = {
        GAME_1: ClaimDecision(ClaimAction.SKIP_CURRENT, current=make_reference(current_claim)),
        GAME_2: ClaimDecision(ClaimAction.BUSY),
        GAME_3: ClaimDecision(ClaimAction.BUILD, claim=make_claim(GAME_3)),
    }
    work_store = FakeWorkStore((GAME_1, GAME_2, GAME_3), events, decisions=decisions)
    snapshot_store = FakeSnapshotStore(events)
    parser = FakeParser(events, invalid_games={GAME_3})
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        parser,
        max_concurrency=1,
    )

    result = await service.build(BuildPossessionsRequest((SEASON,)))  # type: ignore[attr-defined]

    assert result.status is BuildStatus.PARTIAL
    assert (result.skipped_current, result.busy, result.failed) == (1, 1, 1)
    assert snapshot_store.write_calls == []
    assert snapshot_store.verify_calls == []
    assert [(error_code, error_type) for _, error_code, error_type in work_store.fail_calls] == [
        ("invalid_parse", "parse")
    ]


async def test_rebuild_forwards_force_and_uses_new_attempt_with_same_content_identity() -> None:
    events: list[str] = []
    previous = make_claim(GAME_1, generation=1)
    replacement = make_claim(GAME_1, generation=2)
    assert replacement.attempt.snapshot_id != previous.attempt.snapshot_id
    assert replacement.attempt.parse_revision_id == previous.attempt.parse_revision_id
    work_store = FakeWorkStore(
        (GAME_1,),
        events,
        decisions={GAME_1: ClaimDecision(ClaimAction.BUILD, claim=replacement)},
    )
    snapshot_store = FakeSnapshotStore(events)
    service = make_service(
        work_store,
        snapshot_store,
        FakeNormalizer(events),
        FakeParser(events),
    )

    result = await service.build(BuildPossessionsRequest((SEASON,), rebuild=True))  # type: ignore[attr-defined]

    assert work_store.claim_calls == [(GAME_1, True)]
    assert snapshot_store.write_calls[0].attempt.snapshot_id == replacement.attempt.snapshot_id
    assert result.materialized == 1


async def test_verify_reads_exact_current_once_between_two_resolutions() -> None:
    events: list[str] = []
    claim = make_claim(
        GAME_1,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    reference = make_reference(claim)
    work_store = FakeWorkStore((GAME_1,), events)
    work_store.resolve_sequences[GAME_1] = [reference, reference]
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.verifications[reference.attempt.snapshot_id] = SnapshotVerification(
        reference.attempt.snapshot_id,
        VerificationStatus.EXACT,
        1,
        "b" * 64,
    )
    normalizer = FakeNormalizer(events)
    parser = FakeParser(events)
    service = make_service(work_store, snapshot_store, normalizer, parser)

    result = await service.build(  # type: ignore[attr-defined]
        BuildPossessionsRequest((SEASON,), mode=BuildMode.VERIFY)
    )

    assert events == [
        "start_run",
        "select_games",
        f"resolve_current:{GAME_1.value}",
        f"verify_snapshot:{GAME_1.value}",
        f"resolve_current:{GAME_1.value}",
        "finish_run",
    ]
    assert result.status is BuildStatus.SUCCEEDED
    assert result.verified == 1
    assert normalizer.calls == []
    assert parser.calls == []
    assert work_store.claim_calls == []
    assert snapshot_store.write_calls == []


async def test_verify_current_swap_is_stale_failure_without_mutation() -> None:
    events: list[str] = []
    first_claim = make_claim(
        GAME_1,
        generation=1,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    second_claim = make_claim(
        GAME_1,
        generation=2,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    first = make_reference(first_claim)
    second = make_reference(second_claim)
    work_store = FakeWorkStore((GAME_1,), events)
    work_store.resolve_sequences[GAME_1] = [first, second]
    snapshot_store = FakeSnapshotStore(events)
    snapshot_store.verifications[first.attempt.snapshot_id] = SnapshotVerification(
        first.attempt.snapshot_id,
        VerificationStatus.EXACT,
        1,
        "b" * 64,
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
    assert (result.verified, result.failed) == (0, 1)
    assert not any(
        event.startswith(("claim:", "mark_", "fail_game:", "poison_attempt:", "publish:"))
        for event in events
    )
