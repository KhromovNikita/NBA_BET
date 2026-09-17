"""Real database contracts for dual-input selection, snapshots and fenced claims."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    BuildLineupsResult,
    LineupBuildStatus,
    LineupClaimAction,
)
from nba_live_totals.domain import GameId, SeasonId, canonical_json_checksum
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    lineup_rebuild_resources,
    lineup_rebuild_runs,
    lineup_snapshot_attempts,
    play_by_play_events,
    player_box_scores,
    players,
    source_documents,
)
from tests import runtime_config
from tests.integration.lineup_harness import LineupHarness
from tests.integration.possession_harness import BASE_TIME, PossessionHarness

pytestmark = pytest.mark.integration
GAME = GameId("0022500111")
REQUEST = BuildLineupsRequest(seasons=(SeasonId("2025-26"),))
VERSIONS = LineupVersions("nba-v1", "lineup-v1", "nba-v1", "lineups-v1")


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
async def harness() -> AsyncIterator[LineupHarness]:
    engine = create_async_engine(runtime_config.postgres_url())
    value = LineupHarness(engine)
    try:
        yield value
    finally:
        await value.cleanup()
        await engine.dispose()


def store_for(harness: LineupHarness, *, elapsed: int = 0) -> PostgresLineupWorkStore:
    return PostgresLineupWorkStore(
        harness.engine,
        source=harness.source,
        utc_now=lambda: BASE_TIME + timedelta(seconds=elapsed),
    )


async def start_run(store: PostgresLineupWorkStore, harness: LineupHarness) -> UUID:
    run = await store.start_run(REQUEST)
    harness.run_ids.append(run)
    return run


@pytest.mark.parametrize("status", [LineupBuildStatus.PARTIAL, LineupBuildStatus.INTERRUPTED])
async def test_finish_run_persists_exact_quality_and_operational_counters_once(
    harness: LineupHarness, status: LineupBuildStatus
) -> None:
    store = store_for(harness)
    run_id = await start_run(store, harness)
    result = BuildLineupsResult(run_id, status, 15, 1, 6, 2, 3, 4, 5)
    with pytest.raises(ValueError, match="match"):
        await store.finish_run(uuid4(), result)
    await store.finish_run(run_id, result)
    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(lineup_rebuild_runs).where(lineup_rebuild_runs.c.run_id == run_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == status.value
    assert tuple(
        row[name]
        for name in (
            "discovered",
            "materialized",
            "invalid",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ) == (15, 1, 6, 2, 3, 4, 5)
    assert row["finished_at"] == BASE_TIME
    with pytest.raises(RuntimeError, match="terminal"):
        await store.finish_run(run_id, result)


async def durable_rows(harness: LineupHarness) -> tuple[list[dict[str, object]], ...]:
    async with harness.engine.connect() as connection:
        rows = []
        for table in (lineup_rebuild_resources, lineup_snapshot_attempts):
            rows.append(
                [
                    dict(row)
                    for row in (
                        await connection.execute(
                            select(table)
                            .where(table.c.game_id == GAME.value)
                            .order_by(*table.primary_key.columns)
                        )
                    ).mappings()
                ]
            )
        return tuple(rows)


async def test_selection_orders_final_regular_and_playoff_games(harness: LineupHarness) -> None:
    await harness.seed_game(game_id="0022500112")
    await harness.seed_game(season_type="playoffs")
    await harness.seed_game(game_id="0022500113", status="live")
    await harness.seed_game(game_id="0022400115", season="2024-25")
    store = store_for(harness)
    assert await store.select_games(REQUEST) == (GAME, GameId("0022500112"))
    assert await store.select_games(replace(REQUEST, game_id=GAME)) == (GAME,)


@pytest.mark.parametrize("kind", ["box_score", "play_by_play"])
@pytest.mark.parametrize(
    "defect", ["missing", "failed", "unapplied", "wrong_key", "wrong_source", "wrong_type"]
)
async def test_each_exact_applied_resource_is_required(
    harness: LineupHarness,
    kind: str,
    defect: str,
) -> None:
    await harness.seed_game()
    async with harness.engine.begin() as connection:
        predicate = (
            ingestion_resources.c.source == harness.source,
            ingestion_resources.c.resource_key == f"{kind}:{GAME.value}",
        )
        if defect == "missing":
            await connection.execute(delete(ingestion_resources).where(*predicate))
        else:
            changes: dict[str, dict[str, object]] = {
                "failed": {"status": "failed"},
                "unapplied": {"applied_document_id": None},
                "wrong_key": {"resource_key": f"{kind}:0022500999"},
                "wrong_source": {"source": harness.source + "-wrong"},
                "wrong_type": {"resource_type": "schedule"},
            }
            values = changes[defect]
            await connection.execute(update(ingestion_resources).where(*predicate).values(**values))
    store = store_for(harness)
    assert await store.select_games(REQUEST) == ()
    run = await start_run(store, harness)
    with pytest.raises(RuntimeError):
        await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert await durable_rows(harness) == ([], [])


async def test_snapshot_and_fence_use_both_applied_documents_and_scalar_rows(
    harness: LineupHarness,
) -> None:
    box_id, pbp_id = await harness.seed_game()
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(ingestion_resources)
            .where(ingestion_resources.c.source == harness.source)
            .values(latest_document_id=pbp_id)
        )
    store = store_for(harness)
    run = await start_run(store, harness)
    decision = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert decision.action is LineupClaimAction.BUILD
    assert decision.claim is not None
    claim = decision.claim
    assert claim.snapshot.applied_box_score_document_id == box_id
    assert claim.snapshot.applied_pbp_document_id == pbp_id
    assert claim.snapshot.box_score_document.payload == {"game_id": GAME.value, "raw": "box-v1"}
    assert [row.player_id for row in claim.snapshot.canonical_player_boxes] == [930001, 930002]
    assert [event.sequence_number for event in claim.snapshot.canonical_events] == [1, 2]
    assert (claim.snapshot.game.home_team_id, claim.snapshot.game.away_team_id) == (910001, 910002)
    resources, attempts = await durable_rows(harness)
    resource, attempt = resources[0], attempts[0]
    assert (resource["attempt_count"], resource["claim_version"], resource["next_generation"]) == (
        1,
        1,
        2,
    )
    assert (str(attempt["input_box_score_document_id"]), str(attempt["input_pbp_document_id"])) == (
        str(box_id),
        str(pbp_id),
    )
    assert (
        str(resource["active_run_id"]),
        resource["active_claim_version"],
        str(resource["active_snapshot_id"]),
        resource["active_lineup_revision_id"],
        resource["active_input_token_sha256"],
        resource["claimed_until"],
    ) == (
        str(run),
        1,
        str(claim.attempt.snapshot_id),
        claim.attempt.lineup_revision_id,
        claim.attempt.input_token.sha256,
        BASE_TIME + timedelta(seconds=300),
    )
    before = await durable_rows(harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(players)
            .where(players.c.player_id == 930001)
            .values(full_name="Changed elsewhere")
        )
    resumed = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert resumed.action is LineupClaimAction.RESUME
    assert resumed.claim == claim
    assert await durable_rows(harness) == before
    other = await start_run(store, harness)
    assert (
        await store.claim_game(other, GAME, VERSIONS, rebuild=False)
    ).action is LineupClaimAction.BUSY
    assert await durable_rows(harness) == before


@pytest.mark.parametrize("invalid", [False, True])
async def test_current_valid_and_invalid_skip_and_forced_rebuild(
    harness: LineupHarness, invalid: bool
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    first = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert first.claim is not None
    await harness.publish(first.claim, invalid=invalid)
    before = await durable_rows(harness)
    skipped = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert skipped.action is LineupClaimAction.SKIP_CURRENT
    assert skipped.current is not None
    assert skipped.current.result_status.value == ("invalid" if invalid else "valid")
    assert await durable_rows(harness) == before
    rebuilt = await store.claim_game(run, GAME, VERSIONS, rebuild=True)
    assert rebuilt.action is LineupClaimAction.BUILD
    assert rebuilt.claim is not None and rebuilt.claim.attempt.generation == 2
    assert rebuilt.claim.attempt.lineup_revision_id == first.claim.attempt.lineup_revision_id
    assert (await durable_rows(harness))[0][0]["published_snapshot_id"] == before[0][0][
        "published_snapshot_id"
    ]


@pytest.mark.parametrize(
    "change",
    [
        "box_uuid",
        "pbp_uuid",
        "box_checksum",
        "pbp_checksum",
        "player",
        "event",
        "game",
        "normalizer_version",
        "reconstructor_version",
        "rules_version",
        "output_schema_version",
    ],
)
@pytest.mark.parametrize("published", [False, True])
async def test_every_input_and_version_change_invalidates_claim_or_pointer(
    harness: LineupHarness,
    change: str,
    published: bool,
) -> None:
    box_id, pbp_id = await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    first = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert first.claim is not None
    if published:
        await harness.publish(first.claim)
    versions = VERSIONS
    async with harness.engine.begin() as connection:
        if change.endswith("version"):
            versions = replace(VERSIONS, **{change: "changed-v2"})
        elif change.startswith(("box_", "pbp_")):
            kind = "box_score" if change.startswith("box") else "play_by_play"
            doc_id = box_id if kind == "box_score" else pbp_id
            row = dict(
                (
                    await connection.execute(
                        select(source_documents).where(source_documents.c.document_id == doc_id)
                    )
                )
                .mappings()
                .one()
            )
            if change.endswith("uuid"):
                row["document_id"] = uuid4()
                row["endpoint"] = str(row["endpoint"]) + "-alternate"
                await connection.execute(source_documents.insert().values(**row))
                await connection.execute(
                    update(ingestion_resources)
                    .where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == f"{kind}:{GAME.value}",
                    )
                    .values(applied_document_id=row["document_id"])
                )
            else:
                # Controlled corruption tests the checksum component independently of UUID.
                await connection.execute(
                    update(source_documents)
                    .where(source_documents.c.document_id == doc_id)
                    .values(
                        payload={"changed": True},
                        checksum_sha256=canonical_json_checksum({"changed": True}),
                    )
                )
        elif change == "player":
            await connection.execute(
                update(player_box_scores)
                .where(player_box_scores.c.game_id == GAME.value)
                .values(position="F")
            )
        elif change == "event":
            await connection.execute(
                update(play_by_play_events)
                .where(play_by_play_events.c.game_id == GAME.value)
                .values(description="changed")
            )
        else:
            await connection.execute(
                update(games).where(games.c.game_id == GAME.value).values(home_score=111)
            )
    second = await store.claim_game(run, GAME, versions, rebuild=False)
    assert second.action is LineupClaimAction.BUILD
    assert second.claim is not None
    assert second.claim.attempt.generation == 2
    assert second.claim.attempt.input_token.sha256 != first.claim.attempt.input_token.sha256
    assert (
        second.claim.attempt.lineup_revision_id == first.claim.attempt.lineup_revision_id
    ) == change.endswith("uuid")
    resources, attempts = await durable_rows(harness)
    old = next(row for row in attempts if row["generation"] == 1)
    assert old["state"] == ("published" if published else "superseded")
    if published:
        assert resources[0]["publication_state"] == "stale"
        assert str(resources[0]["published_snapshot_id"]) == str(first.claim.attempt.snapshot_id)


async def test_expired_lease_and_simultaneous_first_claim(harness: LineupHarness) -> None:
    await harness.seed_game()
    store = store_for(harness)
    runs = [await start_run(store, harness), await start_run(store, harness)]
    decisions = await asyncio.gather(
        *(store.claim_game(run, GAME, VERSIONS, rebuild=False) for run in runs)
    )
    assert sorted(decision.action.value for decision in decisions) == ["build", "busy"]
    expired_store = store_for(harness, elapsed=300)
    next_claim = await expired_store.claim_game(runs[1], GAME, VERSIONS, rebuild=False)
    assert next_claim.claim is not None and next_claim.claim.attempt.generation == 2
    resources, attempts = await durable_rows(harness)
    assert sorted(str(row["state"]) for row in attempts) == ["allocated", "superseded"]
    assert (resources[0]["claim_version"], resources[0]["next_generation"]) == (2, 3)


@pytest.mark.parametrize("counter", ["claim_version", "next_generation", "attempt_count"])
async def test_counter_exhaustion_rolls_back_supersession(
    harness: LineupHarness, counter: str
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    async with harness.engine.begin() as connection:
        values = {counter: 9_223_372_036_854_775_807}
        if counter == "claim_version":
            values["active_claim_version"] = values[counter]
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.game_id == GAME.value)
                .values(created_by_claim_version=values[counter])
            )
        await connection.execute(
            update(lineup_rebuild_resources)
            .where(lineup_rebuild_resources.c.game_id == GAME.value)
            .values(**values)
        )
    before = await durable_rows(harness)
    with pytest.raises(RuntimeError, match="exhausted"):
        await store_for(harness, elapsed=301).claim_game(run, GAME, VERSIONS, rebuild=False)
    assert await durable_rows(harness) == before


@pytest.mark.parametrize("state", ["writing", "verified"])
@pytest.mark.parametrize("invalid", [False, True])
async def test_resume_retains_expected_metadata(
    harness: LineupHarness, state: str, invalid: bool
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    first = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert first.claim is not None
    issues = (
        [
            {
                "team_id": 910001,
                "reason": "bad_lineup",
                "sequence_number": 1,
                "boundary_side": "before",
            }
        ]
        if invalid
        else []
    )
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(lineup_snapshot_attempts)
            .where(lineup_snapshot_attempts.c.snapshot_id == first.claim.attempt.snapshot_id)
            .values(
                state=state,
                expected_row_count=7,
                expected_output_sha256="d" * 64,
                expected_issue_codes=issues,
                result_status="invalid" if invalid else "valid",
                insert_started_at=BASE_TIME,
                verified_at=BASE_TIME if state == "verified" else None,
            )
        )
    before = await durable_rows(harness)
    resumed = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert resumed.action is LineupClaimAction.RESUME and resumed.claim is not None
    assert resumed.claim.attempt.state.value == state
    assert resumed.claim.attempt.expected_row_count == 7
    assert resumed.claim.attempt.expected_output_sha256 == "d" * 64
    assert resumed.claim.attempt.expected_issue_codes is not None
    assert len(resumed.claim.attempt.expected_issue_codes) == int(invalid)
    assert await durable_rows(harness) == before


@pytest.mark.parametrize("component", ["run", "claim_version", "revision", "token", "state"])
@pytest.mark.parametrize("path", ["fresh", "expired", "stale"])
async def test_resume_rejects_inconsistent_full_fence_without_mutation(
    harness: LineupHarness, component: str, path: str
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    other_run = await start_run(store, harness)
    await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    async with harness.engine.begin() as connection:
        if component == "state":
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.game_id == GAME.value)
                .values(state="superseded")
            )
        else:
            changes: dict[str, dict[str, object]] = {
                "run": {"active_run_id": other_run},
                "claim_version": {"active_claim_version": 2, "claim_version": 2},
                "revision": {"active_lineup_revision_id": "f" * 64},
                "token": {"active_input_token_sha256": "f" * 64},
            }
            await connection.execute(
                update(lineup_rebuild_resources)
                .where(lineup_rebuild_resources.c.game_id == GAME.value)
                .values(**changes[component])
            )
    before = await durable_rows(harness)
    if path == "expired":
        store = store_for(harness, elapsed=300)
    versions = replace(VERSIONS, normalizer_version="nba-v2") if path == "stale" else VERSIONS
    with pytest.raises(RuntimeError, match="fence"):
        await store.claim_game(run, GAME, versions, rebuild=False)
    assert await durable_rows(harness) == before


@pytest.mark.parametrize("kind", ["lineup", "possession"])
async def test_failed_seed_cleanup_preserves_preexisting_game_lineup_and_player_boxes(
    harness: LineupHarness,
    kind: str,
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    decision = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert decision.claim is not None
    await harness.publish(decision.claim)
    before = await durable_rows(harness)
    async with harness.engine.connect() as connection:
        before_boxes = (
            (
                await connection.execute(
                    select(player_box_scores)
                    .where(player_box_scores.c.game_id == GAME.value)
                    .order_by(player_box_scores.c.player_id)
                )
            )
            .mappings()
            .all()
        )
    competitor = (
        LineupHarness(harness.engine) if kind == "lineup" else PossessionHarness(harness.engine)
    )
    with pytest.raises(IntegrityError):
        await competitor.seed_game(game_id=GAME.value)
    await competitor.cleanup()
    assert await durable_rows(harness) == before
    async with harness.engine.connect() as connection:
        after_boxes = (
            (
                await connection.execute(
                    select(player_box_scores)
                    .where(player_box_scores.c.game_id == GAME.value)
                    .order_by(player_box_scores.c.player_id)
                )
            )
            .mappings()
            .all()
        )
        assert (
            await connection.scalar(select(games.c.game_id).where(games.c.game_id == GAME.value))
            == GAME.value
        )
    assert after_boxes == before_boxes
    owned = (
        competitor.base.game_ids if isinstance(competitor, LineupHarness) else competitor.game_ids
    )
    assert GAME.value not in owned


async def test_expired_forced_rebuild_releases_claim_when_prior_snapshot_is_current(
    harness: LineupHarness,
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    first = await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    assert first.claim is not None
    await harness.publish(first.claim)
    forced = await store.claim_game(run, GAME, VERSIONS, rebuild=True)
    assert forced.claim is not None
    skipped = await store_for(harness, elapsed=300).claim_game(run, GAME, VERSIONS, rebuild=False)
    assert skipped.action is LineupClaimAction.SKIP_CURRENT
    resources, attempts = await durable_rows(harness)
    assert resources[0]["active_run_id"] is None
    assert resources[0]["claimed_until"] is None
    assert resources[0]["next_generation"] == 3
    assert next(row for row in attempts if row["generation"] == 2)["state"] == "superseded"


async def test_missing_run_rolls_back_new_resource(harness: LineupHarness) -> None:
    await harness.seed_game()
    with pytest.raises(RuntimeError, match="running materialize"):
        await store_for(harness).claim_game(uuid4(), GAME, VERSIONS, rebuild=False)
    assert await durable_rows(harness) == ([], [])


async def test_query_lock_order_and_player_projection(harness: LineupHarness) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    statements: list[str] = []
    lock_parameters: list[str] = []

    def record_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            lock_parameters.append(str(parameters))

    event.listen(harness.engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await store.claim_game(run, GAME, VERSIONS, rebuild=False)
    finally:
        event.remove(harness.engine.sync_engine, "before_cursor_execute", record_sql)
    lock_statements = [
        statement
        for statement in statements
        if "FOR UPDATE" in statement or "FOR SHARE" in statement
    ]
    assert len(lock_statements) == 5
    assert "box_score:0022500111" in lock_parameters[0]
    assert "play_by_play:0022500111" in lock_parameters[1]
    assert [statement.split("FROM ")[1].split()[0] for statement in lock_statements] == [
        "ingestion_resources",
        "ingestion_resources",
        "games",
        "lineup_rebuild_resources",
        "lineup_rebuild_runs",
    ]
    player_query = next(
        statement for statement in statements if "FROM player_box_scores" in statement
    )
    assert "JOIN" not in player_query
    assert "jersey_number" not in player_query and "seconds_played" not in player_query
    assert not any(
        "FROM players" in statement or "JOIN players" in statement for statement in statements
    )


async def wait_for_blocker(harness: LineupHarness, blocker_pid: int) -> None:
    async with asyncio.timeout(5):
        async with harness.engine.connect() as observer:
            while not await observer.scalar(  # noqa: ASYNC110 - observe database lock state
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                ),
                {"pid": blocker_pid},
            ):
                await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", ["box_score", "play_by_play"])
async def test_writer_holding_either_resource_blocks_snapshot_until_commit(
    harness: LineupHarness, kind: str
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    async with harness.engine.connect() as writer:
        transaction = await writer.begin()
        pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        await writer.execute(
            select(ingestion_resources)
            .where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_key == f"{kind}:{GAME.value}",
            )
            .with_for_update()
        )
        task = asyncio.create_task(store.claim_game(run, GAME, VERSIONS, rebuild=False))
        try:
            await wait_for_blocker(harness, pid)
            assert not task.done()
            table = player_box_scores if kind == "box_score" else play_by_play_events
            values = (
                {"position": "F"} if kind == "box_score" else {"description": "committed event"}
            )
            await writer.execute(
                update(table).where(table.c.game_id == GAME.value).values(**values)
            )
            await transaction.commit()
            result = await asyncio.wait_for(task, 5)
            assert result.claim is not None
            if kind == "box_score":
                assert result.claim.snapshot.canonical_player_boxes[0].position == "F"
            else:
                assert result.claim.snapshot.canonical_events[0].description == "committed event"
        finally:
            if transaction.is_active:
                await transaction.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["box_score", "play_by_play"])
async def test_snapshot_holds_both_resources_before_game_lock_and_through_canonical_reads(
    harness: LineupHarness, kind: str
) -> None:
    await harness.seed_game()
    store = store_for(harness)
    run = await start_run(store, harness)
    async with harness.engine.connect() as game_blocker, harness.engine.connect() as writer:
        game_transaction = await game_blocker.begin()
        writer_transaction = await writer.begin()
        game_pid = await game_blocker.scalar(text("SELECT pg_backend_pid()"))
        await game_blocker.execute(
            select(games).where(games.c.game_id == GAME.value).with_for_update()
        )
        claim_task = asyncio.create_task(store.claim_game(run, GAME, VERSIONS, rebuild=False))
        writer_task = None
        try:
            await wait_for_blocker(harness, game_pid)

            # The claimant is waiting at game; it must already own both inputs.
            async def change_rows() -> None:
                await writer.execute(
                    select(ingestion_resources)
                    .where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == f"{kind}:{GAME.value}",
                    )
                    .with_for_update()
                )
                table = player_box_scores if kind == "box_score" else play_by_play_events
                values = {"position": "F"} if kind == "box_score" else {"description": "new event"}
                await writer.execute(
                    update(table).where(table.c.game_id == GAME.value).values(**values)
                )
                await writer_transaction.commit()

            writer_task = asyncio.create_task(change_rows())
            async with harness.engine.connect() as observer:
                claimant_pid = await observer.scalar(
                    text(
                        "SELECT pid FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                    ),
                    {"pid": game_pid},
                )
            await wait_for_blocker(harness, claimant_pid)
            assert not writer_task.done()
            await game_transaction.commit()
            result = await asyncio.wait_for(claim_task, 5)
            assert result.claim is not None
            assert result.claim.snapshot.canonical_player_boxes[0].position == "G"
            assert result.claim.snapshot.canonical_events[0].description == "test event"
            await asyncio.wait_for(writer_task, 5)
        finally:
            if game_transaction.is_active:
                await game_transaction.rollback()
            for task in (claim_task, writer_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (claim_task, writer_task) if task is not None),
                return_exceptions=True,
            )
            if writer_transaction.is_active:
                await writer_transaction.rollback()
