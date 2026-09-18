"""Offline Phase 2 -> Phase 4 acceptance against real PostgreSQL and ClickHouse."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import BackfillRequest, HistoricalIngestionService
from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    CurrentLineupSnapshotReference,
    LineupBuildClaim,
    LineupBuildMode,
    LineupBuildService,
)
from nba_live_totals.application.possessions import BuildPossessionsRequest
from nba_live_totals.domain import GameId, JsonObject, SeasonId
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import LineupSegment, LineupVersions
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3LineupNormalizer
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    lineup_rebuild_resources,
    lineup_rebuild_runs,
    lineup_snapshot_attempts,
    play_by_play_events,
    player_box_scores,
    players,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)
from tests import runtime_config
from tests.integration.test_possession_snapshot_protocol import (
    GAME_ID as POSSESSION_GAME_ID,
)
from tests.integration.test_possession_snapshot_protocol import (
    FixtureSource,
    Phase3Harness,
    RecordingClickHouse,
    TrackingIngestionStore,
    ingest_fixture,
)

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
pytest_plugins = ("tests.integration.test_possession_snapshot_protocol",)
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "nba" / "lineups"
SEASON = SeasonId("2098-99")
GAME_ID = GameId("0029899002")
HOME_TEAM_ID, AWAY_TEAM_ID = 91_318_001, 91_318_002
PLAYER_IDS = (*range(101, 108), *range(201, 208))
KEYS = ("schedule:2098-99", "box_score:0029899002", "play_by_play:0029899002")
VERSIONS = LineupVersions(
    "nba-pbp-v3-lineup-v1", "causal-lineup-v1", "nba-5x5-v1", "lineup-segments-v1"
)


def load_fixture(name: str) -> JsonObject:
    return cast(JsonObject, json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")))


class LineupFixtureSource(FixtureSource):
    def __init__(self) -> None:
        super().__init__()
        self.payloads = dict(
            zip(
                KEYS,
                (
                    load_fixture("lineup_schedule_2098-99.json"),
                    load_fixture("lineup_box_score_0029899002.json"),
                    load_fixture("lineup_play_by_play_0029899002.json"),
                ),
                strict=True,
            )
        )


class TrackingLineupStore(PostgresLineupWorkStore):
    def __init__(self, engine: AsyncEngine, run_ids: list[UUID]) -> None:
        super().__init__(engine, lease_seconds=300)
        self.run_ids = run_ids
        self.failure_error: Exception | None = None

    async def fail_game(self, claim: LineupBuildClaim, *, error_code: str, error_type: str) -> None:
        try:
            await super().fail_game(claim, error_code=error_code, error_type=error_type)
        except Exception as error:
            self.failure_error = error
            raise

    async def start_run(self, request: BuildLineupsRequest) -> UUID:
        run_id = await super().start_run(request)
        self.run_ids.append(run_id)
        return run_id


class LineupAcceptance:
    def __init__(self, engine: AsyncEngine, client: ClickHouseClient) -> None:
        self.engine, self.client = engine, client
        self.source = LineupFixtureSource()
        self.ingestion_run_ids: list[UUID] = []
        self.run_ids: list[UUID] = []
        self.store = TrackingLineupStore(engine, self.run_ids)
        self.transport = RecordingClickHouse(client)
        self.snapshots = ClickHouseLineupSnapshotStore(self.transport)
        self.service = LineupBuildService(
            normalizer=NBAPlayByPlayV3LineupNormalizer(),
            reconstructor=DeterministicLineupReconstructor(),
            work_store=self.store,
            snapshot_store=self.snapshots,
            versions=VERSIONS,
            max_concurrency=1,
        )

    async def ingest(self, *, refresh: bool = True) -> None:
        result = await HistoricalIngestionService(
            self.source,
            TrackingIngestionStore(self.engine, self.ingestion_run_ids),
            max_concurrency=1,
        ).backfill(BackfillRequest(seasons=(SEASON,), game_id=GAME_ID, refresh=refresh))
        assert (result.discovered, result.failed) == (1, 0)

    async def rows(self, table: Table) -> list[dict[str, object]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(select(table).where(table.c.game_id == GAME_ID.value))
            return [dict(row) for row in rows.mappings()]

    async def current(self) -> CurrentLineupSnapshotReference:
        resolution = await self.store.resolve_current(GAME_ID, VERSIONS)
        assert resolution.reference is not None, resolution
        return resolution.reference

    async def count(self) -> int:
        rows = await self.client.query(
            "SELECT count() AS count FROM nba.lineup_segments_v1 WHERE game_id = {id:String}",
            parameters={"id": GAME_ID.value},
        )
        return cast(int, rows[0]["count"])

    async def run_cli(
        self,
        *arguments: str,
        timeout_seconds: float = 60,
    ) -> tuple[int, str, str]:
        before = await self.cli_run_ids()
        assert before <= set(self.run_ids), "refusing unowned lineup run"
        environment = os.environ | {
            "DATABASE_URL": runtime_config.postgres_url(),
            "CLICKHOUSE_URL": runtime_config.clickhouse_url(),
            "CLICKHOUSE_USER": "nba",
            "CLICKHOUSE_PASSWORD": "nba",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        finally:
            # Stop and reap before returning control to fixture database teardown.
            # Drain pipes as well to avoid wait() deadlocks on buffered output.
            try:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.communicate()
                await process.wait()
            finally:
                self.run_ids.extend(sorted(await self.cli_run_ids() - before))
        assert process.returncode is not None
        return process.returncode, stdout.decode(), stderr.decode()

    async def cli_run_ids(self) -> set[UUID]:
        """Read only this fixture's exact CLI selector, independent of stdout."""
        async with self.engine.connect() as connection:
            rows = await connection.scalars(
                select(lineup_rebuild_runs.c.run_id).where(
                    lineup_rebuild_runs.c.selectors
                    == {
                        "seasons": [SEASON.value],
                        "game_id": GAME_ID.value,
                    }
                )
            )
            return set(rows)

    async def assert_unowned_ids_absent(self) -> None:
        # Refuse collisions before acquiring ownership; never clean pre-existing rows.
        async with self.engine.connect() as connection:
            for table, predicate in (
                (seasons, seasons.c.season == SEASON.value),
                (games, games.c.game_id == GAME_ID.value),
                (players, players.c.player_id.in_(PLAYER_IDS)),
                (teams, teams.c.team_id.in_((HOME_TEAM_ID, AWAY_TEAM_ID))),
                (ingestion_resources, ingestion_resources.c.resource_key.in_(KEYS[1:])),
                (source_documents, source_documents.c.resource_key.in_(KEYS[1:])),
                (lineup_rebuild_resources, lineup_rebuild_resources.c.game_id == GAME_ID.value),
                (lineup_snapshot_attempts, lineup_snapshot_attempts.c.game_id == GAME_ID.value),
                (
                    lineup_rebuild_runs,
                    lineup_rebuild_runs.c.selectors
                    == {
                        "seasons": [SEASON.value],
                        "game_id": GAME_ID.value,
                    },
                ),
            ):
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(table).where(predicate)
                    )
                    == 0
                ), f"refusing fixture collision in {table.name}"
        assert await self.count() == 0, "refusing existing lineup rows"

    async def cleanup(self) -> None:
        # Only called after collision checks and fixture ownership are established.
        await self.client.execute(
            "ALTER TABLE nba.lineup_segments_v1 DELETE WHERE game_id = '0029899002' "
            "AND season_start_year = 2098 SETTINGS mutations_sync = 2"
        )
        async with self.engine.begin() as connection:
            for table in (
                lineup_rebuild_resources,
                lineup_snapshot_attempts,
                play_by_play_events,
                player_box_scores,
                team_box_scores,
                games,
            ):
                await connection.execute(delete(table).where(table.c.game_id == GAME_ID.value))
            await connection.execute(delete(players).where(players.c.player_id.in_(PLAYER_IDS)))
            await connection.execute(
                delete(teams).where(teams.c.team_id.in_((HOME_TEAM_ID, AWAY_TEAM_ID)))
            )
            for table in (ingestion_resources, source_documents):
                await connection.execute(
                    delete(table).where(table.c.source == "nba.com", table.c.resource_key.in_(KEYS))
                )
            await connection.execute(
                delete(ingestion_runs).where(ingestion_runs.c.run_id.in_(self.ingestion_run_ids))
            )
            await connection.execute(
                delete(lineup_rebuild_runs).where(lineup_rebuild_runs.c.run_id.in_(self.run_ids))
            )


@asynccontextmanager
async def acceptance_context() -> AsyncIterator[LineupAcceptance]:
    engine = create_async_engine(runtime_config.postgres_url())
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30
    )
    harness = LineupAcceptance(engine, client)
    possessions = Phase3Harness(engine, client)
    try:
        await harness.assert_unowned_ids_absent()
        await possessions.assert_empty()
        try:
            await ingest_fixture(possessions)
            built = await possessions.service(possessions.work_store()).build(
                BuildPossessionsRequest((SEASON,), game_id=POSSESSION_GAME_ID)
            )
            assert built.materialized == 1
            baseline = await client.query(
                "SELECT * FROM nba.possessions_v1 ORDER BY snapshot_id, possession_number"
            )
            assert len(baseline) >= 7
            try:
                yield harness
            finally:
                assert (
                    await client.query(
                        "SELECT * FROM nba.possessions_v1 ORDER BY snapshot_id, possession_number"
                    )
                    == baseline
                )
        finally:
            await harness.cleanup()
            await possessions.cleanup()
        await harness.assert_unowned_ids_absent()
        await possessions.assert_empty()
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.fixture
async def acceptance() -> AsyncIterator[LineupAcceptance]:
    async with acceptance_context() as harness:
        yield harness


def projection(segments: tuple[LineupSegment, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.segment_number,
            (row.start_boundary.sequence_number, row.start_boundary.side.value),
            (row.end_boundary.sequence_number, row.end_boundary.side.value),
            tuple(sorted(row.home_state.player_ids)),
            tuple(sorted(row.away_state.player_ids)),
            row.home_state.status.value,
            row.away_state.status.value,
        )
        for row in segments
    )


async def test_exact_same_clock_lineups_lineage_idempotence_rebuild_and_verify(
    acceptance: LineupAcceptance,
) -> None:
    h = acceptance
    await h.ingest()
    request = BuildLineupsRequest((SEASON,), game_id=GAME_ID)
    first = await h.service.build(request)
    assert (first.materialized, first.invalid, first.failed, first.busy) == (1, 0, 0, 0)
    assert (h.transport.insert_calls, h.transport.query_calls) == (1, 1)
    current = await h.current()
    assert current.row_count == 3
    assert (
        current.output_sha256 == "5c434c8b15fb47765398df8eb2a01bc2c2f2b48854ac59ca090443ab7ae89530"
    )
    assert projection(await h.snapshots.read_snapshot(current)) == (
        (
            1,
            (1, "before"),
            (3, "before"),
            (101, 102, 103, 104, 105),
            (201, 202, 203, 204, 205),
            "valid",
            "valid",
        ),
        (
            2,
            (3, "after"),
            (4, "before"),
            (102, 103, 104, 105, 106),
            (201, 202, 203, 204, 205),
            "valid",
            "valid",
        ),
        (
            3,
            (4, "after"),
            (5, "after"),
            (102, 103, 104, 105, 106),
            (202, 203, 204, 205, 206),
            "valid",
            "valid",
        ),
    )
    token = current.attempt.input_token
    assert await h.client.query(
        "SELECT DISTINCT input_box_score_document_id, input_pbp_document_id, output_sha256 "
        "FROM nba.lineup_segments_v1 FINAL WHERE snapshot_id = {snapshot:UUID}",
        parameters={"snapshot": str(current.attempt.snapshot_id)},
    ) == [
        {
            "input_box_score_document_id": str(token.applied_box_score_document_id),
            "input_pbp_document_id": str(token.applied_pbp_document_id),
            "output_sha256": current.output_sha256,
        }
    ]
    async with h.engine.connect() as connection:
        for key, document_id, checksum in (
            (
                KEYS[1],
                current.attempt.input_token.applied_box_score_document_id,
                current.attempt.input_token.applied_box_score_sha256,
            ),
            (
                KEYS[2],
                current.attempt.input_token.applied_pbp_document_id,
                current.attempt.input_token.applied_pbp_sha256,
            ),
        ):
            assert (
                await connection.scalar(
                    select(ingestion_resources.c.applied_document_id).where(
                        ingestion_resources.c.source == "nba.com",
                        ingestion_resources.c.resource_key == key,
                    )
                )
                == document_id
            )
            assert (
                await connection.scalar(
                    select(source_documents.c.checksum_sha256).where(
                        source_documents.c.document_id == document_id
                    )
                )
                == checksum
            )
    resource = (await h.rows(lineup_rebuild_resources))[0]
    assert resource["published_snapshot_id"] == current.attempt.snapshot_id
    assert resource["publication_state"] == "current"
    attempts = await h.rows(lineup_snapshot_attempts)
    calls = (h.transport.insert_calls, h.transport.query_calls)
    skipped = await h.service.build(request)
    assert (skipped.skipped_current, skipped.materialized, skipped.failed) == (1, 0, 0)
    assert await h.rows(lineup_snapshot_attempts) == attempts
    assert (h.transport.insert_calls, h.transport.query_calls) == calls
    assert await h.count() == 3
    rebuilt = await h.service.build(BuildLineupsRequest((SEASON,), game_id=GAME_ID, rebuild=True))
    assert (rebuilt.materialized, rebuilt.failed) == (1, 0)
    new = await h.current()
    assert new.attempt.snapshot_id != current.attempt.snapshot_id
    assert new.attempt.generation == current.attempt.generation + 1
    assert new.output_sha256 == current.output_sha256
    assert new.attempt.lineup_revision_id == current.attempt.lineup_revision_id
    assert await h.count() == 6
    before_verify_resources, attempts = (
        await h.rows(lineup_rebuild_resources),
        await h.rows(lineup_snapshot_attempts),
    )
    calls = (h.transport.insert_calls, h.transport.query_calls)
    verified = await h.service.build(
        BuildLineupsRequest((SEASON,), game_id=GAME_ID, mode=LineupBuildMode.VERIFY)
    )
    assert (verified.verified, verified.materialized, verified.failed) == (1, 0, 0)
    assert (h.transport.insert_calls, h.transport.query_calls) == (calls[0], calls[1] + 1)
    assert await h.rows(lineup_rebuild_resources) == before_verify_resources
    assert await h.rows(lineup_snapshot_attempts) == attempts
    assert await h.count() == 6


async def test_invalid_quality_is_published_and_real_cli_reports_exit_one(
    acceptance: LineupAcceptance,
) -> None:
    h = acceptance
    h.source.payloads[KEYS[2]] = load_fixture("lineup_play_by_play_invalid_0029899002.json")
    await h.ingest()
    result = await h.service.build(BuildLineupsRequest((SEASON,), game_id=GAME_ID))
    assert (result.materialized, result.invalid, result.failed, result.busy) == (1, 1, 0, 0)
    current = await h.current()
    segments = await h.snapshots.read_snapshot(current)
    assert projection(segments) == (
        (
            1,
            (1, "before"),
            (3, "before"),
            (101, 102, 103, 104, 105),
            (201, 202, 203, 204, 205),
            "valid",
            "valid",
        ),
        (2, (3, "after"), (4, "before"), (), (201, 202, 203, 204, 205), "invalid", "valid"),
        (3, (4, "after"), (5, "after"), (), (202, 203, 204, 205, 206), "invalid", "valid"),
    )
    for segment in segments[1:]:
        assert segment.home_state.invalid_reason == "outgoing_not_on_court"
        assert segment.home_state.invalid_since == segments[1].start_boundary
    resource = (await h.rows(lineup_rebuild_resources))[0]
    assert resource["published_snapshot_id"] == current.attempt.snapshot_id
    assert resource["publication_state"] == "invalid"
    assert resource["last_error_code"] is None
    assert resource["last_issue_codes"] == [
        {
            "team_id": HOME_TEAM_ID,
            "reason": "outgoing_not_on_court",
            "sequence_number": 3,
            "boundary_side": "after",
        }
    ]
    returncode, output, stderr = await h.run_cli(
        "-m",
        "scripts.build_lineups",
        "--season",
        SEASON.value,
        "--game-id",
        GAME_ID.value,
        "--rebuild",
    )
    assert returncode == 1, (output, stderr)
    assert "materialized=1 invalid=1" in output
    assert "failed=0 busy=0" in output
    assert (await h.current()).output_sha256 == current.output_sha256
    assert await h.count() == 6


@pytest.mark.parametrize("failure", ["raw_canonical_mismatch", "structural"])
async def test_normalization_failure_preserves_previous_pointer(
    acceptance: LineupAcceptance,
    failure: str,
) -> None:
    h = acceptance
    await h.ingest()
    request = BuildLineupsRequest((SEASON,), game_id=GAME_ID)
    assert (await h.service.build(request)).materialized == 1
    current = await h.current()
    rows = await h.snapshots.read_snapshot(current)
    if failure == "raw_canonical_mismatch":
        async with h.engine.begin() as connection:
            await connection.execute(
                update(play_by_play_events)
                .where(
                    play_by_play_events.c.game_id == GAME_ID.value,
                    play_by_play_events.c.sequence_number == 2,
                )
                .values(description="canonical correction without raw correction")
            )
    else:
        payload = deepcopy(h.source.payloads[KEYS[1]])
        # Phase 2 maps the box envelope; Phase 4 additionally requires V1 metadata.
        payload["meta"] = {"version": 2}
        h.source.payloads[KEYS[1]] = payload
        await h.ingest(refresh=True)
    result = await h.service.build(request)
    assert (result.materialized, result.failed, result.invalid) == (0, 1, 0)
    resource = (await h.rows(lineup_rebuild_resources))[0]
    assert resource["published_snapshot_id"] == current.attempt.snapshot_id
    assert h.store.failure_error is None
    assert resource["last_error_code"] == (
        "raw_canonical_event_mismatch"
        if failure == "raw_canonical_mismatch"
        else "unsupported_document_schema"
    )
    assert resource["active_snapshot_id"] is None
    assert resource["publication_state"] == "stale"
    assert sorted(str(row["state"]) for row in await h.rows(lineup_snapshot_attempts)) == [
        "poisoned",
        "published",
    ]
    assert await h.snapshots.read_snapshot(current) == rows
    assert await h.count() == 3
