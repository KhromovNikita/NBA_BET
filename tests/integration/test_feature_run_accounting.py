"""Durable feature run accounting and unclaimed-failure contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.application.features import BuildDatasetResult, FeatureBuildStatus
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
)
from tests import runtime_config
from tests.integration.feature_harness import (
    FEATURE_VERSIONS,
    GAME_ID,
    LINEUP_VERSIONS,
    POSSESSION_VERSIONS,
    REQUEST,
    FeatureHarness,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def harness() -> AsyncIterator[FeatureHarness]:
    engine = create_async_engine(runtime_config.postgres_url())
    value = FeatureHarness(engine)
    try:
        yield value
    finally:
        await value.cleanup()
        await engine.dispose()


def store(harness: FeatureHarness) -> PostgresFeatureWorkStore:
    return PostgresFeatureWorkStore(
        harness.engine,
        possession_versions=POSSESSION_VERSIONS,
        lineup_versions=LINEUP_VERSIONS,
        source=harness.source,
        utc_now=FeatureHarness.now(),
    )


@pytest.mark.parametrize(
    ("status", "counts"),
    [
        (FeatureBuildStatus.SUCCEEDED, (1, 1, 0, 0, 0, 0, 0)),
        (FeatureBuildStatus.PARTIAL, (1, 1, 1, 0, 0, 0, 0)),
        (FeatureBuildStatus.PARTIAL, (2, 1, 0, 0, 0, 1, 0)),
        (FeatureBuildStatus.PARTIAL, (2, 1, 0, 0, 0, 0, 1)),
        (FeatureBuildStatus.FAILED, (1, 0, 0, 0, 0, 1, 0)),
        (FeatureBuildStatus.INTERRUPTED, (1, 0, 0, 0, 0, 1, 0)),
    ],
)
async def test_finish_run_persists_exact_terminal_accounting(
    harness: FeatureHarness,
    status: FeatureBuildStatus,
    counts: tuple[int, int, int, int, int, int, int],
) -> None:
    value = store(harness)
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)
    result = BuildDatasetResult(run_id, status, *counts)

    await value.finish_run(run_id, result)

    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(feature_build_runs).where(feature_build_runs.c.run_id == run_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == status.value
    assert (
        tuple(
            row[name]
            for name in (
                "discovered",
                "materialized",
                "invalid",
                "skipped_current",
                "verified",
                "failed",
                "busy",
            )
        )
        == counts
    )
    assert row["finished_at"] is not None
    with pytest.raises(RuntimeError, match="already terminal"):
        await value.finish_run(run_id, result)


async def test_unclaimed_failure_never_overwrites_live_owner(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    owner = await value.start_run(REQUEST)
    reporter = await value.start_run(REQUEST)
    harness.feature_run_ids.extend((owner, reporter))
    decision = await value.claim_game(owner, target, (), FEATURE_VERSIONS, rebuild=False)
    assert decision.claim is not None

    await value.record_unclaimed_failure(
        reporter,
        GAME_ID,
        error_code="source_unavailable",
        error_type="resolve",
    )

    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(feature_build_resources).where(
                        feature_build_resources.c.game_id == GAME_ID.value
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["active_run_id"] == owner
    assert row["active_snapshot_id"] == decision.claim.attempt.snapshot_id
    assert row["last_error_category"] is None
    assert row["last_error_type"] is None


async def test_unclaimed_failure_records_safe_error_without_changing_pointer(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)

    await value.record_unclaimed_failure(
        run_id,
        GAME_ID,
        error_code="source_unavailable",
        error_type="resolve",
    )

    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(feature_build_resources).where(
                        feature_build_resources.c.game_id == GAME_ID.value
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["publication_state"] == "absent"
    assert row["published_snapshot_id"] is None
    assert row["last_run_id"] == run_id
    assert row["last_error_category"] == "source_unavailable"
    assert row["last_error_type"] == "resolve"
