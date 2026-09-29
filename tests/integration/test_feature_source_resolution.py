"""Task 10 exact target-source resolution against real PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import Table, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.domain import JsonObject, canonical_json_checksum
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    ingestion_resources,
    lineup_rebuild_resources,
    possession_rebuild_resources,
    source_documents,
)
from tests import runtime_config
from tests.integration.feature_harness import (
    GAME_ID,
    LINEUP_VERSIONS,
    POSSESSION_VERSIONS,
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


async def test_resolve_target_uses_applied_pbp_and_accepts_invalid_lineup(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target(invalid_lineup=True)
    latest_id = uuid4()
    latest_payload: JsonObject = {"game_id": GAME_ID.value, "raw": "unapplied-v2"}
    async with harness.engine.begin() as connection:
        await connection.execute(
            source_documents.insert().values(
                document_id=latest_id,
                source=harness.source,
                endpoint="playbyplayv3",
                resource_key=f"play_by_play:{GAME_ID.value}",
                request_parameters={"game_id": GAME_ID.value},
                payload=latest_payload,
                checksum_sha256=canonical_json_checksum(latest_payload),
                requested_at=BASE_TIME,
            )
        )
        await connection.execute(
            update(ingestion_resources)
            .where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_type == "play_by_play",
            )
            .values(latest_document_id=latest_id)
        )

    target = await store(harness).resolve_target(GAME_ID)

    assert target.game.game_id == GAME_ID
    assert target.pbp_snapshot.applied_pbp_document_id != latest_id
    assert target.lineup_reference.result_status.value == "invalid"
    assert target.possession_reference.attempt.input_token.pbp_lineage == (
        target.lineup_reference.attempt.input_token.pbp_lineage
    )


@pytest.mark.parametrize("source", ["possession", "lineup"])
async def test_resolve_target_recomputes_freshness_instead_of_trusting_resource_label(
    harness: FeatureHarness, source: str
) -> None:
    await harness.seed_published_target()
    table = {
        "possession": "possession_snapshot_attempts",
        "lineup": "lineup_snapshot_attempts",
    }[source]
    version_column = {
        "possession": "normalizer_version",
        "lineup": "reconstructor_version",
    }[source]
    async with harness.engine.begin() as connection:
        await connection.exec_driver_sql(
            f"UPDATE {table} SET {version_column} = 'stale-v1' WHERE game_id = $1",
            (GAME_ID.value,),
        )

    with pytest.raises(RuntimeError, match=f"target_{source}_stale"):
        await store(harness).resolve_target(GAME_ID)


@pytest.mark.parametrize(
    ("resource_table", "expected_error"),
    [
        (possession_rebuild_resources, "target_possession_absent"),
        (lineup_rebuild_resources, "target_lineup_absent"),
    ],
)
async def test_resolve_target_rejects_absent_published_pointer(
    harness: FeatureHarness, resource_table: Table, expected_error: str
) -> None:
    await harness.seed_published_target()
    prefix = "published_parse_revision_id"
    if resource_table is lineup_rebuild_resources:
        prefix = "published_lineup_revision_id"
    async with harness.engine.begin() as connection:
        await connection.execute(
            update(resource_table)
            .where(resource_table.c.game_id == GAME_ID.value)
            .values(
                publication_state="absent",
                published_snapshot_id=None,
                **{
                    prefix: None,
                    "published_input_token_sha256": None,
                    "published_at": None,
                },
            )
        )

    with pytest.raises(RuntimeError, match=expected_error):
        await store(harness).resolve_target(GAME_ID)


async def test_resolve_target_maps_corrupt_applied_pbp_to_safe_lineage_error(
    harness: FeatureHarness,
) -> None:
    await harness.seed_published_target()
    async with harness.engine.begin() as connection:
        document_id = await connection.scalar(
            select(ingestion_resources.c.applied_document_id).where(
                ingestion_resources.c.source == harness.source,
                ingestion_resources.c.resource_type == "play_by_play",
                ingestion_resources.c.resource_key == f"play_by_play:{GAME_ID.value}",
            )
        )
        assert document_id is not None
        await connection.execute(
            update(source_documents)
            .where(source_documents.c.document_id == document_id)
            .values(payload={"game_id": GAME_ID.value, "raw": "corrupt"})
        )

    with pytest.raises(RuntimeError, match="source_lineage_mismatch"):
        await store(harness).resolve_target(GAME_ID)
