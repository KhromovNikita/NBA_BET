"""Real PostgreSQL contracts for fenced lineup attempt transitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    LineupAttemptState,
    LineupBuildClaim,
    LineupBuildService,
    LineupNormalizationError,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import (
    BoundarySide,
    GameId,
    JsonObject,
    SeasonId,
    canonical_json_checksum,
)
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import LineupIssue, LineupStatus, LineupVersions
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    ingestion_resources,
    lineup_rebuild_resources,
    lineup_snapshot_attempts,
    source_documents,
)
from tests import runtime_config
from tests.integration.lineup_harness import LineupHarness
from tests.integration.possession_harness import BASE_TIME
from tests.unit.application.lineup_fakes import FakeNormalizer, FakeReconstructor, FakeSnapshotStore

pytestmark = pytest.mark.integration

GAME = GameId("0022500111")
VERSIONS = LineupVersions("nba-lineup-v1", "lineup-v1", "nba-2025-v1", "lineups-v1")
ISSUE = LineupIssue(910001, "bad_lineup", 1, BoundarySide.BEFORE)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(runtime_config.postgres_url())
    yield value
    await value.dispose()


@pytest.fixture
async def harness(engine: AsyncEngine) -> AsyncIterator[LineupHarness]:
    value = LineupHarness(engine)
    try:
        yield value
    finally:
        await value.cleanup()


def store_for(harness: LineupHarness, *, elapsed: int = 0) -> PostgresLineupWorkStore:
    return PostgresLineupWorkStore(
        harness.engine,
        source=harness.source,
        lease_seconds=1,
        utc_now=lambda: BASE_TIME + timedelta(seconds=elapsed),
    )


async def claim(
    store: PostgresLineupWorkStore, harness: LineupHarness, *, rebuild: bool = False
) -> LineupBuildClaim:
    run_id = await store.start_run(BuildLineupsRequest((SeasonId("2025-26"),)))
    harness.run_ids.append(run_id)
    decision = await store.claim_game(run_id, GAME, VERSIONS, rebuild=rebuild)
    assert decision.claim is not None
    return decision.claim


async def row(harness: LineupHarness, table: Any, where: Any) -> dict[str, object]:
    async with harness.engine.connect() as connection:
        return dict((await connection.execute(select(table).where(where))).mappings().one())


async def replace_applied_box_document(harness: LineupHarness) -> None:
    payload: JsonObject = {"game_id": GAME.value, "raw": "box-v2"}
    async with harness.engine.begin() as connection:
        document_id = uuid4()
        await connection.execute(
            source_documents.insert().values(
                document_id=document_id,
                source=harness.source,
                endpoint="boxscoretraditionalv3-v2",
                resource_key=f"box_score:{GAME.value}",
                request_parameters={"game_id": GAME.value},
                payload=payload,
                checksum_sha256=canonical_json_checksum(payload),
                requested_at=BASE_TIME,
            )
        )
        await connection.execute(
            update(ingestion_resources)
            .where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_type == "box_score",
                ingestion_resources.c.resource_key == f"box_score:{GAME.value}",
            )
            .values(applied_document_id=document_id)
        )


async def test_service_normalization_failure_persists_category_and_releases_real_claim(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    events: list[str] = []
    normalizer = FakeNormalizer(events)
    normalizer.error = LineupNormalizationError("unsupported_document_schema")
    snapshots = FakeSnapshotStore(events)
    service = LineupBuildService(
        normalizer=normalizer,
        reconstructor=DeterministicLineupReconstructor(),
        work_store=store_for(harness),
        snapshot_store=snapshots,
        versions=VERSIONS,
        max_concurrency=1,
    )
    result = await service.build(BuildLineupsRequest((SeasonId("2025-26"),), game_id=GAME))
    harness.run_ids.append(result.run_id)
    assert (result.failed, result.materialized) == (1, 0)
    resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    attempt = await row(
        harness, lineup_snapshot_attempts, lineup_snapshot_attempts.c.game_id == GAME.value
    )
    assert attempt["state"] == "poisoned"
    assert attempt["safe_error_code"] == "unsupported_document_schema"
    assert attempt["safe_error_type"] == "normalize"
    assert resource["last_error_code"] == "unsupported_document_schema"
    assert resource["last_error_type"] == "normalize"
    assert resource["active_snapshot_id"] is None
    assert resource["active_run_id"] is None
    assert snapshots.write_calls == []


async def test_service_rerun_is_busy_then_supersedes_after_lease_expiry(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    request = BuildLineupsRequest((SeasonId("2025-26"),), game_id=GAME)
    events: list[str] = []
    unavailable = FakeSnapshotStore(events)
    unavailable.verification = LineupSnapshotVerification(
        uuid4(), LineupVerificationStatus.UNAVAILABLE
    )

    first = await LineupBuildService(
        normalizer=FakeNormalizer(events),
        reconstructor=FakeReconstructor(events),
        work_store=store_for(harness),
        snapshot_store=unavailable,
        versions=VERSIONS,
        max_concurrency=1,
    ).build(request)
    harness.run_ids.append(first.run_id)
    assert (first.materialized, first.failed, first.busy) == (0, 1, 0)
    first_resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    first_snapshot_id = first_resource["active_snapshot_id"]
    assert first_resource["active_run_id"] == first.run_id

    second = await LineupBuildService(
        normalizer=FakeNormalizer(events),
        reconstructor=FakeReconstructor(events),
        work_store=store_for(harness),
        snapshot_store=FakeSnapshotStore(events),
        versions=VERSIONS,
        max_concurrency=1,
    ).build(request)
    harness.run_ids.append(second.run_id)
    assert (second.materialized, second.failed, second.busy) == (0, 0, 1)
    assert second.run_id != first.run_id
    busy_resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    assert busy_resource["active_run_id"] == first.run_id
    assert busy_resource["active_snapshot_id"] == first_snapshot_id

    recovered = await LineupBuildService(
        normalizer=FakeNormalizer(events),
        reconstructor=FakeReconstructor(events),
        work_store=store_for(harness, elapsed=1),
        snapshot_store=FakeSnapshotStore(events),
        versions=VERSIONS,
        max_concurrency=1,
    ).build(request)
    harness.run_ids.append(recovered.run_id)
    assert (recovered.materialized, recovered.failed, recovered.busy) == (1, 0, 0)
    assert len({first.run_id, second.run_id, recovered.run_id}) == 3
    recovered_resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    assert recovered_resource["published_snapshot_id"] != first_snapshot_id
    assert recovered_resource["active_run_id"] is None
    async with harness.engine.connect() as connection:
        attempts = (
            (
                await connection.execute(
                    select(lineup_snapshot_attempts)
                    .where(lineup_snapshot_attempts.c.game_id == GAME.value)
                    .order_by(lineup_snapshot_attempts.c.generation)
                )
            )
            .mappings()
            .all()
        )
    assert [(attempt["generation"], attempt["state"]) for attempt in attempts] == [
        (1, "superseded"),
        (2, "published"),
    ]


async def advance_to_verified(
    store: PostgresLineupWorkStore, claim_value: LineupBuildClaim, *, invalid: bool = False
) -> None:
    issues = (ISSUE,) if invalid else ()
    status = LineupStatus.INVALID if invalid else LineupStatus.VALID
    await store.mark_writing(
        claim_value,
        expected_row_count=2,
        expected_output_sha256="a" * 64,
        expected_issue_codes=issues,
        result_status=status,
    )
    await store.mark_verified(
        claim_value,
        LineupSnapshotVerification(
            claim_value.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            2,
            "a" * 64,
            status,
            issues,
        ),
    )


async def test_lifecycle_persists_exact_metadata_and_invalid_publication_copies_issues(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    candidate = await claim(store, harness, rebuild=True)

    writing = await store.mark_writing(
        candidate,
        expected_row_count=2,
        expected_output_sha256="a" * 64,
        expected_issue_codes=(ISSUE,),
        result_status=LineupStatus.INVALID,
    )
    verified = await store.mark_verified(
        candidate,
        LineupSnapshotVerification(
            candidate.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            2,
            "a" * 64,
            LineupStatus.INVALID,
            (ISSUE,),
        ),
    )
    reference = await store.publish_snapshot(candidate)
    attempt = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )

    assert writing.state is LineupAttemptState.WRITING
    assert verified.state is LineupAttemptState.VERIFIED
    assert (reference.attempt.snapshot_id, reference.result_status, reference.issues) == (
        candidate.attempt.snapshot_id,
        LineupStatus.INVALID,
        (ISSUE,),
    )
    assert (
        attempt["state"],
        attempt["expected_row_count"],
        attempt["expected_output_sha256"],
        attempt["result_status"],
        attempt["verified_at"],
        attempt["published_at"],
    ) == ("published", 2, "a" * 64, "invalid", BASE_TIME, BASE_TIME)
    assert (
        resource["publication_state"],
        resource["published_snapshot_id"],
        resource["published_lineup_revision_id"],
        resource["published_input_token_sha256"],
        resource["last_issue_codes"],
        resource["active_snapshot_id"],
        resource["claimed_until"],
    ) == (
        "invalid",
        candidate.attempt.snapshot_id,
        candidate.attempt.lineup_revision_id,
        candidate.attempt.input_token.sha256,
        [
            {
                "team_id": 910001,
                "reason": "bad_lineup",
                "sequence_number": 1,
                "boundary_side": "before",
            }
        ],
        None,
        None,
    )


async def test_verified_transition_requires_exact_count_hash_status_and_issues(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    candidate = await claim(store, harness)
    await store.mark_writing(
        candidate,
        expected_row_count=2,
        expected_output_sha256="a" * 64,
        expected_issue_codes=(),
        result_status=LineupStatus.VALID,
    )

    for verification in (
        LineupSnapshotVerification(
            candidate.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            1,
            "a" * 64,
            LineupStatus.VALID,
            (),
        ),
        LineupSnapshotVerification(
            candidate.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            2,
            "b" * 64,
            LineupStatus.VALID,
            (),
        ),
        LineupSnapshotVerification(
            candidate.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            2,
            "a" * 64,
            LineupStatus.INVALID,
            (ISSUE,),
        ),
    ):
        with pytest.raises(RuntimeError, match="verification"):
            await store.mark_verified(candidate, verification)
    durable = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    assert (durable["state"], durable["expected_row_count"], durable["result_status"]) == (
        "writing",
        2,
        "valid",
    )


@pytest.mark.parametrize("operation", ["publish", "poison", "fail"])
@pytest.mark.parametrize(
    "stale_input", ["box_document", "pbp_document", "box_projection", "pbp_projection"]
)
@pytest.mark.parametrize("old_invalid", [False, True])
async def test_stale_input_supersedes_under_live_fence_preserving_old_pointer(
    harness: LineupHarness, operation: str, stale_input: str, old_invalid: bool
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    old = await claim(store, harness)
    await advance_to_verified(store, old, invalid=old_invalid)
    old_reference = await store.publish_snapshot(old)
    candidate = await claim(store, harness, rebuild=True)
    before_pointer = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    if operation == "publish":
        await advance_to_verified(store, candidate)
    elif operation == "poison":
        await store.mark_writing(
            candidate,
            expected_row_count=2,
            expected_output_sha256="b" * 64,
            expected_issue_codes=(),
            result_status=LineupStatus.VALID,
        )

    if stale_input == "box_document":
        await replace_applied_box_document(harness)
    elif stale_input == "pbp_document":
        await harness.base.replace_applied_pbp_document(
            GAME.value, payload={"game_id": GAME.value, "raw": "pbp-v2"}, endpoint="playbyplayv3-v2"
        )
    else:
        async with harness.engine.begin() as connection:
            table, values = (
                ("box", {"position": "F"})
                if stale_input == "box_projection"
                else ("pbp", {"description": "changed"})
            )
            from nba_live_totals.infrastructure.postgres.schema import (
                play_by_play_events,
                player_box_scores,
            )

            await connection.execute(
                update(player_box_scores if table == "box" else play_by_play_events)
                .where(
                    (player_box_scores if table == "box" else play_by_play_events).c.game_id
                    == GAME.value
                )
                .values(**values)
            )

    with pytest.raises(RuntimeError, match="stale"):
        if operation == "publish":
            await store.publish_snapshot(candidate)
        elif operation == "poison":
            await store.poison_attempt(
                candidate, error_code="readback_mismatch", error_type="verification"
            )
        else:
            await store.fail_game(
                candidate, error_code="normalizer_failure", error_type="normalizer"
            )
    resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    attempt = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    assert (
        attempt["state"],
        resource["published_snapshot_id"],
        resource["active_snapshot_id"],
        resource["publication_state"],
    ) == (
        "superseded",
        old_reference.attempt.snapshot_id,
        None,
        "stale",
    )
    pointer_fields = (
        "published_snapshot_id",
        "published_lineup_revision_id",
        "published_input_token_sha256",
        "published_at",
    )
    assert tuple(resource[field] for field in pointer_fields) == tuple(
        before_pointer[field] for field in pointer_fields
    )
    assert resource["last_issue_codes"] == (
        [
            {
                "team_id": 910001,
                "reason": "bad_lineup",
                "sequence_number": 1,
                "boundary_side": "before",
            }
        ]
        if old_invalid
        else []
    )


@pytest.mark.parametrize(
    "operation", ["mark_writing", "mark_verified", "publish", "poison", "fail"]
)
async def test_expired_terminal_caller_rejects_without_mutation(
    harness: LineupHarness, operation: str
) -> None:
    await harness.seed_game()
    live = store_for(harness)
    candidate = await claim(live, harness)
    if operation in {"mark_verified", "publish"}:
        await live.mark_writing(
            candidate,
            expected_row_count=2,
            expected_output_sha256="a" * 64,
            expected_issue_codes=(),
            result_status=LineupStatus.VALID,
        )
    if operation == "publish":
        await live.mark_verified(
            candidate,
            LineupSnapshotVerification(
                candidate.attempt.snapshot_id,
                LineupVerificationStatus.EXACT,
                2,
                "a" * 64,
                LineupStatus.VALID,
                (),
            ),
        )
    expired = store_for(harness, elapsed=1)
    before_resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    before_attempt = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    with pytest.raises(RuntimeError, match="fence"):
        if operation == "mark_writing":
            await expired.mark_writing(
                candidate,
                expected_row_count=2,
                expected_output_sha256="a" * 64,
                expected_issue_codes=(),
                result_status=LineupStatus.VALID,
            )
        elif operation == "mark_verified":
            await expired.mark_verified(
                candidate,
                LineupSnapshotVerification(
                    candidate.attempt.snapshot_id,
                    LineupVerificationStatus.EXACT,
                    2,
                    "a" * 64,
                    LineupStatus.VALID,
                    (),
                ),
            )
        elif operation == "publish":
            await expired.publish_snapshot(candidate)
        elif operation == "poison":
            await expired.poison_attempt(
                candidate, error_code="readback_mismatch", error_type="verification"
            )
        else:
            await expired.fail_game(
                candidate, error_code="normalizer_failure", error_type="normalizer"
            )
    assert (
        await row(
            harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
        )
        == before_resource
    )
    assert (
        await row(
            harness,
            lineup_snapshot_attempts,
            lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
        )
        == before_attempt
    )


async def test_exact_expiry_reclaimer_supersedes_and_atomically_allocates_fresh_claim(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    expired_candidate = await claim(store, harness)
    reclaimer_run = await store.start_run(BuildLineupsRequest((SeasonId("2025-26"),)))
    harness.run_ids.append(reclaimer_run)

    reclaimed = await store_for(harness, elapsed=1).claim_game(
        reclaimer_run, GAME, VERSIONS, rebuild=False
    )
    assert reclaimed.claim is not None
    resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    expired_attempt = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == expired_candidate.attempt.snapshot_id,
    )
    assert (
        reclaimed.claim.attempt.snapshot_id != expired_candidate.attempt.snapshot_id
        and reclaimed.claim.attempt.generation == 2
        and reclaimed.claim.claim_version == 2
    )
    assert expired_attempt["state"] == "superseded"
    assert (
        resource["attempt_count"],
        resource["claim_version"],
        resource["next_generation"],
        resource["active_run_id"],
        resource["active_claim_version"],
        resource["active_snapshot_id"],
        resource["active_lineup_revision_id"],
        resource["active_input_token_sha256"],
        resource["claimed_until"],
    ) == (
        2,
        2,
        3,
        reclaimer_run,
        2,
        reclaimed.claim.attempt.snapshot_id,
        reclaimed.claim.attempt.lineup_revision_id,
        reclaimed.claim.attempt.input_token.sha256,
        BASE_TIME + timedelta(seconds=2),
    )


@pytest.mark.parametrize("component", ["run", "claim_version", "snapshot", "revision", "token"])
@pytest.mark.parametrize(
    "operation", ["mark_writing", "mark_verified", "publish", "poison", "fail"]
)
async def test_stolen_full_fence_rejects_every_mutation_without_changes(
    harness: LineupHarness, component: str, operation: str
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    candidate = await claim(store, harness)
    if operation in {"mark_verified", "publish", "poison"}:
        await store.mark_writing(
            candidate,
            expected_row_count=2,
            expected_output_sha256="a" * 64,
            expected_issue_codes=(),
            result_status=LineupStatus.VALID,
        )
    if operation == "publish":
        await store.mark_verified(
            candidate,
            LineupSnapshotVerification(
                candidate.attempt.snapshot_id,
                LineupVerificationStatus.EXACT,
                2,
                "a" * 64,
                LineupStatus.VALID,
                (),
            ),
        )
    changes: dict[str, dict[str, object]] = {
        "claim_version": {"active_claim_version": 2, "claim_version": 2},
        "revision": {"active_lineup_revision_id": "f" * 64},
        "token": {"active_input_token_sha256": "f" * 64},
    }
    other_run = await store.start_run(BuildLineupsRequest((SeasonId("2025-26"),)))
    harness.run_ids.append(other_run)
    async with harness.engine.begin() as connection:
        if component == "run":
            changes[component] = {"active_run_id": other_run}
        elif component == "snapshot":
            original = await row(
                harness,
                lineup_snapshot_attempts,
                lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
            )
            replacement_id = uuid4()
            replacement = {
                key: value for key, value in dict(original).items() if value is not None
            } | {"snapshot_id": replacement_id, "generation": 99}
            await connection.execute(lineup_snapshot_attempts.insert().values(**replacement))
            changes[component] = {"active_snapshot_id": replacement_id}
        await connection.execute(
            update(lineup_rebuild_resources)
            .where(lineup_rebuild_resources.c.game_id == GAME.value)
            .values(**changes[component])
        )
    before_resource = await row(
        harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
    )
    before_attempt = await row(
        harness,
        lineup_snapshot_attempts,
        lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
    )
    with pytest.raises(RuntimeError, match="fence"):
        if operation == "mark_writing":
            await store.mark_writing(
                candidate,
                expected_row_count=2,
                expected_output_sha256="a" * 64,
                expected_issue_codes=(),
                result_status=LineupStatus.VALID,
            )
        elif operation == "mark_verified":
            await store.mark_verified(
                candidate,
                LineupSnapshotVerification(
                    candidate.attempt.snapshot_id,
                    LineupVerificationStatus.EXACT,
                    2,
                    "a" * 64,
                    LineupStatus.VALID,
                    (),
                ),
            )
        elif operation == "publish":
            await store.publish_snapshot(candidate)
        elif operation == "poison":
            await store.poison_attempt(
                candidate, error_code="readback_mismatch", error_type="verification"
            )
        else:
            await store.fail_game(
                candidate, error_code="normalizer_failure", error_type="normalizer"
            )
    assert (
        await row(
            harness, lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME.value
        )
        == before_resource
    )
    assert (
        await row(
            harness,
            lineup_snapshot_attempts,
            lineup_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id,
        )
        == before_attempt
    )
