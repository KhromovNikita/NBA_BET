"""Real PostgreSQL contracts for fenced feature attempt transitions."""

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
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureSnapshotVerification,
    FeatureVerificationStatus,
)
from nba_live_totals.domain.features import FeatureResultStatus
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_snapshot_attempts,
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


def store(harness: FeatureHarness, *, elapsed: int = 0) -> PostgresFeatureWorkStore:
    return PostgresFeatureWorkStore(
        harness.engine,
        possession_versions=POSSESSION_VERSIONS,
        lineup_versions=LINEUP_VERSIONS,
        source=harness.source,
        lease_seconds=300,
        utc_now=lambda: BASE_TIME + timedelta(seconds=elapsed),
    )


async def claim_feature(
    value: PostgresFeatureWorkStore,
    harness: FeatureHarness,
    *,
    rebuild: bool = False,
) -> FeatureBuildClaim:
    target = await value.resolve_target(GAME_ID)
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)
    decision = await value.claim_game(
        run_id, target, (930001, 930002), FEATURE_VERSIONS, rebuild=rebuild
    )
    assert decision.claim is not None
    return decision.claim


async def advance_to_verified(value: PostgresFeatureWorkStore, claim: FeatureBuildClaim) -> None:
    await value.mark_writing(
        claim,
        expected_state_row_count=3,
        expected_state_output_sha256="1" * 64,
        expected_player_row_count=2,
        expected_player_output_sha256="2" * 64,
        expected_feature_output_sha256="3" * 64,
        expected_issue_codes=(),
        result_status=FeatureResultStatus.VALID,
    )
    await value.mark_verified(
        claim,
        FeatureSnapshotVerification(
            claim.attempt.snapshot_id,
            FeatureVerificationStatus.EXACT,
            3,
            "1" * 64,
            2,
            "2" * 64,
            "3" * 64,
            FeatureResultStatus.VALID,
            (),
        ),
    )


async def test_lifecycle_persists_exact_metadata_and_dependency_rows(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target(invalid_lineup=True)
    await harness.seed_history(game_id="0022500101", started_at=BASE_TIME - timedelta(days=1))
    value = store(harness)
    target = await value.resolve_target(GAME_ID)
    run_id = await value.start_run(REQUEST)
    harness.feature_run_ids.append(run_id)
    decision = await value.claim_game(
        run_id, target, (930001, 930002), FEATURE_VERSIONS, rebuild=False
    )
    assert decision.claim is not None
    claim = decision.claim
    async with harness.engine.connect() as connection:
        dependencies_before = (
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

    writing = await value.mark_writing(
        claim,
        expected_state_row_count=3,
        expected_state_output_sha256="1" * 64,
        expected_player_row_count=2,
        expected_player_output_sha256="2" * 64,
        expected_feature_output_sha256="3" * 64,
        expected_issue_codes=("invalid_lineup",),
        result_status=FeatureResultStatus.INVALID,
    )
    verification = FeatureSnapshotVerification(
        claim.attempt.snapshot_id,
        FeatureVerificationStatus.EXACT,
        3,
        "1" * 64,
        2,
        "2" * 64,
        "3" * 64,
        FeatureResultStatus.INVALID,
        ("invalid_lineup",),
    )
    verified = await value.mark_verified(claim, verification)
    published = await value.publish_snapshot(claim)

    assert writing.state is FeatureAttemptState.WRITING
    assert verified.state is FeatureAttemptState.VERIFIED
    assert published.attempt.state is FeatureAttemptState.PUBLISHED
    assert published.state_row_count == 3
    assert published.player_row_count == 2
    assert published.feature_output_sha256 == "3" * 64
    assert published.result_status is FeatureResultStatus.INVALID
    assert published.issues == ("invalid_lineup",)
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
        dependencies_after = (
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
    assert resource["publication_state"] == "invalid"
    assert resource["published_snapshot_id"] == claim.attempt.snapshot_id
    assert resource["active_snapshot_id"] is None
    assert attempt["state"] == "published"
    assert dependencies_after == dependencies_before


@pytest.mark.parametrize("source", ["possession", "lineup"])
async def test_publish_rechecks_target_pointer_and_preserves_previous_pointer(
    harness: FeatureHarness, source: str
) -> None:
    target = await harness.seed_published_target()
    value = store(harness)
    old = await claim_feature(value, harness)
    await advance_to_verified(value, old)
    old_reference = await value.publish_snapshot(old)
    candidate = await claim_feature(value, harness, rebuild=True)
    await advance_to_verified(value, candidate)

    await harness.replace_target_pointer(target, source)

    with pytest.raises(RuntimeError, match="source changed"):
        await value.publish_snapshot(candidate)
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
        attempt_state = await connection.scalar(
            select(feature_snapshot_attempts.c.state).where(
                feature_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id
            )
        )
    assert resource["published_snapshot_id"] == old_reference.attempt.snapshot_id
    assert resource["active_snapshot_id"] is None
    assert attempt_state == "superseded"


async def test_publish_rechecks_dependency_manifest_and_preserves_previous_pointer(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    history = await harness.seed_history(
        game_id="0022500101", started_at=BASE_TIME - timedelta(days=1)
    )
    value = store(harness)
    old = await claim_feature(value, harness)
    await advance_to_verified(value, old)
    old_reference = await value.publish_snapshot(old)
    candidate = await claim_feature(value, harness, rebuild=True)
    await advance_to_verified(value, candidate)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(team_box_scores)
            .where(
                team_box_scores.c.game_id == history.value,
                team_box_scores.c.team_id == 910001,
            )
            .values(points=111)
        )

    with pytest.raises(RuntimeError, match="source changed"):
        await value.publish_snapshot(candidate)

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
    assert resource["published_snapshot_id"] == old_reference.attempt.snapshot_id
    assert resource["active_snapshot_id"] is None


async def test_unrelated_equal_time_game_does_not_block_publication(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    unrelated = await harness.seed_history(game_id="0022500103", started_at=BASE_TIME)
    value = store(harness)
    candidate = await claim_feature(value, harness)
    await advance_to_verified(value, candidate)
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(team_box_scores)
            .where(
                team_box_scores.c.game_id == unrelated.value,
                team_box_scores.c.team_id == 910001,
            )
            .values(points=111)
        )

    published = await value.publish_snapshot(candidate)

    assert published.attempt.snapshot_id == candidate.attempt.snapshot_id


@pytest.mark.parametrize(
    "operation", ["mark_writing", "mark_verified", "publish", "poison", "fail"]
)
async def test_expired_lease_rejects_every_mutation_without_changes(
    harness: FeatureHarness, operation: str
) -> None:
    await harness.seed_published_target()
    live = store(harness)
    candidate = await claim_feature(live, harness)
    verification = FeatureSnapshotVerification(
        candidate.attempt.snapshot_id,
        FeatureVerificationStatus.EXACT,
        3,
        "1" * 64,
        2,
        "2" * 64,
        "3" * 64,
        FeatureResultStatus.VALID,
        (),
    )
    if operation in {"mark_verified", "publish"}:
        await live.mark_writing(
            candidate,
            expected_state_row_count=3,
            expected_state_output_sha256="1" * 64,
            expected_player_row_count=2,
            expected_player_output_sha256="2" * 64,
            expected_feature_output_sha256="3" * 64,
            expected_issue_codes=(),
            result_status=FeatureResultStatus.VALID,
        )
    if operation == "publish":
        await live.mark_verified(candidate, verification)
    async with harness.engine.connect() as connection:
        resource_before = dict(
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
        attempt_before = dict(
            (
                await connection.execute(
                    select(feature_snapshot_attempts).where(
                        feature_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .one()
        )
    expired = store(harness, elapsed=301)

    with pytest.raises(RuntimeError, match="fence"):
        if operation == "mark_writing":
            await expired.mark_writing(
                candidate,
                expected_state_row_count=3,
                expected_state_output_sha256="1" * 64,
                expected_player_row_count=2,
                expected_player_output_sha256="2" * 64,
                expected_feature_output_sha256="3" * 64,
                expected_issue_codes=(),
                result_status=FeatureResultStatus.VALID,
            )
        elif operation == "mark_verified":
            await expired.mark_verified(candidate, verification)
        elif operation == "publish":
            await expired.publish_snapshot(candidate)
        elif operation == "poison":
            await expired.poison_attempt(candidate, error_code="storage_error", error_type="write")
        else:
            await expired.fail_game(candidate, error_code="source_error", error_type="read")

    async with harness.engine.connect() as connection:
        resource_after = dict(
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
        attempt_after = dict(
            (
                await connection.execute(
                    select(feature_snapshot_attempts).where(
                        feature_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert resource_after == resource_before
    assert attempt_after == attempt_before


@pytest.mark.parametrize("operation", ["poison", "fail"])
async def test_terminal_failure_releases_only_candidate_and_preserves_pointer(
    harness: FeatureHarness, operation: str
) -> None:
    await harness.seed_published_target()
    value = store(harness)
    old = await claim_feature(value, harness)
    await advance_to_verified(value, old)
    old_reference = await value.publish_snapshot(old)
    candidate = await claim_feature(value, harness, rebuild=True)
    if operation == "poison":
        await value.mark_writing(
            candidate,
            expected_state_row_count=3,
            expected_state_output_sha256="1" * 64,
            expected_player_row_count=2,
            expected_player_output_sha256="2" * 64,
            expected_feature_output_sha256="3" * 64,
            expected_issue_codes=(),
            result_status=FeatureResultStatus.VALID,
        )
        await value.poison_attempt(candidate, error_code="storage_error", error_type="write")
    else:
        await value.fail_game(candidate, error_code="source_error", error_type="read")

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
                        feature_snapshot_attempts.c.snapshot_id == candidate.attempt.snapshot_id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert resource["published_snapshot_id"] == old_reference.attempt.snapshot_id
    assert resource["active_snapshot_id"] is None
    assert resource["last_error_category"] == (
        "storage_error" if operation == "poison" else "source_error"
    )
    assert attempt["state"] == "poisoned"
