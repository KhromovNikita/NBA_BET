"""Read-only published Phase 5 sources to a verified chronological training JSONL."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildMode
from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    BuildTrainingDatasetResult,
)
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.identifiers import SeasonId
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.domain.model_training import TemporalSplitConfig
from nba_live_totals.domain.possessions import PossessionVersions
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    ClickHouseFeatureSourceReader,
)
from nba_live_totals.infrastructure.clickhouse.migrations import (
    GAME_STATE_FEATURES_SCHEMA_ID,
    LINEUP_SEGMENTS_SCHEMA_ID,
    PLAYER_PREGAME_FEATURES_SCHEMA_ID,
    POSSESSIONS_SCHEMA_ID,
    validate_schema,
)
from nba_live_totals.infrastructure.files.training_dataset import AtomicTrainingDatasetExporter
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.model_source import PublishedModelFeatureSource
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings
from scripts.async_runner import run_owned_async

RunCommand = Callable[[BuildTrainingDatasetRequest], Awaitable[BuildTrainingDatasetResult]]
logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> BuildTrainingDatasetRequest:
    parser = argparse.ArgumentParser(
        description="Build verified offline chronological model training JSONL"
    )
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--validation-from", required=True)
    parser.add_argument("--test-from", required=True)
    parser.add_argument("--embargo-days", default="1")
    parser.add_argument("--output", required=True)
    for name, default in (
        ("history-window", "20"),
        ("ppp-prior-possessions", "20"),
        ("pace-prior-combined-possessions", "40"),
    ):
        parser.add_argument(f"--{name}", default=default)
    args = parser.parse_args(argv)
    seasons: tuple[SeasonId, ...]
    try:
        if args.season is not None:
            if args.season_to is not None:
                parser.error("--season-to requires --season-from")
            seasons = (SeasonId(args.season),)
        else:
            if args.season_to is None:
                parser.error("complete season range required")
            seasons = BuildDatasetRequest.for_range(
                SeasonId(args.season_from), SeasonId(args.season_to), mode=FeatureBuildMode.VERIFY
            ).seasons
        split = TemporalSplitConfig(
            date.fromisoformat(args.validation_from),
            date.fromisoformat(args.test_from),
            int(args.embargo_days),
        )
        config = BaselineConfig(
            int(args.history_window),
            int(args.ppp_prior_possessions),
            int(args.pace_prior_combined_possessions),
        )
        request = BuildTrainingDatasetRequest(seasons, Path(args.output), split, config)
        if not request.output_path.parent.is_dir():
            parser.error("output parent directory must already exist")
        return request
    except ValueError, OSError:
        parser.error("invalid selectors, split, parameters or output path")


async def run_source_work[T](
    operation: Callable[[PublishedModelFeatureSource], Awaitable[T]],
    *,
    settings: Settings | None = None,
) -> T:
    settings = Settings() if settings is None else settings
    configure_logging(settings)
    engine: AsyncEngine | None = None
    client: ClickHouseClient | None = None
    completed = False
    try:
        if settings.database_url.scheme != "postgresql+asyncpg":
            raise ModelingError("model_invalid_database_scheme")
        engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
        client = ClickHouseClient(
            base_url=str(settings.clickhouse_url).rstrip("/"),
            user=settings.clickhouse_user,
            password=settings.clickhouse_password.get_secret_value(),
            timeout_seconds=settings.clickhouse_operation_timeout_seconds,
        )
        for schema in (
            POSSESSIONS_SCHEMA_ID,
            LINEUP_SEGMENTS_SCHEMA_ID,
            GAME_STATE_FEATURES_SCHEMA_ID,
            PLAYER_PREGAME_FEATURES_SCHEMA_ID,
        ):
            await validate_schema(engine, client, schema_id=schema)
        work = PostgresFeatureWorkStore(
            engine,
            possession_versions=PossessionVersions(
                settings.possession_normalizer_version,
                settings.possession_parser_version,
                settings.possession_rules_version,
                settings.possession_output_schema_version,
            ),
            lineup_versions=LineupVersions(
                settings.lineup_normalizer_version,
                settings.lineup_reconstructor_version,
                settings.lineup_rules_version,
                settings.lineup_output_schema_version,
            ),
        )
        source = PublishedModelFeatureSource(
            work,
            ClickHouseFeatureSourceReader(client),
            ClickHouseFeatureSnapshotStore(client),
            versions=FeatureVersions(
                settings.feature_join_version,
                settings.feature_formula_version,
                settings.feature_event_normalizer_version,
                settings.feature_output_schema_version,
                settings.feature_player_output_schema_version,
            ),
            normalizer=NBAPlayByPlayV3PossessionNormalizer(),
        )
        result = await operation(source)
        completed = True
        return result
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        closers: list[Callable[[], Awaitable[None]]] = []
        if client is not None:
            closers.append(client.aclose)
        if engine is not None:
            closers.append(engine.dispose)
        for close in closers:
            try:
                await close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
                logger.error(
                    "model_source_cleanup_failed",
                    extra={
                        "event": "model_source_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
        if not completed and active_error is None and cleanup_error is not None:
            if isinstance(cleanup_error, (asyncio.CancelledError, KeyboardInterrupt)):
                raise cleanup_error
            raise ModelingError("model_source_cleanup_failed") from None


async def run(request: BuildTrainingDatasetRequest) -> BuildTrainingDatasetResult:
    return await run_source_work(
        lambda source: BuildTrainingDatasetService(source, AtomicTrainingDatasetExporter()).build(
            request
        )
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
        print(f"training_dataset_failed code={error.code}", file=sys.stderr)
        return 2
    except Exception:
        print("training_dataset_failed code=model_operational_error", file=sys.stderr)
        return 2
    status = "partial" if result.exit_code else "succeeded"
    print(
        f"status={status} games={result.summary.games} states={result.summary.states} "
        f"trainable={str(result.summary.trainable).lower()} "
        f"artifact_sha256={result.summary.dataset_artifact_sha256}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
