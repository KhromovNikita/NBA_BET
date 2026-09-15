from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from pydantic import AnyUrl, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    BuildPossessionsResult,
    BuildStatus,
)
from nba_live_totals.domain import GameId, PossessionVersions, SeasonId
from nba_live_totals.infrastructure.clickhouse import migrations as migrations_module
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient, ClickHouseError
from nba_live_totals.infrastructure.clickhouse.migrations import (
    ClickHouseMigrationError,
)
from nba_live_totals.settings import Settings
from scripts import build_possessions

RUN_ID = UUID("12345678-1234-5678-9234-567812345678")


def _result(
    *,
    status: BuildStatus = BuildStatus.SUCCEEDED,
    materialized: int = 1,
    skipped_current: int = 0,
    verified: int = 0,
    failed: int = 0,
    busy: int = 0,
) -> BuildPossessionsResult:
    return BuildPossessionsResult(
        run_id=RUN_ID,
        status=status,
        discovered=materialized + skipped_current + verified + failed + busy,
        materialized=materialized,
        skipped_current=skipped_current,
        verified=verified,
        failed=failed,
        busy=busy,
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--season", "2025-26"],
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)),
        ),
        (
            ["--season", "2025-26", "--game-id", "0022500123"],
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), game_id=GameId("0022500123")),
        ),
        (
            ["--season", "2025-26", "--rebuild"],
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), rebuild=True),
        ),
        (
            ["--season", "2025-26", "--verify"],
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),), mode=BuildMode.VERIFY),
        ),
        (
            ["--season-from", "2023-24", "--season-to", "2025-26"],
            BuildPossessionsRequest(
                seasons=(
                    SeasonId("2023-24"),
                    SeasonId("2024-25"),
                    SeasonId("2025-26"),
                )
            ),
        ),
    ],
)
def test_parse_args_composes_exact_request(
    argv: list[str], expected: BuildPossessionsRequest
) -> None:
    assert build_possessions.parse_args(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--season", "2025-26", "--season-from", "2024-25", "--season-to", "2025-26"],
        ["--season-from", "2024-25"],
        ["--season-to", "2025-26"],
        ["--season", "2025-26", "--season-to", "2026-27"],
        ["--season-from", "2025-26", "--season-to", "2024-25"],
        ["--season-from", "2024-25", "--season-to", "2025-26", "--game-id", "0022400001"],
        ["--season", "2025-26", "--game-id", "0022400001"],
        ["--season", "2025-26", "--rebuild", "--verify"],
        ["--season", "2025-26", "--refresh"],
        ["--season", "25-26"],
        ["--season", "2025-26", "--game-id", "bad"],
    ],
)
def test_parse_args_rejects_invalid_selector_or_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        build_possessions.parse_args(argv)

    assert captured.value.code == 2


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (_result(), 0),
        (_result(materialized=0, skipped_current=1), 0),
        (_result(materialized=0, verified=1), 0),
        (
            _result(
                status=BuildStatus.PARTIAL,
                materialized=1,
                failed=1,
            ),
            1,
        ),
        (_result(status=BuildStatus.FAILED, materialized=0, failed=1), 1),
        (
            _result(
                status=BuildStatus.PARTIAL,
                materialized=1,
                busy=1,
            ),
            1,
        ),
        (_result(status=BuildStatus.FAILED, materialized=0, failed=1), 1),
    ],
)
def test_main_prints_exact_summary_and_returns_counter_based_exit_code(
    result: BuildPossessionsResult,
    expected_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run_command(
        request: BuildPossessionsRequest, settings: Settings
    ) -> BuildPossessionsResult:
        assert request.seasons == (SeasonId("2025-26"),)
        assert isinstance(settings, Settings)
        return result

    code = build_possessions.main(["--season", "2025-26"], run_command=run_command)

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.out == (
        f"run={RUN_ID} status={result.status.value} discovered={result.discovered} "
        f"materialized={result.materialized} skipped_current={result.skipped_current} "
        f"verified={result.verified} failed={result.failed} busy={result.busy}\n"
    )
    assert captured.err == ""


def test_main_returns_130_without_summary_on_ctrl_c(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def interrupt(
        request: BuildPossessionsRequest, settings: Settings
    ) -> BuildPossessionsResult:
        raise KeyboardInterrupt

    assert build_possessions.main(["--season", "2025-26"], run_command=interrupt) == 130
    assert capsys.readouterr().out == ""


def test_main_returns_2_without_running_for_invalid_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error: ValidationError
    try:
        Settings(possession_build_max_concurrency=0)
    except ValidationError as caught:
        error = caught

    invoked = False

    async def run_command(
        request: BuildPossessionsRequest, settings: Settings
    ) -> BuildPossessionsResult:
        nonlocal invoked
        invoked = True
        return _result()

    def invalid_settings() -> Settings:
        raise error

    monkeypatch.setattr(build_possessions, "Settings", invalid_settings)

    assert build_possessions.main(["--season", "2025-26"], run_command=run_command) == 2
    captured = capsys.readouterr()
    assert not invoked
    assert captured.out == ""
    assert captured.err == "invalid configuration\n"


def test_main_rejects_non_async_postgres_url_before_constructing_runtime_adapters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    settings = _settings().model_copy(
        update={"database_url": AnyUrl("http://nba:secret@invalid.example/nba")}
    )

    monkeypatch.setattr(build_possessions, "Settings", lambda: settings)
    monkeypatch.setattr(
        build_possessions,
        "configure_logging",
        lambda configured: events.append(("configure_logging", configured)),
    )

    def forbidden_engine(*args: object, **kwargs: object) -> object:
        raise AssertionError("engine construction must follow URL validation")

    monkeypatch.setattr(build_possessions, "create_async_engine", forbidden_engine)

    code = build_possessions.main(["--season", "2025-26"], run_command=build_possessions.run)

    captured = capsys.readouterr()
    assert code == 2
    assert events == [("configure_logging", settings)]
    assert captured.out == ""
    assert captured.err == "invalid possession build configuration\n"
    assert "secret" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        ClickHouseMigrationError("clickhouse_schema_missing"),
        ClickHouseMigrationError("clickhouse_schema_drift"),
        ClickHouseMigrationError("clickhouse_migration_set_invalid"),
        ValueError("unsafe constructor details password=secret"),
    ],
)
def test_main_classifies_schema_and_composition_validation_as_exit_2_without_unsafe_text(
    error: Exception,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(request: BuildPossessionsRequest, settings: Settings) -> BuildPossessionsResult:
        raise error

    assert build_possessions.main(["--season", "2025-26"], run_command=fail) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid possession build configuration\n"
    assert "secret" not in captured.err


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (
            build_possessions.PossessionBuildCLIError("possession_build_cleanup_failed"),
            "possession_build_failed code=possession_build_cleanup_failed\n",
        ),
        (
            ClickHouseError("clickhouse_timeout"),
            "possession_build_failed code=clickhouse_timeout\n",
        ),
        (
            SQLAlchemyError("postgresql://user:secret@host/nba"),
            "possession_build_failed code=possession_build_persistence_error\n",
        ),
    ],
)
def test_main_classifies_operational_failures_as_exit_1_with_bounded_codes(
    error: BaseException,
    expected_error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(request: BuildPossessionsRequest, settings: Settings) -> BuildPossessionsResult:
        raise error

    assert build_possessions.main(["--season", "2025-26"], run_command=fail) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_error
    assert "secret" not in captured.err


def test_main_does_not_reclassify_programming_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = RuntimeError("programming error")

    async def fail(request: BuildPossessionsRequest, settings: Settings) -> BuildPossessionsResult:
        raise primary

    with pytest.raises(RuntimeError) as captured:
        build_possessions.main(["--season", "2025-26"], run_command=fail)

    assert captured.value is primary
    assert capsys.readouterr().out == ""


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database_url": "postgresql+asyncpg://nba:secret@localhost:55432/nba",
            "clickhouse_url": "http://localhost:8123/prefix/",
            "clickhouse_user": "analytical",
            "clickhouse_password": "ch-secret",
            "clickhouse_operation_timeout_seconds": 17.25,
            "possession_resource_lease_seconds": 713,
            "possession_build_max_concurrency": 7,
            "possession_normalizer_version": "normalizer-exact",
            "possession_parser_version": "parser-exact",
            "possession_rules_version": "rules-exact",
            "possession_output_schema_version": "schema-exact",
        }
    )


@pytest.mark.asyncio
async def test_run_composes_exact_adapters_versions_and_dedicated_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = _Engine(events)
    operation_http = _OperationHttpClient(events)
    request = BuildPossessionsRequest(seasons=(SeasonId("2025-26"),))
    expected_result = _result()

    def configure(settings: Settings) -> None:
        events.append(("configure_logging", settings))

    def create_engine(url: str, *, pool_pre_ping: bool) -> _Engine:
        events.append(("create_engine", url, pool_pre_ping))
        return engine

    def create_http_client(**kwargs: object) -> _OperationHttpClient:
        events.append(("create_http_client", kwargs))
        return operation_http

    async def validate_schema(created_engine: object, client: object, *, schema_id: str) -> None:
        events.append(("validate_schema", created_engine, client, schema_id))

    class WorkStore:
        def __init__(self, created_engine: object, *, lease_seconds: int) -> None:
            events.append(("work_store", created_engine, lease_seconds))

    class Normalizer:
        def __init__(self) -> None:
            events.append("normalizer")

    class Parser:
        def __init__(self) -> None:
            events.append("parser")

    class SnapshotStore:
        def __init__(self, client: object) -> None:
            events.append(("snapshot_store", client))

    class Service:
        def __init__(self, **kwargs: object) -> None:
            events.append(("service", kwargs))

        async def build(self, selected: BuildPossessionsRequest) -> BuildPossessionsResult:
            events.append(("build", selected))
            return expected_result

    monkeypatch.setattr(build_possessions, "configure_logging", configure)
    monkeypatch.setattr(build_possessions, "create_async_engine", create_engine)
    monkeypatch.setattr(
        "nba_live_totals.infrastructure.clickhouse.client.httpx.AsyncClient",
        create_http_client,
    )
    monkeypatch.setattr(build_possessions, "validate_schema", validate_schema)
    monkeypatch.setattr(build_possessions, "PostgresPossessionWorkStore", WorkStore)
    monkeypatch.setattr(build_possessions, "NBAPlayByPlayV3PossessionNormalizer", Normalizer)
    monkeypatch.setattr(build_possessions, "AnalyticalPossessionParser", Parser)
    monkeypatch.setattr(build_possessions, "PossessionSnapshotStore", SnapshotStore)
    monkeypatch.setattr(build_possessions, "PossessionBuildService", Service)

    result = await build_possessions.run(request, _settings())

    assert result is expected_result
    assert events[0][0] == "configure_logging"  # type: ignore[index]
    assert events[1] == (
        "create_engine",
        "postgresql+asyncpg://nba:secret@localhost:55432/nba",
        True,
    )
    http_options = events[2][1]  # type: ignore[index]
    assert http_options == {
        "base_url": "http://localhost:8123/prefix",
        "headers": {
            "X-ClickHouse-User": "analytical",
            "X-ClickHouse-Key": "ch-secret",
        },
        "timeout": 17.25,
        "trust_env": False,
    }
    client = events[3][2]  # type: ignore[index]
    assert events[3] == (
        "validate_schema",
        engine,
        client,
        "clickhouse-possession-schema-v1",
    )
    assert events[4] == ("work_store", engine, 713)
    assert events[5:8] == ["normalizer", "parser", ("snapshot_store", client)]
    service_options = events[8][1]  # type: ignore[index]
    assert isinstance(service_options, dict)
    assert service_options["versions"] == PossessionVersions(
        normalizer_version="normalizer-exact",
        parser_version="parser-exact",
        rules_version="rules-exact",
        output_schema_version="schema-exact",
    )
    assert service_options["max_concurrency"] == 7
    assert events[9] == ("build", request)
    assert events[-2:] == ["close_clickhouse", "dispose_postgres"]


@pytest.mark.asyncio
async def test_run_schema_failure_never_constructs_or_begins_build_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = _Engine(events)

    monkeypatch.setattr(build_possessions, "configure_logging", lambda settings: None)
    monkeypatch.setattr(build_possessions, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(
        build_possessions,
        "ClickHouseClient",
        lambda **kwargs: _ClosableClickHouse(events),
    )

    async def invalid_schema(engine: object, client: object, *, schema_id: str) -> None:
        assert schema_id == "clickhouse-possession-schema-v1"
        events.append("schema_invalid")
        raise ClickHouseMigrationError("clickhouse_schema_drift")

    class ForbiddenService:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("build composition began before schema validation")

    monkeypatch.setattr(build_possessions, "validate_schema", invalid_schema)
    monkeypatch.setattr(build_possessions, "PossessionBuildService", ForbiddenService)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    assert captured.value.code == "clickhouse_schema_drift"
    assert events == ["schema_invalid", "close_clickhouse", "dispose_postgres"]


@pytest.mark.asyncio
async def test_run_disposes_postgres_when_clickhouse_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = _Engine(events)
    primary = RuntimeError("credentialed primary")
    monkeypatch.setattr(build_possessions, "configure_logging", lambda settings: None)
    monkeypatch.setattr(build_possessions, "create_async_engine", lambda *a, **k: engine)

    def fail_client(**kwargs: object) -> object:
        raise primary

    monkeypatch.setattr(build_possessions, "ClickHouseClient", fail_client)

    with pytest.raises(RuntimeError) as captured:
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    assert captured.value is primary
    assert events == ["dispose_postgres"]


@pytest.mark.asyncio
async def test_run_cleanup_only_failure_is_bounded_and_still_disposes_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = _Engine(events)
    clickhouse = _ClosableClickHouse(events, close_error=RuntimeError("secret URL"))
    await _patch_successful_run(monkeypatch, engine, clickhouse)

    with pytest.raises(build_possessions.PossessionBuildCLIError) as captured:
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    assert captured.value.code == "possession_build_cleanup_failed"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)
    assert events[-2:] == ["close_clickhouse", "dispose_postgres"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_interrupt",
    [asyncio.CancelledError("cleanup cancelled"), KeyboardInterrupt("cleanup interrupted")],
)
async def test_run_preserves_cleanup_time_interrupt_and_attempts_remaining_closers(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_interrupt: BaseException,
) -> None:
    events: list[object] = []
    engine = _Engine(events)
    clickhouse = _InterruptingClickHouse(events, cleanup_interrupt)
    await _patch_successful_run(monkeypatch, engine, clickhouse)

    with pytest.raises(type(cleanup_interrupt)) as captured:
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    assert captured.value is cleanup_interrupt
    assert events[-3:] == [
        "close_clickhouse_started",
        "close_clickhouse_interrupted",
        "dispose_postgres",
    ]


def test_main_returns_130_when_keyboard_interrupt_arrives_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []
    engine = _Engine(events)
    interrupt = KeyboardInterrupt("cleanup interrupted")
    clickhouse = _InterruptingClickHouse(events, interrupt)
    asyncio.run(_patch_successful_run(monkeypatch, engine, clickhouse))
    monkeypatch.setattr(build_possessions, "Settings", _settings)

    code = build_possessions.main(["--season", "2025-26"], run_command=build_possessions.run)

    assert code == 130
    assert events[-3:] == [
        "close_clickhouse_started",
        "close_clickhouse_interrupted",
        "dispose_postgres",
    ]
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_run_active_primary_wins_over_cleanup_interrupts_and_all_closers_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    primary = RuntimeError("primary")
    engine = _Engine(events, dispose_error=KeyboardInterrupt("postgres cleanup"))
    clickhouse = _InterruptingClickHouse(events, asyncio.CancelledError("CH cleanup"))
    await _patch_successful_run(monkeypatch, engine, clickhouse, build_error=primary)

    with pytest.raises(RuntimeError) as captured:
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    assert captured.value is primary
    assert events[-3:] == [
        "close_clickhouse_started",
        "close_clickhouse_interrupted",
        "dispose_postgres",
    ]


@pytest.mark.asyncio
async def test_run_cleanup_cannot_mask_primary_or_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for primary in (RuntimeError("primary password"), asyncio.CancelledError("cancelled")):
        events: list[object] = []
        engine = _Engine(events, dispose_error=RuntimeError("postgres DSN"))
        clickhouse = _ClosableClickHouse(events, close_error=RuntimeError("clickhouse secret"))
        await _patch_successful_run(monkeypatch, engine, clickhouse, build_error=primary)

        with pytest.raises(type(primary)) as captured:
            await build_possessions.run(
                BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
            )

        assert captured.value is primary
        assert events[-2:] == ["close_clickhouse", "dispose_postgres"]

    assert "clickhouse secret" not in caplog.text
    assert "postgres DSN" not in caplog.text
    assert "primary password" not in caplog.text


@pytest.mark.asyncio
async def test_run_cleanup_log_bounds_unsafe_exception_class_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unsafe_error_type = type("Credential" + "X" * 100, (RuntimeError,), {})
    events: list[object] = []
    primary = RuntimeError("primary")
    engine = _Engine(events)
    clickhouse = _ClosableClickHouse(events, close_error=unsafe_error_type("secret"))
    await _patch_successful_run(monkeypatch, engine, clickhouse, build_error=primary)

    with pytest.raises(RuntimeError):
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    record = next(
        item for item in caplog.records if item.message == "possession_clickhouse_cleanup_failed"
    )
    assert record.event == "possession_clickhouse_cleanup_failed"  # type: ignore[attr-defined]
    assert record.error_code == "clickhouse_cleanup_failed"  # type: ignore[attr-defined]
    assert record.error_type == "UnexpectedError"  # type: ignore[attr-defined]
    assert "resource" not in record.__dict__
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_run_cleanup_logs_use_only_allowlisted_resource_specific_fields(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[object] = []
    primary = RuntimeError("primary")
    engine = _Engine(events, dispose_error=RuntimeError("postgres secret"))
    clickhouse = _ClosableClickHouse(events, close_error=RuntimeError("clickhouse secret"))
    await _patch_successful_run(monkeypatch, engine, clickhouse, build_error=primary)

    with pytest.raises(RuntimeError):
        await build_possessions.run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),)), _settings()
        )

    cleanup_records = [
        record for record in caplog.records if record.message.endswith("_cleanup_failed")
    ]
    assert [record.message for record in cleanup_records] == [
        "possession_clickhouse_cleanup_failed",
        "possession_postgres_cleanup_failed",
    ]
    assert [record.error_code for record in cleanup_records] == [  # type: ignore[attr-defined]
        "clickhouse_cleanup_failed",
        "postgres_cleanup_failed",
    ]
    for record in cleanup_records:
        assert record.event == record.message  # type: ignore[attr-defined]
        assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
        assert "resource" not in record.__dict__
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_pinned_schema_validation_requires_exact_applied_ledger_and_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = SimpleNamespace(
        migration_id="0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        checksum_sha256="a" * 64,
        expected_schema_fingerprint_sha256="b" * 64,
        expected_schema={"database": "nba", "table": "possessions_v1"},
    )
    engine = _SchemaEngine(
        [
            {
                "migration_id": "0001_possessions",
                "checksum_sha256": "a" * 64,
                "expected_schema_fingerprint_sha256": "b" * 64,
                "state": "applied",
            }
        ]
    )
    client = object()
    calls: list[object] = []
    monkeypatch.setattr(migrations_module, "load_migrations", lambda _: (migration,))

    async def projection(
        selected_client: object, *, schema_id: str, database: str, table: str
    ) -> dict[str, object]:
        calls.append((selected_client, schema_id, database, table))
        return {"exact": True}

    monkeypatch.setattr(migrations_module, "fetch_schema_projection", projection)
    monkeypatch.setattr(migrations_module, "schema_fingerprint", lambda value: "b" * 64)

    await migrations_module.validate_schema(
        cast(AsyncEngine, engine),
        cast(ClickHouseClient, client),
        schema_id="clickhouse-possession-schema-v1",
    )

    assert len(engine.connection.statements) == 1
    assert calls == [(client, "clickhouse-possession-schema-v1", "nba", "possessions_v1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger_rows", "expected_code"),
    [
        ([], "clickhouse_migration_set_drift"),
        (
            [
                {
                    "migration_id": "0001_possessions",
                    "checksum_sha256": "0" * 64,
                    "expected_schema_fingerprint_sha256": "b" * 64,
                    "state": "applied",
                }
            ],
            "clickhouse_migration_checksum_drift",
        ),
        (
            [
                {
                    "migration_id": "0001_possessions",
                    "checksum_sha256": "a" * 64,
                    "expected_schema_fingerprint_sha256": "0" * 64,
                    "state": "applied",
                }
            ],
            "clickhouse_migration_fingerprint_drift",
        ),
        (
            [
                {
                    "migration_id": "0001_possessions",
                    "checksum_sha256": "a" * 64,
                    "expected_schema_fingerprint_sha256": "b" * 64,
                    "state": "applying",
                }
            ],
            "clickhouse_schema_not_applied",
        ),
    ],
)
async def test_pinned_schema_validation_rejects_missing_or_drifted_ledger(
    monkeypatch: pytest.MonkeyPatch,
    ledger_rows: list[dict[str, object]],
    expected_code: str,
) -> None:
    migration = SimpleNamespace(
        migration_id="0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        checksum_sha256="a" * 64,
        expected_schema_fingerprint_sha256="b" * 64,
        expected_schema={"database": "nba", "table": "possessions_v1"},
    )
    monkeypatch.setattr(migrations_module, "load_migrations", lambda _: (migration,))

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, _SchemaEngine(ledger_rows)),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
        )

    assert captured.value.code == expected_code


@pytest.mark.asyncio
async def test_pinned_schema_validation_rejects_malformed_latest_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = SimpleNamespace(
        migration_id="0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database=123,
        table="possessions_v1",
        checksum_sha256="a" * 64,
        expected_schema_fingerprint_sha256="b" * 64,
        expected_schema={"database": 123, "table": "possessions_v1"},
    )
    engine = _SchemaEngine(
        [
            {
                "migration_id": "0001_possessions",
                "checksum_sha256": "a" * 64,
                "expected_schema_fingerprint_sha256": "b" * 64,
                "state": "applied",
            }
        ]
    )
    monkeypatch.setattr(migrations_module, "load_migrations", lambda _: (migration,))

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, engine),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
        )

    assert captured.value.code == "clickhouse_migration_set_invalid"


@pytest.mark.asyncio
async def test_pinned_schema_validation_maps_ledger_sql_failure_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrations_module,
        "load_migrations",
        lambda _: (
            SimpleNamespace(
                migration_id="0001_possessions",
                schema_id="clickhouse-possession-schema-v1",
                database="nba",
                table="possessions_v1",
                checksum_sha256="a" * 64,
                expected_schema_fingerprint_sha256="b" * 64,
                expected_schema={"database": "nba", "table": "possessions_v1"},
            ),
        ),
    )
    engine = _SchemaEngine([], execute_error=SQLAlchemyError("postgresql://nba:secret@host/nba"))

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, engine),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
        )

    assert captured.value.code == "clickhouse_migration_ledger_unavailable"
    assert captured.value.__cause__ is None
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_pinned_schema_validation_maps_clickhouse_unavailable_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = SimpleNamespace(
        migration_id="0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        checksum_sha256="a" * 64,
        expected_schema_fingerprint_sha256="b" * 64,
        expected_schema={"database": "nba", "table": "possessions_v1"},
    )
    engine = _SchemaEngine(
        [
            {
                "migration_id": "0001_possessions",
                "checksum_sha256": "a" * 64,
                "expected_schema_fingerprint_sha256": "b" * 64,
                "state": "applied",
            }
        ]
    )
    monkeypatch.setattr(migrations_module, "load_migrations", lambda _: (migration,))

    async def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
        raise ClickHouseError("credentialed unavailable detail")

    monkeypatch.setattr(migrations_module, "fetch_schema_projection", unavailable)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, engine),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
        )

    assert captured.value.code == "clickhouse_schema_unavailable"
    assert captured.value.__cause__ is None
    assert "credentialed" not in str(captured.value)


@pytest.mark.asyncio
async def test_pinned_schema_validation_rejects_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = SimpleNamespace(
        migration_id="0001_possessions",
        schema_id="clickhouse-possession-schema-v1",
        database="nba",
        table="possessions_v1",
        checksum_sha256="a" * 64,
        expected_schema_fingerprint_sha256="b" * 64,
        expected_schema={"database": "nba", "table": "possessions_v1"},
    )
    engine = _SchemaEngine(
        [
            {
                "migration_id": "0001_possessions",
                "checksum_sha256": "a" * 64,
                "expected_schema_fingerprint_sha256": "b" * 64,
                "state": "applied",
            }
        ]
    )
    monkeypatch.setattr(migrations_module, "load_migrations", lambda _: (migration,))

    async def projection(*args: object, **kwargs: object) -> dict[str, object]:
        return {"drifted": True}

    monkeypatch.setattr(migrations_module, "fetch_schema_projection", projection)
    monkeypatch.setattr(migrations_module, "schema_fingerprint", lambda value: "c" * 64)

    with pytest.raises(ClickHouseMigrationError) as captured:
        await migrations_module.validate_schema(
            cast(AsyncEngine, engine),
            cast(ClickHouseClient, object()),
            schema_id="clickhouse-possession-schema-v1",
        )

    assert captured.value.code == "clickhouse_schema_drift"


class _OperationHttpClient:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.timeout = object()

    async def aclose(self) -> None:
        self._events.append("close_clickhouse")


class _Engine:
    def __init__(
        self,
        events: list[object],
        *,
        dispose_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._dispose_error = dispose_error

    async def dispose(self) -> None:
        self._events.append("dispose_postgres")
        if self._dispose_error is not None:
            raise self._dispose_error


class _ClosableClickHouse:
    def __init__(
        self,
        events: list[object],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._close_error = close_error

    async def aclose(self) -> None:
        self._events.append("close_clickhouse")
        if self._close_error is not None:
            raise self._close_error


class _InterruptingClickHouse(_ClosableClickHouse):
    def __init__(self, events: list[object], interrupt: BaseException) -> None:
        super().__init__(events)
        self._interrupt = interrupt

    async def aclose(self) -> None:
        self._events.append("close_clickhouse_started")
        await asyncio.sleep(0)
        self._events.append("close_clickhouse_interrupted")
        raise self._interrupt


async def _patch_successful_run(
    monkeypatch: pytest.MonkeyPatch,
    engine: _Engine,
    clickhouse: _ClosableClickHouse,
    *,
    build_error: BaseException | None = None,
) -> None:
    monkeypatch.setattr(build_possessions, "configure_logging", lambda settings: None)
    monkeypatch.setattr(build_possessions, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(build_possessions, "ClickHouseClient", lambda **kwargs: clickhouse)

    async def validate_schema(engine: object, client: object, *, schema_id: str) -> None:
        assert schema_id == "clickhouse-possession-schema-v1"
        return None

    class Service:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def build(self, request: BuildPossessionsRequest) -> BuildPossessionsResult:
            if build_error is not None:
                raise build_error
            return _result()

    monkeypatch.setattr(build_possessions, "validate_schema", validate_schema)
    monkeypatch.setattr(build_possessions, "PossessionBuildService", Service)


class _Mappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _SchemaConnection:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self._rows = rows
        self._execute_error = execute_error
        self.statements: list[object] = []

    async def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _Result:
        self.statements.append(statement)
        if self._execute_error is not None:
            raise self._execute_error
        if parameters is None:
            return _Result(self._rows)
        migration_id = parameters.get("migration_id")
        return _Result([row for row in self._rows if row.get("migration_id") == migration_id])


class _SchemaConnectionContext:
    def __init__(self, connection: _SchemaConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _SchemaConnection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _SchemaEngine:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        execute_error: BaseException | None = None,
    ) -> None:
        self.connection = _SchemaConnection(rows, execute_error=execute_error)

    def connect(self) -> _SchemaConnectionContext:
        return _SchemaConnectionContext(self.connection)
