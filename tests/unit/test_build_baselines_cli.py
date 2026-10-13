import subprocess
import sys
from pathlib import Path

import pytest

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineArtifactStats,
    BuildBaselinesRequest,
    BuildBaselinesResult,
)
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.settings import Settings
from scripts.build_baselines import main, parse_args


def test_baseline_cli_selectors_parameters_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--season", "2025-26", "--output", str(tmp_path / "out")]
    request = parse_args(args)
    assert request.config == BaselineConfig() and request.output_path.is_absolute()

    async def partial(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult:
        return BuildBaselinesResult(
            request.output_path, BaselineArtifactStats(1, 1, 3, 2, 0, 1, "a" * 64, "b" * 64)
        )

    async def failed(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult:
        raise BaselineExportError("baseline_source_stale")

    assert main(args, run_command=partial) == 1
    assert "artifact_sha256=" in capsys.readouterr().out
    assert main(args, run_command=failed) == 2
    assert "baseline_source_stale" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--history-window", "7"],
        ["--ppp-prior-possessions", "0"],
        ["--season-from", "2024-25"],
        ["--rebuild"],
        ["--game-id", "0022400001"],
    ],
)
def test_invalid_arguments_reject(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--season", "2025-26", "--output", str(tmp_path / "out"), *extra])


def test_secret_error_text_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failed(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult:
        raise ValueError("postgresql://secret:password@example.org")

    assert main(["--season", "2025-26", "--output", str(tmp_path / "out")], run_command=failed) == 2
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "phase,count,expected", [("after", 2, 1), ("before", 2, 130), ("after", 1, 1)]
)
def test_actual_sigints_preserve_publication_status_and_join_worker(
    tmp_path: Path, phase: str, count: int, expected: int
) -> None:
    path = tmp_path / "out.jsonl"
    path.write_bytes(b"old")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.unit.baseline_interrupt_probe", str(path), phase, str(count)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == expected, completed.stdout + completed.stderr
    assert f"exit={expected} joined=True" in completed.stdout
    assert "handler_restored=True" in completed.stdout
    assert not list(tmp_path.glob(".baseline-*.tmp"))
    if phase == "after":
        assert "status=partial" in completed.stdout and "artifact_sha256=" in completed.stdout
        assert path.read_bytes().startswith(b'{"')
    else:
        assert "artifact_sha256=" not in completed.stdout
        assert path.read_bytes() == b"old"
