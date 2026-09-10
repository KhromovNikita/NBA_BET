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
)
from nba_live_totals.domain import GameId, PossessionVersions, SeasonId
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
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
    return PossessionVersions("nba-pbp-v1", "analytical-v1", "nba-2025-v1", "possessions-v1")


async def start_run(store: PostgresPossessionWorkStore, harness: PossessionHarness) -> UUID:
    run_id = await store.start_run(BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)))
    harness.run_ids.append(run_id)
    return run_id


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


async def test_version_change_invalidates_identical_published_content(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    _, original = await _publish_for_claim_test(harness, store)
    changed = PossessionVersions("nba-pbp-v1", "analytical-v2", "nba-2025-v1", "possessions-v1")
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
        states = (
            (await connection.execute(select(possession_snapshot_attempts.c.state))).scalars().all()
        )
    assert sorted(states) == ["allocated", "superseded"]


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
    run_id = await start_run(store, harness)
    async with harness.engine.begin() as connection:
        await connection.execute(possession_rebuild_resources.insert().values(game_id="0022500111"))
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values(claim_version=9_223_372_036_854_775_807)
        )
    with pytest.raises(RuntimeError, match="exhausted"):
        await store.claim_game(run_id, GameId("0022500111"), versions(), rebuild=False)
    async with harness.engine.connect() as connection:
        resource = (await connection.execute(select(possession_rebuild_resources))).mappings().one()
    assert resource["claim_version"] == 9_223_372_036_854_775_807
