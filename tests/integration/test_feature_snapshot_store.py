"""Two-table feature snapshots against an isolated real ClickHouse."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureVerificationStatus,
)
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import ClickHouseFeatureSnapshotStore
from tests import runtime_config
from tests.unit.infrastructure.clickhouse.test_feature_snapshot_store import (
    PLAYER_TABLE,
    STATE_TABLE,
    MemoryClient,
    snapshot_fixture,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def client() -> AsyncIterator[ClickHouseClient]:
    value = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(),
        user="nba",
        password="nba",
        timeout_seconds=30.0,
    )
    claim, _ = snapshot_fixture()
    snapshot_ids = (
        str(claim.attempt.snapshot_id),
        "ffffffff-ffff-4fff-8fff-ffffffffffff",
    )
    try:
        for table in (STATE_TABLE, PLAYER_TABLE):
            for snapshot_id in snapshot_ids:
                await value.execute(
                    f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{snapshot_id}' "
                    "SETTINGS mutations_sync = 2"
                )
        yield value
    finally:
        for table in (STATE_TABLE, PLAYER_TABLE):
            for snapshot_id in snapshot_ids:
                await value.execute(
                    f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{snapshot_id}' "
                    "SETTINGS mutations_sync = 2"
                )
        await value.aclose()


async def test_round_trips_exact_two_table_snapshot_and_ignores_orphan(
    client: ClickHouseClient,
) -> None:
    claim, dataset = snapshot_fixture()
    store = ClickHouseFeatureSnapshotStore(client)

    await store.write_snapshot(claim, dataset)
    verification = await store.verify_snapshot(claim.attempt)

    assert verification.status is FeatureVerificationStatus.EXACT
    published = replace(
        claim.attempt,
        state=FeatureAttemptState.PUBLISHED,
        verified_at=claim.attempt.insert_started_at,
        published_at=claim.attempt.insert_started_at,
    )
    reference = CurrentFeatureSnapshotReference(
        published,
        len(dataset.states),
        state_output_checksum(dataset.states),
        len(dataset.players),
        player_output_checksum(dataset.players),
        feature_output_checksum(dataset),
    )
    assert await store.read_snapshot(reference) == dataset

    memory = MemoryClient()
    await ClickHouseFeatureSnapshotStore(memory).write_snapshot(claim, dataset)
    orphan = [
        dict(row)
        | {
            "snapshot_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "insert_version": 999,
        }
        for row in memory.rows[STATE_TABLE]
    ]
    await client.insert_json_each_row(STATE_TABLE, orphan)
    assert (await store.verify_snapshot(claim.attempt)).status is FeatureVerificationStatus.EXACT


async def test_state_only_partial_snapshot_is_different(client: ClickHouseClient) -> None:
    claim, dataset = snapshot_fixture()
    memory = MemoryClient()
    await ClickHouseFeatureSnapshotStore(memory).write_snapshot(claim, dataset)
    await client.insert_json_each_row(STATE_TABLE, memory.rows[STATE_TABLE])

    verification = await ClickHouseFeatureSnapshotStore(client).verify_snapshot(claim.attempt)

    assert verification.status is FeatureVerificationStatus.DIFFERENT
