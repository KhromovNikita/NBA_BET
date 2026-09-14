"""Real PostgreSQL/ClickHouse crash-protocol acceptance for Phase 3."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    HistoricalIngestionService,
    ResourceKey,
)
from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    ClaimAction,
    PossessionAttemptState,
    PossessionBuildClaim,
    PossessionBuildService,
    PossessionParser,
)
from nba_live_totals.domain import (
    AnalyticalPossessionParser,
    BoxScore,
    GameId,
    JsonObject,
    PlayByPlayEvent,
    PossessionParseContext,
    PossessionParseResult,
    PossessionVersions,
    SeasonId,
    SeasonSchedule,
    SourceDocument,
    canonical_json_checksum,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient, ClickHouseError
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer
from nba_live_totals.infrastructure.nba.mappers import (
    parse_box_score,
    parse_play_by_play,
    parse_schedule,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import PostgresHistoricalIngestionStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)
from tests import runtime_config

pytestmark = pytest.mark.integration

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "nba" / "possessions"
SEASON = SeasonId("2098-99")
GAME_ID = GameId("0029899001")
HOME_TEAM_ID = 91_317_001
AWAY_TEAM_ID = 91_317_002
PLAYER_IDS = (91_317_101, 91_317_201)
VERSIONS = PossessionVersions("nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1")
REQUESTED_AT = datetime(2099, 1, 3, 3, tzinfo=UTC)
RESOURCE_KEYS = (
    str(ResourceKey.schedule(SEASON)),
    str(ResourceKey.box_score(GAME_ID)),
    str(ResourceKey.play_by_play(GAME_ID)),
)


@pytest.fixture(scope="module", autouse=True)
def ensure_pinned_possession_schema() -> None:
    """Restore both schema planes after destructive migration-contract tests."""
    environment = os.environ | {
        "DATABASE_URL": runtime_config.postgres_url(),
        "CLICKHOUSE_URL": runtime_config.clickhouse_url(),
        "CLICKHOUSE_USER": "nba",
        "CLICKHOUSE_PASSWORD": "nba",
    }
    for command in (
        (sys.executable, "-m", "alembic", "upgrade", "head"),
        (sys.executable, "-m", "scripts.migrate_clickhouse"),
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert result.returncode == 0, result.stderr


def load_fixture(name: str) -> JsonObject:
    return cast(JsonObject, json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")))


class FixtureSource:
    """Serve dedicated Phase 3 fixtures through the real NBA mappers."""

    def __init__(self) -> None:
        self.payloads = {
            RESOURCE_KEYS[0]: load_fixture("possession_schedule_2098-99.json"),
            RESOURCE_KEYS[1]: load_fixture("possession_box_score_0029899001.json"),
            RESOURCE_KEYS[2]: load_fixture("possession_play_by_play_0029899001.json"),
        }
        self.fetches: list[ResourceKey] = []

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument:
        return self._document(
            ResourceKey.schedule(season),
            "scheduleleaguev2",
            {"LeagueID": "00", "Season": season.value},
        )

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        return self._document(
            ResourceKey.box_score(game_id),
            "boxscoretraditionalv3",
            {
                "GameID": game_id.value,
                "StartPeriod": 0,
                "EndPeriod": 0,
                "StartRange": 0,
                "EndRange": 0,
                "RangeType": 0,
            },
        )

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        return self._document(
            ResourceKey.play_by_play(game_id),
            "playbyplayv3",
            {"GameID": game_id.value, "StartPeriod": 0, "EndPeriod": 0},
        )

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule:
        return parse_schedule(document)

    def parse_box_score(self, document: SourceDocument) -> BoxScore:
        return parse_box_score(document)

    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
        return parse_play_by_play(document)

    def _document(self, key: ResourceKey, endpoint: str, parameters: JsonObject) -> SourceDocument:
        self.fetches.append(key)
        payload = deepcopy(self.payloads[str(key)])
        return SourceDocument(
            source="nba.com",
            endpoint=endpoint,
            resource_key=str(key),
            requested_at=REQUESTED_AT,
            request_parameters=parameters,
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )


class TrackingIngestionStore(PostgresHistoricalIngestionStore):
    def __init__(self, engine: AsyncEngine, run_ids: list[UUID]) -> None:
        super().__init__(engine, source="nba.com")
        self.run_ids = run_ids

    async def start_run(self, request: BackfillRequest) -> UUID:
        run_id = await super().start_run(request)
        self.run_ids.append(run_id)
        return run_id


class TrackingWorkStore(PostgresPossessionWorkStore):
    def __init__(self, engine: AsyncEngine, run_ids: list[UUID], **kwargs: object) -> None:
        super().__init__(engine, source="nba.com", **kwargs)  # type: ignore[arg-type]
        self.run_ids = run_ids
        self.poison_error: Exception | None = None

    async def start_run(self, request: BuildPossessionsRequest) -> UUID:
        run_id = await super().start_run(request)
        self.run_ids.append(run_id)
        return run_id

    async def poison_attempt(self, *args: object, **kwargs: object) -> None:
        try:
            await super().poison_attempt(*args, **kwargs)  # type: ignore[arg-type]
        except Exception as error:
            self.poison_error = error
            raise


class FailingAfterStartWorkStore(TrackingWorkStore):
    async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]:
        raise RuntimeError("simulated failure immediately after start_run")


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2099, 1, 4, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class RecordingClickHouse:
    """Keep the real store boundary while injecting one transport outcome."""

    def __init__(self, client: ClickHouseClient, *, behavior: str = "normal") -> None:
        self.client = client
        self.behavior = behavior
        self.insert_calls = 0
        self.query_calls = 0

    async def insert_json_each_row(self, table: str, rows: Sequence[Mapping[str, object]]) -> None:
        self.insert_calls += 1
        if self.behavior == "missing":
            raise ClickHouseError("clickhouse_unavailable")
        outgoing = [dict(row) for row in rows]
        if self.behavior == "mismatch":
            outgoing[0]["offense_points"] = cast(int, outgoing[0]["offense_points"]) + 1
        await self.client.insert_json_each_row(table, outgoing)
        if self.behavior == "response_loss":
            raise ClickHouseError("clickhouse_timeout")

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.query_calls += 1
        if self.behavior == "unavailable":
            raise ClickHouseError("clickhouse_unavailable")
        return await self.client.query(statement, parameters=parameters)


class PausedReadClickHouse(RecordingClickHouse):
    """Pause the verify read so a distinct current snapshot can be published."""

    def __init__(self, client: ClickHouseClient) -> None:
        super().__init__(client)
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.read_started.set()
        await self.release_read.wait()
        return await super().query(statement, parameters=parameters)


class Phase3Harness:
    """Own only the far-future fixture identities in both stores."""

    def __init__(self, engine: AsyncEngine, client: ClickHouseClient) -> None:
        self.engine = engine
        self.client = client
        self.source = FixtureSource()
        self.ingestion_run_ids: list[UUID] = []
        self.possession_run_ids: list[UUID] = []
        self.ingestion_store = TrackingIngestionStore(engine, self.ingestion_run_ids)
        self.ingestion = HistoricalIngestionService(
            self.source, self.ingestion_store, max_concurrency=1
        )

    async def cleanup(self) -> None:
        await self.client.execute(
            "ALTER TABLE nba.possessions_v1 DELETE WHERE season_start_year = 2098 "
            "AND game_id = '0029899001' SETTINGS mutations_sync = 2"
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(possession_rebuild_resources).where(
                    possession_rebuild_resources.c.game_id == GAME_ID.value
                )
            )
            await connection.execute(
                delete(possession_snapshot_attempts).where(
                    possession_snapshot_attempts.c.game_id == GAME_ID.value
                )
            )
            await connection.execute(
                delete(play_by_play_events).where(play_by_play_events.c.game_id == GAME_ID.value)
            )
            await connection.execute(
                delete(player_box_scores).where(player_box_scores.c.game_id == GAME_ID.value)
            )
            await connection.execute(
                delete(team_box_scores).where(team_box_scores.c.game_id == GAME_ID.value)
            )
            await connection.execute(delete(games).where(games.c.game_id == GAME_ID.value))
            await connection.execute(delete(players).where(players.c.player_id.in_(PLAYER_IDS)))
            await connection.execute(
                delete(teams).where(teams.c.team_id.in_((HOME_TEAM_ID, AWAY_TEAM_ID)))
            )
            await connection.execute(delete(seasons).where(seasons.c.season == SEASON.value))
            await connection.execute(
                delete(ingestion_resources).where(
                    ingestion_resources.c.source == "nba.com",
                    ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            await connection.execute(
                delete(source_documents).where(
                    source_documents.c.source == "nba.com",
                    source_documents.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            if self.ingestion_run_ids:
                await connection.execute(
                    delete(ingestion_runs).where(
                        ingestion_runs.c.run_id.in_(self.ingestion_run_ids)
                    )
                )
            if self.possession_run_ids:
                await connection.execute(
                    delete(possession_rebuild_runs).where(
                        possession_rebuild_runs.c.run_id.in_(self.possession_run_ids)
                    )
                )

    async def assert_empty(self) -> None:
        async with self.engine.connect() as connection:
            scoped_counts = {
                "games": await connection.scalar(
                    select(func.count()).select_from(games).where(games.c.game_id == GAME_ID.value)
                ),
                "events": await connection.scalar(
                    select(func.count())
                    .select_from(play_by_play_events)
                    .where(play_by_play_events.c.game_id == GAME_ID.value)
                ),
                "team_box_scores": await connection.scalar(
                    select(func.count())
                    .select_from(team_box_scores)
                    .where(team_box_scores.c.game_id == GAME_ID.value)
                ),
                "player_box_scores": await connection.scalar(
                    select(func.count())
                    .select_from(player_box_scores)
                    .where(player_box_scores.c.game_id == GAME_ID.value)
                ),
                "possession_resources": await connection.scalar(
                    select(func.count())
                    .select_from(possession_rebuild_resources)
                    .where(possession_rebuild_resources.c.game_id == GAME_ID.value)
                ),
                "possession_attempts": await connection.scalar(
                    select(func.count())
                    .select_from(possession_snapshot_attempts)
                    .where(possession_snapshot_attempts.c.game_id == GAME_ID.value)
                ),
                "ingestion_resources": await connection.scalar(
                    select(func.count())
                    .select_from(ingestion_resources)
                    .where(
                        ingestion_resources.c.source == "nba.com",
                        ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                    )
                ),
                "source_documents": await connection.scalar(
                    select(func.count())
                    .select_from(source_documents)
                    .where(
                        source_documents.c.source == "nba.com",
                        source_documents.c.resource_key.in_(RESOURCE_KEYS),
                    )
                ),
                "seasons": await connection.scalar(
                    select(func.count())
                    .select_from(seasons)
                    .where(
                        seasons.c.season == SEASON.value,
                        seasons.c.season_type == "regular_season",
                    )
                ),
                "teams": await connection.scalar(
                    select(func.count())
                    .select_from(teams)
                    .where(teams.c.team_id.in_((HOME_TEAM_ID, AWAY_TEAM_ID)))
                ),
                "players": await connection.scalar(
                    select(func.count())
                    .select_from(players)
                    .where(players.c.player_id.in_(PLAYER_IDS))
                ),
            }
            if self.ingestion_run_ids:
                scoped_counts["ingestion_runs"] = await connection.scalar(
                    select(func.count())
                    .select_from(ingestion_runs)
                    .where(ingestion_runs.c.run_id.in_(self.ingestion_run_ids))
                )
            if self.possession_run_ids:
                scoped_counts["possession_runs"] = await connection.scalar(
                    select(func.count())
                    .select_from(possession_rebuild_runs)
                    .where(possession_rebuild_runs.c.run_id.in_(self.possession_run_ids))
                )
        rows = await self.client.query(
            "SELECT count() AS count FROM nba.possessions_v1 "
            "WHERE season_start_year = 2098 "
            "AND game_id = {game_id:FixedString(10)}",
            parameters={"game_id": GAME_ID.value},
        )
        assert all(count == 0 for count in scoped_counts.values()), scoped_counts
        assert rows == [{"count": 0}]

    def work_store(self, *, clock: MutableClock | None = None) -> TrackingWorkStore:
        return TrackingWorkStore(
            self.engine,
            self.possession_run_ids,
            lease_seconds=300,
            **({"utc_now": clock} if clock else {}),
        )

    def service(
        self,
        work_store: TrackingWorkStore,
        client: RecordingClickHouse | None = None,
        parser: PossessionParser | None = None,
    ) -> PossessionBuildService:
        return PossessionBuildService(
            normalizer=NBAPlayByPlayV3PossessionNormalizer(),
            parser=parser or AnalyticalPossessionParser(),
            work_store=work_store,
            snapshot_store=PossessionSnapshotStore(client or RecordingClickHouse(self.client)),
            versions=VERSIONS,
            max_concurrency=1,
        )

    async def resource(self) -> dict[str, object]:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(possession_rebuild_resources).where(
                            possession_rebuild_resources.c.game_id == GAME_ID.value
                        )
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)

    async def attempts(self) -> list[dict[str, object]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(possession_snapshot_attempts)
                        .where(possession_snapshot_attempts.c.game_id == GAME_ID.value)
                        .order_by(possession_snapshot_attempts.c.generation)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def clickhouse_count(self) -> int:
        rows = await self.client.query(
            "SELECT count() AS count FROM nba.possessions_v1 "
            "WHERE season_start_year = 2098 "
            "AND game_id = {game_id:FixedString(10)}",
            parameters={"game_id": GAME_ID.value},
        )
        return cast(int, rows[0]["count"])


async def ingest_fixture(harness: Phase3Harness, *, refresh: bool = False) -> None:
    result = await harness.ingestion.backfill(
        BackfillRequest(seasons=(SEASON,), game_id=GAME_ID, refresh=refresh)
    )
    assert result.discovered == 1
    assert result.failed == 0


def parse_claim(claim: PossessionBuildClaim) -> PossessionParseResult:
    token = claim.attempt.input_token
    context = PossessionParseContext(
        game=claim.attempt.game,
        versions=VERSIONS,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        parse_revision_id=claim.attempt.parse_revision_id,
    )
    return AnalyticalPossessionParser().parse(
        context, NBAPlayByPlayV3PossessionNormalizer().normalize(claim.snapshot)
    )


async def writing_claim(
    store: TrackingWorkStore, claim: PossessionBuildClaim
) -> tuple[PossessionBuildClaim, PossessionParseResult]:
    result = parse_claim(claim)
    assert result.output_sha256 is not None
    attempt = await store.mark_writing(
        claim,
        expected_row_count=len(result.possessions),
        expected_output_sha256=result.output_sha256,
    )
    return replace(claim, attempt=attempt), result


async def test_harness_owns_run_ids_before_the_caller_can_fail(
    harness: Phase3Harness,
) -> None:
    ingestion_run_id = await harness.ingestion_store.start_run(
        BackfillRequest(seasons=(SEASON,), game_id=GAME_ID)
    )
    possession_store = harness.work_store()
    possession_run_id = await possession_store.start_run(
        BuildPossessionsRequest((SEASON,), game_id=GAME_ID)
    )

    assert ingestion_run_id in harness.ingestion_run_ids
    assert possession_run_id in harness.possession_run_ids


async def test_harness_owns_run_id_when_service_fails_immediately_after_start(
    harness: Phase3Harness,
) -> None:
    store = FailingAfterStartWorkStore(
        harness.engine,
        harness.possession_run_ids,
        lease_seconds=300,
    )

    with pytest.raises(RuntimeError, match="simulated failure immediately after start_run"):
        await harness.service(store).build(BuildPossessionsRequest((SEASON,), game_id=GAME_ID))

    assert len(harness.possession_run_ids) == 1
    await harness.cleanup()
    await harness.assert_empty()


@pytest.fixture
async def harness() -> AsyncIterator[Phase3Harness]:
    engine = create_async_engine(runtime_config.postgres_url(), pool_pre_ping=True)
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30
    )
    value = Phase3Harness(engine, client)
    try:
        await value.cleanup()
        await value.assert_empty()
        yield value
    finally:
        try:
            await value.cleanup()
            await value.assert_empty()
        finally:
            await client.aclose()
            await engine.dispose()


async def test_crash_before_write_reclaim_supersedes_without_adopting_attempt(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    clock = MutableClock()
    store = harness.work_store(clock=clock)
    first_run = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    first = await store.claim_game(first_run, GAME_ID, VERSIONS, rebuild=True)
    assert first.action is ClaimAction.BUILD
    assert first.claim is not None
    assert first.claim.attempt.state is PossessionAttemptState.ALLOCATED
    assert await harness.clickhouse_count() == 0

    clock.value += timedelta(seconds=301)
    second_run = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    second = await store.claim_game(second_run, GAME_ID, VERSIONS, rebuild=True)
    assert second.action is ClaimAction.BUILD
    assert second.claim is not None
    assert second.claim.attempt.snapshot_id != first.claim.attempt.snapshot_id
    assert second.claim.attempt.generation == first.claim.attempt.generation + 1
    assert [(row["generation"], row["state"]) for row in await harness.attempts()] == [
        (1, "superseded"),
        (2, "allocated"),
    ]
    resource = await harness.resource()
    assert resource["claim_version"] == 2
    assert resource["active_snapshot_id"] == second.claim.attempt.snapshot_id


async def test_verified_crash_resumes_without_second_insert_and_publishes_exact_rows(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    clock = MutableClock()
    store = harness.work_store(clock=clock)
    run_id = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    decision = await store.claim_game(run_id, GAME_ID, VERSIONS, rebuild=True)
    assert decision.claim is not None
    claim, parsed = await writing_claim(store, decision.claim)
    recording = RecordingClickHouse(harness.client)
    snapshot_store = PossessionSnapshotStore(recording)
    await snapshot_store.write_snapshot(claim, parsed)
    verified = await store.mark_verified(claim, await snapshot_store.verify_snapshot(claim.attempt))
    assert (await harness.resource())["published_snapshot_id"] is None

    resumed = await store.claim_game(run_id, GAME_ID, VERSIONS, rebuild=True)
    assert resumed.action is ClaimAction.RESUME
    assert resumed.claim is not None
    assert resumed.claim.attempt.state is PossessionAttemptState.VERIFIED
    current = await store.publish_snapshot(resumed.claim)
    assert [row["state"] for row in await harness.attempts()] == ["published"]
    assert current.attempt.snapshot_id == verified.snapshot_id
    assert (await harness.resource())["published_snapshot_id"] == verified.snapshot_id
    assert await harness.clickhouse_count() == 7


async def test_writing_crash_same_run_resumes_by_readback_without_second_insert(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    clock = MutableClock()
    store = harness.work_store(clock=clock)
    run_id = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    decision = await store.claim_game(run_id, GAME_ID, VERSIONS, rebuild=True)
    assert decision.claim is not None
    claim, parsed = await writing_claim(store, decision.claim)
    snapshot_store = PossessionSnapshotStore(RecordingClickHouse(harness.client))
    await snapshot_store.write_snapshot(claim, parsed)

    resumed = await store.claim_game(run_id, GAME_ID, VERSIONS, rebuild=True)
    assert resumed.action is ClaimAction.RESUME
    assert resumed.claim is not None
    assert resumed.claim.attempt.state is PossessionAttemptState.WRITING
    verification = await snapshot_store.verify_snapshot(resumed.claim.attempt)
    verified = await store.mark_verified(resumed.claim, verification)
    current = await store.publish_snapshot(replace(resumed.claim, attempt=verified))
    assert current.attempt.snapshot_id == claim.attempt.snapshot_id
    assert current.output_sha256 == parsed.output_sha256
    assert [row["state"] for row in await harness.attempts()] == ["published"]
    assert await harness.clickhouse_count() == 7


@pytest.mark.parametrize(
    ("behavior", "expected_state", "expected_rows"),
    [
        ("response_loss", "published", 7),
        ("missing", "poisoned", 0),
        ("mismatch", "poisoned", 7),
        ("unavailable", "writing", 7),
    ],
)
async def test_ambiguous_write_is_classified_only_by_exact_readback(
    harness: Phase3Harness, behavior: str, expected_state: str, expected_rows: int
) -> None:
    await ingest_fixture(harness)
    store = harness.work_store()
    transport = RecordingClickHouse(harness.client, behavior=behavior)
    result = await harness.service(store, transport).build(
        BuildPossessionsRequest((SEASON,), rebuild=True)
    )
    assert transport.insert_calls == 1
    assert transport.query_calls == 1
    assert result.materialized == (1 if behavior == "response_loss" else 0)
    assert result.failed == (0 if behavior == "response_loss" else 1)
    assert store.poison_error is None, repr(store.poison_error)
    assert [row["state"] for row in await harness.attempts()] == [expected_state]
    assert await harness.clickhouse_count() == expected_rows


@pytest.mark.parametrize("behavior", ["missing", "mismatch"])
async def test_poisoned_same_input_clickhouse_candidate_preserves_old_current(
    harness: Phase3Harness, behavior: str
) -> None:
    await ingest_fixture(harness)
    initial_store = harness.work_store()
    initial = await harness.service(initial_store).build(BuildPossessionsRequest((SEASON,)))
    old = await initial_store.resolve_current(GAME_ID, VERSIONS)
    assert initial.materialized == 1
    assert old is not None

    poisoned_store = harness.work_store()
    poisoned_transport = RecordingClickHouse(harness.client, behavior=behavior)
    poisoned = await harness.service(poisoned_store, poisoned_transport).build(
        BuildPossessionsRequest((SEASON,), rebuild=True)
    )

    resource = await harness.resource()
    assert (poisoned.materialized, poisoned.failed) == (0, 1)
    assert (poisoned_transport.insert_calls, poisoned_transport.query_calls) == (1, 1)
    assert (resource["publication_state"], resource["published_snapshot_id"]) == (
        "current",
        old.attempt.snapshot_id,
    )
    assert await poisoned_store.resolve_current(GAME_ID, VERSIONS) == old
    assert [row["state"] for row in await harness.attempts()] == ["published", "poisoned"]

    rerun_store = harness.work_store()
    rerun_transport = RecordingClickHouse(harness.client)
    rerun = await harness.service(rerun_store, rerun_transport).build(
        BuildPossessionsRequest((SEASON,))
    )
    assert (rerun.skipped_current, rerun.materialized, rerun.failed) == (1, 0, 0)
    assert (rerun_transport.insert_calls, rerun_transport.query_calls) == (0, 0)


@pytest.mark.parametrize("input_change", ["pbp", "game_context"])
async def test_mid_build_input_change_supersedes_candidate_and_preserves_old_pointer(
    harness: Phase3Harness, input_change: str
) -> None:
    await ingest_fixture(harness)
    initial_store = harness.work_store()
    initial = await harness.service(initial_store).build(BuildPossessionsRequest((SEASON,)))
    assert initial.materialized == 1
    old_pointer = (await harness.resource())["published_snapshot_id"]

    store = harness.work_store()
    run_id = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    decision = await store.claim_game(run_id, GAME_ID, VERSIONS, rebuild=True)
    assert decision.claim is not None
    claim, parsed = await writing_claim(store, decision.claim)
    snapshots = PossessionSnapshotStore(RecordingClickHouse(harness.client))
    await snapshots.write_snapshot(claim, parsed)
    claim = replace(
        claim,
        attempt=await store.mark_verified(claim, await snapshots.verify_snapshot(claim.attempt)),
    )
    if input_change == "pbp":
        payload = deepcopy(harness.source.payloads[RESOURCE_KEYS[2]])
        cast(dict[str, object], payload["game"])["videoAvailable"] = 1
        harness.source.payloads[RESOURCE_KEYS[2]] = payload
        await ingest_fixture(harness, refresh=True)
    else:
        async with harness.engine.begin() as connection:
            await connection.execute(
                update(games).where(games.c.game_id == GAME_ID.value).values(home_score=6)
            )

    with pytest.raises(RuntimeError, match="stale"):
        await store.publish_snapshot(claim)
    assert [row["state"] for row in await harness.attempts()] == ["published", "superseded"]
    resource = await harness.resource()
    assert resource["published_snapshot_id"] == old_pointer
    assert resource["publication_state"] == "invalid"


async def test_expired_worker_late_rows_stay_orphaned_behind_new_publication(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    clock = MutableClock()
    store = harness.work_store(clock=clock)
    initial = await harness.service(store).build(BuildPossessionsRequest((SEASON,)))
    assert initial.materialized == 1
    original = await store.resolve_current(GAME_ID, VERSIONS)
    assert original is not None

    old_run = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    old_decision = await store.claim_game(old_run, GAME_ID, VERSIONS, rebuild=True)
    assert old_decision.claim is not None
    old_claim, old_parsed = await writing_claim(store, old_decision.claim)
    clock.value += timedelta(seconds=301)

    new_run = await store.start_run(BuildPossessionsRequest((SEASON,), rebuild=True))
    new_decision = await store.claim_game(new_run, GAME_ID, VERSIONS, rebuild=True)
    assert new_decision.claim is not None
    new_claim, new_parsed = await writing_claim(store, new_decision.claim)
    assert new_claim.attempt.generation == old_claim.attempt.generation + 1

    await PossessionSnapshotStore(RecordingClickHouse(harness.client)).write_snapshot(
        old_claim, old_parsed
    )
    new_snapshots = PossessionSnapshotStore(RecordingClickHouse(harness.client))
    await new_snapshots.write_snapshot(new_claim, new_parsed)
    new_verification = await new_snapshots.verify_snapshot(new_claim.attempt)
    verified = await store.mark_verified(new_claim, new_verification)
    current = await store.publish_snapshot(replace(new_claim, attempt=verified))
    assert current.attempt.snapshot_id == new_claim.attempt.snapshot_id
    assert current.attempt.snapshot_id != old_claim.attempt.snapshot_id
    assert [row["state"] for row in await harness.attempts()] == [
        "published",
        "superseded",
        "published",
    ]
    assert await harness.clickhouse_count() == 21
    assert await new_snapshots.read_snapshot(current) == new_parsed.possessions
    assert (await harness.resource())["published_snapshot_id"] == new_claim.attempt.snapshot_id


@pytest.mark.parametrize("behavior", ["missing", "mismatch"])
async def test_failed_forced_candidate_retains_prior_valid_publication_pointer(
    harness: Phase3Harness,
    behavior: str,
) -> None:
    await ingest_fixture(harness)
    initial_store = harness.work_store()
    assert (
        await harness.service(initial_store).build(BuildPossessionsRequest((SEASON,)))
    ).materialized == 1
    old = await initial_store.resolve_current(GAME_ID, VERSIONS)
    assert old is not None

    failed_store = harness.work_store()
    outcome = await harness.service(
        failed_store,
        RecordingClickHouse(harness.client, behavior=behavior),
    ).build(BuildPossessionsRequest((SEASON,), rebuild=True))
    assert outcome.failed == 1
    resource = await harness.resource()
    assert resource["published_snapshot_id"] == old.attempt.snapshot_id
    assert [row["state"] for row in await harness.attempts()] == [
        "published",
        "poisoned",
    ]
    assert await PossessionSnapshotStore(harness.client).read_snapshot(old)

    recovered_store = harness.work_store()
    recovered = await harness.service(recovered_store).build(
        BuildPossessionsRequest((SEASON,), rebuild=True)
    )
    current = await recovered_store.resolve_current(GAME_ID, VERSIONS)
    attempts = await harness.attempts()

    assert (recovered.materialized, recovered.failed) == (1, 0)
    assert current is not None
    assert [row["generation"] for row in attempts] == [1, 2, 3]
    assert [row["state"] for row in attempts] == ["published", "poisoned", "published"]
    assert current.attempt.snapshot_id == attempts[2]["snapshot_id"]
    assert current.attempt.snapshot_id != attempts[1]["snapshot_id"]
    assert (await harness.resource())["published_snapshot_id"] == current.attempt.snapshot_id
    assert len(await PossessionSnapshotStore(harness.client).read_snapshot(current)) == 7
    assert await harness.clickhouse_count() == (14 if behavior == "missing" else 21)


async def test_two_phase_current_read_detects_pointer_rotation_and_never_mixes_rows(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    first_store = harness.work_store()
    assert (
        await harness.service(first_store).build(BuildPossessionsRequest((SEASON,)))
    ).materialized == 1
    first_reference = await first_store.resolve_current(GAME_ID, VERSIONS)
    assert first_reference is not None
    snapshot_store = PossessionSnapshotStore(harness.client)
    first_rows = await snapshot_store.read_snapshot(first_reference)

    paused_transport = PausedReadClickHouse(harness.client)
    verification = asyncio.create_task(
        harness.service(harness.work_store(), paused_transport).build(
            BuildPossessionsRequest((SEASON,), game_id=GAME_ID, mode=BuildMode.VERIFY)
        )
    )
    await asyncio.wait_for(paused_transport.read_started.wait(), timeout=2)

    changed_payload = deepcopy(harness.source.payloads[RESOURCE_KEYS[2]])
    changed_actions = cast(
        list[dict[str, object]], cast(dict[str, object], changed_payload["game"])["actions"]
    )
    changed_actions[13]["clock"] = "PT0M04.00S"
    harness.source.payloads[RESOURCE_KEYS[2]] = changed_payload
    await ingest_fixture(harness, refresh=True)

    second_store = harness.work_store()
    assert (
        await harness.service(second_store).build(BuildPossessionsRequest((SEASON,), rebuild=True))
    ).materialized == 1
    second_reference = await second_store.resolve_current(GAME_ID, VERSIONS)
    assert second_reference is not None
    assert second_reference != first_reference
    assert second_reference.attempt.snapshot_id != first_reference.attempt.snapshot_id
    second_rows = await snapshot_store.read_snapshot(second_reference)
    assert second_reference.output_sha256 != first_reference.output_sha256
    assert second_rows != first_rows
    assert second_rows[-1].start_seconds_remaining_period_ms == 4000

    paused_transport.release_read.set()
    verification_result = await asyncio.wait_for(verification, timeout=2)
    assert (verification_result.verified, verification_result.failed) == (0, 1)
    assert paused_transport.query_calls == 1
    assert await second_store.resolve_current(GAME_ID, VERSIONS) == second_reference
