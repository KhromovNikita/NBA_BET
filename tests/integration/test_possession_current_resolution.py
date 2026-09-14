"""Real-PostgreSQL contracts for exact published-snapshot resolution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import Executable, RowMapping, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    CurrentSnapshotReference,
    PossessionAttemptState,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import GameContext, GameId, PossessionVersions, SeasonId
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    possession_rebuild_resources,
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
    value = PossessionHarness(engine, source="task-12-current")
    try:
        yield value
    finally:
        await value.cleanup()


def versions() -> PossessionVersions:
    return PossessionVersions("nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1")


async def start_run(store: PostgresPossessionWorkStore, harness: PossessionHarness) -> UUID:
    run_id = await store.start_run(BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)))
    harness.run_ids.append(run_id)
    return run_id


async def published(
    store: PostgresPossessionWorkStore, harness: PossessionHarness
) -> CurrentSnapshotReference:
    decision = await store.claim_game(
        await start_run(store, harness), GameId("0022500111"), versions(), rebuild=False
    )
    assert decision.claim is not None
    claim = decision.claim
    await store.mark_writing(claim, expected_row_count=2, expected_output_sha256="a" * 64)
    await store.mark_verified(
        claim,
        SnapshotVerification(claim.attempt.snapshot_id, VerificationStatus.EXACT, 2, "a" * 64),
    )
    return await store.publish_snapshot(claim)


async def resource(harness: PossessionHarness) -> dict[str, object]:
    async with harness.engine.connect() as connection:
        return dict(
            (await connection.execute(select(possession_rebuild_resources))).mappings().one()
        )


async def test_resolve_current_reconstructs_the_exact_published_attempt_metadata(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    expected = await published(store, harness)

    current = await store.resolve_current(GameId("0022500111"), versions())

    assert current is not None
    assert current == expected
    assert current.attempt.state is PossessionAttemptState.PUBLISHED
    assert (
        current.attempt.input_token.applied_pbp_document_id,
        current.attempt.input_token.sha256,
    ) == (
        expected.attempt.input_token.applied_pbp_document_id,
        expected.attempt.input_token.sha256,
    )


@pytest.mark.parametrize("change", ["pbp", "game"])
async def test_current_resolution_is_effectively_stale_on_input_change_but_read_only(
    harness: PossessionHarness, change: str
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    expected = await published(store, harness)
    before = await resource(harness)
    if change == "pbp":
        await harness.replace_applied_pbp_document(
            "0022500111", payload={"game_id": "0022500111", "raw": "v2"}, endpoint="playbyplayv3-v2"
        )
    else:
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(games).where(games.c.game_id == "0022500111").values(home_score=111)
            )

    resolved = await store.resolve_current(GameId("0022500111"), versions())
    after = await resource(harness)

    assert resolved is None
    assert after == before
    assert after["published_snapshot_id"] == expected.attempt.snapshot_id
    assert after["publication_state"] == "current"


async def test_current_resolution_rejects_pointer_or_attempt_metadata_that_is_not_exact(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    expected = await published(store, harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(possession_rebuild_resources)
            .where(possession_rebuild_resources.c.game_id == "0022500111")
            .values(published_input_token_sha256="b" * 64)
        )

    assert await store.resolve_current(GameId("0022500111"), versions()) is None
    assert (await resource(harness))["published_snapshot_id"] == expected.attempt.snapshot_id


async def test_verify_current_resolution_is_read_only_when_no_resource_exists(
    harness: PossessionHarness,
) -> None:
    await harness.seed_game()
    store = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    run_id = await store.start_run(
        BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), mode=BuildMode.VERIFY)
    )
    harness.run_ids.append(run_id)

    assert await store.resolve_current(GameId("0022500111"), versions()) is None
    async with harness.engine.connect() as connection:
        count = await connection.scalar(
            select(possession_rebuild_resources.c.game_id).where(
                possession_rebuild_resources.c.game_id == "0022500111"
            )
        )
    assert count is None


class LockObservedStore(PostgresPossessionWorkStore):
    """Pause a real resolver after it has acquired its final required row lock."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        source: str,
        utc_now: Callable[[], datetime],
        snapshot_id: UUID,
    ) -> None:
        super().__init__(engine, source=source, utc_now=utc_now)
        self.snapshot_id = snapshot_id
        self.lock_order: list[str] = []
        self.backend_pid: int | None = None
        self.reached_attempt = asyncio.Event()
        self.release_attempt = asyncio.Event()

    async def _lock_phase_two_pbp(self, connection: AsyncConnection, game_id: GameId) -> RowMapping:
        self.lock_order.append("pbp")
        return await super()._lock_phase_two_pbp(connection, game_id)

    async def _lock_game(self, connection: AsyncConnection, game_id: GameId) -> GameContext:
        self.lock_order.append("game")
        return await super()._lock_game(connection, game_id)

    async def _lock_possession_resource(
        self, connection: AsyncConnection, game_id: GameId
    ) -> RowMapping:
        self.lock_order.append("resource")
        return await super()._lock_possession_resource(connection, game_id)

    async def _lock_attempt(self, connection: AsyncConnection, snapshot_id: UUID) -> RowMapping:
        self.lock_order.append("attempt")
        row = await super()._lock_attempt(connection, snapshot_id)
        if snapshot_id == self.snapshot_id:
            self.backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            self.reached_attempt.set()
            await self.release_attempt.wait()
        return row


async def assert_row_update_succeeds(engine: AsyncEngine, statement: Executable) -> None:
    """Prove a competing update succeeds after the resolver releases its lock."""
    async with engine.connect() as connection:
        result = await connection.execute(statement)
        assert result.rowcount == 1
        await connection.rollback()


async def release_resolver(
    resolution: asyncio.Task[CurrentSnapshotReference | None],
    observed: LockObservedStore,
) -> CurrentSnapshotReference | None:
    """Always release a paused resolver, including when lock assertions fail."""
    observed.release_attempt.set()
    if resolution.done():
        return resolution.result()
    try:
        return await asyncio.wait_for(resolution, timeout=2)
    except TimeoutError:
        resolution.cancel()
        await asyncio.gather(resolution, return_exceptions=True)
        raise


@pytest.mark.parametrize("phase", ["pre_clickhouse", "post_clickhouse"])
async def test_each_current_resolution_phase_holds_the_complete_standard_lock_order(
    harness: PossessionHarness,
    phase: str,
) -> None:
    await harness.seed_game()
    primary = PostgresPossessionWorkStore(
        harness.engine, source=harness.source, utc_now=lambda: BASE_TIME
    )
    expected = await published(primary, harness)

    observed = LockObservedStore(
        harness.engine,
        source=harness.source,
        utc_now=lambda: BASE_TIME,
        snapshot_id=expected.attempt.snapshot_id,
    )
    resolution = asyncio.create_task(observed.resolve_current(GameId("0022500111"), versions()))
    competing_updates = (
        update(ingestion_resources)
        .where(
            ingestion_resources.c.source == harness.source,
            ingestion_resources.c.resource_type == "play_by_play",
            ingestion_resources.c.resource_key == "play_by_play:0022500111",
        )
        .values(status="succeeded"),
        update(games).where(games.c.game_id == "0022500111").values(home_score=110),
        update(possession_rebuild_resources)
        .where(possession_rebuild_resources.c.game_id == "0022500111")
        .values(publication_state="current"),
        update(possession_snapshot_attempts)
        .where(possession_snapshot_attempts.c.snapshot_id == expected.attempt.snapshot_id)
        .values(state="published"),
    )
    try:
        await asyncio.wait_for(observed.reached_attempt.wait(), timeout=2)
        assert observed.lock_order == ["pbp", "game", "resource", "attempt"]
        assert observed.backend_pid is not None
        async with harness.engine.connect() as connection:
            held = set(
                (
                    await connection.execute(
                        text(
                            "SELECT relation::regclass::text FROM pg_locks "
                            "WHERE pid = :pid AND granted AND relation IS NOT NULL"
                        ),
                        {"pid": observed.backend_pid},
                    )
                ).scalars()
            )
        assert {
            "ingestion_resources",
            "games",
            "possession_rebuild_resources",
            "possession_snapshot_attempts",
        } <= held, phase
        for statement in competing_updates:
            async with harness.engine.connect() as connection:
                await connection.execute(text("SET lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    await connection.execute(statement)
                await connection.rollback()
    finally:
        assert await release_resolver(resolution, observed) == expected
    for statement in competing_updates:
        await assert_row_update_succeeds(harness.engine, statement)
