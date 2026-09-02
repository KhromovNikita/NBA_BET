import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    ClaimDecision,
    ClaimLostError,
    ResourceClaim,
    ResourceKey,
)
from nba_live_totals.domain import (
    GameId,
    JsonObject,
    SeasonId,
    SeasonSchedule,
    SourceDocument,
    canonical_json_checksum,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import (
    PostgresHistoricalIngestionStore,
)
from nba_live_totals.infrastructure.postgres.schema import (
    ingestion_resources,
    ingestion_runs,
    seasons,
    source_documents,
)
from tests import runtime_config

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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


@dataclass
class StoreHarness:
    engine: AsyncEngine
    source: str = field(default_factory=lambda: f"task-8-{uuid4()}")
    run_ids: list[UUID] = field(default_factory=list)
    season_ids: set[str] = field(default_factory=set)

    def store(
        self,
        *,
        now: datetime = BASE_TIME,
        lease_seconds: int = 600,
    ) -> PostgresHistoricalIngestionStore:
        return PostgresHistoricalIngestionStore(
            self.engine,
            source=self.source,
            lease_seconds=lease_seconds,
            utc_now=lambda: now,
        )

    async def start_run(
        self,
        store: PostgresHistoricalIngestionStore,
        request: BackfillRequest | None = None,
    ) -> UUID:
        run_id = await store.start_run(request or BackfillRequest(seasons=(SeasonId("2025-26"),)))
        self.run_ids.append(run_id)
        return run_id


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
async def harness(engine: AsyncEngine) -> AsyncIterator[StoreHarness]:
    value = StoreHarness(engine)
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(ingestion_resources).where(ingestion_resources.c.source == value.source)
            )
            await connection.execute(
                delete(source_documents).where(source_documents.c.source == value.source)
            )
            if value.run_ids:
                await connection.execute(
                    delete(ingestion_runs).where(ingestion_runs.c.run_id.in_(value.run_ids))
                )
            if value.season_ids:
                await connection.execute(
                    delete(seasons).where(seasons.c.season.in_(value.season_ids))
                )


async def backend_pid(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        pid = await connection.scalar(text("SELECT pg_backend_pid()"))
    assert type(pid) is int
    return pid


async def wait_until_backend_waits_on_lock(engine: AsyncEngine, pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        async with engine.connect() as connection:
            waiting = await connection.scalar(
                text(
                    """
                    SELECT wait_event_type = 'Lock'
                    FROM pg_stat_activity
                    WHERE pid = :pid AND state = 'active'
                    """
                ),
                {"pid": pid},
            )
        if waiting is True:
            return
        if asyncio.get_running_loop().time() >= deadline:
            msg = f"backend {pid} did not reach a database lock wait"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


async def cancel_claim_tasks(*tasks: asyncio.Task[ClaimDecision] | None) -> None:
    active = tuple(task for task in tasks if task is not None and not task.done())
    for task in active:
        task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def make_document(
    harness: StoreHarness,
    key: ResourceKey,
    payload: JsonObject,
    *,
    requested_at: datetime = BASE_TIME,
    source: str | None = None,
) -> SourceDocument:
    endpoint_by_type = {
        "schedule": "scheduleleaguev2",
        "box_score": "boxscoretraditionalv3",
        "play_by_play": "playbyplayv3",
    }
    return SourceDocument(
        source=source or harness.source,
        endpoint=endpoint_by_type[key.resource_type.value],
        resource_key=str(key),
        requested_at=requested_at,
        request_parameters={"resource": str(key)},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


async def test_run_lifecycle_persists_request_and_terminal_counters(
    harness: StoreHarness,
) -> None:
    store = harness.store()
    request = BackfillRequest(
        seasons=(SeasonId("2025-26"),),
        game_id=GameId("0022500123"),
        refresh=True,
    )
    run_id = await harness.start_run(store, request)

    async with harness.engine.connect() as connection:
        started = (
            (
                await connection.execute(
                    select(ingestion_runs).where(ingestion_runs.c.run_id == run_id)
                )
            )
            .mappings()
            .one()
        )

    assert started["request_parameters"] == {
        "seasons": ["2025-26"],
        "game_id": "0022500123",
        "refresh": True,
    }
    assert started["status"] == "running"
    assert started["finished_at"] is None
    assert started["started_at"].tzinfo is not None
    assert (
        started["discovered"],
        started["succeeded"],
        started["skipped"],
        started["failed"],
    ) == (0, 0, 0, 0)

    result = BackfillResult(
        run_id=run_id,
        status=BackfillStatus.PARTIAL,
        discovered=7,
        succeeded=4,
        skipped=2,
        failed=1,
    )
    await store.finish_run(run_id, result)

    async with harness.engine.connect() as connection:
        finished = (
            (
                await connection.execute(
                    select(ingestion_runs).where(ingestion_runs.c.run_id == run_id)
                )
            )
            .mappings()
            .one()
        )

    assert finished["status"] == "partial"
    assert finished["finished_at"] == BASE_TIME
    assert (
        finished["discovered"],
        finished["succeeded"],
        finished["skipped"],
        finished["failed"],
    ) == (7, 4, 2, 1)


async def test_raw_documents_deduplicate_and_keep_immutable_versions(
    harness: StoreHarness,
) -> None:
    clock = MutableClock(BASE_TIME)
    store = PostgresHistoricalIngestionStore(
        harness.engine,
        source=harness.source,
        utc_now=clock,
    )
    run_id = await harness.start_run(store)
    key = ResourceKey.box_score(GameId("0022500811"))
    decision = await store.claim_resource(run_id, key, refresh=False)
    assert decision.action is ClaimAction.FETCH
    claim = cast(ResourceClaim, decision.claim)
    first = make_document(harness, key, {"version": 1})
    second = make_document(
        harness,
        key,
        {"version": 2},
        requested_at=BASE_TIME + timedelta(seconds=1),
    )

    first_id = await store.save_document(claim, first)
    duplicate_id = await store.save_document(claim, first)
    clock.value += timedelta(seconds=1)
    second_id = await store.save_document(claim, second)

    assert duplicate_id == first_id
    assert second_id != first_id
    async with harness.engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    source_documents.c.document_id,
                    source_documents.c.payload,
                    source_documents.c.stored_at,
                )
                .where(
                    source_documents.c.source == harness.source,
                    source_documents.c.resource_key == str(key),
                )
                .order_by(source_documents.c.stored_at, source_documents.c.document_id)
            )
        ).all()

    assert [(row.document_id, row.payload) for row in rows] == [
        (first_id, {"version": 1}),
        (second_id, {"version": 2}),
    ]
    assert rows[0].stored_at <= rows[1].stored_at
    latest = await store.latest_document(key)
    assert latest is not None
    assert latest.document_id == second_id
    assert latest.document == second
    async with harness.engine.connect() as connection:
        applied_document_id = await connection.scalar(
            select(ingestion_resources.c.applied_document_id).where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_type == key.resource_type.value,
                ingestion_resources.c.resource_key == str(key),
            )
        )
    assert applied_document_id is None


async def test_succeeded_resources_skip_or_refresh_with_a_new_fence(
    harness: StoreHarness,
) -> None:
    store = harness.store()
    key = ResourceKey.schedule(SeasonId("2081-82"))
    first_run = await harness.start_run(store)
    first = await store.claim_resource(first_run, key, refresh=False)
    first_claim = cast(ResourceClaim, first.claim)
    first_document = make_document(harness, key, {"version": 1})
    first_document_id = await store.save_document(first_claim, first_document)
    harness.season_ids.add("2081-82")
    await store.apply_schedule(
        first_claim,
        first_document_id,
        SeasonSchedule(SeasonId("2081-82"), (), ()),
    )

    skip_run = await harness.start_run(store)
    skipped = await store.claim_resource(skip_run, key, refresh=False)
    refresh_run = await harness.start_run(store)
    refreshed = await store.claim_resource(refresh_run, key, refresh=True)

    assert skipped.action is ClaimAction.SKIP
    assert skipped.claim is None
    assert skipped.stored_document is not None
    assert skipped.stored_document.document_id == first_document_id
    assert skipped.stored_document.document == first_document
    assert refreshed.action is ClaimAction.FETCH
    assert refreshed.claim == ResourceClaim(key=key, run_id=refresh_run, version=2)
    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_type == key.resource_type.value,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["attempt_count"] == 2
    assert row["last_run_id"] == refresh_run
    assert row["claim_version"] == 2


async def test_skip_returns_atomic_applied_snapshot_before_waiting_refresh(
    harness: StoreHarness,
) -> None:
    key = ResourceKey.schedule(SeasonId("2082-83"))
    initial_store = harness.store()
    initial_run = await harness.start_run(initial_store)
    initial_claim = cast(
        ResourceClaim,
        (await initial_store.claim_resource(initial_run, key, refresh=False)).claim,
    )
    applied_document = make_document(harness, key, {"games": ["applied-v1"]})
    applied_document_id = await initial_store.save_document(initial_claim, applied_document)
    harness.season_ids.add("2082-83")
    await initial_store.apply_schedule(
        initial_claim,
        applied_document_id,
        SeasonSchedule(SeasonId("2082-83"), (), ()),
    )

    skip_engine = create_async_engine(runtime_config.postgres_url(), pool_size=1, max_overflow=0)
    refresh_engine = create_async_engine(runtime_config.postgres_url(), pool_size=1, max_overflow=0)
    skip_store = PostgresHistoricalIngestionStore(skip_engine, source=harness.source)
    refresh_store = PostgresHistoricalIngestionStore(refresh_engine, source=harness.source)
    skip_run = await harness.start_run(skip_store)
    refresh_run = await harness.start_run(refresh_store)
    skip_pid = await backend_pid(skip_engine)
    refresh_pid = await backend_pid(refresh_engine)
    skip_task: asyncio.Task[ClaimDecision] | None = None
    refresh_task: asyncio.Task[ClaimDecision] | None = None
    try:
        async with harness.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            await lock_connection.execute(
                select(ingestion_resources.c.resource_key)
                .where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
                .with_for_update()
            )
            try:
                skip_task = asyncio.create_task(
                    skip_store.claim_resource(skip_run, key, refresh=False)
                )
                await wait_until_backend_waits_on_lock(harness.engine, skip_pid)
                refresh_task = asyncio.create_task(
                    refresh_store.claim_resource(refresh_run, key, refresh=True)
                )
                await wait_until_backend_waits_on_lock(harness.engine, refresh_pid)
                assert not skip_task.done()
                assert not refresh_task.done()
                await transaction.commit()
                skipped = await asyncio.wait_for(skip_task, timeout=2.0)
                refreshed = await asyncio.wait_for(refresh_task, timeout=2.0)
            finally:
                if transaction.is_active:
                    await transaction.rollback()

        assert skipped.action is ClaimAction.SKIP
        assert skipped.stored_document is not None
        assert skipped.stored_document.document_id == applied_document_id
        assert skipped.stored_document.document.payload == {"games": ["applied-v1"]}
        refresh_claim = cast(ResourceClaim, refreshed.claim)
        failed_document = make_document(
            harness,
            key,
            {"games": ["failed-refresh-v2"]},
            requested_at=BASE_TIME + timedelta(seconds=1),
        )
        failed_document_id = await refresh_store.save_document(refresh_claim, failed_document)
        await refresh_store.fail_resource(
            refresh_claim,
            error_code="schema_mismatch",
            error_type="ProviderSchemaError",
        )
    finally:
        await cancel_claim_tasks(skip_task, refresh_task)
        await skip_engine.dispose()
        await refresh_engine.dispose()

    async with harness.engine.connect() as connection:
        resource = (
            await connection.execute(
                select(
                    ingestion_resources.c.status,
                    ingestion_resources.c.latest_document_id,
                    ingestion_resources.c.applied_document_id,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()

    assert resource == ("failed", failed_document_id, applied_document_id)
    assert skipped.stored_document.document.payload == {"games": ["applied-v1"]}

    completing_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    completing_run = await harness.start_run(completing_store)
    completing_claim = cast(
        ResourceClaim,
        (await completing_store.claim_resource(completing_run, key, refresh=True)).claim,
    )
    completed_document = make_document(
        harness,
        key,
        {"games": ["applied-v3"]},
        requested_at=BASE_TIME + timedelta(seconds=2),
    )
    completed_document_id = await completing_store.save_document(
        completing_claim, completed_document
    )
    await completing_store.apply_schedule(
        completing_claim,
        completed_document_id,
        SeasonSchedule(SeasonId("2082-83"), (), ()),
    )

    async with harness.engine.connect() as connection:
        completed_links = (
            await connection.execute(
                select(
                    ingestion_resources.c.latest_document_id,
                    ingestion_resources.c.applied_document_id,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()

    assert completed_links == (completed_document_id, completed_document_id)
    assert skipped.stored_document.document.payload == {"games": ["applied-v1"]}


async def test_failed_resource_reparses_raw_but_fetches_without_raw(
    harness: StoreHarness,
) -> None:
    store = harness.store()
    raw_key = ResourceKey.play_by_play(GameId("0022500812"))
    raw_run = await harness.start_run(store)
    raw_claim = cast(
        ResourceClaim,
        (await store.claim_resource(raw_run, raw_key, refresh=False)).claim,
    )
    raw_document = make_document(harness, raw_key, {"actions": []})
    raw_document_id = await store.save_document(raw_claim, raw_document)
    await store.fail_resource(
        raw_claim,
        error_code="schema_mismatch",
        error_type="PayloadMappingError",
    )

    reparse_run = await harness.start_run(store)
    reparse = await store.claim_resource(reparse_run, raw_key, refresh=False)
    assert reparse.action is ClaimAction.REPARSE
    assert reparse.claim == ResourceClaim(key=raw_key, run_id=reparse_run, version=2)
    assert reparse.stored_document is not None
    assert reparse.stored_document.document_id == raw_document_id
    assert reparse.stored_document.document == raw_document

    empty_key = ResourceKey.box_score(GameId("0022500813"))
    empty_run = await harness.start_run(store)
    empty_claim = cast(
        ResourceClaim,
        (await store.claim_resource(empty_run, empty_key, refresh=False)).claim,
    )
    await store.fail_resource(
        empty_claim,
        error_code="network_timeout",
        error_type="ProviderTimeoutError",
    )
    retry_run = await harness.start_run(store)
    retry = await store.claim_resource(retry_run, empty_key, refresh=False)
    assert retry.action is ClaimAction.FETCH
    assert retry.claim == ResourceClaim(key=empty_key, run_id=retry_run, version=2)


async def test_active_claim_is_busy_and_exact_expiry_can_be_reclaimed(
    harness: StoreHarness,
) -> None:
    first_store = harness.store(now=BASE_TIME, lease_seconds=600)
    active_store = harness.store(now=BASE_TIME + timedelta(seconds=599), lease_seconds=600)
    boundary_store = harness.store(now=BASE_TIME + timedelta(seconds=600), lease_seconds=600)
    key = ResourceKey.box_score(GameId("0022500814"))
    first_run = await harness.start_run(first_store)
    first = await first_store.claim_resource(first_run, key, refresh=False)
    active_run = await harness.start_run(active_store)
    active = await active_store.claim_resource(active_run, key, refresh=False)
    boundary_run = await harness.start_run(boundary_store)
    boundary = await boundary_store.claim_resource(boundary_run, key, refresh=False)

    assert first.claim == ResourceClaim(key=key, run_id=first_run, version=1)
    assert active.action is ClaimAction.BUSY
    assert active.claim is None
    assert boundary.action is ClaimAction.FETCH
    assert boundary.claim == ResourceClaim(key=key, run_id=boundary_run, version=2)


async def test_waiting_claim_uses_post_lock_time_for_expiry_and_full_lease(
    harness: StoreHarness,
) -> None:
    key = ResourceKey.box_score(GameId("0022500819"))
    first_store = harness.store(now=BASE_TIME, lease_seconds=5)
    first_run = await harness.start_run(first_store)
    first = await first_store.claim_resource(first_run, key, refresh=False)
    assert first.action is ClaimAction.FETCH

    contender_engine = create_async_engine(
        runtime_config.postgres_url(),
        pool_size=1,
        max_overflow=0,
    )
    clock = MutableClock(BASE_TIME)
    contender = PostgresHistoricalIngestionStore(
        contender_engine,
        source=harness.source,
        lease_seconds=600,
        utc_now=clock,
    )
    contender_run = await harness.start_run(contender)
    contender_pid = await backend_pid(contender_engine)
    claim_task: asyncio.Task[ClaimDecision] | None = None
    try:
        async with harness.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            lock_pid = await lock_connection.scalar(text("SELECT pg_backend_pid()"))
            assert type(lock_pid) is int
            assert contender_pid != lock_pid
            await lock_connection.execute(
                select(ingestion_resources.c.resource_key)
                .where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
                .with_for_update()
            )
            try:
                claim_task = asyncio.create_task(
                    contender.claim_resource(contender_run, key, refresh=False)
                )
                await wait_until_backend_waits_on_lock(harness.engine, contender_pid)
                assert not claim_task.done()
                clock.value = BASE_TIME + timedelta(seconds=10)
                await transaction.commit()
                decision = await asyncio.wait_for(claim_task, timeout=2.0)
            finally:
                if transaction.is_active:
                    await transaction.rollback()
    finally:
        await cancel_claim_tasks(claim_task)
        await contender_engine.dispose()

    assert decision.action is ClaimAction.FETCH
    assert decision.claim == ResourceClaim(key=key, run_id=contender_run, version=2)
    async with harness.engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    ingestion_resources.c.claimed_until,
                    ingestion_resources.c.updated_at,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()
    assert row.updated_at == BASE_TIME + timedelta(seconds=10)
    assert row.claimed_until == BASE_TIME + timedelta(seconds=610)


async def test_waiting_for_run_lock_gets_full_lease_from_post_lock_time(
    harness: StoreHarness,
) -> None:
    contender_engine = create_async_engine(
        runtime_config.postgres_url(),
        pool_size=1,
        max_overflow=0,
    )
    clock = MutableClock(BASE_TIME)
    contender = PostgresHistoricalIngestionStore(
        contender_engine,
        source=harness.source,
        lease_seconds=600,
        utc_now=clock,
    )
    contender_run = await harness.start_run(contender)
    contender_pid = await backend_pid(contender_engine)
    key = ResourceKey.box_score(GameId("0022500820"))
    claim_task: asyncio.Task[ClaimDecision] | None = None
    try:
        async with harness.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            lock_pid = await lock_connection.scalar(text("SELECT pg_backend_pid()"))
            assert type(lock_pid) is int
            assert contender_pid != lock_pid
            await lock_connection.execute(
                select(ingestion_runs.c.run_id)
                .where(ingestion_runs.c.run_id == contender_run)
                .with_for_update()
            )
            try:
                claim_task = asyncio.create_task(
                    contender.claim_resource(contender_run, key, refresh=False)
                )
                await wait_until_backend_waits_on_lock(harness.engine, contender_pid)
                assert not claim_task.done()
                clock.value = BASE_TIME + timedelta(seconds=10)
                await transaction.commit()
                decision = await asyncio.wait_for(claim_task, timeout=2.0)
            finally:
                if transaction.is_active:
                    await transaction.rollback()
    finally:
        await cancel_claim_tasks(claim_task)
        await contender_engine.dispose()

    assert decision.action is ClaimAction.FETCH
    assert decision.claim == ResourceClaim(key=key, run_id=contender_run, version=1)
    async with harness.engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    ingestion_resources.c.claimed_until,
                    ingestion_resources.c.updated_at,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()
    assert row.updated_at == BASE_TIME + timedelta(seconds=10)
    assert row.claimed_until == BASE_TIME + timedelta(seconds=610)


async def test_concurrent_stores_grant_only_one_missing_resource_claim(
    harness: StoreHarness,
) -> None:
    first_engine = create_async_engine(
        runtime_config.postgres_url(),
        pool_size=1,
        max_overflow=0,
    )
    second_engine = create_async_engine(
        runtime_config.postgres_url(),
        pool_size=1,
        max_overflow=0,
    )
    first_store = PostgresHistoricalIngestionStore(
        first_engine,
        source=harness.source,
        utc_now=lambda: BASE_TIME,
    )
    second_store = PostgresHistoricalIngestionStore(
        second_engine,
        source=harness.source,
        utc_now=lambda: BASE_TIME,
    )
    key = ResourceKey.box_score(GameId("0022500815"))
    first_run = await harness.start_run(first_store)
    second_run = await harness.start_run(second_store)
    first_pid = await backend_pid(first_engine)
    second_pid = await backend_pid(second_engine)
    first_task: asyncio.Task[ClaimDecision] | None = None
    second_task: asyncio.Task[ClaimDecision] | None = None
    try:
        async with harness.engine.connect() as lock_connection:
            transaction = await lock_connection.begin()
            lock_pid = await lock_connection.scalar(text("SELECT pg_backend_pid()"))
            assert type(lock_pid) is int
            assert len({first_pid, second_pid, lock_pid}) == 3
            await lock_connection.execute(
                select(ingestion_runs.c.run_id)
                .where(ingestion_runs.c.run_id == first_run)
                .with_for_update()
            )
            try:
                first_task = asyncio.create_task(
                    first_store.claim_resource(first_run, key, refresh=False)
                )
                await wait_until_backend_waits_on_lock(harness.engine, first_pid)
                assert not first_task.done()

                second_task = asyncio.create_task(
                    second_store.claim_resource(second_run, key, refresh=False)
                )
                await wait_until_backend_waits_on_lock(harness.engine, second_pid)
                assert not second_task.done()

                await transaction.commit()
                decisions = await asyncio.wait_for(
                    asyncio.gather(first_task, second_task),
                    timeout=2.0,
                )
            finally:
                if transaction.is_active:
                    await transaction.rollback()
    finally:
        await cancel_claim_tasks(first_task, second_task)
        await first_engine.dispose()
        await second_engine.dispose()

    assert {decision.action for decision in decisions} == {
        ClaimAction.FETCH,
        ClaimAction.BUSY,
    }
    granted = next(decision for decision in decisions if decision.action is ClaimAction.FETCH)
    assert granted.claim is not None
    assert granted.claim.version == 1
    async with harness.engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    ingestion_resources.c.attempt_count,
                    ingestion_resources.c.claim_version,
                    ingestion_resources.c.last_run_id,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()
    assert (row.attempt_count, row.claim_version, row.last_run_id) == (
        1,
        1,
        granted.claim.run_id,
    )


async def test_stale_claim_cannot_save_or_fail_a_newer_owner(
    harness: StoreHarness,
) -> None:
    first_store = harness.store(now=BASE_TIME, lease_seconds=1)
    second_store = harness.store(now=BASE_TIME + timedelta(seconds=1), lease_seconds=1)
    key = ResourceKey.box_score(GameId("0022500816"))
    first_run = await harness.start_run(first_store)
    first_claim = cast(
        ResourceClaim,
        (await first_store.claim_resource(first_run, key, refresh=False)).claim,
    )
    first_document = make_document(harness, key, {"owner": "first"})
    first_document_id = await first_store.save_document(first_claim, first_document)

    second_run = await harness.start_run(second_store)
    second_claim = cast(
        ResourceClaim,
        (await second_store.claim_resource(second_run, key, refresh=False)).claim,
    )
    assert second_claim.version == 2

    stale_document = make_document(harness, key, {"owner": "stale"})
    with pytest.raises(ClaimLostError, match=r"^claim_lost$"):
        await first_store.save_document(first_claim, stale_document)
    with pytest.raises(ClaimLostError, match=r"^claim_lost$"):
        await first_store.fail_resource(
            first_claim,
            error_code="schema_mismatch",
            error_type="SecretPayloadError",
        )

    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_type == key.resource_type.value,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )
        document_count = await connection.scalar(
            select(func.count())
            .select_from(source_documents)
            .where(source_documents.c.source == harness.source)
        )

    assert row["status"] == "running"
    assert row["last_run_id"] == second_run
    assert row["claim_version"] == 2
    assert row["latest_document_id"] == first_document_id
    assert row["last_error_code"] is None
    assert row["last_error_type"] is None
    assert document_count == 1


async def test_document_identity_and_error_fields_are_validated_before_sql(
    harness: StoreHarness,
) -> None:
    store = harness.store()
    key = ResourceKey.box_score(GameId("0022500817"))
    run_id = await harness.start_run(store)
    claim = cast(
        ResourceClaim,
        (await store.claim_resource(run_id, key, refresh=False)).claim,
    )
    wrong_key = ResourceKey.box_score(GameId("0022500818"))

    with pytest.raises(ValueError, match="resource key"):
        await store.save_document(claim, make_document(harness, wrong_key, {"bad": True}))
    with pytest.raises(ValueError, match="source"):
        await store.save_document(
            claim,
            make_document(harness, key, {"bad": True}, source="other-provider"),
        )
    with pytest.raises(ValueError, match="error code"):
        await store.fail_resource(
            claim,
            error_code="schema_mismatch: raw secret message",
            error_type="PayloadMappingError",
        )
    with pytest.raises(ValueError, match="error type"):
        await store.fail_resource(
            claim,
            error_code="schema_mismatch",
            error_type="ValueError: postgresql://secret",
        )

    async with harness.engine.connect() as connection:
        document_count = await connection.scalar(
            select(func.count())
            .select_from(source_documents)
            .where(source_documents.c.source.in_((harness.source, "other-provider")))
        )
        row = (
            await connection.execute(
                select(
                    ingestion_resources.c.status,
                    ingestion_resources.c.last_error_code,
                    ingestion_resources.c.last_error_type,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()

    assert document_count == 0
    assert (row.status, row.last_error_code, row.last_error_type) == (
        "running",
        None,
        None,
    )


async def test_save_document_revalidates_the_exact_insert_payload_checksum_before_sql(
    harness: StoreHarness,
) -> None:
    store = harness.store()
    key = ResourceKey.box_score(GameId("0022500821"))
    run_id = await harness.start_run(store)
    claim = cast(
        ResourceClaim,
        (await store.claim_resource(run_id, key, refresh=False)).claim,
    )
    document = make_document(harness, key, {"nested": {"values": [1, 2]}})
    object.__setattr__(document, "checksum_sha256", "0" * 64)

    with pytest.raises(ValueError, match="checksum"):
        await store.save_document(claim, document)

    async with harness.engine.connect() as connection:
        document_count = await connection.scalar(
            select(func.count())
            .select_from(source_documents)
            .where(source_documents.c.source == harness.source)
        )
        resource = (
            await connection.execute(
                select(
                    ingestion_resources.c.status,
                    ingestion_resources.c.latest_document_id,
                ).where(
                    ingestion_resources.c.source == harness.source,
                    ingestion_resources.c.resource_type == key.resource_type.value,
                    ingestion_resources.c.resource_key == str(key),
                )
            )
        ).one()

    assert document_count == 0
    assert resource == ("running", None)
