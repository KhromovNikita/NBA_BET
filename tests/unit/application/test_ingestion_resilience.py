import asyncio
import logging
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    ClaimLostError,
    IngestionResourceError,
    ResourceClaim,
    ResourceKey,
    StoredSourceDocument,
)
from nba_live_totals.application.ingestion.service import HistoricalIngestionService
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

PREVIOUS_SEASON = SeasonId("2024-25")
SEASON = SeasonId("2025-26")
FIRST_GAME = GameId("0022500001")
SECOND_GAME = GameId("0022500002")
HOME_TEAM = Team(1, "Home City", "Homes", "HOM", "homes")
AWAY_TEAM = Team(2, "Away City", "Aways", "AWY", "aways")
SECRET = "postgresql://user:password@db/raw?token=secret"


class SensitiveNetworkError(IngestionResourceError):
    def __init__(self) -> None:
        super().__init__("network_timeout")

    def __str__(self) -> str:
        return SECRET


class SensitivePersistenceError(RuntimeError):
    pass


class FaultInjectingSource(FakeHistoricalDataSource):
    def __init__(self, *, games: tuple[Game, ...]) -> None:
        super().__init__(
            schedules={
                SEASON: SeasonSchedule(SEASON, (HOME_TEAM, AWAY_TEAM), games),
                PREVIOUS_SEASON: SeasonSchedule(PREVIOUS_SEASON, (), ()),
            },
            box_scores={game.game_id: _box_score(game) for game in games},
            play_by_play={game.game_id: (_event(game.game_id),) for game in games},
        )
        self.fetch_errors: dict[ResourceKey, Exception] = {}
        self.parse_errors: dict[ResourceKey, Exception] = {}

    async def fetch_schedule(self, season: SeasonId) -> SourceDocument:
        key = ResourceKey.schedule(season)
        if error := self.fetch_errors.get(key):
            self.schedule_fetches.append(season)
            self.events.append(f"fetch:{key}")
            raise error
        return await super().fetch_schedule(season)

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.box_score(game_id)
        if error := self.fetch_errors.get(key):
            self.box_score_fetches.append(game_id)
            self.events.append(f"fetch:{key}")
            raise error
        return await super().fetch_box_score(game_id)

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        key = ResourceKey.play_by_play(game_id)
        if error := self.fetch_errors.get(key):
            self.play_by_play_fetches.append(game_id)
            self.events.append(f"fetch:{key}")
            raise error
        return await super().fetch_play_by_play(game_id)

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule:
        if error := self.parse_errors.get(_document_key(document)):
            self.parsed_keys.append(document.resource_key)
            self.events.append(f"parse:{document.resource_key}")
            raise error
        return super().parse_schedule(document)

    def parse_box_score(self, document: SourceDocument) -> BoxScore:
        if error := self.parse_errors.get(_document_key(document)):
            self.parsed_keys.append(document.resource_key)
            self.events.append(f"parse:{document.resource_key}")
            raise error
        return super().parse_box_score(document)

    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]:
        if error := self.parse_errors.get(_document_key(document)):
            self.parsed_keys.append(document.resource_key)
            self.events.append(f"parse:{document.resource_key}")
            raise error
        return super().parse_play_by_play(document)


class SaveFailingStore(FakeHistoricalIngestionStore):
    async def save_document(self, claim: ResourceClaim, document: SourceDocument) -> UUID:
        if document.resource_key == str(ResourceKey.box_score(FIRST_GAME)):
            raise ValueError(SECRET)
        return await super().save_document(claim, document)


class BlockingGameSource(FaultInjectingSource):
    def __init__(self, *, events: list[str]) -> None:
        super().__init__(games=(_game(FIRST_GAME, date(2026, 1, 1)),))
        self.events = events
        self.started: set[ResourceKey] = set()
        self.cancelled: set[ResourceKey] = set()
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_box_score(self, game_id: GameId) -> SourceDocument:
        return await self._blocked_fetch(ResourceKey.box_score(game_id))

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        return await self._blocked_fetch(ResourceKey.play_by_play(game_id))

    async def _blocked_fetch(self, key: ResourceKey) -> SourceDocument:
        self.events.append(f"fetch_started:{key}")
        self.started.add(key)
        if len(self.started) == 2:
            self.both_started.set()
        try:
            await self.release.wait()
        finally:
            self.events.append(f"fetch_cancelled:{key}")
            self.cancelled.add(key)
        return source_document(key)


class PartiallyBlockingGameSource(FaultInjectingSource):
    def __init__(self) -> None:
        super().__init__(games=(_game(FIRST_GAME, date(2026, 1, 1)),))
        self.play_by_play_started = asyncio.Event()
        self.play_by_play_cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument:
        self.play_by_play_started.set()
        try:
            await self.release.wait()
        finally:
            self.play_by_play_cancelled.set()
        return await super().fetch_play_by_play(game_id)


class BoxAppliedStore(FakeHistoricalIngestionStore):
    def __init__(self) -> None:
        super().__init__()
        self.box_applied = asyncio.Event()

    async def replace_box_score(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        box_score: BoxScore,
    ) -> None:
        await super().replace_box_score(claim, document_id, box_score)
        self.box_applied.set()


class InterruptFinishFailingStore(FakeHistoricalIngestionStore):
    async def finish_run(self, run_id: UUID, result: BackfillResult) -> None:
        self.events.append("finish_run")
        self.finished.append((run_id, result))
        if result.status is BackfillStatus.INTERRUPTED:
            raise SensitivePersistenceError(SECRET)


class ClaimLostWhileFailingStore(FakeHistoricalIngestionStore):
    def __init__(self) -> None:
        super().__init__()
        self.failure_attempts: list[tuple[ResourceClaim, str, str]] = []

    async def fail_resource(
        self,
        claim: ResourceClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        self.failure_attempts.append((claim, error_code, error_type))
        raise ClaimLostError


@pytest.mark.asyncio
async def test_resource_failures_are_safe_and_do_not_stop_siblings_or_later_games(
    caplog: pytest.LogCaptureFixture,
) -> None:
    games = (
        _game(FIRST_GAME, date(2026, 1, 1)),
        _game(SECOND_GAME, date(2026, 1, 2)),
    )
    source = FaultInjectingSource(games=games)
    source.fetch_errors[ResourceKey.box_score(FIRST_GAME)] = SensitiveNetworkError()
    source.parse_errors[ResourceKey.play_by_play(FIRST_GAME)] = ValueError(SECRET)
    store = FakeHistoricalIngestionStore()
    caplog.set_level(logging.INFO, logger="nba_live_totals.application.ingestion.service")

    result = await HistoricalIngestionService(source, store, max_concurrency=2).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert {(failure[0].key, failure[1], failure[2]) for failure in store.failures} == {
        (ResourceKey.box_score(FIRST_GAME), "network_timeout", "SensitiveNetworkError"),
        (ResourceKey.play_by_play(FIRST_GAME), "invalid_play_by_play", "ValueError"),
    }
    assert {row[2].game_id for row in store.applied_box_scores} == {SECOND_GAME}
    assert {row[2][0].game_id for row in store.applied_play_by_play} == {SECOND_GAME}
    assert any(
        document.resource_key == str(ResourceKey.play_by_play(FIRST_GAME))
        for _, document, _ in store.saved
    )
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.PARTIAL,
        discovered=2,
        succeeded=3,
        skipped=0,
        failed=2,
    )
    assert store.finished == [(store.run_id, result)]
    assert SECRET not in caplog.text
    failed_records = {
        record.__dict__["resource_type"]: record
        for record in caplog.records
        if getattr(record, "event", None) == "ingestion_resource_finished"
        and getattr(record, "status", None) == "failed"
    }
    box_record = failed_records["box_score"]
    assert box_record.__dict__["run_id"] == str(store.run_id)
    assert box_record.__dict__["season"] == SEASON.value
    assert box_record.__dict__["game_id"] == FIRST_GAME.value
    assert box_record.__dict__["attempt"] == 1
    assert box_record.__dict__["duration_ms"] >= 0
    assert box_record.__dict__["error_code"] == "network_timeout"
    assert box_record.__dict__["error_type"] == "SensitiveNetworkError"
    assert failed_records["play_by_play"].__dict__["error_code"] == "invalid_play_by_play"
    assert failed_records["play_by_play"].__dict__["error_type"] == "ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "expected_code"),
    [
        ("schedule", "invalid_game"),
        ("box_score", "invalid_box_score"),
        ("play_by_play", "invalid_play_by_play"),
    ],
)
async def test_value_error_maps_to_the_resource_specific_validation_code(
    resource: str,
    expected_code: str,
) -> None:
    source = FaultInjectingSource(games=(_game(FIRST_GAME, date(2026, 1, 1)),))
    key = {
        "schedule": ResourceKey.schedule(SEASON),
        "box_score": ResourceKey.box_score(FIRST_GAME),
        "play_by_play": ResourceKey.play_by_play(FIRST_GAME),
    }[resource]
    source.parse_errors[key] = ValueError(SECRET)
    store = FakeHistoricalIngestionStore()

    await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    failure = next(failure for failure in store.failures if failure[0].key == key)
    assert failure[1:] == (expected_code, "ValueError")


@pytest.mark.asyncio
async def test_unknown_persistence_exception_maps_to_persistence_error() -> None:
    source = FaultInjectingSource(games=(_game(FIRST_GAME, date(2026, 1, 1)),))
    store = SaveFailingStore()

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    failure = next(
        failure for failure in store.failures if failure[0].key == ResourceKey.box_score(FIRST_GAME)
    )
    assert failure[1:] == ("persistence_error", "ValueError")
    assert result.status is BackfillStatus.PARTIAL
    assert result.failed == 1


@pytest.mark.asyncio
async def test_claim_lost_while_recording_failure_keeps_safe_code_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FaultInjectingSource(games=(_game(FIRST_GAME, date(2026, 1, 1)),))
    source.fetch_errors[ResourceKey.box_score(FIRST_GAME)] = SensitiveNetworkError()
    store = ClaimLostWhileFailingStore()
    caplog.set_level(logging.WARNING, logger="nba_live_totals.application.ingestion.service")

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert store.failure_attempts[0][1:] == ("network_timeout", "SensitiveNetworkError")
    assert len(store.applied_play_by_play) == 1
    box_record = next(
        record for record in caplog.records if getattr(record, "resource_type", None) == "box_score"
    )
    assert box_record.__dict__["error_code"] == "claim_lost"
    assert box_record.__dict__["error_type"] == "ClaimLostError"
    assert result.status is BackfillStatus.PARTIAL
    assert result.failed == 1


@pytest.mark.asyncio
async def test_schedule_failure_does_not_stop_later_seasons() -> None:
    source = FaultInjectingSource(games=())
    source.fetch_errors[ResourceKey.schedule(PREVIOUS_SEASON)] = SensitiveNetworkError()
    store = FakeHistoricalIngestionStore()

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(PREVIOUS_SEASON, SEASON))
    )

    assert source.schedule_fetches == [PREVIOUS_SEASON, SEASON]
    assert [row[2].season for row in store.applied_schedules] == [SEASON]
    assert result == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.PARTIAL,
        discovered=0,
        succeeded=1,
        skipped=0,
        failed=1,
    )


@pytest.mark.asyncio
async def test_reparse_uses_returned_document_without_a_network_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FaultInjectingSource(games=())
    key = ResourceKey.schedule(SEASON)
    stored = StoredSourceDocument(
        document_id=UUID("00000000-0000-0000-0000-000000000099"),
        document=source_document(key),
    )
    store = FakeHistoricalIngestionStore(decisions={key: ClaimAction.REPARSE})
    store.latest[key] = stored
    caplog.set_level(logging.INFO, logger="nba_live_totals.application.ingestion.service")

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,))
    )

    assert source.schedule_fetches == []
    assert source.parsed_keys == [str(key)]
    assert store.saved == []
    assert store.applied_schedules[0][1] == stored.document_id
    assert result.succeeded == 1
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ingestion_resource_finished"
    )
    assert record.__dict__["attempt"] == 1
    assert record.__dict__["status"] == "succeeded"


@pytest.mark.asyncio
async def test_refresh_fetches_even_when_a_prior_raw_document_exists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FaultInjectingSource(games=())
    key = ResourceKey.schedule(SEASON)
    store = FakeHistoricalIngestionStore(decisions={key: ClaimAction.FETCH})
    store.latest[key] = StoredSourceDocument(
        document_id=UUID("00000000-0000-0000-0000-000000000099"),
        document=source_document(key),
    )
    caplog.set_level(logging.INFO, logger="nba_live_totals.application.ingestion.service")

    result = await HistoricalIngestionService(source, store, max_concurrency=1).backfill(
        BackfillRequest(seasons=(SEASON,), refresh=True)
    )

    assert source.schedule_fetches == [SEASON]
    assert source.parsed_keys == [str(key)]
    assert len(store.saved) == 1
    assert store.claimed == [(store.run_id, key, True)]
    assert result.succeeded == 1
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ingestion_resource_finished"
    )
    assert record.__dict__["resource_type"] == "schedule"
    assert record.__dict__["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancellation_settles_resource_tasks_then_finishes_interrupted_and_reraises() -> None:
    events: list[str] = []
    source = BlockingGameSource(events=events)
    store = FakeHistoricalIngestionStore(events=events)
    task = asyncio.create_task(
        HistoricalIngestionService(source, store, max_concurrency=1).backfill(
            BackfillRequest(seasons=(SEASON,))
        )
    )
    await asyncio.wait_for(source.both_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.cancelled == {
        ResourceKey.box_score(FIRST_GAME),
        ResourceKey.play_by_play(FIRST_GAME),
    }
    assert store.failures == []
    interrupted = BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.INTERRUPTED,
        discovered=1,
        succeeded=1,
        skipped=0,
        failed=0,
    )
    assert store.finished == [(store.run_id, interrupted)]
    finish_index = events.index("finish_run")
    assert events.index(f"fetch_cancelled:{ResourceKey.box_score(FIRST_GAME)}") < finish_index
    assert events.index(f"fetch_cancelled:{ResourceKey.play_by_play(FIRST_GAME)}") < finish_index
    settled_events = tuple(events)
    await asyncio.sleep(0)
    assert tuple(events) == settled_events


@pytest.mark.asyncio
async def test_interrupted_counters_include_a_sibling_that_completed_before_cancellation() -> None:
    source = PartiallyBlockingGameSource()
    store = BoxAppliedStore()
    task = asyncio.create_task(
        HistoricalIngestionService(source, store, max_concurrency=1).backfill(
            BackfillRequest(seasons=(SEASON,))
        )
    )
    await asyncio.wait_for(source.play_by_play_started.wait(), timeout=1)
    await asyncio.wait_for(store.box_applied.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.play_by_play_cancelled.is_set()
    assert store.failures == []
    assert store.finished[0][1] == BackfillResult(
        run_id=store.run_id,
        status=BackfillStatus.INTERRUPTED,
        discovered=1,
        succeeded=2,
        skipped=0,
        failed=0,
    )


@pytest.mark.asyncio
async def test_finish_failure_during_cancellation_does_not_replace_cancelled_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    source = BlockingGameSource(events=events)
    store = InterruptFinishFailingStore(events=events)
    caplog.set_level(logging.ERROR, logger="nba_live_totals.application.ingestion.service")
    task = asyncio.create_task(
        HistoricalIngestionService(source, store, max_concurrency=1).backfill(
            BackfillRequest(seasons=(SEASON,))
        )
    )
    await asyncio.wait_for(source.both_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.finished[0][1].status is BackfillStatus.INTERRUPTED
    assert SECRET not in caplog.text


def _document_key(document: SourceDocument) -> ResourceKey:
    resource_type, target = document.resource_key.split(":", maxsplit=1)
    if resource_type == "schedule":
        return ResourceKey.schedule(SeasonId(target))
    if resource_type == "box_score":
        return ResourceKey.box_score(GameId(target))
    return ResourceKey.play_by_play(GameId(target))


def _game(game_id: GameId, game_date: date) -> Game:
    return Game(
        game_id=game_id,
        season=SEASON,
        season_type=SeasonType.REGULAR_SEASON,
        game_date=game_date,
        started_at=datetime.combine(game_date, datetime.min.time(), tzinfo=UTC),
        home_team_id=HOME_TEAM.team_id,
        away_team_id=AWAY_TEAM.team_id,
        home_score=101,
        away_score=99,
        status=GameStatus.FINAL,
        arena_name=None,
        arena_city=None,
        arena_state=None,
    )


def _box_score(game: Game) -> BoxScore:
    return BoxScore(
        game_id=game.game_id,
        home_team_id=game.home_team_id,
        away_team_id=game.away_team_id,
        home_score=101,
        away_score=99,
        team_rows=(
            TeamBoxScore(game.game_id, HOME_TEAM, TraditionalStats(points=101)),
            TeamBoxScore(game.game_id, AWAY_TEAM, TraditionalStats(points=99)),
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
