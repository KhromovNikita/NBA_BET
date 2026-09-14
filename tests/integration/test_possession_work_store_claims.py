"""Task 11 real-PostgreSQL selection and fenced-claim contract tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.possessions import (
    BuildPossessionsRequest,
    ClaimAction,
    PossessionBuildClaim,
    PossessionInputToken,
)
from nba_live_totals.domain import GameId, PossessionVersions, SeasonId, parse_revision_id
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    play_by_play_events,
    possession_rebuild_resources,
    possession_snapshot_attempts,
)
from tests import runtime_config
from tests.integration.possession_harness import BASE_TIME, PossessionHarness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(runtime_config.postgres_url())
    yield value
    await value.dispose()


@pytest.fixture
async def harness(engine: AsyncEngine) -> AsyncIterator[PossessionHarness]:
    value = PossessionHarness(engine)
    try:
        yield value
    finally:
        await value.cleanup()


def versions() -> PossessionVersions:
    return PossessionVersions("nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1")


def database_uuid(value: object) -> UUID:
    """Compare asyncpg UUID-compatible values with exact UUID contracts."""
    return value if type(value) is UUID else UUID(str(value))


async def start_run(store: PostgresPossessionWorkStore, harness: PossessionHarness) -> UUID:
    run_id = await store.start_run(BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)))
    harness.run_ids.append(run_id)
    return run_id


async def durable_claim_rows(
    harness: PossessionHarness,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Read the complete durable claim state in deterministic attempt order."""
    async with harness.engine.connect() as connection:
        resource = dict(
            (await connection.execute(select(possession_rebuild_resources))).mappings().one()
        )
        attempts = tuple(
            dict(row)
            for row in (
                (
                    await connection.execute(
                        select(possession_snapshot_attempts).order_by(
                            possession_snapshot_attempts.c.generation
                        )
                    )
                )
                .mappings()
                .all()
            )
        )
    return resource, attempts


async def test_select_games_filters_eligible_final_games_and_orders_deterministically(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game(game_id="0022500112", game_date=BASE_TIME.date())
    await harness.seed_game(
        game_id="0022500111", season_type="playoffs", game_date=BASE_TIME.date()
    )
    await harness.seed_game(game_id="0022500113", status="live")
    store = PostgresPossessionWorkStore(harness.engine, source=harness.source)

    selected = await store.select_games(BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)))
    targeted = await store.select_games(
        BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), game_id=GameId("0022500112"))
    )

    assert selected == (GameId("0022500111"), GameId("0022500112"))
    assert targeted == (GameId("0022500112"),)


@pytest.mark.parametrize("applied,ingestion_status", [(False, "succeeded"), (True, "failed")])
async def test_select_games_excludes_missing_or_unsucceeded_applied_pbp(
    harness: PossessionHarness, applied: bool, ingestion_status: str
) -> None:
    await harness.seed_game(applied=applied, ingestion_status=ingestion_status)
    store = PostgresPossessionWorkStore(harness.engine, source=harness.source)
    assert await store.select_games(BuildPossessionsRequest(seasons=(SeasonId("2025-26"),))) == ()


async def test_claim_builds_immutable_snapshot_with_exact_hashes_and_token(
    harness: PossessionHarness,
) -> None:
    document_id = await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    run_id = await start_run(store, harness)
    decision = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    claim = decision.claim
    assert claim.snapshot.applied_pbp_document_id == document_id
    assert claim.snapshot.source_document.resource_key == "play_by_play:0022500111"
    assert tuple(event.sequence_number for event in claim.snapshot.canonical_events) == (1, 2)
    assert claim.attempt.input_token.applied_pbp_document_id == document_id
    assert claim.attempt.parse_revision_id != claim.attempt.input_token.sha256
    assert claim.claim_version == claim.attempt.generation == 1
    async with harness.engine.connect() as connection:
        resource = (
            (
                await connection.execute(
                    select(possession_rebuild_resources).where(
                        possession_rebuild_resources.c.game_id == "0022500111"
                    )
                )
            )
            .mappings()
            .one()
        )
        attempt = (
            (
                await connection.execute(
                    select(possession_snapshot_attempts).where(
                        possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert (
        database_uuid(resource["active_run_id"]),
        resource["active_claim_version"],
        database_uuid(resource["active_snapshot_id"]),
        resource["active_parse_revision_id"],
        resource["active_input_token_sha256"],
        resource["claimed_until"],
    ) == (
        run_id,
        claim.claim_version,
        claim.attempt.snapshot_id,
        claim.attempt.parse_revision_id,
        claim.attempt.input_token.sha256,
        BASE_TIME + timedelta(seconds=300),
    )
    assert (
        database_uuid(attempt["created_by_run_id"]),
        attempt["created_by_claim_version"],
        attempt["parse_revision_id"],
        attempt["input_token_sha256"],
        attempt["generation"],
    ) == (
        run_id,
        claim.claim_version,
        claim.attempt.parse_revision_id,
        claim.attempt.input_token.sha256,
        claim.attempt.generation,
    )


async def test_same_run_resumes_without_increment_and_other_run_is_busy(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    first_run = await start_run(store, harness)
    first = await store.claim_game(first_run, GameId("0022500111"), versions(), rebuild=False)
    resumed = await store.claim_game(first_run, GameId("0022500111"), versions(), rebuild=False)
    other_run = await start_run(store, harness)
    busy = await store.claim_game(other_run, GameId("0022500111"), versions(), rebuild=False)

    assert first.action is ClaimAction.BUILD
    assert resumed.action is ClaimAction.RESUME
    assert resumed.claim == first.claim
    assert busy.action is ClaimAction.BUSY
    async with harness.engine.connect() as connection:
        resource = (await connection.execute(select(possession_rebuild_resources))).mappings().one()
    assert (resource["claim_version"], resource["next_generation"], resource["attempt_count"]) == (
        1,
        2,
        1,
    )


async def _publish_for_claim_test(
    harness: PossessionHarness, store: PostgresPossessionWorkStore
) -> tuple[UUID, PossessionBuildClaim]:
    run_id = await start_run(store, harness)
    decision = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)
    assert decision.claim is not None
    claim = decision.claim
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_snapshot_attempts)
            .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
            .values(
                state="published",
                expected_row_count=1,
                expected_output_sha256="c" * 64,
                insert_started_at=BASE_TIME,
                verified_at=BASE_TIME,
                published_at=BASE_TIME,
            )
        )
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == claim.attempt.game_id.value)
            .values(
                publication_state="current",
                active_run_id=None,
                active_claim_version=None,
                active_snapshot_id=None,
                active_parse_revision_id=None,
                active_input_token_sha256=None,
                claimed_until=None,
                published_snapshot_id=claim.attempt.snapshot_id,
                published_parse_revision_id=claim.attempt.parse_revision_id,
                published_input_token_sha256=claim.attempt.input_token.sha256,
                published_at=BASE_TIME,
            )
        )
    return run_id, claim


async def test_current_identical_input_skips_but_forced_rebuild_allocates_new_attempt(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    run_id = await start_run(store, harness)

    skipped = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)
    rebuilt = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=True)

    assert skipped.action is ClaimAction.SKIP_CURRENT
    assert rebuilt.action is ClaimAction.BUILD
    assert rebuilt.claim is not None
    assert rebuilt.claim.attempt.snapshot_id != original.attempt.snapshot_id
    assert rebuilt.claim.attempt.generation == original.attempt.generation + 1


@pytest.mark.parametrize(
    "corruption",
    [
        "publication_state",
        "resource_token",
        "attempt_token",
        "version",
        "revision",
        "attempt_state",
    ],
)
async def test_ordinary_claim_never_skips_a_non_effective_current_pointer(
    harness: PossessionHarness, corruption: str
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    async with harness.engine.begin() as connection:
        if corruption == "publication_state":
            await connection.execute(
                update(possession_rebuild_resources)
                .where(possession_rebuild_resources.c.game_id == "0022500111")
                .values(publication_state="invalid")
            )
        elif corruption == "resource_token":
            await connection.execute(
                update(possession_rebuild_resources)
                .where(possession_rebuild_resources.c.game_id == "0022500111")
                .values(published_input_token_sha256="b" * 64)
            )
        elif corruption == "attempt_token":
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == original.attempt.snapshot_id)
                .values(input_token_sha256="b" * 64)
            )
        elif corruption == "version":
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == original.attempt.snapshot_id)
                .values(parser_version="analytical-v2")
            )
        elif corruption == "revision":
            await connection.execute(
                update(possession_rebuild_resources)
                .where(possession_rebuild_resources.c.game_id == "0022500111")
                .values(published_parse_revision_id="b" * 64)
            )
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == original.attempt.snapshot_id)
                .values(parse_revision_id="b" * 64)
            )
        else:
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == original.attempt.snapshot_id)
                .values(state="superseded", published_at=None)
            )

    decision = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=False
    )

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    assert decision.claim.attempt.snapshot_id != original.attempt.snapshot_id


async def test_version_change_invalidates_identical_published_content(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    changed = PossessionVersions("nba-pbp-v1", "analytical-v1", "nba-2025-v1", "possessions-v1")
    run_id = await start_run(store, harness)

    decision = await store.claim_game(run_id, GameId("0022500111"), changed, rebuild=False)

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    assert decision.claim.attempt.parse_revision_id != original.attempt.parse_revision_id
    assert decision.claim.attempt.input_token.sha256 != original.attempt.input_token.sha256


async def test_expired_claim_is_superseded_then_reclaimed_with_new_uuid_and_generation(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    first_store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, lease_seconds=1, utc_now=lambda: BASE_TIME
    )
    first_run = await start_run(first_store, harness)
    first = await first_store.claim_game(first_run, GameId("0022500111"), versions(), rebuild=False)
    second_store = PostgresPossessionWorkStore(
        harness.engine,
        source=harness.source,
        lease_seconds=1,
        utc_now=lambda: BASE_TIME + timedelta(seconds=1),
    )
    second_run = await start_run(second_store, harness)
    second = await second_store.claim_game(
        second_run, GameId("0022500111"), versions(), rebuild=False
    )

    assert second.action is ClaimAction.BUILD
    assert first.claim is not None and second.claim is not None
    assert second.claim.attempt.snapshot_id != first.claim.attempt.snapshot_id
    assert second.claim.attempt.generation == first.claim.attempt.generation + 1
    async with harness.engine.connect() as connection:
        resource = (await connection.execute(select(possession_rebuild_resources))).mappings().one()
        attempts = (
            (
                await connection.execute(
                    select(possession_snapshot_attempts).order_by(
                        possession_snapshot_attempts.c.generation
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [attempt["state"] for attempt in attempts] == ["superseded", "allocated"]
    assert second.claim is not None
    assert (
        database_uuid(resource["active_run_id"]),
        resource["active_claim_version"],
        database_uuid(resource["active_snapshot_id"]),
        resource["active_parse_revision_id"],
        resource["active_input_token_sha256"],
        resource["claimed_until"],
    ) == (
        second_run,
        second.claim.claim_version,
        second.claim.attempt.snapshot_id,
        second.claim.attempt.parse_revision_id,
        second.claim.attempt.input_token.sha256,
        BASE_TIME + timedelta(seconds=2),
    )
    assert (
        database_uuid(attempts[0]["created_by_run_id"]),
        attempts[0]["created_by_claim_version"],
        attempts[0]["generation"],
        database_uuid(attempts[1]["created_by_run_id"]),
        attempts[1]["created_by_claim_version"],
        attempts[1]["generation"],
    ) == (first_run, 1, 1, second_run, 2, 2)
    assert (
        database_uuid(attempts[1]["snapshot_id"]),
        attempts[1]["parse_revision_id"],
        attempts[1]["input_token_sha256"],
        database_uuid(attempts[1]["created_by_run_id"]),
        attempts[1]["created_by_claim_version"],
        attempts[1]["generation"],
        attempts[1]["state"],
    ) == (
        second.claim.attempt.snapshot_id,
        second.claim.attempt.parse_revision_id,
        second.claim.attempt.input_token.sha256,
        second_run,
        second.claim.claim_version,
        second.claim.attempt.generation,
        "allocated",
    )


async def test_concurrent_first_claim_allocates_exactly_one_fenced_attempt(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    first = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    second = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    first_run = await start_run(first, harness)
    second_run = await start_run(second, harness)
    decisions = await asyncio.gather(
        first.claim_game(first_run, GameId("0022500111"), versions(), rebuild=False),
        second.claim_game(second_run, GameId("0022500111"), versions(), rebuild=False),
    )
    assert {decision.action for decision in decisions} == {ClaimAction.BUILD, ClaimAction.BUSY}
    async with harness.engine.connect() as connection:
        resource = (await connection.execute(select(possession_rebuild_resources))).mappings().one()
        count = await connection.scalar(select(possession_snapshot_attempts.c.snapshot_id))
    assert (resource["claim_version"], resource["attempt_count"], count is not None) == (1, 1, True)


async def test_checked_bigint_exhaustion_does_not_partially_increment(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, existing = await _publish_for_claim_test(harness, store)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values(claim_version=9_223_372_036_854_775_807)
        )
    run_id = await start_run(store, harness)
    before_resource, before_attempts = await durable_claim_rows(harness)
    with pytest.raises(RuntimeError, match="exhausted"):
        await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=True)
    after_resource, after_attempts = await durable_claim_rows(harness)
    assert after_resource == before_resource
    assert after_attempts == before_attempts
    assert len(after_attempts) == 1
    assert database_uuid(after_attempts[0]["snapshot_id"]) == existing.attempt.snapshot_id


@pytest.mark.parametrize("exhausted_counter", ["next_generation", "attempt_count"])
async def test_other_checked_bigint_exhaustion_preserves_all_claim_state(
    harness: PossessionHarness, exhausted_counter: str
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, existing = await _publish_for_claim_test(harness, store)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values({exhausted_counter: 9_223_372_036_854_775_807})
        )
    run_id = await start_run(store, harness)
    before_resource, before_attempts = await durable_claim_rows(harness)

    with pytest.raises(RuntimeError, match="exhausted"):
        await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=True)

    after_resource, after_attempts = await durable_claim_rows(harness)
    assert after_resource == before_resource
    assert after_attempts == before_attempts
    assert len(after_attempts) == 1
    assert database_uuid(after_attempts[0]["snapshot_id"]) == existing.attempt.snapshot_id


async def test_claim_rejects_non_contiguous_canonical_sequence(harness: PossessionHarness) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(harness.engine, source=harness.source)
    run_id = await start_run(store, harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(play_by_play_events)
            .where(
                play_by_play_events.c.game_id == "0022500111",
                play_by_play_events.c.sequence_number == 2,
            )
            .values(sequence_number=3)
        )

    with pytest.raises(ValueError, match="contiguous"):
        await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)


async def test_claim_rejects_applied_document_for_a_different_game(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game(game_id="0022500111")
    other_document_id = await harness.seed_game(game_id="0022500112")
    store = PostgresPossessionWorkStore(harness.engine, source=harness.source)
    run_id = await start_run(store, harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(ingestion_resources)
            .where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_type == "play_by_play",
                ingestion_resources.c.resource_key == "play_by_play:0022500111",
            )
            .values(applied_document_id=other_document_id)
        )

    with pytest.raises(RuntimeError, match="does not match game"):
        await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)


async def test_changed_document_lineage_rebuilds_with_same_content_revision(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    document_id = await harness.replace_applied_pbp_document(
        "0022500111",
        payload={"game_id": "0022500111", "raw": "v1"},
        endpoint="playbyplayv3-retry",
    )
    run_id = await start_run(store, harness)

    decision = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    assert decision.claim.snapshot.applied_pbp_document_id == document_id
    assert decision.claim.attempt.parse_revision_id == original.attempt.parse_revision_id
    assert decision.claim.attempt.input_token.sha256 != original.attempt.input_token.sha256


async def test_stale_same_document_identity_with_changed_checksum_prevents_skip(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    stale_token = PossessionInputToken(
        game_id=original.attempt.game_id,
        applied_pbp_document_id=original.snapshot.applied_pbp_document_id,
        applied_pbp_sha256="d" * 64,
        canonical_event_sha256=original.attempt.input_token.canonical_event_sha256,
        game_context_sha256=original.attempt.input_token.game_context_sha256,
        versions=versions(),
    )
    stale_revision = parse_revision_id(
        game_id=original.attempt.game_id,
        pbp_document_sha256=stale_token.applied_pbp_sha256,
        canonical_event_sha256=stale_token.canonical_event_sha256,
        game_context_sha256=stale_token.game_context_sha256,
        versions=versions(),
    )
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_snapshot_attempts)
            .where(possession_snapshot_attempts.c.snapshot_id == original.attempt.snapshot_id)
            .values(
                parse_revision_id=stale_revision,
                input_token_sha256=stale_token.sha256,
                input_pbp_sha256=stale_token.applied_pbp_sha256,
            )
        )
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values(
                published_parse_revision_id=stale_revision,
                published_input_token_sha256=stale_token.sha256,
            )
        )
    run_id = await start_run(store, harness)

    decision = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    assert (
        decision.claim.snapshot.applied_pbp_document_id == original.snapshot.applied_pbp_document_id
    )
    assert decision.claim.attempt.input_token.sha256 != stale_token.sha256
    assert decision.claim.attempt.input_token.applied_pbp_sha256 != stale_token.applied_pbp_sha256


@pytest.mark.parametrize("freshness_change", ["checksum", "events", "game_context"])
async def test_each_content_freshness_component_prevents_current_skip(
    harness: PossessionHarness, freshness_change: str
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    if freshness_change == "checksum":
        await harness.replace_applied_pbp_document(
            "0022500111",
            payload={"game_id": "0022500111", "raw": "v2"},
            endpoint="playbyplayv3-v2",
        )
    elif freshness_change == "events":
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(play_by_play_events)
                .where(
                    play_by_play_events.c.game_id == "0022500111",
                    play_by_play_events.c.sequence_number == 1,
                )
                .values(description="corrected event")
            )
    else:
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(games).where(games.c.game_id == "0022500111").values(home_score=111)
            )
    run_id = await start_run(store, harness)

    decision = await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)

    assert decision.action is ClaimAction.BUILD
    assert decision.claim is not None
    assert decision.claim.attempt.parse_revision_id != original.attempt.parse_revision_id
    assert decision.claim.attempt.input_token.sha256 != original.attempt.input_token.sha256
