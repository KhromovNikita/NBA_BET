from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

import pytest

from nba_live_totals.application.possessions import (
    CurrentSnapshotReference,
    PossessionAttemptState,
    PossessionBuildClaim,
    VerificationStatus,
)
from nba_live_totals.domain import PossessionParseResult
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from tests import runtime_config
from tests.unit.infrastructure.clickhouse.test_possession_snapshot_store import (
    GAME_ID,
    RecordingClient,
    make_claim,
    make_result,
)

pytestmark = pytest.mark.integration

_SNAPSHOT_IDS = (
    UUID("10000000-0000-4000-8000-000000000001"),
    UUID("10000000-0000-4000-8000-000000000002"),
    UUID("10000000-0000-4000-8000-000000000003"),
    UUID("10000000-0000-4000-8000-000000000004"),
    UUID("10000000-0000-4000-8000-000000000005"),
)


@pytest.fixture
async def client() -> AsyncIterator[ClickHouseClient]:
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(),
        user="nba",
        password="nba",
        timeout_seconds=30.0,
    )
    await _clear_rows(client)
    try:
        assert await client.query(
            "SELECT name FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String}",
            parameters={"database": "nba", "table": "possessions_v1"},
        ) == [{"name": "possessions_v1"}]
        yield client
    finally:
        await _clear_rows(client)
        await client.aclose()


async def _clear_rows(client: ClickHouseClient) -> None:
    for snapshot_id in _SNAPSHOT_IDS:
        await client.execute(
            "ALTER TABLE nba.possessions_v1 DELETE "
            f"WHERE game_id = '{GAME_ID.value}' AND snapshot_id = '{snapshot_id}' "
            "SETTINGS mutations_sync = 2"
        )


async def _serialized_rows(
    snapshot_id: UUID,
    generation: int,
) -> tuple[PossessionBuildClaim, PossessionParseResult, list[dict[str, object]]]:
    claim = make_claim(snapshot_id=snapshot_id, generation=generation)
    result = make_result(claim)
    recorder = RecordingClient()
    await PossessionSnapshotStore(recorder).write_snapshot(claim, result)
    return claim, result, recorder.inserts[0][1]


async def test_exact_id_readback_retains_old_and_orphan_snapshots_without_merge_waiting(
    client: ClickHouseClient,
) -> None:
    old_claim, old_result, _ = await _serialized_rows(_SNAPSHOT_IDS[0], 7)
    orphan_claim, orphan_result, _ = await _serialized_rows(_SNAPSHOT_IDS[1], 8)
    assert old_result.output_sha256 is not None
    assert orphan_result.output_sha256 is not None
    old_store = PossessionSnapshotStore(client)
    orphan_store = PossessionSnapshotStore(client)

    await old_store.write_snapshot(old_claim, old_result)
    await orphan_store.write_snapshot(orphan_claim, orphan_result)

    old_attempt = replace(
        old_claim.attempt,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=len(old_result.possessions),
        expected_output_sha256=old_result.output_sha256,
    )
    old_reference = CurrentSnapshotReference(
        attempt=old_attempt,
        row_count=len(old_result.possessions),
        output_sha256=old_result.output_sha256,
    )
    verified_orphan = await orphan_store.verify_snapshot(
        replace(
            orphan_claim.attempt,
            state=PossessionAttemptState.VERIFIED,
            expected_row_count=len(orphan_result.possessions),
            expected_output_sha256=orphan_result.output_sha256,
        )
    )

    assert await old_store.read_snapshot(old_reference) == old_result.possessions
    assert verified_orphan.status is VerificationStatus.EXACT
    assert verified_orphan.row_count == len(orphan_result.possessions)


@pytest.mark.parametrize(
    ("snapshot_id", "version_change", "expected_status"),
    [
        (_SNAPSHOT_IDS[2], 0, VerificationStatus.EXACT),
        (_SNAPSHOT_IDS[3], -1, VerificationStatus.EXACT),
        (_SNAPSHOT_IDS[4], 1, VerificationStatus.MISMATCH),
    ],
)
async def test_final_readback_classifies_equal_lower_and_higher_duplicate_versions(
    client: ClickHouseClient,
    snapshot_id: UUID,
    version_change: int,
    expected_status: VerificationStatus,
) -> None:
    generation = 30
    claim, result, rows = await _serialized_rows(snapshot_id, generation)
    assert result.output_sha256 is not None
    store = PossessionSnapshotStore(client)

    await store.write_snapshot(claim, result)
    duplicate_rows = [dict(row) for row in rows]
    for row in duplicate_rows:
        row["insert_version"] = generation + version_change
    if version_change:
        duplicate_rows[0]["offense_points"] = 1
    await client.insert_json_each_row("nba.possessions_v1", duplicate_rows)

    verification = await store.verify_snapshot(
        replace(
            claim.attempt,
            state=PossessionAttemptState.VERIFIED,
            expected_row_count=len(result.possessions),
            expected_output_sha256=result.output_sha256,
        )
    )

    assert verification.status is expected_status
    if expected_status is VerificationStatus.EXACT:
        assert verification.output_sha256 == result.output_sha256
