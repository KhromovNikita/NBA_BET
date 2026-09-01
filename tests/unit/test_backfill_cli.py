import asyncio
import logging
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast
from uuid import UUID

import pytest
from pydantic import AnyUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
)
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.settings import Settings
from scripts import backfill_games

RUN_ID = UUID("d6aa4559-18f0-42f1-b6be-fe221823053a")


def _result(status: BackfillStatus = BackfillStatus.SUCCEEDED) -> BackfillResult:
    return BackfillResult(
        run_id=RUN_ID,
        status=status,
        discovered=3,
        succeeded=4,
        skipped=2,
        failed=1 if status is not BackfillStatus.SUCCEEDED else 0,
    )


def test_parse_args_builds_single_season_request() -> None:
    request = backfill_games.parse_args(["--season", "2025-26"])

    assert request == BackfillRequest(seasons=(SeasonId("2025-26"),))


def test_parse_args_builds_inclusive_season_range() -> None:
    request = backfill_games.parse_args(["--season-from", "2021-22", "--season-to", "2025-26"])

    assert tuple(season.value for season in request.seasons) == (
        "2021-22",
        "2022-23",
        "2023-24",
        "2024-25",
        "2025-26",
    )
    assert request.game_id is None
    assert request.refresh is False


def test_parse_args_builds_targeted_game_request() -> None:
    request = backfill_games.parse_args(["--season", "2025-26", "--game-id", "0022500123"])

    assert request == BackfillRequest(
        seasons=(SeasonId("2025-26"),),
        game_id=GameId("0022500123"),
    )


def test_parse_args_enables_refresh_without_changing_selection() -> None:
    request = backfill_games.parse_args(["--season", "2025-26", "--refresh"])

    assert request.seasons == (SeasonId("2025-26"),)
    assert request.refresh is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--season-from", "2021-22"],
        ["--season-to", "2025-26"],
        ["--season", "2025-26", "--season-from", "2021-22", "--season-to", "2025-26"],
        ["--season", "2025-26", "--season-to", "2026-27"],
        [
            "--season-from",
            "2021-22",
            "--season-to",
            "2025-26",
            "--game-id",
            "0022500123",
        ],
        ["--season", "2025-25"],
        ["--season-from", "2025-26", "--season-to", "2021-22"],
        ["--season", "2025-26", "--game-id", "0022400123"],
        ["--season", "2025-26", "--game-id", "9992500123"],
    ],
)
def test_parse_args_reports_invalid_selector_or_identifier_as_usage_error(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        backfill_games.parse_args(argv)

    assert captured.value.code == 2


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (BackfillStatus.SUCCEEDED, 0),
        (BackfillStatus.PARTIAL, 1),
        (BackfillStatus.FAILED, 1),
        (BackfillStatus.INTERRUPTED, 1),
    ],
)
def test_main_prints_exactly_one_summary_and_maps_terminal_status(
    capsys: pytest.CaptureFixture[str],
    status: BackfillStatus,
    expected_code: int,
) -> None:
    calls: list[tuple[BackfillRequest, Settings]] = []

    async def run_command(request: BackfillRequest, settings: Settings) -> BackfillResult:
        calls.append((request, settings))
        return _result(status)

    code = backfill_games.main(
        ["--season", "2025-26"],
        run_command=run_command,
    )

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.out == (
        f"run={RUN_ID} status={status.value} discovered=3 "
        f"succeeded=4 skipped=2 failed={1 if status is not BackfillStatus.SUCCEEDED else 0}\n"
    )
    assert captured.err == ""
    assert len(calls) == 1
    assert calls[0][0] == BackfillRequest(seasons=(SeasonId("2025-26"),))
    assert isinstance(calls[0][1], Settings)


def test_main_returns_two_for_argument_error_without_running_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    async def run_command(request: BackfillRequest, settings: Settings) -> BackfillResult:
        nonlocal called
        called = True
        return _result()

    code = backfill_games.main([], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 2
    assert called is False
    assert captured.out == ""
    assert "usage:" in captured.err


def test_main_returns_two_for_invalid_environment_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NBA_API_MAX_CONCURRENCY", "0")
    called = False

    async def run_command(request: BackfillRequest, settings: Settings) -> BackfillResult:
        nonlocal called
        called = True
        return _result()

    code = backfill_games.main(["--season", "2025-26"], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 2
    assert called is False
    assert captured.out == ""
    assert captured.err == "invalid configuration\n"


def test_main_returns_standard_interrupt_status_without_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_command(request: BackfillRequest, settings: Settings) -> BackfillResult:
        return _result()

    def interrupt(coroutine: object) -> NoReturn:
        cast(ClosableCoroutine, coroutine).close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt)

    code = backfill_games.main(["--season", "2025-26"], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 130
    assert captured.out == ""
    assert captured.err == ""


class ClosableCoroutine(Protocol):
    def close(self) -> None: ...


@dataclass
class RecordingEngine:
    events: list[object]
    dispose_error: BaseException | None = None
    dispose_calls: int = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        self.events.append("dispose")
        if self.dispose_error is not None:
            raise self.dispose_error


@dataclass(frozen=True)
class CompositionFixture:
    engine: RecordingEngine
    events: list[object]
    transport: object
    source: object
    store: object


def _install_composition(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: BackfillResult | None = None,
    error: BaseException | None = None,
    dispose_error: BaseException | None = None,
) -> CompositionFixture:
    events: list[object] = []
    engine = RecordingEngine(events, dispose_error=dispose_error)
    transport = object()
    source = object()
    store = object()

    def configure_logging(settings: Settings) -> None:
        events.append(("logging", settings))

    def create_engine(database_url: str, *, pool_pre_ping: bool) -> AsyncEngine:
        events.append(("engine", database_url, pool_pre_ping))
        return cast(AsyncEngine, engine)

    def create_transport(settings: Settings) -> object:
        events.append(("transport", settings))
        return transport

    def create_source(value: object) -> object:
        events.append(("source", value))
        return source

    def create_store(
        value: AsyncEngine,
        *,
        lease_seconds: int,
    ) -> object:
        events.append(("store", value, lease_seconds))
        return store

    class RecordingService:
        def __init__(
            self,
            source_value: object,
            store_value: object,
            max_concurrency: int,
        ) -> None:
            events.append(("service", source_value, store_value, max_concurrency))

        async def backfill(self, request: BackfillRequest) -> BackfillResult:
            events.append(("backfill", request))
            if error is not None:
                raise error
            assert result is not None
            return result

    monkeypatch.setattr(backfill_games, "configure_logging", configure_logging)
    monkeypatch.setattr(backfill_games, "create_async_engine", create_engine)
    monkeypatch.setattr(backfill_games, "NBAApiTransport", create_transport)
    monkeypatch.setattr(backfill_games, "NBAApiHistoricalDataSource", create_source)
    monkeypatch.setattr(backfill_games, "PostgresHistoricalIngestionStore", create_store)
    monkeypatch.setattr(backfill_games, "HistoricalIngestionService", RecordingService)
    return CompositionFixture(engine, events, transport, source, store)


async def test_run_composes_exact_resources_after_logging_and_disposes_engine_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _result()
    fixture = _install_composition(monkeypatch, result=expected)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=AnyUrl("postgresql+asyncpg://user:password@database:5432/nba"),
        nba_api_max_concurrency=3,
        nba_api_resource_lease_seconds=42,
    )
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    actual = await backfill_games.run(request, settings)

    assert actual is expected
    assert fixture.events == [
        ("logging", settings),
        ("engine", str(settings.database_url), True),
        ("transport", settings),
        ("source", fixture.transport),
        ("store", fixture.engine, 42),
        ("service", fixture.source, fixture.store, 3),
        ("backfill", request),
        "dispose",
    ]
    assert fixture.engine.dispose_calls == 1


async def test_run_disposes_engine_once_and_preserves_ordinary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("service failed")
    fixture = _install_composition(monkeypatch, error=error)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    with pytest.raises(RuntimeError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is error
    assert fixture.events[-1] == "dispose"
    assert fixture.engine.dispose_calls == 1


async def test_run_disposes_engine_once_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = asyncio.CancelledError("cancelled")
    fixture = _install_composition(monkeypatch, error=error)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    with pytest.raises(asyncio.CancelledError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is error
    assert fixture.events[-1] == "dispose"
    assert fixture.engine.dispose_calls == 1


async def test_run_preserves_service_exception_when_disposal_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_error = RuntimeError("service failed")
    disposal_error = ValueError("disposal failed")
    fixture = _install_composition(
        monkeypatch,
        error=service_error,
        dispose_error=disposal_error,
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    with pytest.raises(RuntimeError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is service_error
    assert fixture.engine.dispose_calls == 1


async def test_run_preserves_cancellation_when_disposal_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError("cancelled")
    disposal_error = ValueError("disposal failed")
    fixture = _install_composition(
        monkeypatch,
        error=cancellation,
        dispose_error=disposal_error,
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    with pytest.raises(asyncio.CancelledError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is cancellation
    assert fixture.engine.dispose_calls == 1


async def test_run_propagates_disposal_failure_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposal_error = RuntimeError("disposal failed")
    fixture = _install_composition(
        monkeypatch,
        result=_result(),
        dispose_error=disposal_error,
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))

    with pytest.raises(RuntimeError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is disposal_error
    assert fixture.engine.dispose_calls == 1


async def test_suppressed_disposal_failure_logs_only_safe_classification(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql+asyncpg://secret-user:secret-password@database/nba"
    service_error = RuntimeError("service failed")
    fixture = _install_composition(
        monkeypatch,
        error=service_error,
        dispose_error=RuntimeError(f"could not close {secret}"),
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    request = BackfillRequest(seasons=(SeasonId("2025-26"),))
    caplog.set_level(logging.ERROR, logger=backfill_games.__name__)

    with pytest.raises(RuntimeError) as captured:
        await backfill_games.run(request, settings)

    assert captured.value is service_error
    assert fixture.engine.dispose_calls == 1
    cleanup_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "historical_ingestion_engine_dispose_failed"
    ]
    assert len(cleanup_records) == 1
    assert cleanup_records[0].getMessage() == "historical_ingestion_engine_dispose_failed"
    assert cleanup_records[0].error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert cleanup_records[0].exc_info is None
    assert secret not in caplog.text
    assert "secret-password" not in caplog.text


def test_unexpected_run_command_errors_are_not_mapped_to_an_exit_code() -> None:
    error = RuntimeError("programming error")

    async def run_command(request: BackfillRequest, settings: Settings) -> BackfillResult:
        raise error

    with pytest.raises(RuntimeError) as captured:
        backfill_games.main(["--season", "2025-26"], run_command=run_command)

    assert captured.value is error
