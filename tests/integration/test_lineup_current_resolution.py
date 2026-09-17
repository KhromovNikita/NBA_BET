"""Read-only current lineup pointer resolution over real PostgreSQL state."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    CurrentLineupSnapshotResolution,
    LineupPublicationState,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import BoundarySide, GameId, SeasonId
from nba_live_totals.domain.lineups import LineupIssue, LineupStatus, LineupVersions
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.schema import lineup_rebuild_resources
from tests import runtime_config
from tests.integration.lineup_harness import LineupHarness
from tests.integration.possession_harness import BASE_TIME

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


async def publish(harness: LineupHarness, *, invalid: bool = False) -> object:
    store = PostgresLineupWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    run_id = await store.start_run(BuildLineupsRequest((SeasonId("2025-26"),)))
    harness.run_ids.append(run_id)
    decision = await store.claim_game(run_id, GAME, VERSIONS, rebuild=False)
    assert decision.claim is not None
    issues = (ISSUE,) if invalid else ()
    status = LineupStatus.INVALID if invalid else LineupStatus.VALID
    await store.mark_writing(
        decision.claim,
        expected_row_count=2,
        expected_output_sha256="a" * 64,
        expected_issue_codes=issues,
        result_status=status,
    )
    await store.mark_verified(
        decision.claim,
        LineupSnapshotVerification(
            decision.claim.attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            2,
            "a" * 64,
            status,
            issues,
        ),
    )
    return await store.publish_snapshot(decision.claim)


async def resource_row(harness: LineupHarness) -> dict[str, object]:
    async with harness.engine.connect() as connection:
        return dict((await connection.execute(select(lineup_rebuild_resources))).mappings().one())


async def test_resolve_current_returns_absent_without_creating_or_mutating_resource(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    store = PostgresLineupWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    assert await store.resolve_current(GAME, VERSIONS) == CurrentLineupSnapshotResolution(
        LineupPublicationState.ABSENT
    )


@pytest.mark.parametrize("invalid", [False, True])
async def test_resolve_current_returns_exact_persisted_pointer_metadata_without_mutation(
    harness: LineupHarness, invalid: bool
) -> None:
    await harness.seed_game()
    published = await publish(harness, invalid=invalid)
    store = PostgresLineupWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    before = await resource_row(harness)
    resolution = await store.resolve_current(GAME, VERSIONS)
    after = await resource_row(harness)
    assert resolution.status is (
        LineupPublicationState.INVALID if invalid else LineupPublicationState.CURRENT
    )
    assert resolution.reference == published
    assert resolution.reference is not None
    assert resolution.reference.attempt.input_token.versions == VERSIONS
    assert resolution.reference.issues == ((ISSUE,) if invalid else ())
    assert after == before


async def test_resolve_current_returns_stale_without_mutating_pointer_when_inputs_change(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    await publish(harness)
    await harness.base.replace_applied_pbp_document(
        GAME.value,
        payload={"game_id": GAME.value, "raw": "pbp-v2"},
        endpoint="playbyplayv3-v2",
    )
    store = PostgresLineupWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    before = await resource_row(harness)
    resolution = await store.resolve_current(GAME, VERSIONS)
    assert resolution.status is LineupPublicationState.STALE
    assert resolution.reference is None
    assert await resource_row(harness) == before


async def test_resolve_current_fails_closed_for_corrupt_pointer_metadata_without_mutation(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    await publish(harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(lineup_rebuild_resources)
            .where(lineup_rebuild_resources.c.game_id == GAME.value)
            .values(published_input_token_sha256="f" * 64)
        )
    store = PostgresLineupWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    before = await resource_row(harness)
    resolution = await store.resolve_current(GAME, VERSIONS)
    assert (resolution.status, resolution.reference) == (LineupPublicationState.STALE, None)
    assert await resource_row(harness) == before
