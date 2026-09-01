import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    ClaimLostError,
    HistoricalIngestionStore,
    ResourceClaim,
    ResourceKey,
)
from nba_live_totals.domain import (
    BoxScore,
    Game,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    Player,
    PlayerBoxScore,
    SeasonId,
    SeasonSchedule,
    SeasonType,
    SourceDocument,
    Team,
    TeamBoxScore,
    TraditionalStats,
    canonical_json_checksum,
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
class CanonicalHarness:
    engine: AsyncEngine
    source: str = field(default_factory=lambda: f"task-9-{uuid4()}")
    run_ids: list[UUID] = field(default_factory=list)
    game_ids: set[str] = field(default_factory=set)
    team_ids: set[int] = field(default_factory=set)
    player_ids: set[int] = field(default_factory=set)
    season_keys: set[tuple[str, str]] = field(default_factory=set)

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

    async def claim(
        self,
        store: PostgresHistoricalIngestionStore,
        key: ResourceKey,
        *,
        refresh: bool = False,
    ) -> ResourceClaim:
        season = key.target if isinstance(key.target, SeasonId) else SeasonId("2090-91")
        run_id = await store.start_run(BackfillRequest(seasons=(season,), refresh=refresh))
        self.run_ids.append(run_id)
        decision = await store.claim_resource(run_id, key, refresh=refresh)
        return cast(ResourceClaim, decision.claim)

    def track_schedule(self, schedule: SeasonSchedule) -> None:
        self.team_ids.update(team.team_id for team in schedule.teams)
        self.game_ids.update(game.game_id.value for game in schedule.games)
        self.season_keys.update(
            (game.season.value, game.season_type.value) for game in schedule.games
        )

    def track_box_score(self, box_score: BoxScore) -> None:
        self.team_ids.update(row.team.team_id for row in box_score.team_rows)
        self.player_ids.update(row.player.player_id for row in box_score.player_rows)


@pytest.fixture
async def harness(engine: AsyncEngine) -> AsyncIterator[CanonicalHarness]:
    value = CanonicalHarness(engine)
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            if value.game_ids:
                await connection.execute(
                    delete(play_by_play_events).where(
                        play_by_play_events.c.game_id.in_(value.game_ids)
                    )
                )
                await connection.execute(
                    delete(player_box_scores).where(player_box_scores.c.game_id.in_(value.game_ids))
                )
                await connection.execute(
                    delete(team_box_scores).where(team_box_scores.c.game_id.in_(value.game_ids))
                )
                await connection.execute(delete(games).where(games.c.game_id.in_(value.game_ids)))
            if value.player_ids:
                await connection.execute(
                    delete(players).where(players.c.player_id.in_(value.player_ids))
                )
            if value.team_ids:
                await connection.execute(delete(teams).where(teams.c.team_id.in_(value.team_ids)))
            for season, season_type in value.season_keys:
                await connection.execute(
                    delete(seasons).where(
                        seasons.c.season == season,
                        seasons.c.season_type == season_type,
                    )
                )
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


def make_schedule(
    harness: CanonicalHarness,
    *,
    game_id: str,
    season: str,
    home_team_id: int,
    away_team_id: int,
    home_name: str = "Atoms",
    home_score: int = 120,
    away_score: int = 110,
    arena_name: str = "Transaction Arena",
) -> SeasonSchedule:
    season_id = SeasonId(season)
    schedule = SeasonSchedule(
        season=season_id,
        teams=(
            Team(home_team_id, "Atomic City", home_name, "ATM", "atoms"),
            Team(away_team_id, "Rollback", "Guards", "RBG", "guards"),
        ),
        games=(
            Game(
                game_id=GameId(game_id),
                season=season_id,
                season_type=SeasonType.REGULAR_SEASON,
                game_date=date(2091, 1, 2),
                started_at=datetime(2091, 1, 3, 1, 30, tzinfo=UTC),
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                home_score=home_score,
                away_score=away_score,
                status=GameStatus.FINAL,
                arena_name=arena_name,
                arena_city="Atomic City",
                arena_state=None,
            ),
        ),
    )
    harness.track_schedule(schedule)
    return schedule


def make_document(
    harness: CanonicalHarness,
    key: ResourceKey,
    payload: JsonObject,
    *,
    requested_at: datetime = BASE_TIME,
) -> SourceDocument:
    return SourceDocument(
        source=harness.source,
        endpoint=f"test-{key.resource_type.value}",
        resource_key=str(key),
        requested_at=requested_at,
        request_parameters={},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def stats(*, points: int, plus_minus: str) -> TraditionalStats:
    return TraditionalStats(
        seconds_played=14_400,
        field_goals_made=40,
        field_goals_attempted=80,
        field_goals_percentage=Decimal("0.500"),
        three_pointers_made=10,
        three_pointers_attempted=25,
        three_pointers_percentage=Decimal("0.400"),
        free_throws_made=points - 90,
        free_throws_attempted=points - 85,
        free_throws_percentage=Decimal("0.833333"),
        rebounds_offensive=8,
        rebounds_defensive=30,
        rebounds_total=38,
        assists=25,
        steals=7,
        blocks=4,
        turnovers=11,
        fouls_personal=17,
        points=points,
        plus_minus=Decimal(plus_minus),
    )


def player_stats(*, points: int, plus_minus: str) -> TraditionalStats:
    return TraditionalStats(
        seconds_played=1_800,
        field_goals_made=points // 2,
        field_goals_attempted=20,
        field_goals_percentage=Decimal("0.500"),
        three_pointers_made=2,
        three_pointers_attempted=5,
        three_pointers_percentage=Decimal("0.400"),
        free_throws_made=0,
        free_throws_attempted=0,
        free_throws_percentage=Decimal("0"),
        rebounds_offensive=1,
        rebounds_defensive=5,
        rebounds_total=6,
        assists=4,
        steals=1,
        blocks=0,
        turnovers=2,
        fouls_personal=2,
        points=points,
        plus_minus=Decimal(plus_minus),
    )


def make_box_score(
    harness: CanonicalHarness,
    schedule: SeasonSchedule,
    *,
    home_score: int = 120,
    away_score: int = 110,
    include_bench: bool = True,
) -> BoxScore:
    game = schedule.games[0]
    home_team, away_team = schedule.teams
    player_rows = [
        PlayerBoxScore(
            game_id=game.game_id,
            player=Player(9101, "Ada", "Lovelace", "Ada Lovelace"),
            team_id=home_team.team_id,
            position="G",
            jersey_number="1",
            starter=True,
            played=True,
            comment=None,
            statistics=player_stats(points=20, plus_minus="10.25"),
        ),
        PlayerBoxScore(
            game_id=game.game_id,
            player=Player(9102, "Grace", "Hopper", "Grace Hopper"),
            team_id=away_team.team_id,
            position=None,
            jersey_number="2",
            starter=False,
            played=False,
            comment="DNP - Coach's Decision",
            statistics=TraditionalStats.empty(),
        ),
    ]
    if not include_bench:
        player_rows.pop()
    box_score = BoxScore(
        game_id=game.game_id,
        home_team_id=home_team.team_id,
        away_team_id=away_team.team_id,
        home_score=home_score,
        away_score=away_score,
        team_rows=(
            TeamBoxScore(game.game_id, home_team, stats(points=home_score, plus_minus="10.25")),
            TeamBoxScore(game.game_id, away_team, stats(points=away_score, plus_minus="-10.25")),
        ),
        player_rows=tuple(player_rows),
    )
    harness.track_box_score(box_score)
    return box_score


def make_events(game_id: GameId) -> tuple[PlayByPlayEvent, ...]:
    return (
        PlayByPlayEvent(
            game_id=game_id,
            action_number=1,
            action_id=11,
            period=1,
            clock="PT11M59.00S",
            seconds_remaining_period_ms=719_000,
            team_id=9001,
            person_id=9101,
            action_type="2pt",
            sub_type="Jump Shot",
            description="Opening jumper",
            home_score=2,
            away_score=0,
            shot_result="Made",
            shot_distance=16,
            legacy_x=-20,
            legacy_y=150,
            location="h",
            is_field_goal=True,
            points_total=2,
            video_available=True,
        ),
        PlayByPlayEvent(
            game_id=game_id,
            action_number=2,
            action_id=None,
            period=1,
            clock="PT00M00.00S",
            seconds_remaining_period_ms=0,
            team_id=None,
            person_id=None,
            action_type="period",
            sub_type="end",
            description="End of period",
            home_score=None,
            away_score=None,
            shot_result=None,
            shot_distance=None,
            legacy_x=None,
            legacy_y=None,
            location=None,
            is_field_goal=False,
            points_total=0,
            video_available=False,
        ),
    )


async def persist_schedule(
    harness: CanonicalHarness,
    store: PostgresHistoricalIngestionStore,
    schedule: SeasonSchedule,
) -> None:
    key = ResourceKey.schedule(schedule.season)
    claim = await harness.claim(store, key)
    document_id = await store.save_document(claim, make_document(harness, key, {"version": 1}))
    await store.apply_schedule(claim, document_id, schedule)


async def test_schedule_upserts_stable_identities_and_skips_unchanged_checksum(
    harness: CanonicalHarness,
) -> None:
    store = harness.store()
    schedule = make_schedule(
        harness,
        game_id="0029000001",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    key = ResourceKey.schedule(schedule.season)

    first_claim = await harness.claim(store, key)
    first_document = make_document(harness, key, {"version": 1})
    first_document_id = await store.save_document(first_claim, first_document)
    await store.apply_schedule(first_claim, first_document_id, schedule)

    renamed = make_schedule(
        harness,
        game_id="0029000001",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
        home_name="Atomic Renames",
        arena_name="Renamed Arena",
    )
    second_claim = await harness.claim(store, key, refresh=True)
    second_document = make_document(
        harness,
        key,
        {"version": 2},
        requested_at=BASE_TIME + timedelta(seconds=1),
    )
    second_document_id = await store.save_document(second_claim, second_document)
    await store.apply_schedule(second_claim, second_document_id, renamed)

    refresh_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    third_claim = await harness.claim(refresh_store, key, refresh=True)
    duplicate_id = await refresh_store.save_document(third_claim, second_document)
    inconsistent_duplicate = make_schedule(
        harness,
        game_id="0029000001",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
        home_name="Must Not Rewrite",
    )
    applying_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    await applying_store.apply_schedule(third_claim, duplicate_id, inconsistent_duplicate)

    assert isinstance(store, HistoricalIngestionStore)
    assert duplicate_id == second_document_id
    async with harness.engine.connect() as connection:
        counts = (
            await connection.scalar(
                select(func.count())
                .select_from(seasons)
                .where(
                    seasons.c.season == "2090-91",
                    seasons.c.season_type == "regular_season",
                )
            ),
            await connection.scalar(
                select(func.count()).select_from(teams).where(teams.c.team_id.in_((9001, 9002)))
            ),
            await connection.scalar(
                select(func.count()).select_from(games).where(games.c.game_id == "0029000001")
            ),
            await connection.scalar(
                select(func.count())
                .select_from(source_documents)
                .where(source_documents.c.source == harness.source)
            ),
        )
        team_name = await connection.scalar(select(teams.c.name).where(teams.c.team_id == 9001))
        arena_name = await connection.scalar(
            select(games.c.arena_name).where(games.c.game_id == "0029000001")
        )
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert counts == (1, 2, 1, 2)
    assert team_name == "Atomic Renames"
    assert arena_name == "Renamed Arena"
    assert resource["status"] == "succeeded"
    assert resource["claimed_until"] is None
    assert resource["last_error_code"] is None
    assert resource["last_error_type"] is None
    assert resource["latest_document_id"] == second_document_id
    assert resource["applied_document_id"] == second_document_id
    assert resource["last_run_id"] == third_claim.run_id
    assert resource["claim_version"] == 3


async def test_changed_box_score_and_play_by_play_replace_complete_snapshots(
    harness: CanonicalHarness,
) -> None:
    store = harness.store()
    schedule = make_schedule(
        harness,
        game_id="0029000002",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    await persist_schedule(harness, store, schedule)

    box_key = ResourceKey.box_score(schedule.games[0].game_id)
    first_box = make_box_score(harness, schedule)
    first_box_claim = await harness.claim(store, box_key)
    first_box_document_id = await store.save_document(
        first_box_claim,
        make_document(harness, box_key, {"version": 1}),
    )
    await store.replace_box_score(first_box_claim, first_box_document_id, first_box)
    async with harness.engine.connect() as connection:
        dnp_row = (
            (
                await connection.execute(
                    select(player_box_scores).where(
                        player_box_scores.c.game_id == schedule.games[0].game_id.value,
                        player_box_scores.c.player_id == 9102,
                    )
                )
            )
            .mappings()
            .one()
        )
    assert (
        dnp_row["position"],
        dnp_row["jersey_number"],
        dnp_row["starter"],
        dnp_row["played"],
        dnp_row["comment"],
    ) == (None, "2", False, False, "DNP - Coach's Decision")
    assert all(dnp_row[column.name] is None for column in player_box_scores.c[8:])

    changed_box = make_box_score(
        harness,
        schedule,
        home_score=121,
        away_score=109,
        include_bench=False,
    )
    renamed_player_row = replace(
        changed_box.player_rows[0],
        player=Player(9101, "Augusta", "Lovelace", "Augusta Ada Lovelace"),
    )
    changed_box = replace(changed_box, player_rows=(renamed_player_row,))
    second_box_claim = await harness.claim(store, box_key, refresh=True)
    second_box_document = make_document(harness, box_key, {"version": 2})
    second_box_document_id = await store.save_document(second_box_claim, second_box_document)
    await store.replace_box_score(second_box_claim, second_box_document_id, changed_box)

    unchanged_box_claim = await harness.claim(store, box_key, refresh=True)
    unchanged_box_document_id = await store.save_document(unchanged_box_claim, second_box_document)
    await store.replace_box_score(unchanged_box_claim, unchanged_box_document_id, first_box)

    pbp_key = ResourceKey.play_by_play(schedule.games[0].game_id)
    first_events = make_events(schedule.games[0].game_id)
    first_pbp_claim = await harness.claim(store, pbp_key)
    first_pbp_document_id = await store.save_document(
        first_pbp_claim,
        make_document(harness, pbp_key, {"version": 1}),
    )
    await store.replace_play_by_play(first_pbp_claim, first_pbp_document_id, first_events)

    changed_events = (replace(first_events[0], description="Corrected opening jumper"),)
    second_pbp_claim = await harness.claim(store, pbp_key, refresh=True)
    second_pbp_document = make_document(harness, pbp_key, {"version": 2})
    second_pbp_document_id = await store.save_document(second_pbp_claim, second_pbp_document)
    await store.replace_play_by_play(second_pbp_claim, second_pbp_document_id, changed_events)

    unchanged_pbp_claim = await harness.claim(store, pbp_key, refresh=True)
    unchanged_pbp_document_id = await store.save_document(unchanged_pbp_claim, second_pbp_document)
    await store.replace_play_by_play(
        unchanged_pbp_claim,
        unchanged_pbp_document_id,
        first_events,
    )

    async with harness.engine.connect() as connection:
        game_row = (
            await connection.execute(
                select(games.c.home_score, games.c.away_score).where(
                    games.c.game_id == schedule.games[0].game_id.value
                )
            )
        ).one()
        team_rows = (
            (
                await connection.execute(
                    select(team_box_scores)
                    .where(team_box_scores.c.game_id == schedule.games[0].game_id.value)
                    .order_by(team_box_scores.c.team_id)
                )
            )
            .mappings()
            .all()
        )
        player_rows = (
            (
                await connection.execute(
                    select(player_box_scores).where(
                        player_box_scores.c.game_id == schedule.games[0].game_id.value
                    )
                )
            )
            .mappings()
            .all()
        )
        player_name = await connection.scalar(
            select(players.c.full_name).where(players.c.player_id == 9101)
        )
        event_rows = (
            (
                await connection.execute(
                    select(play_by_play_events).where(
                        play_by_play_events.c.game_id == schedule.games[0].game_id.value
                    )
                )
            )
            .mappings()
            .all()
        )
        raw_counts = (
            await connection.scalar(
                select(func.count())
                .select_from(source_documents)
                .where(
                    source_documents.c.source == harness.source,
                    source_documents.c.resource_key == str(box_key),
                )
            ),
            await connection.scalar(
                select(func.count())
                .select_from(source_documents)
                .where(
                    source_documents.c.source == harness.source,
                    source_documents.c.resource_key == str(pbp_key),
                )
            ),
        )
        box_resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(box_key),
                    )
                )
            )
            .mappings()
            .one()
        )
        pbp_resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(pbp_key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert game_row == (121, 109)
    assert [(row["team_id"], row["points"], row["plus_minus"]) for row in team_rows] == [
        (9001, 121, Decimal("10.25")),
        (9002, 109, Decimal("-10.25")),
    ]
    assert [(row["player_id"], row["played"], row["points"]) for row in player_rows] == [
        (9101, True, 20)
    ]
    assert player_name == "Augusta Ada Lovelace"
    assert [row["action_number"] for row in event_rows] == [1]
    assert event_rows[0]["description"] == "Corrected opening jumper"
    assert event_rows[0]["seconds_remaining_period_ms"] == 719_000
    assert raw_counts == (2, 2)
    assert box_resource["status"] == "succeeded"
    assert box_resource["latest_document_id"] == second_box_document_id
    assert box_resource["applied_document_id"] == second_box_document_id
    assert pbp_resource["status"] == "succeeded"
    assert pbp_resource["latest_document_id"] == second_pbp_document_id
    assert pbp_resource["applied_document_id"] == second_pbp_document_id


async def test_invalid_play_by_play_batch_rolls_back_scoped_delete(
    harness: CanonicalHarness,
) -> None:
    store = harness.store()
    schedule = make_schedule(
        harness,
        game_id="0029000003",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    await persist_schedule(harness, store, schedule)
    key = ResourceKey.play_by_play(schedule.games[0].game_id)
    original = make_events(schedule.games[0].game_id)
    first_claim = await harness.claim(store, key)
    first_document_id = await store.save_document(
        first_claim,
        make_document(harness, key, {"version": 1}),
    )
    await store.replace_play_by_play(first_claim, first_document_id, original)

    invalid = (
        replace(original[0], description="would replace"),
        replace(original[1], points_total=-1),
    )
    second_claim = await harness.claim(store, key, refresh=True)
    second_document_id = await store.save_document(
        second_claim,
        make_document(harness, key, {"version": 2}),
    )

    with pytest.raises(IntegrityError):
        await store.replace_play_by_play(second_claim, second_document_id, invalid)

    async with harness.engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    play_by_play_events.c.action_number,
                    play_by_play_events.c.description,
                    play_by_play_events.c.points_total,
                )
                .where(play_by_play_events.c.game_id == schedule.games[0].game_id.value)
                .order_by(play_by_play_events.c.action_number)
            )
        ).all()
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert [tuple(row) for row in rows] == [
        (1, "Opening jumper", 2),
        (2, "End of period", 0),
    ]
    assert resource["status"] == "running"
    assert resource["latest_document_id"] == second_document_id
    assert resource["applied_document_id"] == first_document_id


async def test_expired_old_claim_cannot_replace_newer_box_score(
    harness: CanonicalHarness,
) -> None:
    first_store = harness.store(now=BASE_TIME, lease_seconds=1)
    second_store = harness.store(now=BASE_TIME + timedelta(seconds=1), lease_seconds=60)
    schedule = make_schedule(
        harness,
        game_id="0029000004",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    await persist_schedule(harness, first_store, schedule)
    key = ResourceKey.box_score(schedule.games[0].game_id)
    stale_box = make_box_score(harness, schedule)
    stale_claim = await harness.claim(first_store, key)
    stale_document_id = await first_store.save_document(
        stale_claim,
        make_document(harness, key, {"owner": "old"}),
    )

    current_box = make_box_score(
        harness,
        schedule,
        home_score=121,
        away_score=109,
        include_bench=False,
    )
    current_claim = await harness.claim(second_store, key)
    current_document_id = await second_store.save_document(
        current_claim,
        make_document(harness, key, {"owner": "new"}),
    )
    await second_store.replace_box_score(current_claim, current_document_id, current_box)

    with pytest.raises(ClaimLostError, match=r"^claim_lost$"):
        await first_store.replace_box_score(stale_claim, stale_document_id, stale_box)

    async with harness.engine.connect() as connection:
        game_row = (
            await connection.execute(
                select(games.c.home_score, games.c.away_score).where(
                    games.c.game_id == schedule.games[0].game_id.value
                )
            )
        ).one()
        player_ids = (
            await connection.scalars(
                select(player_box_scores.c.player_id).where(
                    player_box_scores.c.game_id == schedule.games[0].game_id.value
                )
            )
        ).all()
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert game_row == (121, 109)
    assert player_ids == [9101]
    assert resource["status"] == "succeeded"
    assert resource["last_run_id"] == current_claim.run_id
    assert resource["claim_version"] == 2
    assert resource["latest_document_id"] == current_document_id
    assert resource["applied_document_id"] == current_document_id


async def test_aggregate_identity_is_rejected_before_canonical_sql(
    harness: CanonicalHarness,
) -> None:
    store = harness.store()
    schedule = make_schedule(
        harness,
        game_id="0029000005",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    wrong_key = ResourceKey.schedule(SeasonId("2091-92"))
    claim = await harness.claim(store, wrong_key)

    with pytest.raises(ValueError, match="claim resource"):
        await store.apply_schedule(claim, uuid4(), schedule)

    async with harness.engine.connect() as connection:
        game_count = await connection.scalar(
            select(func.count()).select_from(games).where(games.c.game_id == "0029000005")
        )
        resource_status = await connection.scalar(
            select(ingestion_resources.c.status).where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_key == str(wrong_key),
            )
        )

    assert game_count == 0
    assert resource_status == "running"


async def test_unlinked_document_cannot_enter_a_canonical_transaction(
    harness: CanonicalHarness,
) -> None:
    store = harness.store()
    schedule = make_schedule(
        harness,
        game_id="0029000006",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    key = ResourceKey.schedule(schedule.season)
    claim = await harness.claim(store, key)
    linked_document_id = await store.save_document(
        claim,
        make_document(harness, key, {"version": 1}),
    )
    wrong_document_id = uuid4()
    assert wrong_document_id != linked_document_id

    with pytest.raises(ClaimLostError, match=r"^claim_lost$"):
        await store.apply_schedule(claim, wrong_document_id, schedule)

    async with harness.engine.connect() as connection:
        game_count = await connection.scalar(
            select(func.count()).select_from(games).where(games.c.game_id == "0029000006")
        )
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert game_count == 0
    assert resource["status"] == "running"
    assert resource["latest_document_id"] == linked_document_id
    assert resource["applied_document_id"] is None


async def test_expired_unchanged_refresh_handoff_does_not_rewrite_schedule(
    harness: CanonicalHarness,
) -> None:
    schedule = make_schedule(
        harness,
        game_id="0029000007",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    key = ResourceKey.schedule(schedule.season)
    document = make_document(harness, key, {"version": 1})

    initial_store = harness.store(now=BASE_TIME, lease_seconds=1)
    initial_claim = await harness.claim(initial_store, key)
    document_id = await initial_store.save_document(initial_claim, document)
    await initial_store.apply_schedule(initial_claim, document_id, schedule)

    expiring_store = harness.store(now=BASE_TIME + timedelta(seconds=1), lease_seconds=1)
    expiring_claim = await harness.claim(expiring_store, key, refresh=True)
    assert await expiring_store.save_document(expiring_claim, document) == document_id

    takeover_store = harness.store(now=BASE_TIME + timedelta(seconds=2), lease_seconds=60)
    takeover_claim = await harness.claim(takeover_store, key)
    assert await takeover_store.save_document(takeover_claim, document) == document_id
    inconsistent = make_schedule(
        harness,
        game_id="0029000007",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
        home_name="Expired Owner Must Not Rewrite",
        arena_name="Expired Owner Arena",
    )
    handoff_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    await handoff_store.apply_schedule(takeover_claim, document_id, inconsistent)

    async with harness.engine.connect() as connection:
        team_name = await connection.scalar(select(teams.c.name).where(teams.c.team_id == 9001))
        arena_name = await connection.scalar(
            select(games.c.arena_name).where(games.c.game_id == "0029000007")
        )
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )

    assert team_name == "Atoms"
    assert arena_name == "Transaction Arena"
    assert resource["status"] == "succeeded"
    assert resource["claimed_until"] is None
    assert resource["last_run_id"] == takeover_claim.run_id
    assert resource["claim_version"] == 3
    assert resource["latest_document_id"] == document_id
    assert resource["applied_document_id"] == document_id


async def test_failed_changed_raw_keeps_applied_document_until_reparse_succeeds(
    harness: CanonicalHarness,
) -> None:
    schedule = make_schedule(
        harness,
        game_id="0029000008",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
    )
    key = ResourceKey.schedule(schedule.season)
    initial_store = harness.store(now=BASE_TIME)
    first_claim = await harness.claim(initial_store, key)
    first_document_id = await initial_store.save_document(
        first_claim,
        make_document(harness, key, {"version": 1}),
    )
    await initial_store.apply_schedule(first_claim, first_document_id, schedule)

    refresh_store = harness.store(now=BASE_TIME + timedelta(seconds=1))
    refresh_claim = await harness.claim(refresh_store, key, refresh=True)
    second_document = make_document(harness, key, {"version": 2})
    second_document_id = await refresh_store.save_document(refresh_claim, second_document)
    await refresh_store.fail_resource(
        refresh_claim,
        error_code="schema_mismatch",
        error_type="ProviderSchemaError",
    )

    async with harness.engine.connect() as connection:
        failed_resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )
    assert failed_resource["status"] == "failed"
    assert failed_resource["latest_document_id"] == second_document_id
    assert failed_resource["applied_document_id"] == first_document_id

    reparse_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    reparse_run_id = await reparse_store.start_run(BackfillRequest(seasons=(schedule.season,)))
    harness.run_ids.append(reparse_run_id)
    reparse_decision = await reparse_store.claim_resource(
        reparse_run_id,
        key,
        refresh=False,
    )
    reparse_claim = cast(ResourceClaim, reparse_decision.claim)
    assert reparse_decision.stored_document is not None
    assert reparse_decision.stored_document.document_id == second_document_id
    renamed = make_schedule(
        harness,
        game_id="0029000008",
        season="2090-91",
        home_team_id=9001,
        away_team_id=9002,
        home_name="Reparsed Atoms",
    )
    applying_store = harness.store(now=BASE_TIME + timedelta(seconds=2))
    await applying_store.apply_schedule(reparse_claim, second_document_id, renamed)

    async with harness.engine.connect() as connection:
        team_name = await connection.scalar(select(teams.c.name).where(teams.c.team_id == 9001))
        resource = (
            (
                await connection.execute(
                    select(ingestion_resources).where(
                        ingestion_resources.c.source == harness.source,
                        ingestion_resources.c.resource_key == str(key),
                    )
                )
            )
            .mappings()
            .one()
        )
    assert team_name == "Reparsed Atoms"
    assert resource["status"] == "succeeded"
    assert resource["latest_document_id"] == second_document_id
    assert resource["applied_document_id"] == second_document_id
