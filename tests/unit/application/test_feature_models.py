"""Behavioral tests for immutable Phase 5 application contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from uuid import UUID

import pytest

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    BuildDatasetResult,
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureBuildMode,
    FeatureBuildStatus,
    FeatureClaimAction,
    FeatureClaimDecision,
    FeatureInputToken,
    FeaturePublicationState,
    FeatureSnapshotAttempt,
    FeatureSnapshotVerification,
    FeatureSourceSnapshot,
    FeatureTargetRows,
    FeatureVerificationStatus,
    HistoricalPossessionSource,
)
from nba_live_totals.application.possessions import CurrentSnapshotReference
from nba_live_totals.domain import GameId, SeasonId
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum,
    canonical_team_boxes_checksum,
    dependency_input_checksum,
    dependency_revision_checksum,
    feature_input_token_checksum,
    feature_revision_id,
)
from nba_live_totals.domain.features import (
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureResultStatus,
    FeatureVersions,
    HistoricalPlayerBox,
    HistoricalTeamBox,
)
from nba_live_totals.domain.possession_hashing import game_context_checksum

from .feature_fakes import (
    NOW,
    make_source,
    make_target,
    uuid4_value,
)


def make_attempt(
    *, state: FeatureAttemptState = FeatureAttemptState.ALLOCATED
) -> FeatureSnapshotAttempt:
    source = make_source()
    token = FeatureInputToken.from_source(source, FeatureVersions())
    populated = state is not FeatureAttemptState.ALLOCATED
    verified = state in {FeatureAttemptState.VERIFIED, FeatureAttemptState.PUBLISHED}
    published = state is FeatureAttemptState.PUBLISHED
    return FeatureSnapshotAttempt(
        game=source.target.game,
        snapshot_id=uuid4_value(401),
        generation=1,
        feature_revision_id=token.feature_revision_id,
        input_token=token,
        state=state,
        created_by_run_id=uuid4_value(402),
        created_by_claim_version=1,
        created_at=NOW,
        expected_state_row_count=2 if populated else None,
        expected_state_output_sha256="5" * 64 if populated else None,
        expected_player_row_count=10 if populated else None,
        expected_player_output_sha256="6" * 64 if populated else None,
        expected_feature_output_sha256="7" * 64 if populated else None,
        expected_issue_codes=() if populated else None,
        result_status=FeatureResultStatus.VALID if populated else None,
        insert_started_at=NOW if populated else None,
        verified_at=NOW if verified else None,
        published_at=NOW if published else None,
    )


def make_reference() -> CurrentFeatureSnapshotReference:
    attempt = make_attempt(state=FeatureAttemptState.PUBLISHED)
    return CurrentFeatureSnapshotReference(
        attempt,
        2,
        "5" * 64,
        10,
        "6" * 64,
        "7" * 64,
    )


def test_target_snapshot_requires_full_game_context_and_three_way_pbp_lineage() -> None:
    target, _, _ = make_target()
    assert target.game.started_at == target.game.started_at.astimezone(
        target.game.started_at.tzinfo
    )
    with pytest.raises(ValueError, match="lineage"):
        replace(
            target,
            pbp_snapshot=replace(
                target.pbp_snapshot,
                applied_pbp_document_id=uuid4_value(999),
            ),
        )


def test_target_rows_recompute_exact_hashes_and_use_only_valid_lineup_sides() -> None:
    target, possessions, segments = make_target()
    rows = FeatureTargetRows(target, possessions, segments)
    assert rows.target_player_ids == (101, 102, 103, 104, 105, 201, 202, 203, 204, 205)

    # Invalid players are never guessed from roster or target box-score rows.
    invalid_target, invalid_possessions, invalid_segments = make_target(invalid=True)
    invalid_rows = FeatureTargetRows(invalid_target, invalid_possessions, invalid_segments)
    assert invalid_rows.target_player_ids == (201, 202, 203, 204, 205)

    with pytest.raises(ValueError, match="possession output"):
        FeatureTargetRows(
            target,
            (replace(possessions[0], source_event_count=2),),
            segments,
        )


def test_source_snapshot_validates_manifest_order_content_hashes_and_target_players() -> None:
    source = make_source()
    assert source.historical_possession_sources[0].dependency == source.manifest.dependencies[0]
    assert source.dependency_input_sha256 == dependency_input_checksum(source.manifest)
    assert tuple(source.target_player_ids) == tuple(sorted(source.target_player_ids))

    with pytest.raises(ValueError, match="dependency input"):
        replace(source, dependency_input_sha256="0" * 64)
    with pytest.raises(ValueError, match="target player"):
        replace(source, target_player_ids=tuple(reversed(source.target_player_ids)))
    with pytest.raises(ValueError, match="team-box"):
        replace(source, historical_team_boxes=source.historical_team_boxes[:-1])


def _replace_source_dependency(
    source: FeatureSourceSnapshot,
    *,
    dependency: FeatureDependency,
    team_boxes: tuple[HistoricalTeamBox, ...],
    player_boxes: tuple[HistoricalPlayerBox, ...] | None = None,
    possession_reference: CurrentSnapshotReference | None = None,
) -> FeatureSourceSnapshot:
    typed_player_boxes = source.historical_player_boxes if player_boxes is None else player_boxes
    reference = (
        source.historical_possession_sources[0].possession_reference
        if possession_reference is None
        else possession_reference
    )
    manifest = FeatureDependencyManifest((dependency,))
    return replace(
        source,
        manifest=manifest,
        historical_team_boxes=team_boxes,
        historical_player_boxes=typed_player_boxes,
        historical_possession_sources=(HistoricalPossessionSource(dependency, reference),),
        dependency_input_sha256=dependency_input_checksum(manifest),
        dependency_revision_sha256=dependency_revision_checksum(manifest),
    )


def test_source_binds_unhashed_team_box_dates_roles_and_start_time_to_reference() -> None:
    source = make_source()
    dependency = source.manifest.dependencies[0]
    teams = source.historical_team_boxes

    with pytest.raises(ValueError, match="game date"):
        _replace_source_dependency(
            source,
            dependency=dependency,
            team_boxes=(
                replace(teams[0], game_date=teams[0].game_date + timedelta(days=1)),
                teams[1],
            ),
        )
    with pytest.raises(ValueError, match="home and away roles"):
        _replace_source_dependency(
            source,
            dependency=dependency,
            team_boxes=(replace(teams[0], is_home=False), teams[1]),
        )
    shifted = tuple(replace(row, started_at=row.started_at - timedelta(hours=1)) for row in teams)
    with pytest.raises(ValueError, match="start time"):
        _replace_source_dependency(source, dependency=dependency, team_boxes=shifted)


def test_source_binds_team_ids_to_historical_possession_game_context() -> None:
    source = make_source()
    teams = tuple(
        replace(row, team_id=30 if row.is_home else 40) for row in source.historical_team_boxes
    )
    dependency = replace(
        source.manifest.dependencies[0],
        canonical_team_box_sha256=canonical_team_boxes_checksum(teams),
    )
    with pytest.raises(ValueError, match="team IDs"):
        _replace_source_dependency(source, dependency=dependency, team_boxes=teams)


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(days=1)])
def test_source_rejects_equal_and_future_dependencies(offset: timedelta) -> None:
    source = make_source()
    started_at = source.target.game.started_at + offset
    teams = tuple(replace(row, started_at=started_at) for row in source.historical_team_boxes)
    dependency = replace(source.manifest.dependencies[0], started_at=started_at)
    with pytest.raises(ValueError, match="precede"):
        _replace_source_dependency(source, dependency=dependency, team_boxes=teams)


def test_source_rejects_target_game_as_its_own_dependency() -> None:
    source = make_source()
    target = source.target
    reference = target.possession_reference
    started_at = target.game.started_at - timedelta(days=1)
    teams = tuple(
        replace(
            row,
            game_id=target.game.game_id,
            started_at=started_at,
            game_date=target.game.game_date,
        )
        for row in source.historical_team_boxes
    )
    players = tuple(
        replace(row, game_id=target.game.game_id) for row in source.historical_player_boxes
    )
    dependency = replace(
        source.manifest.dependencies[0],
        dependency_game_id=target.game.game_id,
        started_at=started_at,
        game_context_sha256=game_context_checksum(reference.attempt.game),
        canonical_team_box_sha256=canonical_team_boxes_checksum(teams),
        canonical_player_box_sha256=canonical_player_boxes_checksum(players),
        possession_snapshot_id=reference.attempt.snapshot_id,
        parse_revision_id=reference.attempt.parse_revision_id,
        possession_input_token_sha256=reference.attempt.input_token.sha256,
        possession_output_sha256=reference.output_sha256,
    )
    with pytest.raises(ValueError, match="target game"):
        _replace_source_dependency(
            source,
            dependency=dependency,
            team_boxes=teams,
            player_boxes=players,
            possession_reference=reference,
        )


def test_input_token_recomputes_operational_and_semantic_hashes() -> None:
    source = make_source()
    versions = FeatureVersions()
    token = FeatureInputToken.from_source(source, versions)
    target = source.target
    assert token.sha256 == feature_input_token_checksum(
        game_id=target.game.game_id,
        target_started_at=target.game.started_at,
        game_context_sha256=target.pbp_snapshot.game_context_sha256,
        possession_snapshot_id=target.possession_reference.attempt.snapshot_id,
        parse_revision_id=target.possession_reference.attempt.parse_revision_id,
        possession_input_token_sha256=target.possession_reference.attempt.input_token.sha256,
        possession_output_sha256=target.possession_reference.output_sha256,
        lineup_snapshot_id=target.lineup_reference.attempt.snapshot_id,
        lineup_revision_id=target.lineup_reference.attempt.lineup_revision_id,
        lineup_input_token_sha256=target.lineup_reference.attempt.input_token.sha256,
        lineup_output_sha256=target.lineup_reference.output_sha256,
        lineup_result_status=target.lineup_reference.result_status,
        dependency_input_sha256=source.dependency_input_sha256,
        versions=versions,
    )
    assert token.feature_revision_id == feature_revision_id(
        game_id=target.game.game_id,
        target_started_at=target.game.started_at,
        game_context_sha256=target.pbp_snapshot.game_context_sha256,
        parse_revision_id=target.possession_reference.attempt.parse_revision_id,
        possession_output_sha256=target.possession_reference.output_sha256,
        lineup_revision_id=target.lineup_reference.attempt.lineup_revision_id,
        lineup_output_sha256=target.lineup_reference.output_sha256,
        lineup_result_status=target.lineup_reference.result_status,
        dependency_revision_sha256=source.dependency_revision_sha256,
        versions=versions,
    )


def test_request_enums_ranges_and_frozen_contract() -> None:
    request = BuildDatasetRequest.for_range(SeasonId("2024-25"), SeasonId("2025-26"))
    assert request.seasons == (SeasonId("2024-25"), SeasonId("2025-26"))
    assert FeatureBuildMode.MATERIALIZE.value == "materialize"
    assert FeatureBuildStatus.INTERRUPTED.value == "interrupted"
    assert FeatureAttemptState.SUPERSEDED.value == "superseded"
    assert FeatureClaimAction.SKIP_CURRENT.value == "skip_current"
    assert FeatureVerificationStatus.UNAVAILABLE.value == "unavailable"
    assert FeaturePublicationState.INVALID.value == "invalid"
    with pytest.raises(FrozenInstanceError):
        request.rebuild = True  # type: ignore[misc]
    with pytest.raises(ValueError, match="mutually exclusive"):
        BuildDatasetRequest((SeasonId("2025-26"),), mode=FeatureBuildMode.VERIFY, rebuild=True)
    with pytest.raises(ValueError, match="selected season"):
        BuildDatasetRequest((SeasonId("2025-26"),), game_id=GameId("0022400001"))


def test_attempt_enforces_complete_output_group_uuid4_and_lifecycle_timestamps() -> None:
    allocated = make_attempt()
    assert allocated.expected_feature_output_sha256 is None
    assert replace(allocated, state=FeatureAttemptState.POISONED).insert_started_at is None
    writing = make_attempt(state=FeatureAttemptState.WRITING)
    assert replace(writing, state=FeatureAttemptState.SUPERSEDED).expected_state_row_count == 2
    with pytest.raises(ValueError, match="all null or all populated"):
        replace(allocated, expected_state_row_count=2)
    with pytest.raises(ValueError, match="verified_at"):
        replace(writing, state=FeatureAttemptState.VERIFIED)
    with pytest.raises(ValueError, match="populated expected metadata"):
        replace(allocated, state=FeatureAttemptState.SUPERSEDED, verified_at=NOW)
    with pytest.raises(ValueError, match="published_at"):
        replace(
            writing,
            state=FeatureAttemptState.PUBLISHED,
            verified_at=NOW,
        )
    with pytest.raises(ValueError, match="UUIDv4"):
        replace(allocated, snapshot_id=UUID("00000000-0000-1000-8000-000000000401"))


def test_issue_codes_reject_string_and_bytes_containers() -> None:
    writing = make_attempt(state=FeatureAttemptState.WRITING)
    for invalid in ("issue", b"issue"):
        with pytest.raises(ValueError, match="tuple-like iterable"):
            replace(
                writing,
                expected_issue_codes=invalid,  # type: ignore[arg-type]
                result_status=FeatureResultStatus.INVALID,
            )


def test_claim_reference_decision_resolution_and_verification_are_consistent() -> None:
    source = make_source()
    attempt = make_attempt()
    claim = FeatureBuildClaim(attempt.created_by_run_id, 1, attempt, source)
    assert claim.attempt.input_token == FeatureInputToken.from_source(source, FeatureVersions())
    reference = make_reference()
    decision = FeatureClaimDecision(FeatureClaimAction.SKIP_CURRENT, current=reference)
    resolution = CurrentFeatureSnapshotResolution(FeaturePublicationState.CURRENT, reference)
    assert decision.current is resolution.reference
    with pytest.raises(ValueError, match="require a claim"):
        FeatureClaimDecision(FeatureClaimAction.BUILD)
    with pytest.raises(ValueError, match="valid snapshot"):
        CurrentFeatureSnapshotResolution(
            FeaturePublicationState.INVALID,
            reference,
        )

    verification = FeatureSnapshotVerification(
        reference.attempt.snapshot_id,
        FeatureVerificationStatus.EXACT,
        2,
        "5" * 64,
        10,
        "6" * 64,
        "7" * 64,
        FeatureResultStatus.VALID,
        (),
    )
    assert verification.is_exact
    with pytest.raises(ValueError, match="metadata"):
        FeatureSnapshotVerification(
            reference.attempt.snapshot_id,
            FeatureVerificationStatus.EXACT,
        )


def test_result_counters_account_for_every_game_and_invalid_forces_partial() -> None:
    result = BuildDatasetResult(uuid4_value(500), FeatureBuildStatus.PARTIAL, 2, 1, 1, 1, 0, 0, 0)
    assert result.invalid == 1
    with pytest.raises(ValueError, match="account"):
        BuildDatasetResult(uuid4_value(500), FeatureBuildStatus.INTERRUPTED, 2, 1, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="partial"):
        BuildDatasetResult(uuid4_value(500), FeatureBuildStatus.SUCCEEDED, 1, 1, 1, 0, 0, 0, 0)
