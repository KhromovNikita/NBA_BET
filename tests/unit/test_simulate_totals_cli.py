"""CLI guards, exact query parsing, safe diagnostics and real interrupt boundaries."""

import asyncio
import os
import subprocess
import sys
from importlib import util
from pathlib import Path

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SimulateTotalsRequest,
    SimulateTotalsResult,
    SimulationCounters,
)
from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.simulation import SimulationConfig
from nba_live_totals.domain.simulation_hashing import simulation_config_projection
from scripts import simulate_totals


def arguments(path: Path) -> list[str]:
    return [
        "--season",
        "2025-26",
        "--model",
        str(path.parent / "m.nbamodel"),
        "--output",
        str(path),
    ]


def test_query_decimals_are_exact_scaled_and_cli_deduplicates(tmp_path: Path) -> None:
    assert simulate_totals.parse_total_line("210.5") == 210500000
    assert simulate_totals.parse_quantile_level("0.95") == 950000
    request = simulate_totals.parse_args(
        [
            *arguments(tmp_path / "out"),
            "--line",
            "210.5",
            "--line",
            "210",
            "--line",
            "210",
            "--quantile",
            "1",
            "--quantile",
            "0",
            "--quantile",
            ".5",
        ],
        environ={},
    )
    assert request.query.lines_scaled == (210000000, 210500000)
    assert request.query.quantiles_scaled == (0, 500000, 1000000)


@pytest.mark.parametrize(
    "value",
    [
        "210.25",
        "-1",
        "NaN",
        "Infinity",
        "1e1000000",
        "9223372036855",
        "1e-10000000",
        "1.00000000000000000000000000000000000001",
    ],
)
def test_line_rejects_nonhalf_nonfinite_negative_or_overflow(value: str) -> None:
    with pytest.raises(ValueError):
        simulate_totals.parse_total_line(value)


@pytest.mark.parametrize(
    "value",
    [
        "0.0000001",
        "-0.1",
        "1.000001",
        "NaN",
        "1e-999999999",
        "0.5000000000000000000000000000000000001",
    ],
)
def test_quantile_rejects_rounding_and_invalid_range(value: str) -> None:
    with pytest.raises(ValueError):
        simulate_totals.parse_quantile_level(value)


@pytest.mark.parametrize(
    "flags,env,n,batch",
    [
        ([], {}, 10000, 1024),
        ([], {"MONTE_CARLO_SIMULATIONS": "17"}, 17, 17),
        (["--simulations", "3"], {"MONTE_CARLO_SIMULATIONS": "invalid"}, 3, 3),
        (["--simulations", "8", "--batch-size", "2"], {}, 8, 2),
    ],
)
def test_count_precedence_and_derived_batch(
    tmp_path: Path, flags: list[str], env: dict[str, str], n: int, batch: int
) -> None:
    request = simulate_totals.parse_args([*arguments(tmp_path / "out"), *flags], environ=env)
    assert request.config.simulations == n and request.config.batch_size == batch


@pytest.mark.parametrize(
    "extra",
    [
        ["--state-number", "1"],
        ["--season-to", "2026-27"],
        ["--simulations", "0"],
        ["--simulations", "3", "--batch-size", "4"],
        ["--seed", "-1"],
        ["--line", "NaN"],
        ["--quantile", "1.1"],
        ["--output", "missing-parent/out"],
    ],
)
def test_invalid_flags_fail_before_runtime(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        simulate_totals.parse_args([*arguments(tmp_path / "out"), *extra], environ={})
    with pytest.raises(SystemExit):
        simulate_totals.parse_args(
            arguments(tmp_path / "out"), environ={"MONTE_CARLO_SIMULATIONS": "oops"}
        )


@pytest.mark.parametrize(
    "mutation", ["unknown", "duplicate", "space", "crlf", "missing_lf", "bool", "float", "oversize"]
)
def test_config_rejects_noncanonical_and_invalid_values(tmp_path: Path, mutation: str) -> None:
    config = simulation_config_projection(SimulationConfig())
    if mutation == "unknown":
        config["other"] = None
    elif mutation == "bool":
        config["simulations"] = True
    elif mutation == "float":
        config["simulations"] = 10000.0  # type: ignore[assignment]
    content = (
        b'{"simulations":10000.0}\n'
        if mutation == "float"
        else canonical_json_bytes(config) + b"\n"
    )
    if mutation == "duplicate":
        content = content.replace(b"{", b'{"seed":42,', 1)
    elif mutation == "space":
        content = b" " + content
    elif mutation == "crlf":
        content = content.replace(b"\n", b"\r\n")
    elif mutation == "missing_lf":
        content = content.rstrip(b"\n")
    elif mutation == "oversize":
        content = b" " * (16 * 1024 * 1024 + 1)
    path = tmp_path / "config.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        simulate_totals.load_simulation_config(path)


def test_config_ignores_env_and_forbids_kernel_flags_but_allows_queries(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = SimulationConfig(simulations=3, batch_size=2, seed=2**64 - 1)
    path.write_bytes(canonical_json_bytes(simulation_config_projection(config)) + b"\n")
    args = [*arguments(tmp_path / "out"), "--config", str(path)]
    assert simulate_totals.load_simulation_config(path) == config
    assert (
        simulate_totals.parse_args(
            [*args, "--line", "210.5"], environ={"MONTE_CARLO_SIMULATIONS": "bad"}
        ).config
        == config
    )
    for flag in (
        "--simulations",
        "--seed",
        "--batch-size",
        "--max-overtimes",
        "--max-possessions-per-horizon",
    ):
        with pytest.raises(SystemExit):
            simulate_totals.parse_args([*args, flag, "3"], environ={})


def test_model_and_config_path_aliases_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_bytes(canonical_json_bytes(simulation_config_projection(SimulationConfig())) + b"\n")
    output = tmp_path / "out"
    os.link(path, output)
    with pytest.raises(SystemExit):
        simulate_totals.parse_args([*arguments(output), "--config", str(path)], environ={})
    with pytest.raises(SystemExit):
        simulate_totals.parse_args([*arguments(path), "--model", str(output)], environ={})


def test_selectors_range_and_exact_state(tmp_path: Path) -> None:
    base = arguments(tmp_path / "out")[2:]
    request = simulate_totals.parse_args(
        [*base, "--season-from", "2024-25", "--season-to", "2025-26"], environ={}
    )
    assert len(request.seasons) == 2
    with pytest.raises(SystemExit):
        simulate_totals.parse_args(
            [
                *base,
                "--season-from",
                "2024-25",
                "--season-to",
                "2025-26",
                "--game-id",
                "0022500001",
            ],
            environ={},
        )
    request = simulate_totals.parse_args(
        [*arguments(tmp_path / "out"), "--game-id", "0022500001", "--state-number", "0"], environ={}
    )
    assert request.state_number == 0


@pytest.mark.parametrize("code", [0, 1, 2, 130])
def test_main_returns_safe_statuses_and_complete_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    async def command(request: SimulateTotalsRequest) -> SimulateTotalsResult:
        if code == 2:
            raise SimulationError("simulation_source_mismatch")
        if code == 130:
            raise asyncio.CancelledError
        counts = (
            SimulationCounters(1, 2, 1, 0, 1, 0, ())
            if code == 1
            else SimulationCounters(1, 2, 2, 0, 0, 0, ())
        )
        return SimulateTotalsResult(counts, "a" * 64, "b" * 64)

    assert simulate_totals.main(arguments(tmp_path / "out"), run_command=command) == code
    output = capsys.readouterr()
    if code < 2:
        for field in (
            "games=1",
            "states=2",
            "complete=",
            "incomplete=0",
            "unavailable=",
            "unresolved=0",
            "run_semantic_revision=",
            "artifact_sha256=",
        ):
            assert field in output.out
    elif code == 2:
        assert "code=simulation_source_mismatch" in output.err


@pytest.mark.parametrize("kind", ["secret", "model", "missing_ml"])
def test_errors_never_print_arbitrary_exception_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    async def command(request: SimulateTotalsRequest) -> SimulateTotalsResult:
        if kind == "model":
            raise ModelingError("model_source_changed")
        if kind == "missing_ml":
            raise SimulationError("ml_dependencies_missing")
        raise RuntimeError("postgresql://private:password@host/db")

    assert simulate_totals.main(arguments(tmp_path / "out"), run_command=command) == 2
    output = capsys.readouterr()
    assert "private" not in output.err and "password" not in output.err
    if kind == "missing_ml":
        assert "uv sync --extra ml" in output.err


def test_import_and_help_do_not_initialize_settings_or_native_modules() -> None:
    script = """import importlib.abc,os,sys
os.environ["DATABASE_URL"]="invalid"
os.environ["MONTE_CARLO_SIMULATIONS"]="invalid"
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.split(".")[0] in {"numpy","catboost"}: raise AssertionError("native import on help")
sys.meta_path.insert(0,Block())
from scripts.simulate_totals import main
raise SystemExit(main(["--help"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--state-number" in result.stdout


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("before", 130),
        ("cpu", 130),
        ("after", 1),
        pytest.param(
            "native",
            130,
            marks=pytest.mark.skipif(
                util.find_spec("numpy") is None, reason="optional NumPy required"
            ),
        ),
    ],
)
def test_actual_sigints_join_workers_and_preserve_commit(
    tmp_path: Path, phase: str, expected: int
) -> None:
    path = tmp_path / "out"
    path.write_bytes(b"old")
    result = subprocess.run(
        [sys.executable, "-m", "tests.unit.simulation_interrupt_probe", str(path), phase],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "joined=True" in result.stdout and "handler_restored=True" in result.stdout
    if phase == "after":
        assert path.read_bytes().startswith(b'{"') and "artifact_sha256=" in result.stdout
    else:
        assert path.read_bytes() == b"old"


async def test_explicit_count_reaches_source_settings_despite_invalid_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("MONTE_CARLO_SIMULATIONS", "invalid")
    result = SimulateTotalsResult(SimulationCounters(1, 1, 1, 0, 0, 0, ()), "a" * 64, "b" * 64)
    composed = AsyncMock(return_value=result)
    monkeypatch.setattr(simulate_totals, "run_source_work", composed)
    request = simulate_totals.parse_args([*arguments(tmp_path / "out"), "--simulations", "3"])
    assert await simulate_totals.run(request) == result
    assert composed.call_args.kwargs["settings"].monte_carlo_simulations == 3


async def test_source_composition_accepts_prevalidated_count_without_reinitializing_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from nba_live_totals.settings import Settings
    from scripts import build_training_dataset

    settings = Settings.model_construct(monte_carlo_simulations=3)
    engine, client, work = AsyncMock(), AsyncMock(), Mock()

    def forbidden_settings() -> Settings:
        raise AssertionError("explicit settings must not be reconstructed")

    monkeypatch.setattr(build_training_dataset, "Settings", forbidden_settings)
    monkeypatch.setattr(build_training_dataset, "configure_logging", lambda settings: None)
    monkeypatch.setattr(
        build_training_dataset, "create_async_engine", lambda *args, **kwargs: engine
    )
    monkeypatch.setattr(build_training_dataset, "ClickHouseClient", lambda **kwargs: client)
    monkeypatch.setattr(
        build_training_dataset, "PostgresFeatureWorkStore", lambda *args, **kwargs: work
    )
    validate = AsyncMock()
    monkeypatch.setattr(build_training_dataset, "validate_schema", validate)

    async def operation(source: object) -> str:
        return "checked"

    assert await build_training_dataset.run_source_work(operation, settings=settings) == "checked"
    assert validate.await_count == 4
    work.start_run.assert_not_called()
    work.claim_game.assert_not_called()
    client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
