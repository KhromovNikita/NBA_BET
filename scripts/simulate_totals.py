"""Offline, verified full-game Monte Carlo export without live data access."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildMode
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SimulateTotalsRequest,
    SimulateTotalsResult,
)
from nba_live_totals.application.simulation.service import SimulateTotalsService
from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.simulation import SimulationConfig, SimulationQuery
from nba_live_totals.domain.simulation_hashing import simulation_config_projection
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.simulation_codec import parse_simulation_config
from nba_live_totals.infrastructure.files.simulation_export import AtomicSimulationExporter
from nba_live_totals.infrastructure.simulation.executor import OwnedSimulationExecutor
from nba_live_totals.infrastructure.simulation.numpy_engine import NumpySimulationEngine
from nba_live_totals.settings import Settings
from scripts.async_runner import run_owned_async
from scripts.build_training_dataset import run_source_work

RunCommand = Callable[[SimulateTotalsRequest], Awaitable[SimulateTotalsResult]]
MAX_CONFIG_BYTES = 16 * 1024 * 1024
_KERNEL_FLAGS = (
    "simulations",
    "seed",
    "batch_size",
    "max_overtimes",
    "max_possessions_per_horizon",
)


def _scaled_decimal(value: str, maximum: int, quantum: int) -> int:
    try:
        if not value or len(value) > 256:
            raise ValueError("invalid query decimal")
        number = Decimal(value)
        if not number.is_finite() or not 0 <= number <= Decimal(f"{maximum}e-6"):
            raise ValueError("query decimal outside supported range")
        if not number:
            return 0
        parts = number.as_tuple()
        digits = list(parts.digits)
        exponent = cast(int, parts.exponent) + 6
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        if exponent < 0:
            raise ValueError("query decimal requires rounding")
        result = int("".join(str(digit) for digit in digits) + "0" * exponent)
        if result > maximum or result % quantum:
            raise ValueError("query decimal outside supported grid")
        return result
    except InvalidOperation, ArithmeticError:
        raise ValueError("invalid query decimal") from None


def parse_total_line(value: str) -> int:
    return _scaled_decimal(value, 2**63 - 1, 500000)


def parse_quantile_level(value: str) -> int:
    return _scaled_decimal(value, 1000000, 1)


def load_simulation_config(path: Path) -> SimulationConfig:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ValueError("oversized simulation configuration")
        record = json.loads(content)
        config = parse_simulation_config(record)
        if canonical_json_bytes(simulation_config_projection(config)) + b"\n" != content:
            raise ValueError("simulation configuration must be canonical JSON plus LF")
        return config
    except OSError, ValueError, TypeError, KeyError, RecursionError:
        raise ValueError("invalid simulation configuration") from None


def parse_args(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SimulateTotalsRequest:
    parser = argparse.ArgumentParser(
        description="Export offline full-game Monte Carlo totals from verified features and models"
    )
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    parser.add_argument("--state-number")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config")
    for flag in _KERNEL_FLAGS:
        parser.add_argument("--" + flag.replace("_", "-"))
    parser.add_argument("--line", action="append", default=[])
    parser.add_argument("--quantile", action="append", default=[])
    args = parser.parse_args(argv)
    seasons: tuple[SeasonId, ...]
    try:
        game = GameId(args.game_id) if args.game_id is not None else None
        if args.season is not None:
            if args.season_to is not None:
                parser.error("--season-to requires --season-from")
            seasons = (SeasonId(args.season),)
        else:
            if args.season_to is None or game is not None:
                parser.error("complete season range required; --game-id requires --season")
            seasons = BuildDatasetRequest.for_range(
                SeasonId(args.season_from), SeasonId(args.season_to), mode=FeatureBuildMode.VERIFY
            ).seasons
        if args.config is not None:
            if any(getattr(args, name) is not None for name in _KERNEL_FLAGS):
                parser.error("--config cannot be combined with kernel flags")
            config = load_simulation_config(Path(args.config))
        else:
            env = os.environ if environ is None else environ
            n = int(
                args.simulations
                if args.simulations is not None
                else env.get("MONTE_CARLO_SIMULATIONS", "10000")
            )
            config = SimulationConfig(
                simulations=n,
                seed=42 if args.seed is None else int(args.seed),
                batch_size=min(1024, n) if args.batch_size is None else int(args.batch_size),
                max_overtimes=10 if args.max_overtimes is None else int(args.max_overtimes),
                max_possessions_per_horizon=10000
                if args.max_possessions_per_horizon is None
                else int(args.max_possessions_per_horizon),
            )
        query = SimulationQuery(
            tuple(sorted({parse_total_line(value) for value in args.line})),
            tuple(sorted({parse_quantile_level(value) for value in args.quantile}))
            if args.quantile
            else (50000, 500000, 950000),
        )
        request = SimulateTotalsRequest(
            seasons,
            Path(args.model),
            Path(args.output),
            config,
            query,
            game,
            None if args.state_number is None else int(args.state_number),
            None if args.config is None else Path(args.config),
        )
        if not request.output_path.parent.is_dir():
            parser.error("output parent directory must already exist")
        return request
    except ValueError, OSError, ArithmeticError:
        parser.error("invalid selectors, simulation parameters, queries or artifact paths")


async def run(request: SimulateTotalsRequest) -> SimulateTotalsResult:
    return await run_source_work(
        lambda source: SimulateTotalsService(
            source,
            VerifiedModelBundleStore(),
            OwnedSimulationExecutor(NumpySimulationEngine()),
            AtomicSimulationExporter(),
        ).simulate(request),
        settings=Settings(monte_carlo_simulations=request.config.simulations),
    )


def main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int:
    try:
        request = parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        result = run_owned_async(lambda: run_command(request))
    except KeyboardInterrupt, asyncio.CancelledError:
        return 130
    except (SimulationError, ModelingError) as error:
        print(f"simulation_failed code={error.code}", file=sys.stderr)
        if error.code == "ml_dependencies_missing":
            print("Install optional ML dependencies: uv sync --extra ml", file=sys.stderr)
        return 2
    except Exception:
        print("simulation_failed code=simulation_operational_error", file=sys.stderr)
        return 2
    counts = result.counts
    status = "partial" if result.exit_code else "succeeded"
    print(
        f"status={status} games={counts.games} states={counts.states} "
        f"complete={counts.complete} incomplete={counts.incomplete} "
        f"unavailable={counts.unavailable} "
        f"unresolved={counts.unresolved} unresolved_counts={dict(counts.unresolved_counts)} "
        f"run_semantic_revision={result.run_semantic_revision} "
        f"artifact_sha256={result.artifact_sha256}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
