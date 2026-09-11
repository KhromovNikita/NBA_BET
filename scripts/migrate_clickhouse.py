"""Explicit ClickHouse schema migration command."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient, ClickHouseError
from nba_live_totals.infrastructure.clickhouse.migrations import (
    ClickHouseMigrationError,
    migrate_clickhouse,
)
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings

RunCommand = Callable[[Settings], Awaitable[None]]
logger = logging.getLogger(__name__)


async def _invoke(run_command: RunCommand, settings: Settings) -> None:
    await run_command(settings)


async def run(settings: Settings) -> None:
    configure_logging(settings)
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    client: ClickHouseClient | None = None
    try:
        client = ClickHouseClient(
            base_url=str(settings.clickhouse_url).rstrip("/"),
            user=settings.clickhouse_user,
            password=settings.clickhouse_password.get_secret_value(),
            timeout_seconds=settings.clickhouse_operation_timeout_seconds,
        )
        await migrate_clickhouse(engine, client)
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        closers = (
            (("clickhouse", client.aclose), ("postgres", engine.dispose))
            if client is not None
            else (("postgres", engine.dispose),)
        )
        for resource, close in closers:
            try:
                await close()
            except BaseException as error:
                if active_error is None and cleanup_error is None:
                    cleanup_error = error
                else:
                    logger.error(
                        "clickhouse_migration_cleanup_failed",
                        extra={
                            "event": "clickhouse_migration_cleanup_failed",
                            "resource": resource,
                            "error_type": type(error).__name__,
                        },
                    )
        if active_error is None and cleanup_error is not None:
            raise cleanup_error


def main(
    argv: Sequence[str] | None = None,
    *,
    run_command: RunCommand = run,
) -> int:
    parser = argparse.ArgumentParser(description="Apply forward-only ClickHouse migrations")
    try:
        parser.parse_args(argv)
        settings = Settings()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except ValidationError:
        print("invalid configuration", file=sys.stderr)
        return 2

    try:
        asyncio.run(_invoke(run_command, settings))
    except KeyboardInterrupt:
        return 130
    except (ClickHouseError, ClickHouseMigrationError) as error:
        print(f"clickhouse_migration_failed code={error.code}", file=sys.stderr)
        return 1

    print("clickhouse_migrations=applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
