from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import AnyUrl

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    BuildDatasetResult,
    FeatureBuildMode,
    FeatureBuildStatus,
)
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.settings import Settings
from scripts import build_dataset

RUN_ID = UUID("12345678-1234-5678-9234-567812345678")


def result(*, invalid: int = 0, failed: int = 0, busy: int = 0) -> BuildDatasetResult:
    materialized = 0 if failed or busy else 1
    status = (
        FeatureBuildStatus.SUCCEEDED
        if not invalid and not failed and not busy
        else FeatureBuildStatus.PARTIAL
        if materialized
        else FeatureBuildStatus.FAILED
    )
    return BuildDatasetResult(
        RUN_ID, status, materialized + failed + busy, materialized, invalid, 0, 0, failed, busy
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--season", "2025-26"], BuildDatasetRequest((SeasonId("2025-26"),))),
        (
            ["--season", "2025-26", "--game-id", "0022500123"],
            BuildDatasetRequest((SeasonId("2025-26"),), game_id=GameId("0022500123")),
        ),
        (
            ["--season", "2025-26", "--rebuild"],
            BuildDatasetRequest((SeasonId("2025-26"),), rebuild=True),
        ),
        (
            ["--season", "2025-26", "--verify"],
            BuildDatasetRequest((SeasonId("2025-26"),), mode=FeatureBuildMode.VERIFY),
        ),
        (
            ["--season-from", "2023-24", "--season-to", "2025-26"],
            BuildDatasetRequest.for_range(SeasonId("2023-24"), SeasonId("2025-26")),
        ),
    ],
)
def test_parse_args(argv: list[str], expected: BuildDatasetRequest) -> None:
    assert build_dataset.parse_args(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--season-from", "2024-25"],
        ["--season", "2025-26", "--rebuild", "--verify"],
        ["--season-from", "2024-25", "--season-to", "2025-26", "--game-id", "0022400001"],
        ["--season", "25-26"],
    ],
)
def test_parse_args_rejects_invalid_combinations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        build_dataset.parse_args(argv)
    assert captured.value.code == 2


@pytest.mark.parametrize(
    ("value", "code"),
    [(result(), 0), (result(invalid=1), 1), (result(failed=1), 1), (result(busy=1), 1)],
)
def test_main_summary_order_and_exit_code(
    value: BuildDatasetResult, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    async def command(request: BuildDatasetRequest, settings: Settings) -> BuildDatasetResult:
        return value

    assert build_dataset.main(["--season", "2025-26"], run_command=command) == code
    assert capsys.readouterr().out == (
        f"run={RUN_ID} status={value.status.value} discovered={value.discovered} "
        f"materialized={value.materialized} invalid={value.invalid} "
        f"skipped_current={value.skipped_current} verified={value.verified} "
        f"failed={value.failed} busy={value.busy}\n"
    )


def test_main_returns_130_for_ctrl_c(capsys: pytest.CaptureFixture[str]) -> None:
    async def command(request: BuildDatasetRequest, settings: Settings) -> BuildDatasetResult:
        raise KeyboardInterrupt

    assert build_dataset.main(["--season", "2025-26"], run_command=command) == 130
    assert capsys.readouterr().out == ""


class Engine:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def dispose(self) -> None:
        self.events.append("dispose")


class Client:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def aclose(self) -> None:
        self.events.append("close")


@pytest.mark.asyncio
async def test_run_validates_four_schemas_composes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    engine = Engine(events)
    client = Client(events)
    monkeypatch.setattr(build_dataset, "configure_logging", lambda settings: events.append("log"))
    monkeypatch.setattr(build_dataset, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(build_dataset, "ClickHouseClient", lambda **kwargs: client)

    async def validate(engine_arg: object, client_arg: object, *, schema_id: str) -> None:
        assert engine_arg is engine and client_arg is client
        events.append(schema_id)

    monkeypatch.setattr(build_dataset, "validate_schema", validate)
    for name in (
        "PostgresFeatureWorkStore",
        "NBAPlayByPlayV3FeatureEventNormalizer",
        "ClickHouseFeatureSourceReader",
        "DeterministicFeatureCalculator",
        "ClickHouseFeatureSnapshotStore",
    ):

        def build_adapter(*args: object, _name: str = name, **kwargs: object) -> object:
            events.append(_name)
            return object()

        monkeypatch.setattr(build_dataset, name, build_adapter)

    class Service:
        def __init__(self, **kwargs: object) -> None:
            events.append(("service", tuple(sorted(kwargs))))

        async def build(self, request: BuildDatasetRequest) -> BuildDatasetResult:
            events.append("build")
            return result()

    monkeypatch.setattr(build_dataset, "FeatureBuildService", Service)
    settings = Settings(_env_file=None).model_copy(  # type: ignore[call-arg]
        update={"database_url": AnyUrl("postgresql+asyncpg://nba:nba@localhost:5432/nba")}
    )
    request = BuildDatasetRequest((SeasonId("2025-26"),))

    assert await build_dataset.run(request, settings) == result()
    assert events[1:5] == [
        "clickhouse-possession-schema-v1",
        "clickhouse-lineup-segments-schema-v1",
        "clickhouse-game-state-features-schema-v1",
        "clickhouse-player-pregame-features-schema-v1",
    ]
    assert events[-2:] == ["close", "dispose"]


def test_main_never_prints_unsafe_configuration_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        build_dataset,
        "Settings",
        lambda: Settings(_env_file=None).model_copy(  # type: ignore[call-arg]
            update={"database_url": AnyUrl("http://user:secret@invalid.example/nba")}
        ),
    )
    assert build_dataset.main(["--season", "2025-26"], run_command=build_dataset.run) == 2
    captured = capsys.readouterr()
    assert "secret" not in captured.err
    assert captured.err == "invalid dataset build configuration\n"
