"""Command-line composition root for historical NBA game ingestion."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    HistoricalIngestionService,
)
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.infrastructure.nba import (
    NBAApiHistoricalDataSource,
    NBAApiTransport,
)
from nba_live_totals.infrastructure.postgres.ingestion_store import (
    PostgresHistoricalIngestionStore,
)
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[BackfillRequest, Settings], Awaitable[BackfillResult]]
logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill historical NBA games")
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    parser.add_argument("--refresh", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> BackfillRequest:
    """Parse and validate an explicit season or contiguous season range."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    season_value = cast(str | None, arguments.season)
    season_from_value = cast(str | None, arguments.season_from)
    season_to_value = cast(str | None, arguments.season_to)
    game_id_value = cast(str | None, arguments.game_id)
    refresh = cast(bool, arguments.refresh)

    try:
        game_id = GameId(game_id_value) if game_id_value is not None else None
        if season_value is not None:
            if season_to_value is not None:
                parser.error("--season-to requires --season-from")
            return BackfillRequest(
                seasons=(SeasonId(season_value),),
                game_id=game_id,
                refresh=refresh,
            )

        if season_from_value is None:
            parser.error("a season selector is required")
        if season_to_value is None:
            parser.error("--season-from requires --season-to")
        if game_id is not None:
            parser.error("--game-id requires --season")
        return BackfillRequest.for_range(
            SeasonId(season_from_value),
            SeasonId(season_to_value),
            refresh=refresh,
        )
    except ValueError as error:
        parser.error(str(error))


async def run(request: BackfillRequest, settings: Settings) -> BackfillResult:
    """Compose the historical-ingestion adapters and release their database pool."""
    configure_logging(settings)
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    try:
        transport = NBAApiTransport(settings)
        source = NBAApiHistoricalDataSource(transport)
        store = PostgresHistoricalIngestionStore(
            engine,
            lease_seconds=int(settings.nba_api_resource_lease_seconds),
        )
        service = HistoricalIngestionService(
            source,
            store,
            max_concurrency=int(settings.nba_api_max_concurrency),
        )
        return await service.backfill(request)
    finally:
        active_error = sys.exception()
        try:
            await engine.dispose()
        except BaseException as cleanup_error:
            if active_error is None:
                raise
            logger.error(
                "historical_ingestion_engine_dispose_failed",
                extra={
                    "event": "historical_ingestion_engine_dispose_failed",
                    "error_type": type(cleanup_error).__name__,
                },
            )


async def _invoke(
    run_command: RunCommand,
    request: BackfillRequest,
    settings: Settings,
) -> BackfillResult:
    return await run_command(request, settings)


def main(
    argv: Sequence[str] | None = None,
    *,
    run_command: RunCommand = run,
) -> int:
    """Run one backfill and return its documented shell exit status."""
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

    print(
        f"run={result.run_id} status={result.status.value} "
        f"discovered={result.discovered} succeeded={result.succeeded} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    return 0 if result.status is BackfillStatus.SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
