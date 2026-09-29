"""Read-only current feature pointer resolution over real PostgreSQL state."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeaturePublicationState,
    FeatureSnapshotVerification,
    FeatureTargetSnapshot,
    FeatureVerificationStatus,
)
from nba_live_totals.domain.features import FeatureResultStatus
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_snapshot_dependencies,
    team_box_scores,
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
from tests.integration.possession_harness import BASE_TIME

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


async def publish(
    harness: FeatureHarness, *, invalid: bool = False
) -> tuple[FeatureTargetSnapshot, CurrentFeatureSnapshotReference]:
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)
    decision = await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)
    assert decision.claim is not None
    claim = decision.claim
    issues = ("bad_feature",) if invalid else ()
    status = FeatureResultStatus.INVALID if invalid else FeatureResultStatus.VALID
    await value.mark_writing(
        claim,
        expected_state_row_count=1,
        expected_state_output_sha256="1" * 64,
        expected_player_row_count=0,
        expected_player_output_sha256="2" * 64,
        expected_feature_output_sha256="3" * 64,
        expected_issue_codes=issues,
        result_status=status,
    )
    await value.mark_verified(
        claim,
        FeatureSnapshotVerification(
            claim.attempt.snapshot_id,
            FeatureVerificationStatus.EXACT,
            1,
            "1" * 64,
            0,
            "2" * 64,
            "3" * 64,
            status,
            issues,
        ),
    )
    return target, await value.publish_snapshot(claim)


async def resource_row(harness: FeatureHarness) -> dict[str, object]:
    async with harness.engine.connect() as connection:
        return dict((await connection.execute(select(feature_build_resources))).mappings().one())


async def test_resolve_current_returns_absent_without_creating_resource(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)

    resolution = await value.resolve_current(target, (), FEATURE_VERSIONS)

    assert resolution == CurrentFeatureSnapshotResolution(FeaturePublicationState.ABSENT)
    async with harness.engine.connect() as connection:
        assert await connection.scalar(select(feature_build_resources.c.game_id)) is None


@pytest.mark.parametrize("invalid", [False, True])
async def test_resolve_current_returns_exact_pointer_without_mutation(
    harness: FeatureHarness, *, invalid: bool
) -> None:
    await harness.seed_published_target()
    target, published = await publish(harness, invalid=invalid)
    before = await resource_row(harness)

    resolution = await store(harness).resolve_current(target, (), FEATURE_VERSIONS)

    assert resolution.status is (
        FeaturePublicationState.INVALID if invalid else FeaturePublicationState.CURRENT
    )
    assert resolution.reference == published
    assert await resource_row(harness) == before


async def test_resolve_current_returns_stale_for_source_or_pointer_drift(
    harness: FeatureHarness,
) -> None:
    target_id = await harness.seed_published_target()
    target, _ = await publish(harness)
    await harness.replace_target_pointer(target_id, "possession")
    before = await resource_row(harness)

    resolution = await store(harness).resolve_current(target, (), FEATURE_VERSIONS)

    assert (resolution.status, resolution.reference) == (
        FeaturePublicationState.STALE,
        None,
    )
    assert await resource_row(harness) == before


async def test_resolve_current_fails_closed_for_corrupt_pointer_metadata(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    target, _ = await publish(harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_build_resources)
            .where(feature_build_resources.c.game_id == GAME_ID.value)
            .values(published_feature_input_token_sha256="0" * 64)
        )
    before = await resource_row(harness)

    resolution = await store(harness).resolve_current(target, (), FEATURE_VERSIONS)

    assert (resolution.status, resolution.reference) == (
        FeaturePublicationState.STALE,
        None,
    )
    assert await resource_row(harness) == before


async def test_resolve_current_rechecks_dependency_identity(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    history = await harness.seed_history(
        game_id="0022500101", started_at=BASE_TIME - timedelta(days=1)
    )
    target, _ = await publish(harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(team_box_scores)
            .where(
                team_box_scores.c.game_id == history.value,
                team_box_scores.c.team_id == 910001,
            )
            .values(points=111)
        )

    resolution = await store(harness).resolve_current(target, (), FEATURE_VERSIONS)

    assert (resolution.status, resolution.reference) == (
        FeaturePublicationState.STALE,
        None,
    )


async def test_resolve_current_fails_closed_for_corrupt_dependency_rows(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    await harness.seed_history(game_id="0022500101", started_at=BASE_TIME - timedelta(days=1))
    target, published = await publish(harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_snapshot_dependencies)
            .where(feature_snapshot_dependencies.c.snapshot_id == published.attempt.snapshot_id)
            .values(canonical_team_box_sha256="0" * 64)
        )

    resolution = await store(harness).resolve_current(target, (), FEATURE_VERSIONS)

    assert (resolution.status, resolution.reference) == (
        FeaturePublicationState.STALE,
        None,
    )
