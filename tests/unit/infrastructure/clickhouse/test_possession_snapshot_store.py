from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from nba_live_totals.application.possessions import (
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionAttemptState,
    PossessionBuildClaim,
    PossessionInputToken,
    PossessionSnapshotAttempt,
    VerificationStatus,
)
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    ParseStatus,
    PlayByPlayEvent,
    Possession,
    PossessionEndReason,
    PossessionParseContext,
    PossessionParseResult,
    PossessionVersions,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
    parse_revision_id,
    possession_output_checksum,
)
from nba_live_totals.infrastructure.clickhouse.possessions import (
    PossessionSnapshotStore,
    PossessionSnapshotStoreError,
)

GAME_ID = GameId("0022500123")
SNAPSHOT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DOCUMENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
VERSIONS = PossessionVersions(
    normalizer_version="nba-pbp-v1",
    parser_version="analytical-v1",
    rules_version="nba-2025-v1",
    output_schema_version="possessions-v1",
)
SHA_A = "a" * 64
SHA_B = "b" * 64


class RecordingClient:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.inserts: list[tuple[str, list[dict[str, object]]]] = []
        self.queries: list[tuple[str, Mapping[str, str | int] | None]] = []

    async def insert_json_each_row(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        self.inserts.append((table, [dict(row) for row in rows]))

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.queries.append((statement, parameters))
        return self.rows


def make_game() -> GameContext:
    return GameContext(
        game_id=GAME_ID,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=10,
        away_team_id=20,
        home_score=110,
        away_score=108,
    )


def make_possession(number: int) -> Possession:
    is_home = number % 2 == 1
    home_score_before = 2 * (number // 2)
    away_score_before = 2 * ((number - 1) // 2)
    home_score = home_score_before + (2 if is_home else 0)
    away_score = away_score_before + (0 if is_home else 2)
    return Possession(
        game_id=GAME_ID,
        possession_number=number,
        offense_team_id=10 if is_home else 20,
        defense_team_id=20 if is_home else 10,
        offense_possession_number=(number + 1) // 2,
        period=1,
        start_boundary=EventBoundary(number, BoundarySide.BEFORE),
        end_boundary=EventBoundary(number, BoundarySide.AFTER),
        start_seconds_remaining_period_ms=720_000 - number * 1_000,
        end_seconds_remaining_period_ms=719_500 - number * 1_000,
        seconds_remaining_game_ms=2_879_500 - number * 1_000,
        home_score_before=home_score_before,
        away_score_before=away_score_before,
        home_score=home_score,
        away_score=away_score,
        score_diff=home_score - away_score,
        offense_points=2,
        defense_points=0,
        home_possessions=(number + 1) // 2,
        away_possessions=number // 2,
        end_reason=PossessionEndReason.MADE_FIELD_GOAL,
        source_event_count=1,
    )


def make_claim(*, generation: int = 7, snapshot_id: UUID = SNAPSHOT_ID) -> PossessionBuildClaim:
    game = make_game()
    payload: JsonObject = {"game": {"gameId": GAME_ID.value}, "actions": []}
    document = SourceDocument(
        source="nba_stats",
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{GAME_ID.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
    events = (
        PlayByPlayEvent(
            game_id=GAME_ID,
            sequence_number=1,
            action_number=1,
            action_id=None,
            period=1,
            clock="PT12M00.00S",
            seconds_remaining_period_ms=720_000,
            team_id=None,
            person_id=None,
            action_type="period",
            sub_type="start",
            description="",
            home_score=0,
            away_score=0,
            shot_result=None,
            shot_distance=None,
            legacy_x=None,
            legacy_y=None,
            location=None,
            is_field_goal=False,
            points_total=0,
            video_available=False,
        ),
    )
    snapshot = GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=DOCUMENT_ID,
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )
    token = PossessionInputToken.from_snapshot(snapshot, VERSIONS)
    revision = parse_revision_id(
        game_id=game.game_id,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        versions=token.versions,
    )
    attempt = PossessionSnapshotAttempt(
        game=game,
        snapshot_id=snapshot_id,
        generation=generation,
        parse_revision_id=revision,
        input_token=token,
        state=PossessionAttemptState.ALLOCATED,
    )
    return PossessionBuildClaim(
        run_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        claim_version=1,
        attempt=attempt,
        snapshot=snapshot,
    )


def make_result(claim: PossessionBuildClaim, count: int = 2) -> PossessionParseResult:
    possessions = tuple(make_possession(number) for number in range(1, count + 1))
    context = PossessionParseContext(
        game=claim.attempt.game,
        versions=VERSIONS,
        pbp_document_sha256=claim.attempt.input_token.applied_pbp_sha256,
        canonical_event_sha256=claim.attempt.input_token.canonical_event_sha256,
        game_context_sha256=claim.attempt.input_token.game_context_sha256,
        parse_revision_id=claim.attempt.parse_revision_id,
    )
    return PossessionParseResult(
        context=context,
        status=ParseStatus.VALID,
        possessions=possessions,
        issues=(),
        output_sha256=possession_output_checksum(
            parse_revision_id=claim.attempt.parse_revision_id,
            game=claim.attempt.game,
            possessions=possessions,
        ),
    )


async def test_write_snapshot_sends_one_ordered_full_json_each_row_request() -> None:
    claim = make_claim()
    result = make_result(claim)
    assert result.output_sha256 is not None
    client = RecordingClient()
    store = PossessionSnapshotStore(client)

    await store.write_snapshot(claim, result)

    assert len(client.inserts) == 1
    table, rows = client.inserts[0]
    assert table == "nba.possessions_v1"
    assert [row["possession_number"] for row in rows] == [1, 2]
    assert all(list(row) == list(rows[0]) for row in rows)
    assert list(rows[0]) == [
        "snapshot_id",
        "insert_version",
        "parse_revision_id",
        "normalizer_version",
        "parser_version",
        "rules_version",
        "input_pbp_document_id",
        "input_pbp_sha256",
        "input_event_sha256",
        "game_context_sha256",
        "output_schema_version",
        "output_sha256",
        "game_id",
        "season",
        "season_start_year",
        "season_type",
        "game_date",
        "home_team_id",
        "away_team_id",
        "possession_number",
        "offense_possession_number",
        "period",
        "offense_team_id",
        "defense_team_id",
        "start_sequence_number",
        "start_boundary_side",
        "end_sequence_number",
        "end_boundary_side",
        "start_seconds_remaining_period_ms",
        "end_seconds_remaining_period_ms",
        "seconds_remaining_game_ms",
        "home_score_before",
        "away_score_before",
        "home_score",
        "away_score",
        "score_diff",
        "offense_points",
        "defense_points",
        "home_possessions",
        "away_possessions",
        "end_reason",
        "source_event_count",
    ]
    assert {row["snapshot_id"] for row in rows} == {str(claim.attempt.snapshot_id)}
    assert {row["insert_version"] for row in rows} == {claim.attempt.generation}
    assert {row["parse_revision_id"] for row in rows} == {claim.attempt.parse_revision_id}
    assert {row["season_start_year"] for row in rows} == {2025}
    assert {row["output_sha256"] for row in rows} == {result.output_sha256}

    with pytest.raises(PossessionSnapshotStoreError, match="already_sent"):
        await store.write_snapshot(claim, result)
    assert len(client.inserts) == 1


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [
        ("missing", VerificationStatus.MISSING),
        ("fewer", VerificationStatus.MISMATCH),
        ("extra", VerificationStatus.MISMATCH),
        ("changed", VerificationStatus.MISMATCH),
        ("noncontiguous", VerificationStatus.MISMATCH),
        ("bad_boundary", VerificationStatus.MISMATCH),
        ("bad_type", VerificationStatus.MISMATCH),
    ],
)
async def test_verify_snapshot_classifies_exact_id_rows_without_exposing_them(
    change: str,
    expected_status: VerificationStatus,
) -> None:
    claim = make_claim()
    result = make_result(claim)
    assert result.output_sha256 is not None
    writer = RecordingClient()
    await PossessionSnapshotStore(writer).write_snapshot(claim, result)
    rows = [dict(row) for row in writer.inserts[0][1]]
    if change == "missing":
        rows = []
    elif change == "fewer":
        rows.pop()
    elif change == "extra":
        rows.append(dict(rows[-1]))
    elif change == "changed":
        rows[1]["offense_points"] = 1
    elif change == "noncontiguous":
        rows[1]["possession_number"] = 3
    elif change == "bad_boundary":
        rows[0]["end_boundary_side"] = "before"
    elif change == "bad_type":
        rows[0]["home_score"] = "2"
    reader = RecordingClient(rows)

    verification = await PossessionSnapshotStore(reader).verify_snapshot(claim.attempt)

    assert verification.snapshot_id == claim.attempt.snapshot_id
    assert verification.status is expected_status
    if expected_status is VerificationStatus.MISSING:
        assert verification.row_count == 0
        assert verification.output_sha256 is None
    else:
        assert verification.row_count is None
        assert verification.output_sha256 is None
    statement, parameters = reader.queries[0]
    assert "FINAL" in statement
    assert "ORDER BY possession_number" in statement
    assert "max(" not in statement.casefold()
    assert "optimize" not in statement.casefold()
    assert parameters == {
        "season_start_year": 2025,
        "game_id": "0022500123",
        "snapshot_id": str(claim.attempt.snapshot_id),
    }


async def test_verify_and_read_reconstruct_typed_rows_before_hashing() -> None:
    claim = make_claim()
    result = make_result(claim)
    assert result.output_sha256 is not None
    writer = RecordingClient()
    await PossessionSnapshotStore(writer).write_snapshot(claim, result)
    rows = writer.inserts[0][1]
    reader = RecordingClient(rows)
    store = PossessionSnapshotStore(reader)
    published_attempt = replace(
        claim.attempt,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=2,
        expected_output_sha256=result.output_sha256,
    )
    reference = CurrentSnapshotReference(
        attempt=published_attempt,
        row_count=2,
        output_sha256=result.output_sha256,
    )

    verification = await store.verify_snapshot(
        replace(published_attempt, state=PossessionAttemptState.VERIFIED)
    )
    restored = await store.read_snapshot(reference)

    assert verification.status is VerificationStatus.EXACT
    assert verification.row_count == 2
    assert verification.output_sha256 == result.output_sha256
    assert restored == result.possessions

    malformed = RecordingClient([{**rows[0], "home_score": "2"}, rows[1]])
    with pytest.raises(PossessionSnapshotStoreError, match="mismatch"):
        await PossessionSnapshotStore(malformed).read_snapshot(reference)
