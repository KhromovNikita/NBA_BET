import asyncio
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    PredictModelsRequest,
    PredictModelsResult,
    TrainModelsRequest,
    TrainModelsResult,
)
from nba_live_totals.settings import Settings
from scripts import build_training_dataset, predict_models, train_models


def test_training_cli_is_database_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "invalid-not-a-db-url")
    assert train_models.main(["--help"]) == 0


def test_modeling_selectors_defaults_and_decimal_bounds(tmp_path: Path) -> None:
    request = build_training_dataset.parse_args(
        [
            "--season-from",
            "2024-25",
            "--season-to",
            "2025-26",
            "--validation-from",
            "2025-11-01",
            "--test-from",
            "2025-12-01",
            "--output",
            str(tmp_path / "training"),
        ]
    )
    assert len(request.seasons) == 2
    assert request.split.validation_from == date(2025, 11, 1)
    assert request.split.embargo_days == 1
    training = train_models.parse_args(
        [
            "--dataset",
            str(tmp_path / "training"),
            "--output",
            str(tmp_path / "m.nbamodel"),
            "--iterations",
            "12",
            "--early-stopping-rounds",
            "3",
            "--learning-rate",
            "0.0300",
        ]
    )
    assert training.config.learning_rate == Decimal("0.03")
    assert training.config.iterations == 12
    prediction = predict_models.parse_args(
        [
            "--season",
            "2025-26",
            "--game-id",
            "0022500001",
            "--model",
            str(tmp_path / "m.nbamodel"),
            "--output",
            str(tmp_path / "predictions"),
        ]
    )
    assert prediction.game_id is not None and prediction.model_path.is_absolute()


@pytest.mark.parametrize(
    "extra",
    [
        ["--learning-rate", "NaN"],
        ["--iterations", "10"],
        ["--depth", "11"],
        ["--random-seed", "-1"],
    ],
)
def test_invalid_training_flags_reject(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        train_models.parse_args(
            ["--dataset", str(tmp_path / "data"), "--output", str(tmp_path / "m.nbamodel"), *extra]
        )


def test_dataset_has_no_target_game_or_materialization_flags(tmp_path: Path) -> None:
    base = [
        "--season",
        "2025-26",
        "--validation-from",
        "2025-11-01",
        "--test-from",
        "2025-12-01",
        "--output",
        str(tmp_path / "out"),
    ]
    for extra in (
        ["--game-id", "0022500001"],
        ["--refresh"],
        ["--embargo-days", "0"],
        ["--season-to", "2026-27"],
    ):
        with pytest.raises(SystemExit):
            build_training_dataset.parse_args([*base, *extra])


def test_safe_errors_quality_summary_and_missing_extra(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def partial(request: PredictModelsRequest) -> PredictModelsResult:
        return PredictModelsResult(request.output_path, 1, 2, 1, 1, "a" * 64)

    args = [
        "--season",
        "2025-26",
        "--model",
        str(tmp_path / "m.nbamodel"),
        "--output",
        str(tmp_path / "out"),
    ]
    assert predict_models.main(args, run_command=partial) == 1
    assert "artifact_sha256=" in capsys.readouterr().out

    async def missing(request: TrainModelsRequest) -> TrainModelsResult:
        raise ModelingError("ml_dependencies_missing")

    training_args = ["--dataset", str(tmp_path / "data"), "--output", str(tmp_path / "m.nbamodel")]
    assert train_models.main(training_args, run_command=missing) == 2
    assert "uv sync --extra ml" in capsys.readouterr().err

    async def secret(request: TrainModelsRequest) -> TrainModelsResult:
        raise RuntimeError("postgresql://private:password@host/db")

    assert train_models.main(training_args, run_command=secret) == 2
    assert "private" not in capsys.readouterr().err


@pytest.mark.parametrize("phase,expected", [("before", 130), ("after", 1)])
def test_actual_modeling_sigints_preserve_commit_and_restore_handler(
    tmp_path: Path, phase: str, expected: int
) -> None:
    path = tmp_path / "training.jsonl"
    path.write_bytes(b"old")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.unit.modeling_interrupt_probe", str(path), phase],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == expected, completed.stdout + completed.stderr
    assert "joined=True" in completed.stdout and "handler_restored=True" in completed.stdout
    if phase == "before":
        assert path.read_bytes() == b"old"
    else:
        assert path.read_bytes().startswith(b'{"') and "artifact_sha256=" in completed.stdout


async def test_source_composition_checks_four_schemas_without_writes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, client, work = AsyncMock(), AsyncMock(), Mock()
    validate = AsyncMock()
    monkeypatch.setattr(build_training_dataset, "Settings", Settings.model_construct)
    monkeypatch.setattr(build_training_dataset, "configure_logging", lambda settings: None)
    monkeypatch.setattr(
        build_training_dataset, "create_async_engine", lambda *args, **kwargs: engine
    )
    monkeypatch.setattr(build_training_dataset, "ClickHouseClient", lambda **kwargs: client)
    monkeypatch.setattr(
        build_training_dataset, "PostgresFeatureWorkStore", lambda *args, **kwargs: work
    )
    monkeypatch.setattr(build_training_dataset, "validate_schema", validate)

    async def operation(source: object) -> str:
        return "checked"

    assert await build_training_dataset.run_source_work(operation) == "checked"
    assert validate.await_count == 4
    work.start_run.assert_not_called()
    work.claim_game.assert_not_called()
    client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_source_client_cleanup_cancellation_keeps_committed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, client = AsyncMock(), AsyncMock()
    client.aclose.side_effect = asyncio.CancelledError
    monkeypatch.setattr(build_training_dataset, "Settings", Settings.model_construct)
    monkeypatch.setattr(build_training_dataset, "configure_logging", lambda settings: None)
    monkeypatch.setattr(
        build_training_dataset, "create_async_engine", lambda *args, **kwargs: engine
    )
    monkeypatch.setattr(build_training_dataset, "ClickHouseClient", lambda **kwargs: client)
    monkeypatch.setattr(
        build_training_dataset, "PostgresFeatureWorkStore", lambda *args, **kwargs: Mock()
    )
    monkeypatch.setattr(build_training_dataset, "validate_schema", AsyncMock())

    async def operation(source: object) -> str:
        return "committed"

    assert await build_training_dataset.run_source_work(operation) == "committed"
    engine.dispose.assert_awaited_once()
