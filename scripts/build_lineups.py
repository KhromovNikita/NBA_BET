"""Command-line composition root for deterministic analytical lineup builds."""

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

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    BuildLineupsResult,
    LineupBuildMode,
    LineupBuildService,
)
from nba_live_totals.domain import (
    GameId,
    SeasonId,
)
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.infrastructure.clickhouse.client import (
    ClickHouseClient,
    ClickHouseError,
)
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.clickhouse.migrations import (
    LINEUP_SEGMENTS_SCHEMA_ID,
    ClickHouseMigrationError,
    validate_schema,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3LineupNormalizer
from nba_live_totals.infrastructure.postgres.lineup_store import (
    PostgresLineupWorkStore,
)
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[BuildLineupsRequest, Settings], Awaitable[BuildLineupsResult]]
logger = logging.getLogger(__name__)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class LineupBuildCLIError(RuntimeError):
    """A bounded composition error safe to classify at the CLI boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_error_code(code: str) -> str:
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "lineup_build_operational_error"


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) else "UnexpectedError"


def _log_cleanup_failure(
    *,
    event: str,
    error_code: str,
    error: BaseException,
) -> None:
    logger.error(
        event,
        extra={
            "event": event,
            "error_code": error_code,
            "error_type": _safe_error_type(error),
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build analytical NBA lineups")
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rebuild", action="store_true")
    modes.add_argument("--verify", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> BuildLineupsRequest:
    """Parse one season or one complete inclusive contiguous season range."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    season_value = cast(str | None, arguments.season)
    season_from_value = cast(str | None, arguments.season_from)
    season_to_value = cast(str | None, arguments.season_to)
    game_id_value = cast(str | None, arguments.game_id)
    rebuild = cast(bool, arguments.rebuild)
    verify = cast(bool, arguments.verify)
    mode = LineupBuildMode.VERIFY if verify else LineupBuildMode.MATERIALIZE

    try:
        game_id = GameId(game_id_value) if game_id_value is not None else None
        if season_value is not None:
            if season_to_value is not None:
                parser.error("--season-to requires --season-from")
            return BuildLineupsRequest(
                seasons=(SeasonId(season_value),),
                game_id=game_id,
                mode=mode,
                rebuild=rebuild,
            )

        if season_from_value is None:
            parser.error("a season selector is required")
        if season_to_value is None:
            parser.error("--season-from requires --season-to")
        if game_id is not None:
            parser.error("--game-id requires --season")
        return BuildLineupsRequest.for_range(
            SeasonId(season_from_value),
            SeasonId(season_to_value),
            mode=mode,
            rebuild=rebuild,
        )
    except ValueError as error:
        parser.error(str(error))


async def run(
    request: BuildLineupsRequest,
    settings: Settings,
) -> BuildLineupsResult:
    """Compose Phase 4 adapters, validate schema, run, and release owned clients."""
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
        await validate_schema(engine, client, schema_id=LINEUP_SEGMENTS_SCHEMA_ID)
        versions = LineupVersions(
            normalizer_version=settings.lineup_normalizer_version,
            reconstructor_version=settings.lineup_reconstructor_version,
            rules_version=settings.lineup_rules_version,
            output_schema_version=settings.lineup_output_schema_version,
        )
        work_store = PostgresLineupWorkStore(
            engine,
            lease_seconds=settings.lineup_claim_lease_seconds,
        )
        normalizer = NBAPlayByPlayV3LineupNormalizer()
        reconstructor = DeterministicLineupReconstructor()
        snapshot_store = ClickHouseLineupSnapshotStore(client)
        service = LineupBuildService(
            normalizer=normalizer,
            reconstructor=reconstructor,
            work_store=work_store,
            snapshot_store=snapshot_store,
            versions=versions,
            max_concurrency=settings.lineup_build_max_concurrency,
        )
        return await service.build(request)
    finally:
        active_error = sys.exception()
        cleanup_error: tuple[str, str, BaseException] | None = None
        cleanup_interrupt: BaseException | None = None
        closers: list[tuple[str, str, Callable[[], Awaitable[None]]]] = []
        if client is not None:
            closers.append(
                (
                    "lineup_clickhouse_cleanup_failed",
                    "clickhouse_cleanup_failed",
                    client.aclose,
                )
            )
        if engine is not None:
            closers.append(
                (
                    "lineup_postgres_cleanup_failed",
                    "postgres_cleanup_failed",
                    engine.dispose,
                )
            )
        for event, error_code, close in closers:
            try:
                await close()
            except (asyncio.CancelledError, KeyboardInterrupt) as error:
                if active_error is None and cleanup_interrupt is None:
                    cleanup_interrupt = error
                else:
                    _log_cleanup_failure(
                        event=event,
                        error_code=error_code,
                        error=error,
                    )
            except BaseException as error:
                if active_error is None and cleanup_interrupt is None and cleanup_error is None:
                    cleanup_error = (event, error_code, error)
                else:
                    _log_cleanup_failure(
                        event=event,
                        error_code=error_code,
                        error=error,
                    )
        if active_error is None:
            if cleanup_interrupt is not None:
                if cleanup_error is not None:
                    retained_event, retained_code, retained_error = cleanup_error
                    _log_cleanup_failure(
                        event=retained_event,
                        error_code=retained_code,
                        error=retained_error,
                    )
                raise cleanup_interrupt
            if cleanup_error is not None:
                raise LineupBuildCLIError("lineup_build_cleanup_failed") from None


async def _invoke(
    run_command: RunCommand,
    request: BuildLineupsRequest,
    settings: Settings,
) -> BuildLineupsResult:
    return await run_command(request, settings)


def main(
    argv: Sequence[str] | None = None,
    *,
    run_command: RunCommand = run,
) -> int:
    """Run one lineup operation and return its documented shell exit status."""
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
    except ClickHouseMigrationError, ValueError:
        print("invalid lineup build configuration", file=sys.stderr)
        return 2
    except LineupBuildCLIError as error:
        print(f"lineup_build_failed code={_safe_error_code(error.code)}", file=sys.stderr)
        return 1
    except ClickHouseError as error:
        print(f"lineup_build_failed code={_safe_error_code(error.code)}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("lineup_build_failed code=lineup_build_persistence_error", file=sys.stderr)
        return 1

    print(
        f"run={result.run_id} status={result.status.value} "
        f"discovered={result.discovered} materialized={result.materialized} "
        f"invalid={result.invalid} "
        f"skipped_current={result.skipped_current} verified={result.verified} "
        f"failed={result.failed} busy={result.busy}"
    )
    return 0 if result.invalid == result.failed == result.busy == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
