import asyncio
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    ClaimDecision,
    HistoricalDataSource,
    HistoricalIngestionService,
    HistoricalIngestionStore,
    ResourceKey,
    StoredSourceDocument,
)
from nba_live_totals.domain import (
    BoxScore,
    Game,
    GameId,
    GameStatus,
    PlayByPlayEvent,
    SeasonId,
    SeasonSchedule,
    SeasonType,
    SourceDocument,
    Team,
    TeamBoxScore,
    TraditionalStats,
)
from tests.unit.application.ingestion_fakes import (
    FakeHistoricalDataSource,
    FakeHistoricalIngestionStore,
    source_document,
)

SEASON = SeasonId("2025-26")
REGULAR_FINAL = GameId("0022500002")
PLAYOFF_FINAL = GameId("0042500001")
EXCLUDED_FINAL = GameId("0012500003")
SCHEDULED_GAME = GameId("0022500004")
MISSING_GAME = GameId("0022500999")
THIRD_FINAL = GameId("0022500005")
HOME_TEAM = Team(1, "Home City", "Homes", "HOM", "homes")
AWAY_TEAM = Team(2, "Away City", "Aways", "AWY", "aways")


@pytest.mark.asyncio
async def test_backfill_processes_only_sorted_accepted_final_games() -> None:
    events: list[str] = []
    source = make_source(events=events)
    store = FakeHistoricalIngestionStore(events=events)
    request = BackfillRequest(seasons=(SEASON,))

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(request)

    assert isinstance(source, HistoricalDataSource)
    assert isinstance(store, HistoricalIngestionStore)
    assert store.started == [request]
    assert source.schedule_fetches == [SEASON]
    assert source.box_score_fetches == [PLAYOFF_FINAL, REGULAR_FINAL]
    assert source.play_by_play_fetches == [PLAYOFF_FINAL, REGULAR_FINAL]
    assert len(store.applied_schedules) == 1
    assert [row[2].game_id for row in store.applied_box_scores] == [
        PLAYOFF_FINAL,
        REGULAR_FINAL,
    ]
    assert [row[2][0].game_id for row in store.applied_play_by_play] == [
        PLAYOFF_FINAL,
        REGULAR_FINAL,
    ]
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.SUCCEEDED,
        discovered=2,
        succeeded=5,
        skipped=0,
        failed=0,
    )
    assert store.finished == [(store.run_id, result)]

    for key in (
        ResourceKey.schedule(SEASON),
        ResourceKey.box_score(PLAYOFF_FINAL),
        ResourceKey.play_by_play(PLAYOFF_FINAL),
        ResourceKey.box_score(REGULAR_FINAL),
        ResourceKey.play_by_play(REGULAR_FINAL),
    ):
        claim_index = events.index(f"claim:{key}")
        fetch_index = events.index(f"fetch:{key}")
        save_index = events.index(f"save:{key}")
        parse_index = events.index(f"parse:{key}")
        apply_index = events.index(f"apply:{key}")
        assert claim_index < fetch_index < save_index < parse_index < apply_index


@pytest.mark.asyncio
async def test_targeted_backfill_queues_only_the_exact_selected_final_game() -> None:
    source = make_source()
    store = FakeHistoricalIngestionStore()
    request = BackfillRequest(seasons=(SEASON,), game_id=REGULAR_FINAL)

    result = await HistoricalIngestionService(source, store, max_concurrency=2).backfill(request)

    assert source.box_score_fetches == [REGULAR_FINAL]
    assert source.play_by_play_fetches == [REGULAR_FINAL]
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.SUCCEEDED,
        discovered=1,
        succeeded=3,
        skipped=0,
        failed=0,
    )
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
@pytest.mark.parametrize("game_id", [MISSING_GAME, EXCLUDED_FINAL, SCHEDULED_GAME])
async def test_targeted_backfill_does_not_queue_an_unavailable_final_game(
    game_id: GameId,
) -> None:
    source = make_source()
    store = FakeHistoricalIngestionStore()

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,), game_id=game_id)
    )

    assert source.box_score_fetches == []
    assert source.play_by_play_fetches == []
    assert result.discovered == 0
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.status is BackfillStatus.SUCCEEDED
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_skip_is_terminal_no_work_and_counts_as_successful_completion() -> None:
    source = make_source()
    schedule_key = ResourceKey.schedule(SEASON)
    store = FakeHistoricalIngestionStore(decisions={schedule_key: ClaimAction.SKIP})

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert source.schedule_fetches == []
    assert source.parsed_keys == []
    assert store.saved == []
    assert store.applied_schedules == []
    assert store.applied_box_scores == []
    assert store.applied_play_by_play == []
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.SUCCEEDED,
        discovered=0,
        succeeded=0,
        skipped=1,
        failed=0,
    )
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_busy_game_resource_is_terminal_and_finishes_partial() -> None:
    source = make_source()
    busy_key = ResourceKey.box_score(REGULAR_FINAL)
    store = FakeHistoricalIngestionStore(decisions={busy_key: ClaimAction.BUSY})

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,), game_id=REGULAR_FINAL)
    )

    assert source.box_score_fetches == []
    assert source.play_by_play_fetches == [REGULAR_FINAL]
    assert store.failures == []
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.PARTIAL,
        discovered=1,
        succeeded=2,
        skipped=0,
        failed=1,
    )
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_busy_schedule_always_finishes_partial() -> None:
    source = make_source()
    schedule_key = ResourceKey.schedule(SEASON)
    store = FakeHistoricalIngestionStore(decisions={schedule_key: ClaimAction.BUSY})

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.PARTIAL,
        discovered=0,
        succeeded=0,
        skipped=0,
        failed=1,
    )
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_busy_skip_and_success_outcomes_keep_distinct_counters() -> None:
    source = make_source()
    box_key = ResourceKey.box_score(REGULAR_FINAL)
    play_by_play_key = ResourceKey.play_by_play(REGULAR_FINAL)
    store = FakeHistoricalIngestionStore(
        decisions={
            box_key: ClaimAction.BUSY,
            play_by_play_key: ClaimAction.SKIP,
        }
    )

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,), game_id=REGULAR_FINAL)
    )

    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.PARTIAL,
        discovered=1,
        succeeded=1,
        skipped=1,
        failed=1,
    )
    assert source.box_score_fetches == []
    assert source.play_by_play_fetches == []
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_box_score_and_play_by_play_start_together_within_one_game_slot() -> None:
    source = make_source()
    store = SiblingClaimBarrierStore(REGULAR_FINAL)

    async with asyncio.timeout(1):
        result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
            BackfillRequest(seasons=(SEASON,), game_id=REGULAR_FINAL)
        )

    assert set(store.sibling_arrivals) == {
        ResourceKey.box_score(REGULAR_FINAL),
        ResourceKey.play_by_play(REGULAR_FINAL),
    }
    assert result.succeeded == 3
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_reparse_uses_the_decision_document_without_fetching_or_saving() -> None:
    events: list[str] = []
    source = make_source(events=events, games=())
    schedule_key = ResourceKey.schedule(SEASON)
    stored = StoredSourceDocument(
        document_id=UUID("00000000-0000-0000-0000-000000000099"),
        document=source_document(schedule_key),
    )
    store = FakeHistoricalIngestionStore(
        events=events,
        decisions={schedule_key: ClaimAction.REPARSE},
    )
    store.latest[schedule_key] = stored

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert source.schedule_fetches == []
    assert store.saved == []
    assert f"latest:{schedule_key}" not in events
    assert source.parsed_keys == [str(schedule_key)]
    assert store.applied_schedules[0][1] == stored.document_id
    assert (
        events.index(f"claim:{schedule_key}")
        < events.index(f"parse:{schedule_key}")
        < events.index(f"apply:{schedule_key}")
    )
    assert result.succeeded == 1
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_seasons_are_processed_sequentially() -> None:
    previous_season = SeasonId("2024-25")
    events: list[str] = []
    source = FakeHistoricalDataSource(
        schedules={
            previous_season: SeasonSchedule(previous_season, (), ()),
            SEASON: SeasonSchedule(SEASON, (), ()),
        },
        box_scores={},
        play_by_play={},
        events=events,
    )
    store = FakeHistoricalIngestionStore(events=events)

    result = await HistoricalIngestionService(source, store, max_concurrency=2).backfill(
        BackfillRequest(seasons=(previous_season, SEASON))
    )

    assert events.index(f"apply:{ResourceKey.schedule(previous_season)}") < events.index(
        f"claim:{ResourceKey.schedule(SEASON)}"
    )
    assert result.discovered == 0
    assert result.succeeded == 2
    assert store.finished == [(store.run_id, result)]


@pytest.mark.asyncio
async def test_max_concurrency_bounds_complete_per_game_tasks() -> None:
    games = (*_schedule_games(), _game(THIRD_FINAL, date(2026, 1, 4), GameStatus.FINAL))
    base_source = make_source(games=games)
    source = BlockingGameSource(base_source)
    store = FakeHistoricalIngestionStore(events=source.events)
    task = asyncio.create_task(
        HistoricalIngestionService(source, store, max_concurrency=2).backfill(
            BackfillRequest(seasons=(SEASON,))
        )
    )

    try:
        await asyncio.wait_for(source.two_games_started.wait(), timeout=1)
        assert source.started_games == [PLAYOFF_FINAL, REGULAR_FINAL]
        assert source.maximum_active_games == 2
        assert THIRD_FINAL not in source.started_games
    finally:
        source.release_games.set()
        result = await task

    assert source.started_games == [PLAYOFF_FINAL, REGULAR_FINAL, THIRD_FINAL]
    assert result.discovered == 3
    assert result.succeeded == 7
    assert store.finished == [(store.run_id, result)]


@pytest.mark.parametrize("max_concurrency", [0, -1, True, 1.5])
def test_service_requires_positive_integer_concurrency(max_concurrency: object) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        HistoricalIngestionService(
            make_source(),
            FakeHistoricalIngestionStore(),
            max_concurrency=max_concurrency,  # type: ignore[arg-type]
        )


class BlockingGameSource(FakeHistoricalDataSource):
    """Hold box fetches so tests can observe the game-level concurrency bound."""

    def __init__(self, source: FakeHistoricalDataSource) -> None:
        super().__init__(
            schedules=source.schedules,
            box_scores=source.box_scores,
            play_by_play=source.play_by_play,
            events=source.events,
        )
        self.started_games: list[GameId] = []
        self.active_games = 0
        self.maximum_active_games = 0
        self.two_games_started = asyncio.Event()
        self.release_games = asyncio.Event()

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        self.started_games.append(game_id)
        self.active_games += 1
        self.maximum_active_games = max(self.maximum_active_games, self.active_games)
        if self.active_games == 2:
            self.two_games_started.set()
        try:
            await self.release_games.wait()
            return await super().fetch_box_score(game_id)
        finally:
            self.active_games -= 1


class SiblingClaimBarrierStore(FakeHistoricalIngestionStore):
    """Require both resources for one game to claim before either may continue."""

    def __init__(self, game_id: GameId) -> None:
        super().__init__()
        self._sibling_keys = {
            ResourceKey.box_score(game_id),
            ResourceKey.play_by_play(game_id),
        }
        self.sibling_arrivals: list[ResourceKey] = []
        self._siblings_arrived = asyncio.Event()

    async def claim_resource(
        self, run_id: UUID, key: ResourceKey, *, refresh: bool
    ) -> ClaimDecision:
        if key in self._sibling_keys:
            self.sibling_arrivals.append(key)
            if set(self.sibling_arrivals) == self._sibling_keys:
                self._siblings_arrived.set()
            await self._siblings_arrived.wait()
        return await super().claim_resource(run_id, key, refresh=refresh)


def make_source(
    *,
    events: list[str] | None = None,
    games: tuple[Game, ...] | None = None,
) -> FakeHistoricalDataSource:
    schedule_games = games if games is not None else _schedule_games()
    accepted_final_games = tuple(
        game
        for game in schedule_games
        if game.status is GameStatus.FINAL and not game.game_id.is_excluded
    )
    return FakeHistoricalDataSource(
        schedules={
            SEASON: SeasonSchedule(
                season=SEASON,
                teams=(HOME_TEAM, AWAY_TEAM),
                games=schedule_games,
            )
        },
        box_scores={game.game_id: _box_score(game) for game in accepted_final_games},
        play_by_play={game.game_id: (_event(game.game_id),) for game in accepted_final_games},
        events=events,
    )


def _schedule_games() -> tuple[Game, ...]:
    return (
        _game(REGULAR_FINAL, date(2026, 1, 3), GameStatus.FINAL),
        _game(EXCLUDED_FINAL, date(2025, 10, 1), GameStatus.FINAL),
        _game(SCHEDULED_GAME, date(2025, 12, 1), GameStatus.SCHEDULED),
        _game(PLAYOFF_FINAL, date(2026, 1, 2), GameStatus.FINAL),
    )


def _game(game_id: GameId, game_date: date, status: GameStatus) -> Game:
    is_final = status is GameStatus.FINAL
    return Game(
        game_id=game_id,
        season=SEASON,
        season_type=(
            SeasonType.PLAYOFFS if game_id.value.startswith("004") else SeasonType.REGULAR_SEASON
        ),
        game_date=game_date,
        started_at=datetime.combine(game_date, datetime.min.time(), tzinfo=UTC),
        home_team_id=HOME_TEAM.team_id,
        away_team_id=AWAY_TEAM.team_id,
        home_score=101 if is_final else None,
        away_score=99 if is_final else None,
        status=status,
        arena_name=None,
        arena_city=None,
        arena_state=None,
    )


def _box_score(game: Game) -> BoxScore:
    assert game.home_score is not None
    assert game.away_score is not None
    return BoxScore(
        game_id=game.game_id,
        home_team_id=game.home_team_id,
        away_team_id=game.away_team_id,
        home_score=game.home_score,
        away_score=game.away_score,
        team_rows=(
            TeamBoxScore(
                game.game_id,
                HOME_TEAM,
                TraditionalStats(points=game.home_score),
            ),
            TeamBoxScore(
                game.game_id,
                AWAY_TEAM,
                TraditionalStats(points=game.away_score),
            ),
        ),
        player_rows=(),
    )


def _event(game_id: GameId) -> PlayByPlayEvent:
    return PlayByPlayEvent(
        game_id=game_id,
        action_number=1,
        action_id=1,
        period=1,
        clock="PT12M00.00S",
        seconds_remaining_period_ms=720_000,
        team_id=None,
        person_id=None,
        action_type="period",
        sub_type="start",
        description="Start of period",
        home_score=0,
        away_score=0,
        shot_result=None,
        shot_distance=None,
        legacy_x=None,
        legacy_y=None,
        location=None,
        is_field_goal=False,
        points_total=0,
        video_available=False,
    )
