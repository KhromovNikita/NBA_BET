from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

import pytest
from pydantic import AnyUrl, HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.infrastructure.clickhouse.client import (
    ClickHouseClient,
    ClickHouseError,
)
from nba_live_totals.infrastructure.clickhouse.migrations import ClickHouseMigrationError
from nba_live_totals.settings import Settings
from scripts import migrate_clickhouse


def test_main_prints_one_safe_summary_after_success(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[Settings] = []

    async def run_command(settings: Settings) -> None:
        calls.append(settings)

    code = migrate_clickhouse.main([], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "clickhouse_migrations=applied\n"
    assert captured.err == ""
    assert len(calls) == 1


def test_main_rejects_arguments_without_running_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    async def run_command(settings: Settings) -> None:
        nonlocal called
        called = True

    code = migrate_clickhouse.main(["--downgrade"], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 2
    assert called is False
    assert captured.out == ""
    assert "usage:" in captured.err


def test_main_returns_two_for_invalid_configuration_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLICKHOUSE_OPERATION_TIMEOUT_SECONDS", "not-a-number-secret")

    async def run_command(settings: Settings) -> None:
        raise AssertionError("must not run")

    code = migrate_clickhouse.main([], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "invalid configuration\n"
    assert "not-a-number-secret" not in captured.err


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (ClickHouseError("clickhouse_unavailable"), "clickhouse_unavailable"),
        (ClickHouseMigrationError("clickhouse_schema_drift"), "clickhouse_schema_drift"),
    ],
)
def test_main_maps_only_safe_operational_errors(
    error: RuntimeError,
    expected_code: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_command(settings: Settings) -> None:
        raise error

    code = migrate_clickhouse.main([], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"clickhouse_migration_failed code={expected_code}\n"


def test_main_returns_standard_interrupt_status_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_command(settings: Settings) -> None:
        return None

    def interrupt(coroutine: object) -> NoReturn:
        cast(ClosableCoroutine, coroutine).close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt)

    code = migrate_clickhouse.main([], run_command=run_command)

    captured = capsys.readouterr()
    assert code == 130
    assert captured.out == ""
    assert captured.err == ""


def test_unexpected_command_errors_are_not_mapped_to_an_exit_code() -> None:
    error = RuntimeError("programming error")

    async def run_command(settings: Settings) -> None:
        raise error

    with pytest.raises(RuntimeError) as captured:
        migrate_clickhouse.main([], run_command=run_command)

    assert captured.value is error


class ClosableCoroutine(Protocol):
    def close(self) -> None: ...


@dataclass
class RecordingEngine:
    events: list[object]
    dispose_error: BaseException | None = None

    async def dispose(self) -> None:
        self.events.append("dispose_engine")
        if self.dispose_error is not None:
            raise self.dispose_error


@dataclass
class RecordingClient:
    events: list[object]
    close_error: BaseException | None = None

    async def aclose(self) -> None:
        self.events.append("close_clickhouse")
        if self.close_error is not None:
            raise self.close_error


async def test_run_uses_operation_timeout_and_closes_clickhouse_then_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = RecordingEngine(events)
    client = RecordingClient(events)

    def configure_logging(settings: Settings) -> None:
        events.append(("logging", settings))

    def create_engine(url: str, *, pool_pre_ping: bool) -> AsyncEngine:
        events.append(("engine", url, pool_pre_ping))
        return cast(AsyncEngine, engine)

    def create_client(**options: object) -> ClickHouseClient:
        events.append(("clickhouse", options))
        return cast(ClickHouseClient, client)

    async def migrate(
        postgres: AsyncEngine,
        clickhouse: ClickHouseClient,
    ) -> None:
        events.append(("migrate", postgres, clickhouse))

    monkeypatch.setattr(migrate_clickhouse, "configure_logging", configure_logging)
    monkeypatch.setattr(migrate_clickhouse, "create_async_engine", create_engine)
    monkeypatch.setattr(migrate_clickhouse, "ClickHouseClient", create_client)
    monkeypatch.setattr(migrate_clickhouse, "migrate_clickhouse", migrate)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=AnyUrl("postgresql+asyncpg://user:password@database:5432/nba"),
        clickhouse_url=HttpUrl("http://clickhouse:8123"),
        clickhouse_user="nba",
        clickhouse_password=SecretStr("secret"),
        clickhouse_operation_timeout_seconds=41.5,
        healthcheck_timeout_seconds=1.25,
    )

    await migrate_clickhouse.run(settings)

    assert events == [
        ("logging", settings),
        ("engine", str(settings.database_url), True),
        (
            "clickhouse",
            {
                "base_url": "http://clickhouse:8123",
                "user": "nba",
                "password": "secret",
                "timeout_seconds": 41.5,
            },
        ),
        ("migrate", engine, client),
        "close_clickhouse",
        "dispose_engine",
    ]


async def test_run_disposes_postgres_if_clickhouse_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = RecordingEngine(events)
    construction_error = RuntimeError("clickhouse construction failed")

    monkeypatch.setattr(
        migrate_clickhouse,
        "create_async_engine",
        lambda *args, **kwargs: cast(AsyncEngine, engine),
    )

    def fail_client(**options: object) -> NoReturn:
        raise construction_error

    monkeypatch.setattr(migrate_clickhouse, "ClickHouseClient", fail_client)

    with pytest.raises(RuntimeError) as captured:
        await migrate_clickhouse.run(Settings(_env_file=None))  # type: ignore[call-arg]

    assert captured.value is construction_error
    assert events == ["dispose_engine"]


@pytest.mark.parametrize("primary_error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_run_preserves_primary_error_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    primary_error: BaseException,
) -> None:
    events: list[object] = []
    database_secret = "postgresql+asyncpg://database-user:database-password@database/nba"
    clickhouse_secret = "http://clickhouse-user:clickhouse-password@clickhouse:8123"
    engine = RecordingEngine(events, dispose_error=RuntimeError(database_secret))
    client = RecordingClient(events, close_error=RuntimeError(clickhouse_secret))

    monkeypatch.setattr(
        migrate_clickhouse,
        "create_async_engine",
        lambda *args, **kwargs: cast(AsyncEngine, engine),
    )
    monkeypatch.setattr(
        migrate_clickhouse,
        "ClickHouseClient",
        lambda **kwargs: cast(ClickHouseClient, client),
    )

    async def fail_migration(engine: AsyncEngine, client: ClickHouseClient) -> None:
        raise primary_error

    monkeypatch.setattr(migrate_clickhouse, "migrate_clickhouse", fail_migration)

    with pytest.raises(BaseException) as captured:
        await migrate_clickhouse.run(Settings(_env_file=None))  # type: ignore[call-arg]

    assert captured.value is primary_error
    assert events == ["close_clickhouse", "dispose_engine"]
    formatted = "".join(traceback.format_exception(captured.value))
    assert database_secret not in formatted
    assert clickhouse_secret not in formatted


@pytest.mark.parametrize("cleanup_resource", ["clickhouse", "postgres"])
async def test_run_maps_cleanup_only_failures_to_safe_error_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_resource: str,
) -> None:
    events: list[object] = []
    secret = (
        "postgresql+asyncpg://database-user:database-password@database/nba; "
        "SELECT database_secret_sql"
    )
    engine = RecordingEngine(events, dispose_error=RuntimeError(secret))
    client = RecordingClient(events, close_error=RuntimeError(secret))

    monkeypatch.setattr(
        migrate_clickhouse,
        "create_async_engine",
        lambda *args, **kwargs: cast(AsyncEngine, engine),
    )
    monkeypatch.setattr(
        migrate_clickhouse,
        "ClickHouseClient",
        lambda **kwargs: cast(ClickHouseClient, client),
    )

    async def succeed_migration(engine: AsyncEngine, client: ClickHouseClient) -> None:
        return None

    monkeypatch.setattr(migrate_clickhouse, "migrate_clickhouse", succeed_migration)

    if cleanup_resource == "clickhouse":
        engine.dispose_error = None
    else:
        client.close_error = None

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrate_clickhouse.run(Settings(_env_file=None))  # type: ignore[call-arg]

    assert captured.value.code == "clickhouse_migration_cleanup_failed"
    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in formatted
    assert "database-password" not in formatted
    assert "database_secret_sql" not in formatted
