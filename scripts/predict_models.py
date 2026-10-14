"""Offline feature-only prediction from one explicitly selected checked model bundle."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildMode
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import PredictModelsRequest, PredictModelsResult
from nba_live_totals.application.modeling.prediction_service import PredictModelsService
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.model_predictions import AtomicModelPredictionExporter
from scripts.async_runner import run_owned_async
from scripts.build_training_dataset import run_source_work

RunCommand = Callable[[PredictModelsRequest], Awaitable[PredictModelsResult]]


def parse_args(argv: Sequence[str] | None = None) -> PredictModelsRequest:
    parser = argparse.ArgumentParser(
        description="Export causal offline model predictions from verified features"
    )
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
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
                parser.error("complete range required; --game-id requires --season")
            seasons = BuildDatasetRequest.for_range(
                SeasonId(args.season_from), SeasonId(args.season_to), mode=FeatureBuildMode.VERIFY
            ).seasons
        request = PredictModelsRequest(seasons, Path(args.model), Path(args.output), game)
        if not request.output_path.parent.is_dir():
            parser.error("output parent directory must already exist")
        return request
    except ValueError, OSError:
        parser.error("invalid selectors or artifact paths")


async def run(request: PredictModelsRequest) -> PredictModelsResult:
    return await run_source_work(
        lambda source: PredictModelsService(
            source, VerifiedModelBundleStore(), AtomicModelPredictionExporter()
        ).predict(request)
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
    except ModelingError as error:
        print(f"model_prediction_failed code={error.code}", file=sys.stderr)
        if error.code == "ml_dependencies_missing":
            print("Install optional ML dependencies: uv sync --extra ml", file=sys.stderr)
        return 2
    except Exception:
        print("model_prediction_failed code=model_operational_error", file=sys.stderr)
        return 2
    status = "partial" if result.exit_code else "succeeded"
    print(
        f"status={status} games={result.games} states={result.states} "
        f"available={result.available} unavailable={result.unavailable} "
        f"artifact_sha256={result.artifact_sha256}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
