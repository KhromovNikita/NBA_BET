"""Database-independent offline CPU training from one checked canonical dataset."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal, DecimalException
from pathlib import Path

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import TrainModelsRequest, TrainModelsResult
from nba_live_totals.application.modeling.training_service import TrainModelsService
from nba_live_totals.domain.model_training import TrainingConfig
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from nba_live_totals.infrastructure.ml.process import SubprocessTrainingWorker
from scripts.async_runner import run_owned_async

RunCommand = Callable[[TrainModelsRequest], Awaitable[TrainModelsResult]]


def parse_args(argv: Sequence[str] | None = None) -> TrainModelsRequest:
    parser = argparse.ArgumentParser(description="Train three offline CPU CatBoost rate models")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    for name, default in (
        ("iterations", "1000"),
        ("depth", "6"),
        ("learning-rate", "0.03"),
        ("l2-leaf-reg", "3"),
        ("random-seed", "42"),
        ("early-stopping-rounds", "100"),
        ("max-rows-per-partition", "1000000"),
    ):
        parser.add_argument(f"--{name}", default=default)
    args = parser.parse_args(argv)
    try:
        config = TrainingConfig(
            int(args.iterations),
            int(args.depth),
            Decimal(args.learning_rate),
            Decimal(args.l2_leaf_reg),
            int(args.random_seed),
            int(args.early_stopping_rounds),
            int(args.max_rows_per_partition),
        )
        request = TrainModelsRequest(Path(args.dataset), Path(args.output), config)
        if not request.output_path.parent.is_dir():
            parser.error("output parent directory must already exist")
        return request
    except ValueError, OSError, DecimalException:
        parser.error("invalid training parameters or artifact paths")


async def run(request: TrainModelsRequest) -> TrainModelsResult:
    return await TrainModelsService(
        CanonicalTrainingDatasetReader(), SubprocessTrainingWorker(), VerifiedModelBundleStore()
    ).train(request)


def main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int:
    try:
        request = parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        result = run_owned_async(lambda: run_command(request))
    except KeyboardInterrupt, asyncio.CancelledError:
        return 130
    except ModelingError as error:
        print(f"model_training_failed code={error.code}", file=sys.stderr)
        if error.code == "ml_dependencies_missing":
            print("Install optional ML dependencies: uv sync --extra ml", file=sys.stderr)
        return 2
    except Exception:
        print("model_training_failed code=model_operational_error", file=sys.stderr)
        return 2
    print(
        f"status=succeeded games={result.manifest.dataset.games} "
        f"states={result.manifest.dataset.states} heads=3 "
        f"model_revision_sha256={result.manifest.model_revision_sha256} "
        f"artifact_sha256={result.artifact_sha256}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
