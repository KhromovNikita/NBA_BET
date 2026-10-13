"""Offline baseline CLI: read published snapshots and atomically export JSONL."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import BuildBaselinesRequest, BuildBaselinesResult
from nba_live_totals.application.baselines.service import BaselineBuildService
from nba_live_totals.domain.baselines import BaselineConfig, DeterministicBaselineCalculator
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupVersions
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
from nba_live_totals.infrastructure.files.baseline_export import AtomicJsonlBaselineExporter
from nba_live_totals.infrastructure.postgres.baseline_source import PublishedFeatureBaselineSource
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[BuildBaselinesRequest, Settings], Awaitable[BuildBaselinesResult]]
logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> BuildBaselinesRequest:
    parser = argparse.ArgumentParser(
        description="Export deterministic offline NBA baseline estimates"
    )
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    parser.add_argument("--output", required=True)
    parser.add_argument("--history-window", default="20")
    parser.add_argument("--ppp-prior-possessions", default="20")
    parser.add_argument("--pace-prior-combined-possessions", default="40")
    args = parser.parse_args(argv)
    try:
        config = BaselineConfig(
            int(args.history_window),
            int(args.ppp_prior_possessions),
            int(args.pace_prior_combined_possessions),
        )
        path = Path(args.output).resolve()
        game = GameId(args.game_id) if args.game_id is not None else None
        if args.season is not None:
            if args.season_to is not None:
                parser.error("--season-to requires --season-from")
            return BuildBaselinesRequest(
                (SeasonId(args.season),), path, game_id=game, config=config
            )
        if args.season_to is None or game is not None:
            parser.error("complete season range required; --game-id requires --season")
        return BuildBaselinesRequest.from_range(
            SeasonId(args.season_from), SeasonId(args.season_to), output_path=path, config=config
        )
    except ValueError, OSError:
        parser.error("invalid baseline selector, parameters or output path")


async def run(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult:
    configure_logging(settings)
    engine: AsyncEngine | None = None
    client: ClickHouseClient | None = None
    result: BuildBaselinesResult | None = None
    try:
        if settings.database_url.scheme != "postgresql+asyncpg":
            raise BaselineExportError("baseline_invalid_database_scheme")
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
        source = PublishedFeatureBaselineSource(
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
        )
        result = await BaselineBuildService(
            source, DeterministicBaselineCalculator(), AtomicJsonlBaselineExporter()
        ).build(request)
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
                    "baseline_cleanup_failed",
                    extra={
                        "event": "baseline_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
        if result is None and active_error is None and cleanup_error is not None:
            if isinstance(cleanup_error, (asyncio.CancelledError, KeyboardInterrupt)):
                raise cleanup_error
            raise BaselineExportError("baseline_cleanup_failed") from None


async def _invoke(
    command: RunCommand, request: BuildBaselinesRequest, settings: Settings
) -> BuildBaselinesResult:
    return await command(request, settings)


def main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int:
    try:
        request = parse_args(argv)
        settings = Settings()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except ValidationError, OSError, ValueError:
        print("invalid baseline configuration", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_invoke(run_command, request, settings))
    except KeyboardInterrupt, asyncio.CancelledError:
        return 130
    except BaselineExportError as error:
        print(f"baseline_export_failed code={error.code}", file=sys.stderr)
        return 2
    except Exception:
        print("baseline_export_failed code=baseline_operational_error", file=sys.stderr)
        return 2
    stats = result.stats
    status = "partial" if result.exit_code else "succeeded"
    print(
        f"status={status} games={stats.games} states={stats.states} available={stats.available} "
        f"partial={stats.partial} unavailable={stats.unavailable} "
        f"artifact_sha256={stats.artifact_sha256}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
