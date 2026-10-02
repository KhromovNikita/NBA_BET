"""Contracts for exact ClickHouse feature-source reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from nba_live_totals.application.features import (
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureInputToken,
    FeatureSnapshotAttempt,
    FeatureSourceSnapshot,
    FeatureTargetSnapshot,
    HistoricalPossessionSource,
)
from nba_live_totals.application.lineups import (
    CurrentLineupSnapshotReference,
    GameLineupInputSnapshot,
    LineupAttemptState,
    LineupBuildClaim,
    LineupInputToken,
    LineupSnapshotAttempt,
)
from nba_live_totals.application.possessions import (
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionAttemptState,
    PossessionBuildClaim,
    PossessionInputToken,
    PossessionSnapshotAttempt,
)
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    Game,
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
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum as feature_player_boxes_checksum,
)
from nba_live_totals.domain.feature_hashing import (
    canonical_team_boxes_checksum,
    dependency_input_checksum,
    dependency_revision_checksum,
)
from nba_live_totals.domain.features import (
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureVersions,
    HistoricalPlayerBox,
    HistoricalTeamBox,
)
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    lineup_output_checksum,
    lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupPlayerBox,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    TeamLineupState,
)
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSourceReader,
    FeatureSourceReaderError,
)
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore

GAME_ID = GameId("0022500123")
POSSESSION_VERSIONS = PossessionVersions(
    "nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1"
)
LINEUP_VERSIONS = LineupVersions(
    "nba-pbp-v3-lineup-v1", "causal-lineup-v1", "nba-5x5-v1", "lineup-v1"
)


class RoutedClient:
    def __init__(
        self,
        possession_rows: list[dict[str, object]],
        lineup_rows: list[dict[str, object]],
        history_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.possession_rows = possession_rows
        self.lineup_rows = lineup_rows
        self.history_rows = history_rows or []
        self.queries: list[tuple[str, Mapping[str, str | int] | None]] = []

    async def insert_json_each_row(self, table: str, rows: Sequence[Mapping[str, object]]) -> None:
        raise AssertionError("source reader must not insert")

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        self.queries.append((statement, parameters))
        if "possessions_v1" in statement and " IN (" in statement:
            return [dict(row) for row in self.history_rows]
        if "possessions_v1" in statement:
            return [dict(row) for row in self.possession_rows]
        if "lineup_segments_v1" in statement:
            return [dict(row) for row in self.lineup_rows]
        raise AssertionError("unexpected table")


def game_fixture() -> tuple[Game, GameContext]:
    game = Game(
        GAME_ID,
        SeasonId("2025-26"),
        SeasonType.REGULAR_SEASON,
        date(2026, 1, 2),
        datetime(2026, 1, 2, 1, tzinfo=UTC),
        10,
        20,
        110,
        108,
        GameStatus.FINAL,
        None,
        None,
        None,
    )
    return game, GameContext.from_game(game)


def document(resource: str) -> SourceDocument:
    payload: JsonObject = {"game_id": GAME_ID.value, "resource": resource}
    return SourceDocument(
        source="nba_stats",
        endpoint="playbyplayv3" if resource == "play_by_play" else "boxscoretraditionalv3",
        resource_key=f"{resource}:{GAME_ID.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def event() -> PlayByPlayEvent:
    return PlayByPlayEvent(
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
    )


def possessions() -> tuple[Possession, ...]:
    first = Possession(
        GAME_ID,
        1,
        10,
        20,
        1,
        1,
        EventBoundary(1, BoundarySide.BEFORE),
        EventBoundary(1, BoundarySide.AFTER),
        719_000,
        718_500,
        2_878_500,
        0,
        0,
        2,
        0,
        2,
        2,
        0,
        1,
        0,
        PossessionEndReason.MADE_FIELD_GOAL,
        1,
    )
    second = Possession(
        GAME_ID,
        2,
        20,
        10,
        1,
        1,
        EventBoundary(2, BoundarySide.BEFORE),
        EventBoundary(2, BoundarySide.AFTER),
        718_000,
        717_500,
        2_877_500,
        2,
        0,
        2,
        2,
        0,
        2,
        0,
        1,
        1,
        PossessionEndReason.MADE_FIELD_GOAL,
        1,
    )
    return first, second


def lineup_segment() -> LineupSegment:
    return LineupSegment(
        1,
        EventBoundary(1, BoundarySide.BEFORE),
        EventBoundary(2, BoundarySide.AFTER),
        1,
        1,
        720_000,
        700_000,
        TeamLineupState(LineupStatus.VALID, frozenset({101, 102, 103, 104, 105}), None, None),
        TeamLineupState(LineupStatus.VALID, frozenset({201, 202, 203, 204, 205}), None, None),
    )


def target_fixture() -> tuple[
    FeatureTargetSnapshot, list[dict[str, object]], list[dict[str, object]]
]:
    game, context = game_fixture()
    pbp_document = document("play_by_play")
    canonical_events = (event(),)
    pbp_snapshot = GamePbpSnapshot(
        context,
        UUID("11111111-1111-4111-8111-111111111111"),
        pbp_document.checksum_sha256,
        canonical_events,
        canonical_event_checksum(canonical_events),
        game_context_checksum(context),
        pbp_document,
    )
    possession_token = PossessionInputToken.from_snapshot(pbp_snapshot, POSSESSION_VERSIONS)
    revision = parse_revision_id(
        game_id=GAME_ID,
        pbp_document_sha256=possession_token.applied_pbp_sha256,
        canonical_event_sha256=possession_token.canonical_event_sha256,
        game_context_sha256=possession_token.game_context_sha256,
        versions=POSSESSION_VERSIONS,
    )
    possession_values = possessions()
    possession_hash = possession_output_checksum(
        parse_revision_id=revision, game=context, possessions=possession_values
    )
    possession_attempt = PossessionSnapshotAttempt(
        context,
        UUID("22222222-2222-4222-8222-222222222222"),
        7,
        revision,
        possession_token,
        PossessionAttemptState.PUBLISHED,
        2,
        possession_hash,
    )
    possession_claim = PossessionBuildClaim(
        UUID("33333333-3333-4333-8333-333333333333"),
        1,
        possession_attempt,
        pbp_snapshot,
    )
    possession_result = PossessionParseResult(
        PossessionParseContext(
            context,
            POSSESSION_VERSIONS,
            possession_token.applied_pbp_sha256,
            possession_token.canonical_event_sha256,
            possession_token.game_context_sha256,
            revision,
        ),
        ParseStatus.VALID,
        possession_values,
        (),
        possession_hash,
    )
    possession_store = PossessionSnapshotStore.__new__(PossessionSnapshotStore)
    possession_rows = [
        possession_store._serialize_row(possession_claim, possession_result, row)
        for row in possession_values
    ]
    possession_reference = CurrentSnapshotReference(possession_attempt, 2, possession_hash)

    box_document = document("box_score")
    boxes = tuple(
        LineupPlayerBox(GAME_ID, 10 if player_id < 200 else 20, player_id, "G", True, True, None)
        for player_id in (101, 102, 103, 104, 105, 201, 202, 203, 204, 205)
    )
    lineup_snapshot = GameLineupInputSnapshot(
        context,
        UUID("44444444-4444-4444-8444-444444444444"),
        box_document.checksum_sha256,
        pbp_snapshot.applied_pbp_document_id,
        pbp_snapshot.applied_pbp_sha256,
        boxes,
        canonical_events,
        canonical_player_boxes_checksum(boxes),
        pbp_snapshot.canonical_event_sha256,
        pbp_snapshot.game_context_sha256,
        box_document,
        pbp_document,
    )
    lineup_token = LineupInputToken.from_snapshot(lineup_snapshot, LINEUP_VERSIONS)
    lineup_revision = lineup_revision_id(
        game_id=GAME_ID,
        box_score_document_sha256=lineup_token.applied_box_score_sha256,
        pbp_document_sha256=lineup_token.applied_pbp_sha256,
        canonical_player_box_sha256=lineup_token.canonical_player_box_sha256,
        canonical_event_sha256=lineup_token.canonical_event_sha256,
        game_context_sha256=lineup_token.game_context_sha256,
        versions=LINEUP_VERSIONS,
    )
    segment = lineup_segment()
    lineup_result = LineupReconstructionResult(
        LineupReconstructionContext(
            context,
            LINEUP_VERSIONS,
            lineup_token.applied_box_score_sha256,
            lineup_token.applied_pbp_sha256,
            lineup_token.canonical_player_box_sha256,
            lineup_token.canonical_event_sha256,
            lineup_token.game_context_sha256,
            lineup_revision,
        ),
        LineupStatus.VALID,
        (segment,),
        (),
    )
    lineup_hash = lineup_output_checksum(lineup_result)
    lineup_attempt = LineupSnapshotAttempt(
        context,
        UUID("55555555-5555-4555-8555-555555555555"),
        8,
        lineup_revision,
        lineup_token,
        LineupAttemptState.PUBLISHED,
        1,
        lineup_hash,
        (),
        LineupStatus.VALID,
    )
    lineup_claim = LineupBuildClaim(
        UUID("66666666-6666-4666-8666-666666666666"),
        1,
        lineup_attempt,
        lineup_snapshot,
    )
    lineup_store = ClickHouseLineupSnapshotStore.__new__(ClickHouseLineupSnapshotStore)
    lineup_rows = [lineup_store._serialize_row(lineup_claim, lineup_result, segment)]
    lineup_reference = CurrentLineupSnapshotReference(lineup_attempt, 1, lineup_hash)
    target = FeatureTargetSnapshot(game, pbp_snapshot, possession_reference, lineup_reference)
    return target, possession_rows, lineup_rows


def claimed_history_fixture(
    target: FeatureTargetSnapshot,
) -> tuple[FeatureBuildClaim, list[dict[str, object]]]:
    history_id = GameId("0022500122")
    history_context = GameContext(
        history_id,
        SeasonId("2025-26"),
        SeasonType.REGULAR_SEASON,
        date(2026, 1, 1),
        GameStatus.FINAL,
        10,
        20,
        105,
        100,
    )
    payload: JsonObject = {"game_id": history_id.value, "resource": "play_by_play"}
    pbp_document = SourceDocument(
        source="nba_stats",
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{history_id.value}",
        requested_at=datetime(2026, 1, 2, tzinfo=UTC),
        request_parameters={"GameID": history_id.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
    history_events = (replace(event(), game_id=history_id),)
    snapshot = GamePbpSnapshot(
        history_context,
        UUID("77777777-7777-4777-8777-777777777777"),
        pbp_document.checksum_sha256,
        history_events,
        canonical_event_checksum(history_events),
        game_context_checksum(history_context),
        pbp_document,
    )
    token = PossessionInputToken.from_snapshot(snapshot, POSSESSION_VERSIONS)
    revision = parse_revision_id(
        game_id=history_id,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        versions=POSSESSION_VERSIONS,
    )
    history_possessions = tuple(
        replace(possession, game_id=history_id) for possession in possessions()
    )
    output_sha256 = possession_output_checksum(
        parse_revision_id=revision,
        game=history_context,
        possessions=history_possessions,
    )
    attempt = PossessionSnapshotAttempt(
        history_context,
        UUID("88888888-8888-4888-8888-888888888888"),
        3,
        revision,
        token,
        PossessionAttemptState.PUBLISHED,
        2,
        output_sha256,
    )
    possession_claim = PossessionBuildClaim(
        UUID("99999999-9999-4999-8999-999999999999"), 1, attempt, snapshot
    )
    result = PossessionParseResult(
        PossessionParseContext(
            history_context,
            POSSESSION_VERSIONS,
            token.applied_pbp_sha256,
            token.canonical_event_sha256,
            token.game_context_sha256,
            revision,
        ),
        ParseStatus.VALID,
        history_possessions,
        (),
        output_sha256,
    )
    possession_store = PossessionSnapshotStore.__new__(PossessionSnapshotStore)
    history_rows = [
        possession_store._serialize_row(possession_claim, result, row)
        for row in history_possessions
    ]
    team_boxes = (
        HistoricalTeamBox(
            history_id,
            10,
            datetime(2026, 1, 1, 1, tzinfo=UTC),
            date(2026, 1, 1),
            True,
            seconds_played=14_400,
            points=105,
        ),
        HistoricalTeamBox(
            history_id,
            20,
            datetime(2026, 1, 1, 1, tzinfo=UTC),
            date(2026, 1, 1),
            False,
            seconds_played=14_400,
            points=100,
        ),
    )
    player_boxes = (
        HistoricalPlayerBox(history_id, 10, 101, True, seconds_played=1_800, points=20),
    )
    dependency = FeatureDependency(
        history_id,
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        game_context_checksum(history_context),
        UUID("aaaaaaaa-1111-4111-8111-111111111111"),
        "a" * 64,
        canonical_team_boxes_checksum(team_boxes),
        feature_player_boxes_checksum(player_boxes),
        attempt.snapshot_id,
        attempt.parse_revision_id,
        attempt.input_token.sha256,
        output_sha256,
        0,
        ("home_team_window", "player_history"),
    )
    manifest = FeatureDependencyManifest((dependency,))
    source = FeatureSourceSnapshot(
        target,
        (101,),
        manifest,
        team_boxes,
        player_boxes,
        (
            HistoricalPossessionSource(
                dependency,
                CurrentSnapshotReference(attempt, 2, output_sha256),
            ),
        ),
        dependency_input_checksum(manifest),
        dependency_revision_checksum(manifest),
    )
    versions = FeatureVersions()
    feature_token = FeatureInputToken.from_source(source, versions)
    feature_attempt = FeatureSnapshotAttempt(
        game=target.game,
        snapshot_id=UUID("bbbbbbbb-1111-4111-8111-111111111111"),
        generation=1,
        feature_revision_id=feature_token.feature_revision_id,
        input_token=feature_token,
        state=FeatureAttemptState.ALLOCATED,
        created_by_run_id=UUID("cccccccc-1111-4111-8111-111111111111"),
        created_by_claim_version=1,
        created_at=target.game.started_at,
    )
    return (
        FeatureBuildClaim(feature_attempt.created_by_run_id, 1, feature_attempt, source),
        history_rows,
    )


def test_reader_uses_bounded_safe_error_contract() -> None:
    error = FeatureSourceReaderError("feature_target_source_mismatch")
    assert error.code == "feature_target_source_mismatch"
    assert str(error) == "feature_target_source_mismatch"
    assert ClickHouseFeatureSourceReader is not None


async def test_read_target_uses_exact_final_queries_and_derives_valid_players() -> None:
    target, possession_rows, lineup_rows = target_fixture()
    client = RoutedClient(possession_rows, lineup_rows)

    rows = await ClickHouseFeatureSourceReader(client).read_target(target)

    assert len(rows.possessions) == 2
    assert len(rows.lineup_segments) == 1
    assert rows.target_player_ids == (101, 102, 103, 104, 105, 201, 202, 203, 204, 205)
    assert len(client.queries) == 2
    assert all(" FINAL " in statement for statement, _ in client.queries)
    assert all("snapshot_id =" in statement for statement, _ in client.queries)
    assert all("max(" not in statement.lower() for statement, _ in client.queries)


async def test_read_target_rejects_missing_rows() -> None:
    target, _, lineup_rows = target_fixture()
    client = RoutedClient([], lineup_rows)

    with pytest.raises(FeatureSourceReaderError, match="feature_target_source_mismatch"):
        await ClickHouseFeatureSourceReader(client).read_target(target)


async def test_lineage_mismatch_performs_no_clickhouse_query() -> None:
    target, possession_rows, lineup_rows = target_fixture()
    client = RoutedClient(possession_rows, lineup_rows)
    object.__setattr__(target.lineup_reference.attempt.input_token, "applied_pbp_sha256", "0" * 64)

    with pytest.raises(FeatureSourceReaderError, match="feature_target_lineage_mismatch"):
        await ClickHouseFeatureSourceReader(client).read_target(target)

    assert client.queries == []


async def test_read_inputs_uses_one_manifest_bound_history_query() -> None:
    target, possession_rows, lineup_rows = target_fixture()
    claim, history_rows = claimed_history_fixture(target)
    client = RoutedClient(possession_rows, lineup_rows, history_rows)

    inputs = await ClickHouseFeatureSourceReader(client).read_inputs(claim)

    assert len(inputs.possessions) == 2
    assert {row.game_id for row in inputs.possessions} == {GAME_ID}
    assert tuple(row.game_id for row in inputs.history_possessions) == (
        GameId("0022500122"),
        GameId("0022500122"),
    )
    assert inputs.historical_team_boxes == claim.source.historical_team_boxes
    assert inputs.historical_player_boxes == claim.source.historical_player_boxes
    assert len(client.queries) == 3
    history_statement, history_parameters = client.queries[-1]
    assert "snapshot_id IN" in history_statement
    assert " FINAL " in history_statement
    assert history_parameters == {
        "snapshot_id_0": str(
            claim.source.historical_possession_sources[0].possession_reference.attempt.snapshot_id
        )
    }


async def test_read_inputs_rejects_missing_manifest_snapshot() -> None:
    target, possession_rows, lineup_rows = target_fixture()
    claim, _ = claimed_history_fixture(target)
    client = RoutedClient(possession_rows, lineup_rows, [])

    with pytest.raises(FeatureSourceReaderError, match="feature_history_source_mismatch"):
        await ClickHouseFeatureSourceReader(client).read_inputs(claim)


async def test_history_queries_are_chunked_at_fixed_bound() -> None:
    client = RoutedClient([], [], [])
    reader = ClickHouseFeatureSourceReader(client)
    sources = tuple(
        SimpleNamespace(
            possession_reference=SimpleNamespace(
                attempt=SimpleNamespace(snapshot_id=uuid4()),
                row_count=0,
                output_sha256="a" * 64,
            )
        )
        for _ in range(40)
    )

    class EmptyDecoder:
        @staticmethod
        def _reconstruct(attempt: object, rows: object) -> SimpleNamespace:
            return SimpleNamespace(possessions=(), output_sha256="a" * 64)

    cast(Any, reader)._possessions = EmptyDecoder()
    claim = SimpleNamespace(source=SimpleNamespace(historical_possession_sources=sources))

    assert await cast(Any, reader)._read_history(claim) == ()
    assert len(client.queries) == 2
    assert [len(parameters or {}) for _, parameters in client.queries] == [32, 8]


async def test_claim_identity_mismatch_performs_no_clickhouse_query() -> None:
    target, possession_rows, lineup_rows = target_fixture()
    claim, history_rows = claimed_history_fixture(target)
    client = RoutedClient(possession_rows, lineup_rows, history_rows)
    object.__setattr__(claim.attempt.input_token, "sha256", "0" * 64)

    with pytest.raises(FeatureSourceReaderError, match="feature_claim_source_mismatch"):
        await ClickHouseFeatureSourceReader(client).read_inputs(claim)

    assert client.queries == []
