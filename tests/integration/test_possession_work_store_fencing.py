"""Real-PostgreSQL contracts for fenced possession attempt transitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.possessions import (
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
from nba_live_totals.domain import (
    GameId,
    IssueSeverity,
    PossessionIssue,
    PossessionVersions,
    SeasonId,
)
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
)
from tests import runtime_config
from tests.integration.possession_harness import BASE_TIME, PossessionHarness

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(runtime_config.postgres_url())
    yield value
    await value.dispose()


@pytest.fixture
async def harness(engine: AsyncEngine) -> AsyncIterator[PossessionHarness]:
    value = PossessionHarness(engine, source="task-12-fencing")
    try:
        yield value
    finally:
        await value.cleanup()


def versions() -> PossessionVersions:
    return PossessionVersions("nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1")


async def start_run(
    store: PostgresPossessionWorkStore,
    harness: PossessionHarness,
    *,
    mode: BuildMode = BuildMode.MATERIALIZE,
) -> UUID:
    run_id = await store.start_run(
        BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), mode=mode)
    )
    harness.run_ids.append(run_id)
    return run_id


async def claim(
    store: PostgresPossessionWorkStore, harness: PossessionHarness
) -> PossessionBuildClaim:
    decision = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=False
    )
    assert decision.claim is not None
    return decision.claim


async def row(harness: PossessionHarness, table: Any, where: Any) -> dict[str, object]:
    async with harness.engine.connect() as connection:
        return dict((await connection.execute(select(table).where(where))).mappings().one())


async def publish(
    store: PostgresPossessionWorkStore, claim_value: PossessionBuildClaim
) -> CurrentSnapshotReference:
    writing = await store.mark_writing(
        claim_value, expected_row_count=2, expected_output_sha256="a" * 64
    )
    assert writing.state is PossessionAttemptState.WRITING
    verified = await store.mark_verified(
        claim_value,
        SnapshotVerification(
            claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64
        ),
    )
    assert verified.state is PossessionAttemptState.VERIFIED
    return await store.publish_snapshot(claim_value)


async def test_lifecycle_persists_expected_output_before_publish_and_clears_exact_fence(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    claim_value = await claim(store, harness)

    writing = await store.mark_writing(
        claim_value, expected_row_count=2, expected_output_sha256="a" * 64
    )
    durable_writing = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == claim_value.attempt.snapshot_id,
    )
    assert writing.state is PossessionAttemptState.WRITING
    assert (
        durable_writing["state"],
        durable_writing["expected_row_count"],
        durable_writing["expected_output_sha256"],
        durable_writing["insert_started_at"],
    ) == ("writing", 2, "a" * 64, BASE_TIME)

    verified = await store.mark_verified(
        claim_value,
        SnapshotVerification(
            claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64
        ),
    )
    reference = await store.publish_snapshot(claim_value)
    resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    published = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == claim_value.attempt.snapshot_id,
    )
    assert verified.state is PossessionAttemptState.VERIFIED
    assert (reference.attempt.snapshot_id, reference.row_count, reference.output_sha256) == (
        claim_value.attempt.snapshot_id,
        2,
        "a" * 64,
    )
    assert (published["state"], published["verified_at"], published["published_at"]) == (
        "published",
        BASE_TIME,
        BASE_TIME,
    )
    assert (
        resource["publication_state"],
        resource["published_snapshot_id"],
        resource["published_parse_revision_id"],
        resource["published_input_token_sha256"],
        resource["active_run_id"],
        resource["active_claim_version"],
        resource["active_snapshot_id"],
        resource["claimed_until"],
    ) == (
        "current",
        claim_value.attempt.snapshot_id,
        claim_value.attempt.parse_revision_id,
        claim_value.attempt.input_token.sha256,
        None,
        None,
        None,
        None,
    )


async def test_verification_requires_the_exact_snapshot_count_and_hash_without_mutating_writing(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    claim_value = await claim(store, harness)
    await store.mark_writing(claim_value, expected_row_count=2, expected_output_sha256="a" * 64)

    for verification in (
        SnapshotVerification(
            claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 1, "a" * 64
        ),
        SnapshotVerification(
            claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 2, "b" * 64
        ),
        SnapshotVerification(claim_value.attempt.snapshot_id, VerificationStatus.MISSING),
    ):
        with pytest.raises(RuntimeError, match="verification"):
            await store.mark_verified(claim_value, verification)
        durable = await row(
            harness,
            possession_snapshot_attempts,
            possession_snapshot_attempts.c.snapshot_id == claim_value.attempt.snapshot_id,
        )
        assert (
            durable["state"],
            durable["expected_row_count"],
            durable["expected_output_sha256"],
        ) == (
            "writing",
            2,
            "a" * 64,
        )


async def test_poison_and_expiry_supersede_are_terminal_and_uncertain_writing_is_preserved(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    first = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, lease_seconds=1, utc_now=lambda: BASE_TIME
    )
    writing_claim = await claim(first, harness)
    await first.mark_writing(writing_claim, expected_row_count=2, expected_output_sha256="a" * 64)
    second = PostgresPossessionWorkStore(
        harness.engine,
        source=harness.source,
        lease_seconds=1,
        utc_now=lambda: BASE_TIME + timedelta(seconds=1),
    )
    replacement = await claim(second, harness)
    old = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == writing_claim.attempt.snapshot_id,
    )
    assert old["state"] == "superseded"
    assert replacement.attempt.snapshot_id != writing_claim.attempt.snapshot_id

    await second.poison_attempt(
        replacement, error_code="verification_mismatch", error_type="verification"
    )
    poisoned = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == replacement.attempt.snapshot_id,
    )
    resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    assert (
        poisoned["state"],
        poisoned["safe_error_code"],
        poisoned["safe_error_type"],
        poisoned["published_at"],
    ) == (
        "poisoned",
        "verification_mismatch",
        "verification",
        None,
    )
    assert (
        resource["active_run_id"],
        resource["active_snapshot_id"],
        resource["publication_state"],
    ) == (None, None, "invalid")


async def test_live_uncertain_writing_resumes_without_speculative_poison_or_lease_clear(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    run_id = await start_run(store, harness)
    first = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)
    assert first.claim is not None
    await store.mark_writing(first.claim, expected_row_count=2, expected_output_sha256="a" * 64)

    resumed = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)
    durable = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == first.claim.attempt.snapshot_id,
    )
    resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    assert resumed.claim is not None
    assert (resumed.claim.attempt.state, durable["state"], resource["active_snapshot_id"]) == (
        PossessionAttemptState.WRITING,
        "writing",
        first.claim.attempt.snapshot_id,
    )


@pytest.mark.parametrize("change", ["pbp", "game"])
async def test_publish_rejects_a_candidate_when_any_current_input_component_changed(
    harness: PossessionHarness, change: str
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    candidate = await claim(store, harness)
    await store.mark_writing(candidate, expected_row_count=2, expected_output_sha256="a" * 64)
    await store.mark_verified(
        candidate,
        SnapshotVerification(candidate.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64),
    )
    if change == "pbp":
        await harness.replace_applied_pbp_document(
            "0022500111", payload={"game_id": "0022500111", "raw": "v2"}, endpoint="playbyplayv3-v2"
        )
    else:
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(games).where(games.c.game_id == "0022500111").values(home_score=111)
            )

    with pytest.raises(RuntimeError, match="stale"):
        await store.publish_snapshot(candidate)
    resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    attempt = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    assert (
        attempt["state"],
        resource["publication_state"],
        resource["published_snapshot_id"],
        resource["active_snapshot_id"],
    ) == (
        "superseded",
        "invalid",
        None,
        None,
    )


async def test_failed_candidate_preserves_only_a_fresh_old_publication(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    old_claim = await claim(store, harness)
    old = await publish(store, old_claim)
    fresh_candidate = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=True
    )
    assert fresh_candidate.claim is not None
    issue = PossessionIssue(IssueSeverity.ERROR, "invalid_control", sequence_number=1)
    await store.fail_game(
        fresh_candidate.claim, issues=(issue,), error_code="invalid_control", error_type="parse"
    )
    fresh_resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    assert (
        fresh_resource["publication_state"],
        fresh_resource["published_snapshot_id"],
        fresh_resource["active_snapshot_id"],
    ) == (
        "current",
        old.attempt.snapshot_id,
        None,
    )

    stale_candidate = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=True
    )
    assert stale_candidate.claim is not None
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(games).where(games.c.game_id == "0022500111").values(home_score=111)
        )
    await store.fail_game(
        stale_candidate.claim, issues=(issue,), error_code="invalid_control", error_type="parse"
    )
    stale_resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    assert (stale_resource["publication_state"], stale_resource["published_snapshot_id"]) == (
        "invalid",
        old.attempt.snapshot_id,
    )


async def test_poisoned_same_input_rebuild_preserves_current_and_next_rerun_skips(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    old = await publish(store, await claim(store, harness))
    rebuild = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=True
    )
    assert rebuild.claim is not None
    await store.mark_writing(rebuild.claim, expected_row_count=2, expected_output_sha256="b" * 64)

    await store.poison_attempt(
        rebuild.claim, error_code="verification_mismatch", error_type="verification"
    )

    durable_resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    assert (
        durable_resource["publication_state"],
        durable_resource["published_snapshot_id"],
        durable_resource["active_snapshot_id"],
    ) == ("current", old.attempt.snapshot_id, None)
    assert await store.resolve_current(GameId("0022500111"), versions()) == old

    rerun = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=False
    )
    assert rerun.action is ClaimAction.SKIP_CURRENT
    assert rerun.current == old


async def test_stale_claim_rejects_every_fenced_mutation_without_rewriting_terminal_attempt(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    first = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, lease_seconds=1, utc_now=lambda: BASE_TIME
    )
    stale = await claim(first, harness)
    second = PostgresPossessionWorkStore(
        harness.engine,
        source=harness.source,
        lease_seconds=1,
        utc_now=lambda: BASE_TIME + timedelta(seconds=1),
    )
    await claim(second, harness)
    issue = PossessionIssue(IssueSeverity.ERROR, "invalid_control", sequence_number=1)
    operations = (
        lambda: first.mark_writing(stale, expected_row_count=2, expected_output_sha256="a" * 64),
        lambda: first.mark_verified(
            stale,
            SnapshotVerification(stale.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64),
        ),
        lambda: first.poison_attempt(stale, error_code="invalid_control", error_type="parse"),
        lambda: first.publish_snapshot(stale),
        lambda: first.fail_game(
            stale, issues=(issue,), error_code="invalid_control", error_type="parse"
        ),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="fence"):
            await operation()
    durable = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == stale.attempt.snapshot_id,
    )
    assert durable["state"] == "superseded"


@pytest.mark.parametrize("expired_by_seconds", [0, 1])
@pytest.mark.parametrize(
    ("operation", "prepared_state"),
    [
        ("mark_writing", PossessionAttemptState.ALLOCATED),
        ("mark_verified", PossessionAttemptState.WRITING),
        ("poison_attempt", PossessionAttemptState.ALLOCATED),
        ("publish_snapshot", PossessionAttemptState.VERIFIED),
        ("fail_game", PossessionAttemptState.ALLOCATED),
    ],
)
async def test_expired_active_lease_rejects_every_claim_mutation_without_a_reclaimer(
    harness: PossessionHarness,
    operation: str,
    prepared_state: PossessionAttemptState,
    expired_by_seconds: int,
) -> None:
    await harness.seed_game()
    setup_store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    claim_value = await claim(setup_store, harness)
    if prepared_state is PossessionAttemptState.WRITING:
        await setup_store.mark_writing(
            claim_value, expected_row_count=2, expected_output_sha256="a" * 64
        )
    elif prepared_state is PossessionAttemptState.VERIFIED:
        await setup_store.mark_writing(
            claim_value, expected_row_count=2, expected_output_sha256="a" * 64
        )
        await setup_store.mark_verified(
            claim_value,
            SnapshotVerification(
                claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64
            ),
        )
    now = BASE_TIME + timedelta(seconds=expired_by_seconds)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values(claimed_until=now)
        )
    expired_store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: now
    )
    before_resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    before_attempt = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == claim_value.attempt.snapshot_id,
    )
    issue = PossessionIssue(IssueSeverity.ERROR, "invalid_control", sequence_number=1)

    with pytest.raises(RuntimeError, match="fence"):
        if operation == "mark_writing":
            await expired_store.mark_writing(
                claim_value, expected_row_count=2, expected_output_sha256="a" * 64
            )
        elif operation == "mark_verified":
            await expired_store.mark_verified(
                claim_value,
                SnapshotVerification(
                    claim_value.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64
                ),
            )
        elif operation == "poison_attempt":
            await expired_store.poison_attempt(
                claim_value, error_code="invalid_control", error_type="parse"
            )
        elif operation == "publish_snapshot":
            await expired_store.publish_snapshot(claim_value)
        else:
            await expired_store.fail_game(
                claim_value, issues=(issue,), error_code="invalid_control", error_type="parse"
            )

    after_resource = await row(
        harness,
        possession_rebuild_resources,
        possession_rebuild_resources.c.game_id == "0022500111",
    )
    after_attempt = await row(
        harness,
        possession_snapshot_attempts,
        possession_snapshot_attempts.c.snapshot_id == claim_value.attempt.snapshot_id,
    )
    assert after_resource == before_resource
    assert after_attempt == before_attempt


async def test_finish_run_persists_exact_interrupted_and_terminal_counters(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    interrupted = await start_run(store, harness)
    await store.finish_run(
        interrupted,
        BuildPossessionsResult(interrupted, BuildStatus.INTERRUPTED, 3, 1, 0, 0, 0, 0),
    )
    partial = await start_run(store, harness)
    await store.finish_run(
        partial,
        BuildPossessionsResult(partial, BuildStatus.PARTIAL, 2, 1, 0, 0, 1, 0),
    )
    interrupted_row = await row(
        harness, possession_rebuild_runs, possession_rebuild_runs.c.run_id == interrupted
    )
    partial_row = await row(
        harness, possession_rebuild_runs, possession_rebuild_runs.c.run_id == partial
    )
    assert tuple(
        interrupted_row[key]
        for key in (
            "status",
            "discovered",
            "materialized",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ) == ("interrupted", 3, 1, 0, 0, 0, 0)
    assert tuple(
        partial_row[key]
        for key in (
            "status",
            "discovered",
            "materialized",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ) == ("partial", 2, 1, 0, 0, 1, 0)
