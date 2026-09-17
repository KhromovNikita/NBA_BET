from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from nba_live_totals.application.lineups import (
    CurrentLineupSnapshotReference,
    GameLineupInputSnapshot,
    LineupAttemptState,
    LineupBuildClaim,
    LineupInputToken,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    lineup_output_checksum,
    lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupPlayerBox,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    TeamLineupState,
)
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseError
from nba_live_totals.infrastructure.clickhouse.lineups import (
    ClickHouseLineupSnapshotStore,
    LineupSnapshotStoreError,
)

GAME_ID = GameId("0022500123")
VERSIONS = LineupVersions("nba-pbp-v3-lineup-v1", "causal-lineup-v1", "nba-5x5-v1", "lineup-v1")


def make_game() -> GameContext:
    return GameContext(
        GAME_ID,
        SeasonId("2025-26"),
        SeasonType.REGULAR_SEASON,
        date(2026, 1, 2),
        GameStatus.FINAL,
        10,
        20,
        110,
        108,
    )


def make_document(resource: str) -> SourceDocument:
    payload: JsonObject = {"game": {"gameId": GAME_ID.value}, "resource": resource}
    return SourceDocument(
        source="nba_stats",
        endpoint="boxscoretraditionalv3" if resource == "box_score" else "playbyplayv3",
        resource_key=f"{resource}:{GAME_ID.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_snapshot() -> GameLineupInputSnapshot:
    game = make_game()
    boxes = (
        LineupPlayerBox(GAME_ID, 10, 101, "G", True, True, None),
        LineupPlayerBox(GAME_ID, 20, 201, "G", True, True, None),
    )
    events = (
        PlayByPlayEvent(
            GAME_ID,
            1,
            1,
            None,
            1,
            "PT12M00.00S",
            720_000,
            None,
            None,
            "period",
            "start",
            "fixture",
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            False,
            0,
            False,
        ),
    )
    box_document = make_document("box_score")
    pbp_document = make_document("play_by_play")
    return GameLineupInputSnapshot(
        game,
        UUID("00000000-0000-4000-8000-000000000001"),
        box_document.checksum_sha256,
        UUID("00000000-0000-4000-8000-000000000002"),
        pbp_document.checksum_sha256,
        boxes,
        events,
        canonical_player_boxes_checksum(boxes),
        canonical_event_checksum(events),
        game_context_checksum(game),
        box_document,
        pbp_document,
    )


def make_result(claim: LineupBuildClaim, *, invalid: bool = False) -> LineupReconstructionResult:
    token = claim.attempt.input_token
    context = LineupReconstructionContext(
        claim.attempt.game,
        token.versions,
        token.applied_box_score_sha256,
        token.applied_pbp_sha256,
        token.canonical_player_box_sha256,
        token.canonical_event_sha256,
        token.game_context_sha256,
        claim.attempt.lineup_revision_id,
    )
    home = (
        TeamLineupState(
            LineupStatus.INVALID,
            frozenset(),
            "invalid_initial_lineup",
            EventBoundary(1, BoundarySide.BEFORE),
        )
        if invalid
        else TeamLineupState(LineupStatus.VALID, frozenset({101, 102, 103, 104, 105}), None, None)
    )
    away = TeamLineupState(LineupStatus.VALID, frozenset({201, 202, 203, 204, 205}), None, None)
    segment = LineupSegment(
        1,
        EventBoundary(1, BoundarySide.BEFORE),
        EventBoundary(2, BoundarySide.AFTER),
        1,
        1,
        720_000,
        700_000,
        home,
        away,
    )
    issues = (LineupIssue(10, "invalid_initial_lineup", 1, BoundarySide.BEFORE),) if invalid else ()
    return LineupReconstructionResult(
        context, LineupStatus.INVALID if invalid else LineupStatus.VALID, (segment,), issues
    )


def make_claim(*, invalid: bool = False) -> LineupBuildClaim:
    snapshot = make_snapshot()
    token = LineupInputToken.from_snapshot(snapshot, VERSIONS)
    revision = lineup_revision_id(
        game_id=GAME_ID,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        versions=VERSIONS,
    )
    provisional = LineupSnapshotAttempt(
        snapshot.game,
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        7,
        revision,
        token,
        LineupAttemptState.ALLOCATED,
    )
    claim = LineupBuildClaim(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), 1, provisional, snapshot)
    result = make_result(claim, invalid=invalid)
    attempt = replace(
        provisional,
        state=LineupAttemptState.WRITING,
        expected_row_count=1,
        expected_output_sha256=lineup_output_checksum(result),
        expected_issue_codes=result.issues,
        result_status=result.status,
    )
    return replace(claim, attempt=attempt)


def make_reference(claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
    attempt = replace(claim.attempt, state=LineupAttemptState.PUBLISHED)
    assert attempt.expected_row_count is not None and attempt.expected_output_sha256 is not None
    return CurrentLineupSnapshotReference(
        attempt, attempt.expected_row_count, attempt.expected_output_sha256
    )


class RecordingClient:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.inserts: list[tuple[str, list[dict[str, object]]]] = []
        self.queries: list[tuple[str, Mapping[str, str | int] | None]] = []
        self.insert_error: ClickHouseError | None = None
        self.query_error: ClickHouseError | None = None

    async def insert_json_each_row(self, table: str, rows: Sequence[Mapping[str, object]]) -> None:
        if self.insert_error is not None:
            raise self.insert_error
        self.inserts.append((table, [dict(row) for row in rows]))

    async def query(
        self, statement: str, *, parameters: Mapping[str, str | int] | None = None
    ) -> list[dict[str, object]]:
        if self.query_error is not None:
            raise self.query_error
        self.queries.append((statement, parameters))
        return self.rows


def make_writing_result(
    *, invalid: bool = False
) -> tuple[LineupBuildClaim, LineupReconstructionResult]:
    claim = make_claim(invalid=invalid)
    return claim, make_result(claim, invalid=invalid)


async def written_rows(
    *, invalid: bool = False
) -> tuple[LineupBuildClaim, LineupReconstructionResult, list[dict[str, object]]]:
    claim, result = make_writing_result(invalid=invalid)
    client = RecordingClient()
    await ClickHouseLineupSnapshotStore(client).write_snapshot(claim, result)
    return claim, result, client.inserts[0][1]


def make_many_segment_result() -> tuple[LineupBuildClaim, LineupReconstructionResult]:
    claim, initial = make_writing_result(invalid=True)
    away = TeamLineupState(
        LineupStatus.INVALID,
        frozenset(),
        "invalid_initial_lineup",
        EventBoundary(1, BoundarySide.BEFORE),
    )
    segments = tuple(
        replace(
            initial.segments[0],
            segment_number=number,
            start_boundary=EventBoundary(
                number, BoundarySide.BEFORE if number == 1 else BoundarySide.AFTER
            ),
            end_boundary=EventBoundary(
                number + 1, BoundarySide.AFTER if number == 34 else BoundarySide.BEFORE
            ),
            home_state=replace(
                initial.segments[0].home_state,
                invalid_since=EventBoundary(
                    number, BoundarySide.BEFORE if number == 1 else BoundarySide.AFTER
                ),
            ),
            away_state=away,
        )
        for number in range(1, 35)
    )
    issues = (
        LineupIssue(10, "invalid_initial_lineup", 1, BoundarySide.BEFORE),
        LineupIssue(20, "invalid_initial_lineup", 1, BoundarySide.BEFORE),
        *(LineupIssue(10, "invalid_initial_lineup", n, BoundarySide.AFTER) for n in range(2, 32)),
    )
    result = replace(initial, segments=segments, issues=issues)
    return replace(
        claim,
        attempt=replace(
            claim.attempt,
            expected_row_count=34,
            expected_output_sha256=lineup_output_checksum(result),
            expected_issue_codes=issues,
        ),
    ), result


async def test_readback_caps_distinct_issues_in_segment_home_away_order() -> None:
    claim, result = make_many_segment_result()
    client = RecordingClient()
    store = ClickHouseLineupSnapshotStore(client)
    await store.write_snapshot(claim, result)
    client.rows = client.inserts[0][1]

    verification = await store.verify_snapshot(claim.attempt)
    assert verification.status is LineupVerificationStatus.EXACT
    assert verification.issues == result.issues
    assert len(verification.issues) == 32
    assert await store.read_snapshot(make_reference(claim)) == result.segments


async def test_successful_write_rejects_repeat_without_second_insert() -> None:
    claim, result = make_writing_result()
    client = RecordingClient()
    store = ClickHouseLineupSnapshotStore(client)
    await store.write_snapshot(claim, result)
    with pytest.raises(LineupSnapshotStoreError, match="already_sent"):
        await store.write_snapshot(claim, result)
    assert len(client.inserts) == 1


async def test_partial_multisegment_prefix_is_different() -> None:
    claim, result = make_many_segment_result()
    client = RecordingClient()
    store = ClickHouseLineupSnapshotStore(client)
    await store.write_snapshot(claim, result)
    client.rows = client.inserts[0][1][:2]
    assert (await store.verify_snapshot(claim.attempt)).status is LineupVerificationStatus.DIFFERENT


async def test_write_serializes_one_ordered_block_with_complete_metadata() -> None:
    claim, result = make_writing_result()
    client = RecordingClient()

    await ClickHouseLineupSnapshotStore(client).write_snapshot(claim, result)

    assert len(client.inserts) == 1
    table, rows = client.inserts[0]
    assert table == "nba.lineup_segments_v1"
    assert [row["segment_number"] for row in rows] == [1]
    row = rows[0]
    assert list(row) == [
        "snapshot_id",
        "insert_version",
        "lineup_revision_id",
        "normalizer_version",
        "reconstructor_version",
        "rules_version",
        "input_box_score_document_id",
        "input_box_score_sha256",
        "input_pbp_document_id",
        "input_pbp_sha256",
        "canonical_player_box_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
        "output_schema_version",
        "output_sha256",
        "result_status",
        "game_id",
        "season",
        "season_start_year",
        "season_type",
        "game_date",
        "home_team_id",
        "away_team_id",
        "segment_number",
        "start_sequence_number",
        "start_boundary_side",
        "end_sequence_number",
        "end_boundary_side",
        "start_period",
        "end_period",
        "start_seconds_remaining_period_ms",
        "end_seconds_remaining_period_ms",
        "home_status",
        "home_player_ids",
        "home_invalid_reason",
        "home_invalid_since_sequence_number",
        "home_invalid_since_boundary_side",
        "away_status",
        "away_player_ids",
        "away_invalid_reason",
        "away_invalid_since_sequence_number",
        "away_invalid_since_boundary_side",
        "is_valid",
    ]
    assert row["snapshot_id"] == str(claim.attempt.snapshot_id)
    assert row["lineup_revision_id"] == claim.attempt.lineup_revision_id
    assert row["output_sha256"] == lineup_output_checksum(result)
    assert row["result_status"] == "valid"
    assert row["home_player_ids"] == [101, 102, 103, 104, 105]
    assert row["away_player_ids"] == [201, 202, 203, 204, 205]
    assert row["home_invalid_reason"] == ""
    assert row["home_invalid_since_sequence_number"] == 0
    assert row["home_invalid_since_boundary_side"] == "none"
    assert row["is_valid"] == 1


async def test_write_serializes_invalid_side_with_empty_array_and_safe_since_metadata() -> None:
    claim, result = make_writing_result(invalid=True)
    client = RecordingClient()

    await ClickHouseLineupSnapshotStore(client).write_snapshot(claim, result)

    row = client.inserts[0][1][0]
    assert row["result_status"] == "invalid"
    assert row["home_status"] == "invalid"
    assert row["home_player_ids"] == []
    assert row["home_invalid_reason"] == "invalid_initial_lineup"
    assert row["home_invalid_since_sequence_number"] == 1
    assert row["home_invalid_since_boundary_side"] == "before"
    assert row["away_status"] == "valid"
    assert row["away_invalid_reason"] == ""
    assert row["is_valid"] == 0


async def test_write_marks_id_sent_before_uncertain_transport_and_never_resends() -> None:
    claim, result = make_writing_result()
    client = RecordingClient()
    client.insert_error = ClickHouseError("clickhouse_timeout")
    store = ClickHouseLineupSnapshotStore(client)

    with pytest.raises(LineupSnapshotStoreError, match="unavailable"):
        await store.write_snapshot(claim, result)
    with pytest.raises(LineupSnapshotStoreError, match="already_sent"):
        await store.write_snapshot(claim, result)
    assert client.inserts == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("exact", LineupVerificationStatus.EXACT),
        ("absent", LineupVerificationStatus.ABSENT),
        ("extra", LineupVerificationStatus.DIFFERENT),
        ("gap", LineupVerificationStatus.DIFFERENT),
        ("overlap", LineupVerificationStatus.DIFFERENT),
        ("bad_metadata", LineupVerificationStatus.DIFFERENT),
        ("bad_status", LineupVerificationStatus.DIFFERENT),
        ("bad_order", LineupVerificationStatus.DIFFERENT),
        ("bad_cardinality", LineupVerificationStatus.DIFFERENT),
        ("bad_sentinel", LineupVerificationStatus.DIFFERENT),
        ("bool_as_int", LineupVerificationStatus.DIFFERENT),
        ("hash_drift", LineupVerificationStatus.DIFFERENT),
    ],
)
async def test_verify_fails_closed_for_non_exact_final_readback(
    change: str, expected: LineupVerificationStatus
) -> None:
    claim, result, rows = await written_rows()
    assert result.status is LineupStatus.VALID
    if change == "absent":
        rows = []
    elif change == "extra":
        rows.append(dict(rows[0]))
    elif change == "gap":
        rows.append(
            {
                **rows[0],
                "segment_number": 2,
                "start_sequence_number": 4,
                "start_boundary_side": "after",
                "end_sequence_number": 5,
            }
        )
    elif change == "overlap":
        rows.append(
            {
                **rows[0],
                "segment_number": 2,
                "start_sequence_number": 1,
                "start_boundary_side": "after",
            }
        )
    elif change == "bad_metadata":
        rows[0]["game_id"] = "0022500124"
    elif change == "bad_status":
        rows[0]["result_status"] = "invalid"
    elif change == "bad_order":
        rows[0]["home_player_ids"] = [102, 101, 103, 104, 105]
    elif change == "bad_cardinality":
        rows[0]["away_player_ids"] = [201, 202, 203, 204]
    elif change == "bad_sentinel":
        rows[0]["home_invalid_since_boundary_side"] = "before"
    elif change == "bool_as_int":
        rows[0]["is_valid"] = True
    elif change == "hash_drift":
        rows[0]["end_seconds_remaining_period_ms"] = 699_999

    client = RecordingClient(rows)
    verification = await ClickHouseLineupSnapshotStore(client).verify_snapshot(claim.attempt)

    assert verification.status is expected
    if expected is LineupVerificationStatus.EXACT:
        assert verification.row_count == 1
        assert verification.output_sha256 == lineup_output_checksum(result)
        assert verification.result_status is LineupStatus.VALID
        assert verification.issues == ()
    elif expected is LineupVerificationStatus.ABSENT:
        assert verification.row_count is None
    else:
        assert verification.row_count is None
    statement, parameters = client.queries[0]
    assert "FINAL" in statement and "ORDER BY segment_number" in statement
    assert "max(" not in statement.casefold()
    assert parameters == {
        "season_start_year": 2025,
        "game_id": "0022500123",
        "snapshot_id": str(claim.attempt.snapshot_id),
    }


async def test_verify_reports_unavailable_without_semantic_metadata() -> None:
    claim, _ = make_writing_result()
    client = RecordingClient()
    client.query_error = ClickHouseError("clickhouse_unavailable")

    verification = await ClickHouseLineupSnapshotStore(client).verify_snapshot(claim.attempt)

    assert verification == LineupSnapshotVerification(
        claim.attempt.snapshot_id, LineupVerificationStatus.UNAVAILABLE
    )


async def test_read_reconstructs_segments_and_rejects_wrong_declared_issue_projection() -> None:
    claim, result, rows = await written_rows(invalid=True)
    reference = make_reference(claim)
    store = ClickHouseLineupSnapshotStore(RecordingClient(rows))

    assert await store.read_snapshot(reference) == result.segments

    rows[0]["home_invalid_reason"] = "wrong_reason"
    with pytest.raises(LineupSnapshotStoreError, match="mismatch"):
        await ClickHouseLineupSnapshotStore(RecordingClient(rows)).read_snapshot(reference)


async def test_write_rejects_semantic_result_that_does_not_match_expected_attempt_metadata() -> (
    None
):
    claim, result = make_writing_result()
    mismatched = replace(claim, attempt=replace(claim.attempt, expected_row_count=2))

    with pytest.raises(LineupSnapshotStoreError, match="expected_output_mismatch"):
        await ClickHouseLineupSnapshotStore(RecordingClient()).write_snapshot(mismatched, result)


async def test_write_rejects_non_writing_attempt() -> None:
    claim, result = make_writing_result()
    wrong_state = replace(claim, attempt=replace(claim.attempt, state=LineupAttemptState.VERIFIED))

    with pytest.raises(LineupSnapshotStoreError, match="write_not_writing"):
        await ClickHouseLineupSnapshotStore(RecordingClient()).write_snapshot(wrong_state, result)
