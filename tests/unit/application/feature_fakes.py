"""Focused valid fixtures for Phase 5 application contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from nba_live_totals.application.features import (
    FeatureSourceSnapshot,
    FeatureTargetRows,
    FeatureTargetSnapshot,
    HistoricalPossessionSource,
)
from nba_live_totals.application.lineups import (
    CurrentLineupSnapshotReference,
    LineupAttemptState,
    LineupInputToken,
    LineupSnapshotAttempt,
)
from nba_live_totals.application.possessions import (
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionAttemptState,
    PossessionInputToken,
    PossessionSnapshotAttempt,
)
from nba_live_totals.domain import (
    Game,
    GameId,
    GameStatus,
    SeasonId,
    SeasonType,
    game_context_checksum,
    parse_revision_id,
    possession_output_checksum,
)
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum,
    canonical_team_boxes_checksum,
    dependency_input_checksum,
    dependency_revision_checksum,
)
from nba_live_totals.domain.features import (
    FeatureDependency,
    FeatureDependencyManifest,
    HistoricalPlayerBox,
    HistoricalTeamBox,
)
from nba_live_totals.domain.lineup_hashing import lineup_output_checksum, lineup_revision_id
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
)
from nba_live_totals.domain.possessions import Possession
from tests.unit.application.possession_fakes import (
    VERSIONS as POSSESSION_VERSIONS,
)
from tests.unit.application.possession_fakes import make_possession, make_snapshot
from tests.unit.domain.lineup_helpers import make_invalid_state, make_segment

TARGET_GAME_ID = GameId("0022500123")
HISTORY_GAME_ID = GameId("0022400122")
NOW = datetime(2026, 1, 4, tzinfo=UTC)


def uuid4_value(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def make_game(game_id: GameId = TARGET_GAME_ID, *, historical: bool = False) -> Game:
    started_at = (
        datetime(2025, 12, 20, tzinfo=UTC) if historical else datetime(2026, 1, 2, tzinfo=UTC)
    )
    return Game(
        game_id=game_id,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=started_at.date(),
        started_at=started_at,
        home_team_id=10,
        away_team_id=20,
        home_score=2,
        away_score=0,
        status=GameStatus.FINAL,
        arena_name=None,
        arena_city=None,
        arena_state=None,
    )


def make_possession_reference(
    game_id: GameId = TARGET_GAME_ID,
) -> tuple[CurrentSnapshotReference, GamePbpSnapshot, tuple[Possession, ...]]:
    snapshot = make_snapshot(game_id)
    possession = make_possession(game_id)
    token = PossessionInputToken.from_snapshot(snapshot, POSSESSION_VERSIONS)
    output_sha256 = possession_output_checksum(
        parse_revision_id=parse_revision_id(
            game_id=game_id,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=token.versions,
        ),
        game=snapshot.game,
        possessions=(possession,),
    )
    attempt = PossessionSnapshotAttempt(
        game=snapshot.game,
        snapshot_id=uuid4_value(100 if game_id == TARGET_GAME_ID else 101),
        generation=1,
        parse_revision_id=parse_revision_id(
            game_id=game_id,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=token.versions,
        ),
        input_token=token,
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256=output_sha256,
    )
    return CurrentSnapshotReference(attempt, 1, output_sha256), snapshot, (possession,)


def make_lineup_reference(
    possession_reference: CurrentSnapshotReference,
    *,
    invalid: bool = False,
) -> tuple[CurrentLineupSnapshotReference, tuple[LineupSegment, ...]]:
    possession_token = possession_reference.attempt.input_token
    game = possession_reference.attempt.game
    versions = LineupVersions("lineup-normalizer-v1", "lineup-v1", "nba-v1", "lineups-v1")
    token = LineupInputToken(
        game_id=game.game_id,
        applied_box_score_document_id=uuid4_value(200),
        applied_box_score_sha256="2" * 64,
        applied_pbp_document_id=possession_token.applied_pbp_document_id,
        applied_pbp_sha256=possession_token.applied_pbp_sha256,
        canonical_player_box_sha256="3" * 64,
        canonical_event_sha256=possession_token.canonical_event_sha256,
        game_context_sha256=possession_token.game_context_sha256,
        versions=versions,
    )
    revision = lineup_revision_id(
        game_id=game.game_id,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        versions=versions,
    )
    segment = make_segment(
        home_state=make_invalid_state() if invalid else make_segment().home_state
    )
    context = LineupReconstructionContext(
        game=game,
        versions=versions,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        lineup_revision_id=revision,
    )
    issues: tuple[LineupIssue, ...] = ()
    if invalid:
        issues = (LineupIssue(10, "invalid_initial_lineup", 1, segment.start_boundary.side),)
    result = LineupReconstructionResult(
        context,
        LineupStatus.INVALID if invalid else LineupStatus.VALID,
        (segment,),
        issues,
    )
    output_sha256 = lineup_output_checksum(result)
    attempt = LineupSnapshotAttempt(
        game=game,
        snapshot_id=uuid4_value(201),
        generation=1,
        lineup_revision_id=revision,
        input_token=token,
        state=LineupAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256=output_sha256,
        expected_issue_codes=issues,
        result_status=result.status,
    )
    return CurrentLineupSnapshotReference(attempt, 1, output_sha256), (segment,)


def make_target(
    *, invalid: bool = False
) -> tuple[FeatureTargetSnapshot, tuple[Possession, ...], tuple[LineupSegment, ...]]:
    possession_reference, pbp_snapshot, possessions = make_possession_reference()
    lineup_reference, segments = make_lineup_reference(possession_reference, invalid=invalid)
    target = FeatureTargetSnapshot(
        make_game(), pbp_snapshot, possession_reference, lineup_reference
    )
    return target, possessions, segments


def make_history_boxes() -> tuple[tuple[HistoricalTeamBox, ...], tuple[HistoricalPlayerBox, ...]]:
    started_at = make_game(HISTORY_GAME_ID, historical=True).started_at
    team_boxes = (
        HistoricalTeamBox(
            HISTORY_GAME_ID, 10, started_at, started_at.date(), True, 2880, points=100
        ),
        HistoricalTeamBox(
            HISTORY_GAME_ID, 20, started_at, started_at.date(), False, 2880, points=90
        ),
    )
    player_boxes = (
        HistoricalPlayerBox(HISTORY_GAME_ID, 10, 101, True, 1200, points=20),
        HistoricalPlayerBox(HISTORY_GAME_ID, 20, 201, True, 1200, points=18),
    )
    return team_boxes, player_boxes


def make_dependency_source() -> tuple[
    HistoricalPossessionSource,
    tuple[HistoricalTeamBox, ...],
    tuple[HistoricalPlayerBox, ...],
]:
    reference, _, _ = make_possession_reference(HISTORY_GAME_ID)
    team_boxes, player_boxes = make_history_boxes()
    dependency = FeatureDependency(
        dependency_game_id=HISTORY_GAME_ID,
        started_at=team_boxes[0].started_at,
        game_context_sha256=game_context_checksum(reference.attempt.game),
        applied_box_score_document_id=uuid4_value(300),
        applied_box_score_sha256="4" * 64,
        canonical_team_box_sha256=canonical_team_boxes_checksum(team_boxes),
        canonical_player_box_sha256=canonical_player_boxes_checksum(player_boxes),
        possession_snapshot_id=reference.attempt.snapshot_id,
        parse_revision_id=reference.attempt.parse_revision_id,
        possession_input_token_sha256=reference.attempt.input_token.sha256,
        possession_output_sha256=reference.output_sha256,
        ordinal=0,
        inclusion_reasons=("home_team_window", "player_history"),
    )
    return HistoricalPossessionSource(dependency, reference), team_boxes, player_boxes


def make_source() -> FeatureSourceSnapshot:
    target, possessions, segments = make_target()
    rows = FeatureTargetRows(target, possessions, segments)
    history, team_boxes, player_boxes = make_dependency_source()
    manifest = FeatureDependencyManifest((history.dependency,))
    return FeatureSourceSnapshot(
        target=target,
        target_player_ids=rows.target_player_ids,
        manifest=manifest,
        historical_team_boxes=team_boxes,
        historical_player_boxes=player_boxes,
        historical_possession_sources=(history,),
        dependency_input_sha256=dependency_input_checksum(manifest),
        dependency_revision_sha256=dependency_revision_checksum(manifest),
    )
