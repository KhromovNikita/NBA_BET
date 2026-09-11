"""Command-line composition root for deterministic analytical possession builds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    BuildPossessionsResult,
    PossessionBuildService,
)
from nba_live_totals.domain import (
    AnalyticalPossessionParser,
    GameId,
    PossessionVersions,
    SeasonId,
)
from nba_live_totals.infrastructure.clickhouse.client import (
    ClickHouseClient,
    ClickHouseError,
)
from nba_live_totals.infrastructure.clickhouse.migrations import (
    ClickHouseMigrationError,
    fetch_schema_projection,
    load_migrations,
    schema_fingerprint,
)
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer
from nba_live_totals.infrastructure.postgres.possession_store import (
    PostgresPossessionWorkStore,
)
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[BuildPossessionsRequest, Settings], Awaitable[BuildPossessionsResult]]
logger = logging.getLogger(__name__)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class PossessionBuildCLIError(RuntimeError):
    """A bounded composition error safe to classify at the CLI boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_error_code(code: str) -> str:
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "possession_build_operational_error"


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) else "UnexpectedError"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build analytical NBA possessions")
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--season")
    selectors.add_argument("--season-from")
    parser.add_argument("--season-to")
    parser.add_argument("--game-id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rebuild", action="store_true")
    modes.add_argument("--verify", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> BuildPossessionsRequest:
    """Parse one season or one complete inclusive contiguous season range."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    season_value = cast(str | None, arguments.season)
    season_from_value = cast(str | None, arguments.season_from)
    season_to_value = cast(str | None, arguments.season_to)
    game_id_value = cast(str | None, arguments.game_id)
    rebuild = cast(bool, arguments.rebuild)
    verify = cast(bool, arguments.verify)
    mode = BuildMode.VERIFY if verify else BuildMode.MATERIALIZE

    try:
        game_id = GameId(game_id_value) if game_id_value is not None else None
        if season_value is not None:
            if season_to_value is not None:
                parser.error("--season-to requires --season-from")
            return BuildPossessionsRequest(
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
        return BuildPossessionsRequest.for_range(
            SeasonId(season_from_value),
            SeasonId(season_to_value),
            mode=mode,
            rebuild=rebuild,
        )
    except ValueError as error:
        parser.error(str(error))


async def _validate_pinned_schema(
    engine: AsyncEngine,
    client: ClickHouseClient,
) -> None:
    """Read-only validation of the migration ledger and exact latest CH catalog."""
    migrations = load_migrations()
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT migration_id, checksum_sha256, "
                    "expected_schema_fingerprint_sha256, state "
                    "FROM clickhouse_schema_migrations ORDER BY migration_id"
                )
            )
            rows = [dict(row) for row in result.mappings().all()]
    except SQLAlchemyError:
        raise ClickHouseMigrationError("clickhouse_migration_ledger_unavailable") from None

    ledger = {str(row.get("migration_id")): row for row in rows}
    if set(ledger) != {migration.migration_id for migration in migrations}:
        raise ClickHouseMigrationError("clickhouse_migration_set_drift")
    for migration in migrations:
        row = ledger[migration.migration_id]
        if row.get("checksum_sha256") != migration.checksum_sha256:
            raise ClickHouseMigrationError("clickhouse_migration_checksum_drift")
        if (
            row.get("expected_schema_fingerprint_sha256")
            != migration.expected_schema_fingerprint_sha256
        ):
            raise ClickHouseMigrationError("clickhouse_migration_fingerprint_drift")
        if row.get("state") != "applied":
            raise ClickHouseMigrationError("clickhouse_schema_not_applied")

    latest = migrations[-1]
    database = latest.expected_schema.get("database")
    table = latest.expected_schema.get("table")
    if type(database) is not str or type(table) is not str:
        raise ClickHouseMigrationError("clickhouse_migration_set_invalid")
    try:
        actual = await fetch_schema_projection(client, database=database, table=table)
    except ClickHouseError:
        raise ClickHouseMigrationError("clickhouse_schema_unavailable") from None
    if schema_fingerprint(actual) != latest.expected_schema_fingerprint_sha256:
        raise ClickHouseMigrationError("clickhouse_schema_drift")


async def run(
    request: BuildPossessionsRequest,
    settings: Settings,
) -> BuildPossessionsResult:
    """Compose Phase 3 adapters, validate schema, run, and release owned clients."""
    configure_logging(settings)
    engine: AsyncEngine | None = None
    client: ClickHouseClient | None = None
    try:
        engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
        client = ClickHouseClient(
            base_url=str(settings.clickhouse_url).rstrip("/"),
            user=settings.clickhouse_user,
            password=settings.clickhouse_password.get_secret_value(),
            timeout_seconds=settings.clickhouse_operation_timeout_seconds,
        )
        await _validate_pinned_schema(engine, client)
        versions = PossessionVersions(
            normalizer_version=settings.possession_normalizer_version,
            parser_version=settings.possession_parser_version,
            rules_version=settings.possession_rules_version,
            output_schema_version=settings.possession_output_schema_version,
        )
        work_store = PostgresPossessionWorkStore(
            engine,
            lease_seconds=settings.possession_resource_lease_seconds,
        )
        normalizer = NBAPlayByPlayV3PossessionNormalizer()
        parser = AnalyticalPossessionParser()
        snapshot_store = PossessionSnapshotStore(client)
        service = PossessionBuildService(
            normalizer=normalizer,
            parser=parser,
            work_store=work_store,
            snapshot_store=snapshot_store,
            versions=versions,
            max_concurrency=settings.possession_build_max_concurrency,
        )
        return await service.build(request)
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        closers: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if client is not None:
            closers.append(("clickhouse", client.aclose))
        if engine is not None:
            closers.append(("postgres", engine.dispose))
        for resource, close in closers:
            try:
                await close()
            except BaseException as error:
                if active_error is None and cleanup_error is None:
                    cleanup_error = error
                else:
                    logger.error(
                        "possession_build_cleanup_failed",
                        extra={
                            "event": "possession_build_cleanup_failed",
                            "resource": resource,
                            "error_type": _safe_error_type(error),
                        },
                    )
        if active_error is None and cleanup_error is not None:
            raise PossessionBuildCLIError("possession_build_cleanup_failed") from None


async def _invoke(
    run_command: RunCommand,
    request: BuildPossessionsRequest,
    settings: Settings,
) -> BuildPossessionsResult:
    return await run_command(request, settings)


def main(
    argv: Sequence[str] | None = None,
    *,
    run_command: RunCommand = run,
) -> int:
    """Run one possession operation and return its documented shell exit status."""
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
        print("invalid possession build configuration", file=sys.stderr)
        return 2
    except PossessionBuildCLIError as error:
        print(f"possession_build_failed code={_safe_error_code(error.code)}", file=sys.stderr)
        return 1
    except ClickHouseError as error:
        print(f"possession_build_failed code={_safe_error_code(error.code)}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("possession_build_failed code=possession_build_persistence_error", file=sys.stderr)
        return 1

    print(
        f"run={result.run_id} status={result.status.value} "
        f"discovered={result.discovered} materialized={result.materialized} "
        f"skipped_current={result.skipped_current} verified={result.verified} "
        f"failed={result.failed} busy={result.busy}"
    )
    return 0 if result.failed == result.busy == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
