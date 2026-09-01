"""Real-PostgreSQL acceptance tests for the historical-ingestion pipeline."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    HistoricalDataSource,
    HistoricalIngestionService,
    ResourceKey,
)
from nba_live_totals.domain import (
    BoxScore,
    GameId,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonSchedule,
    SourceDocument,
    canonical_json_checksum,
)
from nba_live_totals.domain.source import JsonScalar, JsonValue
from nba_live_totals.infrastructure.nba.errors import ProviderConnectionError
from nba_live_totals.infrastructure.nba.mappers import (
    parse_box_score,
    parse_play_by_play,
    parse_schedule,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import (
    PostgresHistoricalIngestionStore,
)
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)
from tests import runtime_config

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "nba"
SEASON = SeasonId("2098-99")
REGULAR_GAME = GameId("0029899001")
PLAYOFF_GAME = GameId("0049899001")
GAME_IDS = (REGULAR_GAME.value, PLAYOFF_GAME.value)
TEAM_IDS = (91313001, 91313002)
PLAYER_IDS = (91313101, 91313102, 91313201)
REQUESTED_AT = datetime(2099, 6, 30, 12, tzinfo=UTC)
RESOURCE_KEYS = (
    str(ResourceKey.schedule(SEASON)),
    str(ResourceKey.box_score(REGULAR_GAME)),
    str(ResourceKey.play_by_play(REGULAR_GAME)),
    str(ResourceKey.box_score(PLAYOFF_GAME)),
    str(ResourceKey.play_by_play(PLAYOFF_GAME)),
)


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    """Apply the real migrations only to the guarded loopback test database."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


class TrackingPostgresStore(PostgresHistoricalIngestionStore):
    """Use the production store while retaining exact run IDs for cleanup."""

    def __init__(self, engine: AsyncEngine, *, source: str) -> None:
        super().__init__(engine, source=source)
        self.run_ids: list[UUID] = []

    async def start_run(self, request: BackfillRequest) -> UUID:
        run_id = await super().start_run(request)
        self.run_ids.append(run_id)
        return run_id


class FixtureHistoricalDataSource:
    """Serve real-shape endpoint fixtures through the production parsers."""

    def __init__(self, *, source: str) -> None:
        self.source = source
        self.payloads = _fixture_payloads()
        self.fetches: list[ResourceKey] = []
        self.failed_play_by_play: set[GameId] = set()

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument:
        key = ResourceKey.schedule(season)
        return self._document(
            key,
            endpoint="scheduleleaguev2",
            parameters={"LeagueID": "00", "Season": season.value},
        )

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.box_score(game_id)
        return self._document(
            key,
            endpoint="boxscoretraditionalv3",
            parameters={
                "GameID": game_id.value,
                "StartPeriod": 0,
                "EndPeriod": 0,
                "StartRange": 0,
                "EndRange": 0,
                "RangeType": 0,
            },
        )

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.play_by_play(game_id)
        self.fetches.append(key)
        if game_id in self.failed_play_by_play:
            raise ProviderConnectionError
        return self._source_document(
            key,
            endpoint="playbyplayv3",
            parameters={
                "GameID": game_id.value,
                "StartPeriod": 0,
                "EndPeriod": 0,
            },
        )

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule:
        return parse_schedule(document)

    def parse_box_score(self, document: SourceDocument) -> BoxScore:
        return parse_box_score(document)

    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
        return parse_play_by_play(document)

    def _document(
        self,
        key: ResourceKey,
        *,
        endpoint: str,
        parameters: JsonObject,
    ) -> SourceDocument:
        self.fetches.append(key)
        return self._source_document(key, endpoint=endpoint, parameters=parameters)

    def _source_document(
        self,
        key: ResourceKey,
        *,
        endpoint: str,
        parameters: JsonObject,
    ) -> SourceDocument:
        payload = deepcopy(self.payloads[str(key)])
        return SourceDocument(
            source=self.source,
            endpoint=endpoint,
            resource_key=str(key),
            requested_at=REQUESTED_AT,
            request_parameters=parameters,
            payload=payload,
            checksum_sha256=canonical_json_checksum(payload),
        )


class E2EHarness:
    """Own one isolated source identity and every row it can create."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.source_name = "nba.com"
        self.source = FixtureHistoricalDataSource(source=self.source_name)
        self.store = TrackingPostgresStore(engine, source=self.source_name)
        self.service = HistoricalIngestionService(self.source, self.store, max_concurrency=1)

    async def close(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(play_by_play_events).where(play_by_play_events.c.game_id.in_(GAME_IDS))
            )
            await connection.execute(
                delete(player_box_scores).where(player_box_scores.c.game_id.in_(GAME_IDS))
            )
            await connection.execute(
                delete(team_box_scores).where(team_box_scores.c.game_id.in_(GAME_IDS))
            )
            await connection.execute(delete(games).where(games.c.game_id.in_(GAME_IDS)))
            await connection.execute(delete(players).where(players.c.player_id.in_(PLAYER_IDS)))
            await connection.execute(delete(teams).where(teams.c.team_id.in_(TEAM_IDS)))
            await connection.execute(delete(seasons).where(seasons.c.season == SEASON.value))
            await connection.execute(
                delete(ingestion_resources).where(
                    ingestion_resources.c.source == self.source_name,
                    ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            await connection.execute(
                delete(source_documents).where(
                    source_documents.c.source == self.source_name,
                    source_documents.c.resource_key.in_(RESOURCE_KEYS),
                )
            )
            if self.store.run_ids:
                await connection.execute(
                    delete(ingestion_runs).where(ingestion_runs.c.run_id.in_(self.store.run_ids))
                )

        residue = await self.snapshot()
        assert all(count == 0 for count in residue.values()), residue
        async with self.engine.connect() as connection:
            run_residue = cast(
                int,
                await connection.scalar(
                    select(func.count())
                    .select_from(ingestion_runs)
                    .where(ingestion_runs.c.run_id.in_(self.store.run_ids))
                ),
            )
        assert run_residue == 0

    async def snapshot(self) -> dict[str, int]:
        async with self.engine.connect() as connection:
            return {
                "seasons": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(seasons)
                        .where(seasons.c.season == SEASON.value)
                    ),
                ),
                "teams": cast(
                    int,
                    await connection.scalar(
                        select(func.count()).select_from(teams).where(teams.c.team_id.in_(TEAM_IDS))
                    ),
                ),
                "games": cast(
                    int,
                    await connection.scalar(
                        select(func.count()).select_from(games).where(games.c.game_id.in_(GAME_IDS))
                    ),
                ),
                "players": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(players)
                        .where(players.c.player_id.in_(PLAYER_IDS))
                    ),
                ),
                "team_box_scores": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(team_box_scores)
                        .where(team_box_scores.c.game_id.in_(GAME_IDS))
                    ),
                ),
                "player_box_scores": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(player_box_scores)
                        .where(player_box_scores.c.game_id.in_(GAME_IDS))
                    ),
                ),
                "play_by_play_events": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(play_by_play_events)
                        .where(play_by_play_events.c.game_id.in_(GAME_IDS))
                    ),
                ),
                "source_documents": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(source_documents)
                        .where(
                            source_documents.c.source == self.source_name,
                            source_documents.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    ),
                ),
                "ingestion_resources": cast(
                    int,
                    await connection.scalar(
                        select(func.count())
                        .select_from(ingestion_resources)
                        .where(
                            ingestion_resources.c.source == self.source_name,
                            ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    ),
                ),
            }

    async def resource_rows(self) -> dict[str, dict[str, object]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(ingestion_resources).where(
                            ingestion_resources.c.source == self.source_name,
                            ingestion_resources.c.resource_key.in_(RESOURCE_KEYS),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {cast(str, row["resource_key"]): dict(row) for row in rows}

    async def run_rows(self) -> list[dict[str, object]]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(ingestion_runs)
                        .where(ingestion_runs.c.run_id.in_(self.store.run_ids))
                        .order_by(ingestion_runs.c.started_at, ingestion_runs.c.run_id)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]


@pytest.fixture
async def harness() -> AsyncIterator[E2EHarness]:
    engine = create_async_engine(runtime_config.postgres_url())
    value = E2EHarness(engine)
    existing = await value.snapshot()
    if any(existing.values()):
        await engine.dispose()
        pytest.fail(f"isolated Task 13 fixture identities already exist: {existing}")
    try:
        yield value
    finally:
        try:
            await value.close()
        finally:
            await engine.dispose()


async def test_two_runs_are_fetch_and_row_idempotent(harness: E2EHarness) -> None:
    request = BackfillRequest(seasons=(SEASON,))

    first = await harness.service.backfill(request)

    assert isinstance(harness.source, HistoricalDataSource)
    assert first == BackfillResult(
        run_id=harness.store.run_ids[0],
        status=BackfillStatus.SUCCEEDED,
        discovered=2,
        succeeded=5,
        skipped=0,
        failed=0,
    )
    assert harness.source.fetches == [
        ResourceKey.schedule(SEASON),
        ResourceKey.box_score(REGULAR_GAME),
        ResourceKey.play_by_play(REGULAR_GAME),
        ResourceKey.box_score(PLAYOFF_GAME),
        ResourceKey.play_by_play(PLAYOFF_GAME),
    ]
    first_snapshot = await harness.snapshot()
    assert first_snapshot == {
        "seasons": 2,
        "teams": 2,
        "games": 2,
        "players": 3,
        "team_box_scores": 4,
        "player_box_scores": 6,
        "play_by_play_events": 6,
        "source_documents": 5,
        "ingestion_resources": 5,
    }
    first_resources = await harness.resource_rows()
    assert set(first_resources) == {
        str(ResourceKey.schedule(SEASON)),
        str(ResourceKey.box_score(REGULAR_GAME)),
        str(ResourceKey.play_by_play(REGULAR_GAME)),
        str(ResourceKey.box_score(PLAYOFF_GAME)),
        str(ResourceKey.play_by_play(PLAYOFF_GAME)),
    }
    assert all(row["status"] == "succeeded" for row in first_resources.values())
    assert all(row["attempt_count"] == 1 for row in first_resources.values())
    assert all(row["latest_document_id"] is not None for row in first_resources.values())
    assert all(
        row["latest_document_id"] == row["applied_document_id"] for row in first_resources.values()
    )

    fetches_after_first_run = list(harness.source.fetches)
    second = await harness.service.backfill(request)

    assert second == BackfillResult(
        run_id=harness.store.run_ids[1],
        status=BackfillStatus.SUCCEEDED,
        discovered=2,
        succeeded=0,
        skipped=5,
        failed=0,
    )
    assert harness.source.fetches == fetches_after_first_run
    assert await harness.snapshot() == first_snapshot
    assert await harness.resource_rows() == first_resources
    assert [row["status"] for row in await harness.run_rows()] == [
        "succeeded",
        "succeeded",
    ]


async def test_partial_run_resumes_only_failed_play_by_play(
    harness: E2EHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = BackfillRequest(seasons=(SEASON,), game_id=REGULAR_GAME)
    harness.source.failed_play_by_play.add(REGULAR_GAME)

    with caplog.at_level(logging.INFO):
        first = await harness.service.backfill(request)

    assert first == BackfillResult(
        run_id=harness.store.run_ids[0],
        status=BackfillStatus.PARTIAL,
        discovered=1,
        succeeded=2,
        skipped=0,
        failed=1,
    )
    assert harness.source.fetches == [
        ResourceKey.schedule(SEASON),
        ResourceKey.box_score(REGULAR_GAME),
        ResourceKey.play_by_play(REGULAR_GAME),
    ]
    assert "leagueSchedule" not in caplog.text
    assert "boxScoreTraditional" not in caplog.text
    assert "actions" not in caplog.text
    first_snapshot = await harness.snapshot()
    assert first_snapshot == {
        "seasons": 2,
        "teams": 2,
        "games": 2,
        "players": 3,
        "team_box_scores": 2,
        "player_box_scores": 3,
        "play_by_play_events": 0,
        "source_documents": 2,
        "ingestion_resources": 3,
    }
    first_resources = await harness.resource_rows()
    failed_resource = first_resources[str(ResourceKey.play_by_play(REGULAR_GAME))]
    assert failed_resource["status"] == "failed"
    assert failed_resource["last_error_code"] == "network_connection"
    assert failed_resource["last_error_type"] == "ProviderConnectionError"
    assert failed_resource["latest_document_id"] is None
    assert failed_resource["applied_document_id"] is None
    stable_document_ids = {
        key: row["applied_document_id"]
        for key, row in first_resources.items()
        if row["status"] == "succeeded"
    }

    harness.source.failed_play_by_play.clear()
    fetches_before_resume = list(harness.source.fetches)
    second = await harness.service.backfill(request)

    assert second == BackfillResult(
        run_id=harness.store.run_ids[1],
        status=BackfillStatus.SUCCEEDED,
        discovered=1,
        succeeded=1,
        skipped=2,
        failed=0,
    )
    assert harness.source.fetches == [
        *fetches_before_resume,
        ResourceKey.play_by_play(REGULAR_GAME),
    ]
    assert await harness.snapshot() == {
        **first_snapshot,
        "play_by_play_events": 3,
        "source_documents": 3,
    }
    resumed_resources = await harness.resource_rows()
    assert all(row["status"] == "succeeded" for row in resumed_resources.values())
    assert resumed_resources[str(ResourceKey.play_by_play(REGULAR_GAME))]["attempt_count"] == 2
    assert all(resumed_resources[key]["attempt_count"] == 1 for key in stable_document_ids)
    assert all(
        resumed_resources[key]["applied_document_id"] == document_id
        for key, document_id in stable_document_ids.items()
    )
    assert all(
        row["latest_document_id"] == row["applied_document_id"]
        for row in resumed_resources.values()
    )
    run_rows = await harness.run_rows()
    assert [
        (
            row["status"],
            row["discovered"],
            row["succeeded"],
            row["skipped"],
            row["failed"],
        )
        for row in run_rows
    ] == [
        ("partial", 1, 2, 0, 1),
        ("succeeded", 1, 1, 2, 0),
    ]


def _fixture_payloads() -> dict[str, JsonObject]:
    schedule = _load_fixture("schedule_2025-26.json")
    league_schedule = cast(dict[str, JsonValue], schedule["leagueSchedule"])
    league_schedule["seasonYear"] = SEASON.value
    game_dates = cast(list[JsonValue], league_schedule["gameDates"])
    game_date = cast(dict[str, JsonValue], game_dates[0])
    fixture_games = cast(list[JsonValue], game_date["games"])
    playoff_game = deepcopy(cast(dict[str, JsonValue], fixture_games[0]))
    playoff_game["gameId"] = PLAYOFF_GAME.value
    playoff_game["gameDateTimeUTC"] = "2099-06-29T01:00:00Z"
    fixture_games.append(playoff_game)

    replacements: dict[str, dict[JsonScalar, JsonScalar]] = {
        "gameId": {"0022500123": REGULAR_GAME.value},
        "homeTeamId": {10: TEAM_IDS[0]},
        "awayTeamId": {20: TEAM_IDS[1]},
        "teamId": {10: TEAM_IDS[0], 20: TEAM_IDS[1]},
        "personId": {101: PLAYER_IDS[0], 102: PLAYER_IDS[1], 201: PLAYER_IDS[2]},
    }
    schedule = cast(JsonObject, _replace_fields(schedule, replacements))

    box_score = cast(
        JsonObject,
        _replace_fields(_load_fixture("box_score_0022500123.json"), replacements),
    )
    playoff_box_score = cast(
        JsonObject,
        _replace_fields(
            deepcopy(box_score),
            {"gameId": {REGULAR_GAME.value: PLAYOFF_GAME.value}},
        ),
    )
    play_by_play = cast(
        JsonObject,
        _replace_fields(_load_fixture("play_by_play_0022500123.json"), replacements),
    )
    playoff_play_by_play = cast(
        JsonObject,
        _replace_fields(
            deepcopy(play_by_play),
            {"gameId": {REGULAR_GAME.value: PLAYOFF_GAME.value}},
        ),
    )
    return {
        str(ResourceKey.schedule(SEASON)): schedule,
        str(ResourceKey.box_score(REGULAR_GAME)): box_score,
        str(ResourceKey.play_by_play(REGULAR_GAME)): play_by_play,
        str(ResourceKey.box_score(PLAYOFF_GAME)): playoff_box_score,
        str(ResourceKey.play_by_play(PLAYOFF_GAME)): playoff_play_by_play,
    }


def _load_fixture(filename: str) -> JsonObject:
    return cast(
        JsonObject,
        json.loads((FIXTURE_DIRECTORY / filename).read_text(encoding="utf-8")),
    )


def _replace_fields(
    value: JsonValue,
    replacements: dict[str, dict[JsonScalar, JsonScalar]],
) -> JsonValue:
    if isinstance(value, dict):
        replaced: dict[str, JsonValue] = {}
        for key, item in value.items():
            field_replacements = replacements.get(key)
            if field_replacements is not None and not isinstance(item, (dict, list)):
                replaced[key] = field_replacements.get(item, item)
            else:
                replaced[key] = _replace_fields(item, replacements)
        return replaced
    if isinstance(value, list):
        return [_replace_fields(item, replacements) for item in value]
    return value
