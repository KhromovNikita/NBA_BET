"""Task 10 causal feature claim contracts against real PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import sys
from asyncio import gather
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Table, func, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.application.features import FeatureClaimAction
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
    lineup_rebuild_resources,
    possession_rebuild_resources,
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


def store(harness: FeatureHarness, *, elapsed: int = 0) -> PostgresFeatureWorkStore:
    return PostgresFeatureWorkStore(
        harness.engine,
        possession_versions=POSSESSION_VERSIONS,
        lineup_versions=LINEUP_VERSIONS,
        source=harness.source,
        utc_now=FeatureHarness.now(elapsed=elapsed),
    )


async def start_run(value: PostgresFeatureWorkStore, harness: FeatureHarness) -> UUID:
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)
    return run_id


async def test_first_claim_persists_atomic_attempt_and_empty_manifest(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await start_run(value, harness)

    decision = await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)

    assert decision.action is FeatureClaimAction.BUILD
    assert decision.claim is not None
    claim = decision.claim
    assert claim.source.target == target
    assert claim.source.target_player_ids == ()
    assert claim.source.manifest.dependencies == ()
    async with harness.engine.connect() as connection:
        resource = (
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
        attempt = (
            (
                await connection.execute(
                    select(feature_snapshot_attempts).where(
                        feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .one()
        )
        dependencies = (
            (
                await connection.execute(
                    select(feature_snapshot_dependencies).where(
                        feature_snapshot_dependencies.c.snapshot_id == claim.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert resource["active_snapshot_id"] == claim.attempt.snapshot_id
    assert attempt["state"] == "allocated"
    assert dependencies == []


async def test_same_run_resumes_other_run_is_busy_and_expired_claim_is_superseded(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    initial = store(harness)
    target = await initial.resolve_target(GAME_ID)
    first_run = await start_run(initial, harness)
    first = await initial.claim_game(first_run, target, (), FEATURE_VERSIONS, rebuild=False)
    resumed = await initial.claim_game(first_run, target, (), FEATURE_VERSIONS, rebuild=False)
    other_run = await start_run(initial, harness)
    busy = await initial.claim_game(other_run, target, (), FEATURE_VERSIONS, rebuild=False)
    expired_store = store(harness, elapsed=301)
    replacement = await expired_store.claim_game(
        other_run, target, (), FEATURE_VERSIONS, rebuild=False
    )

    assert first.action is FeatureClaimAction.BUILD
    assert resumed.action is FeatureClaimAction.RESUME
    assert busy.action is FeatureClaimAction.BUSY
    assert replacement.action is FeatureClaimAction.BUILD
    assert first.claim is not None and replacement.claim is not None
    assert replacement.claim.attempt.snapshot_id != first.claim.attempt.snapshot_id
    async with harness.engine.connect() as connection:
        states = tuple(
            await connection.scalars(
                select(feature_snapshot_attempts.c.state)
                .where(feature_snapshot_attempts.c.game_id == GAME_ID.value)
                .order_by(feature_snapshot_attempts.c.generation)
            )
        )
    assert states == ("superseded", "allocated")


async def test_claim_builds_strict_causal_union_manifest_and_persists_target_players(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    older = await harness.seed_history(
        game_id="0022500101", started_at=BASE_TIME - timedelta(days=2)
    )
    newer = await harness.seed_history(
        game_id="0022500102", started_at=BASE_TIME - timedelta(days=1)
    )
    await harness.seed_history(game_id="0022500103", started_at=BASE_TIME)
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await start_run(value, harness)

    decision = await value.claim_game(
        run_id,
        target,
        (930001, 930002, 939999),
        FEATURE_VERSIONS,
        rebuild=False,
    )

    assert decision.claim is not None
    claim = decision.claim
    dependencies = claim.source.manifest.dependencies
    assert tuple(row.dependency_game_id for row in dependencies) == (older, newer)
    assert tuple(row.ordinal for row in dependencies) == (0, 1)
    assert all(row.started_at < target.game.started_at for row in dependencies)
    assert all(
        row.inclusion_reasons == ("away_team_window", "home_team_window", "player_history")
        for row in dependencies
    )
    assert claim.source.target_player_ids == (930001, 930002, 939999)
    assert len(claim.source.historical_team_boxes) == 4
    assert len(claim.source.historical_player_boxes) == 4
    assert len(claim.source.historical_possession_sources) == 2
    async with harness.engine.connect() as connection:
        durable = (
            (
                await connection.execute(
                    select(feature_snapshot_dependencies)
                    .where(feature_snapshot_dependencies.c.snapshot_id == claim.attempt.snapshot_id)
                    .order_by(feature_snapshot_dependencies.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
    assert [row["inclusion_reasons"] for row in durable] == [7, 7]


@pytest.mark.parametrize("invalid", [False, True])
async def test_exact_published_snapshot_skips_unless_rebuild_is_forced(
    harness: FeatureHarness, *, invalid: bool
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    first_run = await start_run(value, harness)
    first = await value.claim_game(first_run, target, (), FEATURE_VERSIONS, rebuild=False)
    assert first.claim is not None
    await harness.publish_feature(first.claim, invalid=invalid)
    second_run = await start_run(value, harness)

    skipped = await value.claim_game(second_run, target, (), FEATURE_VERSIONS, rebuild=False)
    rebuilt = await value.claim_game(second_run, target, (), FEATURE_VERSIONS, rebuild=True)

    assert skipped.action is FeatureClaimAction.SKIP_CURRENT
    assert skipped.current is not None
    assert skipped.current.attempt.snapshot_id == first.claim.attempt.snapshot_id
    assert skipped.current.result_status.value == ("invalid" if invalid else "valid")
    assert rebuilt.action is FeatureClaimAction.BUILD
    assert rebuilt.claim is not None
    assert rebuilt.claim.attempt.snapshot_id != first.claim.attempt.snapshot_id


async def test_stale_published_feature_pointer_is_retained_and_rebuilt(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    first_run = await start_run(value, harness)
    first = await value.claim_game(first_run, target, (), FEATURE_VERSIONS, rebuild=False)
    assert first.claim is not None
    await harness.publish_feature(first.claim)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_build_resources)
            .where(feature_build_resources.c.game_id == GAME_ID.value)
            .values(published_feature_input_token_sha256="0" * 64)
        )
    second_run = await start_run(value, harness)

    replacement = await value.claim_game(second_run, target, (), FEATURE_VERSIONS, rebuild=False)

    assert replacement.action is FeatureClaimAction.BUILD
    assert replacement.claim is not None
    async with harness.engine.connect() as connection:
        resource = (
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
    assert resource["publication_state"] == "stale"
    assert resource["published_snapshot_id"] == first.claim.attempt.snapshot_id


async def test_published_attempt_source_field_drift_is_classified_stale(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    first_run = await start_run(value, harness)
    first = await value.claim_game(first_run, target, (), FEATURE_VERSIONS, rebuild=False)
    assert first.claim is not None
    await harness.publish_feature(first.claim)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_snapshot_attempts)
            .where(feature_snapshot_attempts.c.snapshot_id == first.claim.attempt.snapshot_id)
            .values(possession_output_sha256="0" * 64)
        )
    second_run = await start_run(value, harness)

    replacement = await value.claim_game(second_run, target, (), FEATURE_VERSIONS, rebuild=False)

    assert replacement.action is FeatureClaimAction.BUILD


@pytest.mark.parametrize(
    ("resource_table", "expected_error"),
    [
        (possession_rebuild_resources, "target_possession_stale"),
        (lineup_rebuild_resources, "target_lineup_stale"),
    ],
)
async def test_claim_rechecks_target_sources_before_allocating(
    harness: FeatureHarness, resource_table: Table, expected_error: str
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await start_run(value, harness)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(resource_table)
            .where(resource_table.c.game_id == GAME_ID.value)
            .values(published_input_token_sha256="0" * 64)
        )

    with pytest.raises(RuntimeError, match=expected_error):
        await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)

    async with harness.engine.connect() as connection:
        resource = await connection.scalar(
            select(feature_build_resources.c.game_id).where(
                feature_build_resources.c.game_id == GAME_ID.value
            )
        )
    assert resource is None


async def test_same_run_resumes_writing_and_verified_attempts(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await start_run(value, harness)
    first = await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)
    assert first.claim is not None
    snapshot_id = first.claim.attempt.snapshot_id
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_snapshot_attempts)
            .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
            .values(
                state="writing",
                expected_state_row_count=1,
                expected_state_output_sha256="d" * 64,
                expected_player_row_count=0,
                expected_player_output_sha256="e" * 64,
                expected_feature_output_sha256="f" * 64,
                expected_issue_codes=[],
                result_status="valid",
                insert_started_at=BASE_TIME,
            )
        )
    writing = await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(feature_snapshot_attempts)
            .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
            .values(state="verified", verified_at=BASE_TIME)
        )
    verified = await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)

    assert writing.action is FeatureClaimAction.RESUME
    assert writing.claim is not None
    assert writing.claim.attempt.state.value == "writing"
    assert verified.action is FeatureClaimAction.RESUME
    assert verified.claim is not None
    assert verified.claim.attempt.state.value == "verified"


async def test_simultaneous_claims_allocate_once(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    runs = (await start_run(value, harness), await start_run(value, harness))

    decisions = await gather(
        *(value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False) for run_id in runs)
    )

    assert sorted(decision.action.value for decision in decisions) == ["build", "busy"]
    async with harness.engine.connect() as connection:
        attempt_count = await connection.scalar(
            select(func.count())
            .select_from(feature_snapshot_attempts)
            .where(feature_snapshot_attempts.c.game_id == GAME_ID.value)
        )
    assert attempt_count == 1


async def test_claim_counter_exhaustion_does_not_allocate(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await start_run(value, harness)
    maximum = 9_223_372_036_854_775_807
    async with harness.engine.begin() as connection:
        await connection.execute(
            feature_build_resources.insert().values(
                game_id=GAME_ID.value,
                attempt_count=maximum,
                claim_version=maximum,
                next_generation=maximum,
            )
        )

    with pytest.raises(RuntimeError, match="feature claim counter exhausted"):
        await value.claim_game(run_id, target, (), FEATURE_VERSIONS, rebuild=False)

    async with harness.engine.connect() as connection:
        attempt_count = await connection.scalar(
            select(func.count())
            .select_from(feature_snapshot_attempts)
            .where(feature_snapshot_attempts.c.game_id == GAME_ID.value)
        )
    assert attempt_count == 0
