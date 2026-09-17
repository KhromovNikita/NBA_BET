from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

import pytest

from nba_live_totals.application.lineups import (
    CurrentLineupSnapshotReference,
    LineupAttemptState,
    LineupBuildClaim,
    LineupVerificationStatus,
)
from nba_live_totals.domain.lineups import LineupReconstructionResult
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from tests import runtime_config
from tests.unit.infrastructure.clickhouse.test_lineup_snapshot_store import (
    GAME_ID,
    RecordingClient,
    make_writing_result,
)

pytestmark = pytest.mark.integration

_SNAPSHOT_IDS = (
    UUID("20000000-0000-4000-8000-000000000001"),
    UUID("20000000-0000-4000-8000-000000000002"),
    UUID("20000000-0000-4000-8000-000000000003"),
    UUID("20000000-0000-4000-8000-000000000004"),
    UUID("20000000-0000-4000-8000-000000000005"),
)


@pytest.fixture
async def client() -> AsyncIterator[ClickHouseClient]:
    value = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30.0
    )
    possession_count = await _possession_count(value)
    await _clear_owned_lineup_rows(value)
    try:
        assert await value.query(
            "SELECT name FROM system.tables WHERE database = {database:String} "
            "AND name = {table:String}",
            parameters={"database": "nba", "table": "lineup_segments_v1"},
        ) == [{"name": "lineup_segments_v1"}]
        yield value
    finally:
        await _clear_owned_lineup_rows(value)
        assert await _possession_count(value) == possession_count
        await value.aclose()


async def _possession_count(client: ClickHouseClient) -> int:
    rows = await client.query("SELECT count() AS count FROM nba.possessions_v1")
    count = rows[0]["count"]
    assert type(count) is int
    return count


async def _clear_owned_lineup_rows(client: ClickHouseClient) -> None:
    for snapshot_id in _SNAPSHOT_IDS:
        await client.execute(
            "ALTER TABLE nba.lineup_segments_v1 DELETE "
            f"WHERE game_id = '{GAME_ID.value}' AND snapshot_id = '{snapshot_id}' "
            "SETTINGS mutations_sync = 2"
        )


async def _serialized_rows(
    snapshot_id: UUID,
) -> tuple[LineupBuildClaim, LineupReconstructionResult, list[dict[str, object]]]:
    claim, result = make_writing_result()
    claim = replace(claim, attempt=replace(claim.attempt, snapshot_id=snapshot_id))
    recorder = RecordingClient()
    await ClickHouseLineupSnapshotStore(recorder).write_snapshot(claim, result)
    return claim, result, recorder.inserts[0][1]


async def test_final_readback_retains_old_published_and_orphan_lineup_ids(
    client: ClickHouseClient,
) -> None:
    old_claim, old_result, _ = await _serialized_rows(_SNAPSHOT_IDS[0])
    orphan_claim, _, _ = await _serialized_rows(_SNAPSHOT_IDS[1])
    old_store = ClickHouseLineupSnapshotStore(client)
    orphan_store = ClickHouseLineupSnapshotStore(client)

    await old_store.write_snapshot(old_claim, old_result)
    await orphan_store.write_snapshot(orphan_claim, make_writing_result()[1])

    published = replace(old_claim.attempt, state=LineupAttemptState.PUBLISHED)
    assert published.expected_row_count is not None
    assert published.expected_output_sha256 is not None
    reference = CurrentLineupSnapshotReference(
        published, published.expected_row_count, published.expected_output_sha256
    )
    verification = await orphan_store.verify_snapshot(orphan_claim.attempt)

    assert await old_store.read_snapshot(reference) == old_result.segments
    assert verification.status is LineupVerificationStatus.EXACT


@pytest.mark.parametrize(
    ("snapshot_id", "version_change", "expected"),
    [
        (_SNAPSHOT_IDS[2], 0, LineupVerificationStatus.EXACT),
        (_SNAPSHOT_IDS[3], -1, LineupVerificationStatus.EXACT),
        (_SNAPSHOT_IDS[4], 1, LineupVerificationStatus.DIFFERENT),
    ],
)
async def test_final_readback_classifies_replacing_merge_versions(
    client: ClickHouseClient,
    snapshot_id: UUID,
    version_change: int,
    expected: LineupVerificationStatus,
) -> None:
    claim, result, rows = await _serialized_rows(snapshot_id)
    store = ClickHouseLineupSnapshotStore(client)

    await store.write_snapshot(claim, result)
    replacement = [dict(row) for row in rows]
    for row in replacement:
        row["insert_version"] = claim.attempt.generation + version_change
    if version_change > 0:
        replacement[0]["end_seconds_remaining_period_ms"] = 699_999
    await client.insert_json_each_row("nba.lineup_segments_v1", replacement)

    verification = await store.verify_snapshot(claim.attempt)

    assert verification.status is expected
