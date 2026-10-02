"""Contracts for immutable two-table ClickHouse feature snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureInputToken,
    FeatureVerificationStatus,
)
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureStateKind,
    GameStateFeature,
    LineupAggregateFeatures,
    LiveFeatures,
    PlayerPregameFeatures,
    TeamPregameFeatures,
)
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    FeatureSnapshotStoreError,
)

from .test_feature_source_reader import (
    claimed_history_fixture,
    target_fixture,
)

STATE_TABLE = "nba.game_state_features_v1"
PLAYER_TABLE = "nba.player_pregame_features_v1"


def make_dataset() -> FeatureDataset:
    home_ids = (101, 102, 103, 104, 105)
    away_ids = (201, 202, 203, 204, 205)
    home_pregame = TeamPregameFeatures(
        10,
        True,
        games_used_5=5,
        games_used_10=8,
        games_used_20=8,
        off_rating_5=1_100_001,
        off_rating_10=1_090_002,
        off_rating_20=None,
        rest_days=2,
    )
    away_pregame = replace(home_pregame, team_id=20, is_home=False)
    home_lineup = LineupAggregateFeatures(10, home_ids, 5, lineup_avg_off_rating=1_111_001)
    away_lineup = LineupAggregateFeatures(20, away_ids, 5, lineup_avg_off_rating=999_002)
    game_id, _ = target_fixture()[0].game.game_id, home_ids
    states = (
        GameStateFeature(
            game_id,
            0,
            FeatureStateKind.PREGAME,
            0,
            EventBoundary(1, BoundarySide.BEFORE),
            home_player_ids=home_ids,
            away_player_ids=away_ids,
            seconds_remaining_game_ms=2_880_000,
            seconds_remaining_period_ms=720_000,
            home_pregame=home_pregame,
            away_pregame=away_pregame,
            home_lineup=home_lineup,
            away_lineup=away_lineup,
            live=LiveFeatures(),
            offense_difference=112,
        ),
        GameStateFeature(
            game_id,
            1,
            FeatureStateKind.AFTER_POSSESSION,
            1,
            EventBoundary(3, BoundarySide.AFTER),
            home_lineup_status=LineupStatus.INVALID,
            away_lineup_status=LineupStatus.VALID,
            away_player_ids=away_ids,
            home_invalid_reason="unknown_substitution",
            home_invalid_since=EventBoundary(2, BoundarySide.AFTER),
            period=1,
            seconds_remaining_game_ms=2_860_000,
            seconds_remaining_period_ms=700_000,
            home_score=2,
            score_diff=2,
            home_possessions=1,
            home_pregame=home_pregame,
            away_pregame=away_pregame,
            home_lineup=LineupAggregateFeatures(10, ()),
            away_lineup=away_lineup,
            live=LiveFeatures(current_pace=1_000_000),
        ),
    )
    players = (
        PlayerPregameFeatures(
            game_id,
            201,
            20,
            False,
            games_used=7,
            seconds_played=12_345,
            off_rating=1_050_000,
        ),
        PlayerPregameFeatures(
            game_id,
            101,
            10,
            True,
            games_used=8,
            seconds_played=13_579,
            off_rating=1_100_000,
        ),
    )
    return FeatureDataset(game_id, states, players, ("source_quality",))


class MemoryClient:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, object]]] = {
            STATE_TABLE: [],
            PLAYER_TABLE: [],
        }
        self.insert_calls: list[str] = []
        self.query_calls: list[tuple[str, Mapping[str, str | int] | None]] = []
        self.fail_queries = False
        self.fail_after_persist: str | None = None
        self.fail_before_persist: str | None = None

    async def insert_json_each_row(self, table: str, rows: Sequence[Mapping[str, object]]) -> None:
        self.insert_calls.append(table)
        if self.fail_before_persist == table:
            raise ClickHouseError("clickhouse_unavailable")
        self.rows[table].extend(dict(row) for row in rows)
        if self.fail_after_persist == table:
            raise ClickHouseError("clickhouse_timeout")

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.query_calls.append((statement, parameters))
        if self.fail_queries:
            raise ClickHouseError("clickhouse_unavailable")
        table = PLAYER_TABLE if "player_pregame_features_v1" in statement else STATE_TABLE
        snapshot_id = None if parameters is None else parameters["snapshot_id"]
        return [dict(row) for row in self.rows[table] if row["snapshot_id"] == snapshot_id]


def snapshot_fixture(
    dataset: FeatureDataset | None = None,
) -> tuple[FeatureBuildClaim, FeatureDataset]:
    value = make_dataset() if dataset is None else dataset
    target, _, _ = target_fixture()
    allocated, _ = claimed_history_fixture(target)
    source = replace(
        allocated.source,
        target_player_ids=tuple(row.player_id for row in value.players),
    )
    token = FeatureInputToken.from_source(source, allocated.attempt.input_token.versions)
    attempt = replace(
        allocated.attempt,
        feature_revision_id=token.feature_revision_id,
        input_token=token,
        state=FeatureAttemptState.WRITING,
        expected_state_row_count=len(value.states),
        expected_state_output_sha256=state_output_checksum(value.states),
        expected_player_row_count=len(value.players),
        expected_player_output_sha256=player_output_checksum(value.players),
        expected_feature_output_sha256=feature_output_checksum(value),
        expected_issue_codes=value.issues,
        result_status=value.status,
        insert_started_at=allocated.attempt.created_at,
    )
    return FeatureBuildClaim(allocated.run_id, allocated.claim_version, attempt, source), value


async def test_write_serializes_both_tables_exactly_in_canonical_order() -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()

    await ClickHouseFeatureSnapshotStore(client).write_snapshot(claim, dataset)

    assert client.insert_calls == [STATE_TABLE, PLAYER_TABLE]
    state_rows = client.rows[STATE_TABLE]
    player_rows = client.rows[PLAYER_TABLE]
    assert [row["state_number"] for row in state_rows] == [0, 1]
    assert [row["player_id"] for row in player_rows] == [101, 201]
    assert all(row["snapshot_id"] == str(claim.attempt.snapshot_id) for row in state_rows)
    assert all(
        row["feature_output_sha256"] == feature_output_checksum(dataset) for row in state_rows
    )
    assert state_rows[0]["home_player_ids"] == [101, 102, 103, 104, 105]
    assert state_rows[0]["home_invalid_reason"] == ""
    assert state_rows[0]["home_invalid_since_sequence_number"] == 0
    assert state_rows[0]["home_invalid_since_boundary_side"] == "none"
    assert state_rows[1]["home_player_ids"] == []
    assert state_rows[1]["home_invalid_reason"] == "unknown_substitution"
    assert state_rows[1]["home_invalid_since_sequence_number"] == 2
    assert state_rows[1]["home_invalid_since_boundary_side"] == "after"
    assert [row["is_valid"] for row in state_rows] == [1, 0]
    assert state_rows[0]["home_back_to_back"] == 0
    assert state_rows[0]["home_off_rating_20"] is None
    assert player_rows[0]["is_home"] == 1
    assert player_rows[1]["is_home"] == 0
    assert player_rows[0]["history_cutoff_started_at"] == "2026-01-02 01:00:00.000"


async def test_verify_and_read_reconstruct_exact_two_table_dataset() -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    store = ClickHouseFeatureSnapshotStore(client)
    await store.write_snapshot(claim, dataset)

    verification = await store.verify_snapshot(claim.attempt)

    assert verification.status is FeatureVerificationStatus.EXACT
    assert verification.state_row_count == len(dataset.states)
    assert verification.player_row_count == len(dataset.players)
    assert verification.feature_output_sha256 == feature_output_checksum(dataset)
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


async def test_verify_classifies_absent_unavailable_and_corruption() -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    store = ClickHouseFeatureSnapshotStore(client)
    assert (await store.verify_snapshot(claim.attempt)).status is FeatureVerificationStatus.ABSENT

    client.fail_queries = True
    assert (
        await store.verify_snapshot(claim.attempt)
    ).status is FeatureVerificationStatus.UNAVAILABLE
    client.fail_queries = False
    await store.write_snapshot(claim, dataset)
    client.rows[STATE_TABLE][0]["is_valid"] = 2
    assert (
        await store.verify_snapshot(claim.attempt)
    ).status is FeatureVerificationStatus.DIFFERENT


@pytest.mark.parametrize(
    ("table", "persisted", "expected_code"),
    [
        (STATE_TABLE, False, "feature_snapshot_partial_write"),
        (STATE_TABLE, True, "feature_snapshot_partial_write"),
        (PLAYER_TABLE, False, "feature_snapshot_partial_write"),
    ],
)
async def test_uncertain_partial_write_is_never_resent(
    table: str, persisted: bool, expected_code: str
) -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    client.fail_after_persist = table if persisted else None
    client.fail_before_persist = table if not persisted else None
    store = ClickHouseFeatureSnapshotStore(client)

    with pytest.raises(FeatureSnapshotStoreError, match=expected_code):
        await store.write_snapshot(claim, dataset)
    calls = list(client.insert_calls)
    with pytest.raises(FeatureSnapshotStoreError, match="feature_snapshot_already_sent"):
        await store.write_snapshot(claim, dataset)
    assert client.insert_calls == calls


async def test_lost_player_response_recovers_by_exact_readback_without_resend() -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    client.fail_after_persist = PLAYER_TABLE
    store = ClickHouseFeatureSnapshotStore(client)

    await store.write_snapshot(claim, dataset)

    assert client.insert_calls == [STATE_TABLE, PLAYER_TABLE]
    with pytest.raises(FeatureSnapshotStoreError, match="feature_snapshot_already_sent"):
        await store.write_snapshot(claim, dataset)
    assert client.insert_calls == [STATE_TABLE, PLAYER_TABLE]


async def test_zero_expected_players_skips_empty_insert_and_is_exact() -> None:
    dataset = replace(make_dataset(), players=())
    claim, dataset = snapshot_fixture(dataset)
    client = MemoryClient()
    store = ClickHouseFeatureSnapshotStore(client)

    await store.write_snapshot(claim, dataset)

    assert client.insert_calls == [STATE_TABLE]
    assert (await store.verify_snapshot(claim.attempt)).status is FeatureVerificationStatus.EXACT


@pytest.mark.parametrize(
    ("table", "row_index", "field", "value"),
    [
        (STATE_TABLE, 0, "snapshot_id", "ffffffff-ffff-4fff-8fff-ffffffffffff"),
        (STATE_TABLE, 0, "home_back_to_back", 2),
        (STATE_TABLE, 0, "feature_output_sha256", "0" * 64),
        (STATE_TABLE, 1, "state_number", 7),
        (STATE_TABLE, 1, "home_player_ids", [999]),
        (PLAYER_TABLE, 0, "is_home", 2),
        (PLAYER_TABLE, 0, "history_cutoff_started_at", "2026-01-02 01:00:00.001"),
        (PLAYER_TABLE, 0, "player_output_sha256", "0" * 64),
    ],
)
async def test_verify_rejects_metadata_sentinel_order_and_hash_drift(
    table: str, row_index: int, field: str, value: object
) -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    store = ClickHouseFeatureSnapshotStore(client)
    await store.write_snapshot(claim, dataset)
    client.rows[table][row_index][field] = value

    assert (
        await store.verify_snapshot(claim.attempt)
    ).status is FeatureVerificationStatus.DIFFERENT


@pytest.mark.parametrize("kind", ["duplicate", "partial", "extra_column"])
async def test_verify_rejects_duplicate_partial_and_projection_drift(kind: str) -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    store = ClickHouseFeatureSnapshotStore(client)
    await store.write_snapshot(claim, dataset)
    if kind == "duplicate":
        client.rows[STATE_TABLE].append(dict(client.rows[STATE_TABLE][0]))
    elif kind == "partial":
        client.rows[PLAYER_TABLE].pop()
    else:
        client.rows[STATE_TABLE][0]["unexpected"] = 1

    assert (
        await store.verify_snapshot(claim.attempt)
    ).status is FeatureVerificationStatus.DIFFERENT


async def test_write_rejects_output_or_player_universe_drift_before_transport() -> None:
    claim, dataset = snapshot_fixture()
    client = MemoryClient()
    source = replace(claim.source, target_player_ids=(999,))
    token = FeatureInputToken.from_source(source, claim.attempt.input_token.versions)
    attempt = replace(
        claim.attempt,
        input_token=token,
        feature_revision_id=token.feature_revision_id,
    )
    bad_claim = FeatureBuildClaim(claim.run_id, claim.claim_version, attempt, source)

    with pytest.raises(FeatureSnapshotStoreError, match="feature_snapshot_lineage_mismatch"):
        await ClickHouseFeatureSnapshotStore(client).write_snapshot(bad_claim, dataset)
    assert client.insert_calls == []


def test_error_contract_is_bounded() -> None:
    error = FeatureSnapshotStoreError("feature_snapshot_partial_write")
    assert error.code == "feature_snapshot_partial_write"
    assert str(error) == "feature_snapshot_partial_write"
    assert ClickHouseFeatureSnapshotStore.__name__ == "ClickHouseFeatureSnapshotStore"
