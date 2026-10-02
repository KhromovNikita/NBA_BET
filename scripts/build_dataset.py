"""Command-line composition root for deterministic offline feature datasets."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    BuildDatasetResult,
    FeatureBuildMode,
    FeatureBuildService,
)
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.domain.features import DeterministicFeatureCalculator, FeatureVersions
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.domain.possessions import PossessionVersions
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient, ClickHouseError
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    ClickHouseFeatureSourceReader,
)
from nba_live_totals.infrastructure.clickhouse.migrations import (
    GAME_STATE_FEATURES_SCHEMA_ID,
    LINEUP_SEGMENTS_SCHEMA_ID,
    PLAYER_PREGAME_FEATURES_SCHEMA_ID,
    POSSESSIONS_SCHEMA_ID,
    ClickHouseMigrationError,
    validate_schema,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3FeatureEventNormalizer
from nba_live_totals.infrastructure.postgres import PostgresFeatureWorkStore
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[BuildDatasetRequest, Settings], Awaitable[BuildDatasetResult]]
logger = logging.getLogger(__name__)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class DatasetBuildCLIError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_error_code(code: str) -> str:
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "dataset_build_operational_error"


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) else "UnexpectedError"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deterministic NBA feature datasets")
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rebuild", action="store_true")
    modes.add_argument("--verify", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> BuildDatasetRequest:
    parser = _parser()
    arguments = parser.parse_args(argv)
    season = cast(str | None, arguments.season)
    season_from = cast(str | None, arguments.season_from)
    season_to = cast(str | None, arguments.season_to)
    game_value = cast(str | None, arguments.game_id)
    mode = FeatureBuildMode.VERIFY if cast(bool, arguments.verify) else FeatureBuildMode.MATERIALIZE
    try:
        game_id = GameId(game_value) if game_value is not None else None
        if season is not None:
            if season_to is not None:
                parser.error("--season-to requires --season-from")
            return BuildDatasetRequest(
                (SeasonId(season),),
                game_id=game_id,
                mode=mode,
                rebuild=cast(bool, arguments.rebuild),
            )
        if season_from is None:
            parser.error("a season selector is required")
        if season_to is None:
            parser.error("--season-from requires --season-to")
        if game_id is not None:
            parser.error("--game-id requires --season")
        return BuildDatasetRequest.for_range(SeasonId(season_from), SeasonId(season_to), mode=mode)
    except ValueError as error:
        parser.error(str(error))


async def run(request: BuildDatasetRequest, settings: Settings) -> BuildDatasetResult:
    """Validate all source/output schemas, compose adapters, and release resources."""
    configure_logging(settings)
    engine: AsyncEngine | None = None
    client: ClickHouseClient | None = None
    if settings.database_url.scheme != "postgresql+asyncpg":
        raise ValueError("database URL must use postgresql+asyncpg")
    try:
        engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
        client = ClickHouseClient(
            base_url=str(settings.clickhouse_url).rstrip("/"),
            user=settings.clickhouse_user,
            password=settings.clickhouse_password.get_secret_value(),
            timeout_seconds=settings.clickhouse_operation_timeout_seconds,
        )
        for schema_id in (
            POSSESSIONS_SCHEMA_ID,
            LINEUP_SEGMENTS_SCHEMA_ID,
            GAME_STATE_FEATURES_SCHEMA_ID,
            PLAYER_PREGAME_FEATURES_SCHEMA_ID,
        ):
            await validate_schema(engine, client, schema_id=schema_id)
        possession_versions = PossessionVersions(
            settings.possession_normalizer_version,
            settings.possession_parser_version,
            settings.possession_rules_version,
            settings.possession_output_schema_version,
        )
        lineup_versions = LineupVersions(
            settings.lineup_normalizer_version,
            settings.lineup_reconstructor_version,
            settings.lineup_rules_version,
            settings.lineup_output_schema_version,
        )
        versions = FeatureVersions(
            settings.feature_join_version,
            settings.feature_formula_version,
            settings.feature_event_normalizer_version,
            settings.feature_output_schema_version,
            settings.feature_player_output_schema_version,
        )
        work_store = PostgresFeatureWorkStore(
            engine,
            possession_versions=possession_versions,
            lineup_versions=lineup_versions,
            lease_seconds=settings.feature_claim_lease_seconds,
        )
        service = FeatureBuildService(
            normalizer=NBAPlayByPlayV3FeatureEventNormalizer(),
            source_reader=ClickHouseFeatureSourceReader(client),
            calculator=DeterministicFeatureCalculator(),
            work_store=work_store,
            snapshot_store=ClickHouseFeatureSnapshotStore(client),
            versions=versions,
        )
        return await service.build(request)
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        cleanup_interrupt: BaseException | None = None
        closers: list[Callable[[], Awaitable[None]]] = []
        if client is not None:
            closers.append(client.aclose)
        if engine is not None:
            closers.append(engine.dispose)
        for close in closers:
            try:
                await close()
            except (asyncio.CancelledError, KeyboardInterrupt) as error:
                if active_error is None and cleanup_interrupt is None:
                    cleanup_interrupt = error
                else:
                    logger.error(
                        "dataset_cleanup_failed",
                        extra={
                            "event": "dataset_cleanup_failed",
                            "error_code": "cleanup_failed",
                            "error_type": _safe_error_type(error),
                        },
                    )
            except BaseException as error:
                if active_error is None and cleanup_error is None:
                    cleanup_error = error
                else:
                    logger.error(
                        "dataset_cleanup_failed",
                        extra={
                            "event": "dataset_cleanup_failed",
                            "error_code": "cleanup_failed",
                            "error_type": _safe_error_type(error),
                        },
                    )
        if active_error is None:
            if cleanup_interrupt is not None:
                raise cleanup_interrupt
            if cleanup_error is not None:
                raise DatasetBuildCLIError("dataset_build_cleanup_failed") from None


async def _invoke(
    command: RunCommand, request: BuildDatasetRequest, settings: Settings
) -> BuildDatasetResult:
    return await command(request, settings)


def main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int:
    try:
        request = parse_args(argv)
        settings = Settings()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except ValidationError:
        print("invalid configuration", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(_invoke(run_command, request, settings))
    except KeyboardInterrupt:
        return 130
    except (ClickHouseMigrationError, ValueError):
        print("invalid dataset build configuration", file=sys.stderr)
        return 2
    except (DatasetBuildCLIError, ClickHouseError) as error:
        print(f"dataset_build_failed code={_safe_error_code(error.code)}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("dataset_build_failed code=dataset_build_persistence_error", file=sys.stderr)
        return 1
    print(
        f"run={result.run_id} status={result.status.value} discovered={result.discovered} "
        f"materialized={result.materialized} invalid={result.invalid} "
        f"skipped_current={result.skipped_current} verified={result.verified} "
        f"failed={result.failed} busy={result.busy}"
    )
    return 0 if result.invalid == result.failed == result.busy == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
