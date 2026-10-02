"""Exact feature-source reads against a real isolated ClickHouse."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import ClickHouseFeatureSourceReader
from tests import runtime_config
from tests.unit.infrastructure.clickhouse.test_feature_source_reader import (
    claimed_history_fixture,
    target_fixture,
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
    target, _, _ = target_fixture()
    cleanup_targets = (
        ("nba.possessions_v1", target.possession_reference.attempt.snapshot_id),
        ("nba.possessions_v1", "88888888-8888-4888-8888-888888888888"),
        ("nba.possessions_v1", "ffffffff-ffff-4fff-8fff-ffffffffffff"),
        ("nba.lineup_segments_v1", target.lineup_reference.attempt.snapshot_id),
    )
    try:
        for table, snapshot_id in cleanup_targets:
            await value.execute(
                f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{snapshot_id}' "
                "SETTINGS mutations_sync = 2"
            )
        yield value
    finally:
        for table, snapshot_id in cleanup_targets:
            await value.execute(
                f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{snapshot_id}' "
                "SETTINGS mutations_sync = 2"
            )
        await value.aclose()


async def test_reads_exact_target_rows_from_both_analytical_tables(
    client: ClickHouseClient,
) -> None:
    target, possession_rows, lineup_rows = target_fixture()
    await client.insert_json_each_row("nba.possessions_v1", possession_rows)
    await client.insert_json_each_row("nba.lineup_segments_v1", lineup_rows)

    result = await ClickHouseFeatureSourceReader(client).read_target(target)

    assert len(result.possessions) == target.possession_reference.row_count
    assert len(result.lineup_segments) == target.lineup_reference.row_count
    assert result.target_player_ids == (
        101,
        102,
        103,
        104,
        105,
        201,
        202,
        203,
        204,
        205,
    )


async def test_reads_only_manifest_bound_history_and_ignores_newer_orphan(
    client: ClickHouseClient,
) -> None:
    target, possession_rows, lineup_rows = target_fixture()
    claim, history_rows = claimed_history_fixture(target)
    orphan_rows = [
        dict(row)
        | {
            "snapshot_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "insert_version": 999,
        }
        for row in possession_rows
    ]
    await client.insert_json_each_row(
        "nba.possessions_v1", possession_rows + history_rows + orphan_rows
    )
    await client.insert_json_each_row("nba.lineup_segments_v1", lineup_rows)

    inputs = await ClickHouseFeatureSourceReader(client).read_inputs(claim)

    assert len(inputs.possessions) == 2
    assert len(inputs.history_possessions) == 2
    assert {row.game_id.value for row in inputs.history_possessions} == {"0022500122"}
    assert inputs.historical_team_boxes == claim.source.historical_team_boxes
    assert inputs.historical_player_boxes == claim.source.historical_player_boxes
