from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeaturePublicationState,
    FeatureTargetRows,
    FeatureTargetSnapshot,
)
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.possessions import CurrentSnapshotReference, PossessionInputToken
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    JsonObject,
    canonical_event_checksum,
    parse_revision_id,
    possession_output_checksum,
)
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import FeatureDataset, FeatureVersions
from nba_live_totals.domain.lineup_hashing import lineup_output_checksum, lineup_revision_id
from nba_live_totals.domain.lineups import (
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupStatus,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer
from nba_live_totals.infrastructure.nba.mappers import parse_play_by_play
from nba_live_totals.infrastructure.postgres.model_source import PublishedModelFeatureSource
from tests.unit.application.feature_fakes import make_target, uuid4_value
from tests.unit.application.test_feature_models import make_reference
from tests.unit.domain.baseline_helpers import make_state
from tests.unit.infrastructure.nba.possession_helpers import load_json_fixture, make_source_document


def make_model_source(
    *, invalid: bool = False, possession_period: int = 4
) -> tuple[PublishedModelFeatureSource, AsyncMock, AsyncMock, AsyncMock]:
    target, possessions, segments = make_target()
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    game_payload = cast(JsonObject, payload["game"])
    raw = cast(list[JsonObject], game_payload["actions"])
    start = dict(raw[0], period=possession_period, actionType="period", subType="start")
    shot = dict(
        raw[1],
        period=possession_period,
        teamId=10,
        clock="PT1M00.00S",
        scoreHome="2",
        shotValue=2,
        pointsTotal=2,
        shotDistance=2,
    )
    end = dict(
        raw[0],
        period=4,
        actionNumber=3,
        actionId=30,
        clock="PT0M00.00S",
        subType="end",
        scoreHome="2",
        pointsTotal=2,
    )
    game_payload["actions"] = [start, shot, end]
    document = make_source_document(payload)
    events = parse_play_by_play(document)
    snapshot = replace(
        target.pbp_snapshot,
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        source_document=document,
    )
    pos = replace(
        possessions[0],
        period=possession_period,
        end_boundary=EventBoundary(2, BoundarySide.AFTER),
        end_seconds_remaining_period_ms=60_000,
        seconds_remaining_game_ms=60_000 + (4 - possession_period) * 720_000,
    )
    pt = PossessionInputToken.from_snapshot(
        snapshot, target.possession_reference.attempt.input_token.versions
    )
    revision = parse_revision_id(
        game_id=pt.game_id,
        pbp_document_sha256=pt.applied_pbp_sha256,
        canonical_event_sha256=pt.canonical_event_sha256,
        game_context_sha256=pt.game_context_sha256,
        versions=pt.versions,
    )
    ph = possession_output_checksum(
        parse_revision_id=revision, game=snapshot.game, possessions=(pos,)
    )
    pa = replace(
        target.possession_reference.attempt,
        input_token=pt,
        parse_revision_id=revision,
        expected_output_sha256=ph,
    )
    pr = CurrentSnapshotReference(pa, 1, ph)
    lt = replace(
        target.lineup_reference.attempt.input_token,
        applied_pbp_sha256=pt.applied_pbp_sha256,
        canonical_event_sha256=pt.canonical_event_sha256,
    )
    lr = lineup_revision_id(
        game_id=lt.game_id,
        box_score_document_sha256=lt.applied_box_score_sha256,
        pbp_document_sha256=lt.applied_pbp_sha256,
        canonical_player_box_sha256=lt.canonical_player_box_sha256,
        canonical_event_sha256=lt.canonical_event_sha256,
        game_context_sha256=lt.game_context_sha256,
        versions=lt.versions,
    )
    context = LineupReconstructionContext(
        snapshot.game,
        lt.versions,
        lt.applied_box_score_sha256,
        lt.applied_pbp_sha256,
        lt.canonical_player_box_sha256,
        lt.canonical_event_sha256,
        lt.game_context_sha256,
        lr,
    )
    lh = lineup_output_checksum(
        LineupReconstructionResult(context, LineupStatus.VALID, segments, ())
    )
    la = replace(
        target.lineup_reference.attempt,
        input_token=lt,
        lineup_revision_id=lr,
        expected_output_sha256=lh,
    )
    lineup = replace(target.lineup_reference, attempt=la, output_sha256=lh)
    target = FeatureTargetSnapshot(target.game, snapshot, pr, lineup)
    rows = FeatureTargetRows(target, (pos,), segments)
    states = (
        make_state(
            game_id=target.game.game_id, state_number=0, seconds_remaining_period_ms=720_000
        ),
        make_state(
            game_id=target.game.game_id,
            state_number=1,
            boundary=pos.end_boundary,
            period=pos.period,
            seconds_remaining_game_ms=pos.seconds_remaining_game_ms,
            seconds_remaining_period_ms=60_000,
            home_score=2,
            away_score=0,
            score_diff=2,
            home_possessions=1,
            away_possessions=0,
        ),
    )
    if invalid:
        states = (
            states[0],
            replace(
                states[1],
                home_player_ids=(),
                home_lineup_status=LineupStatus.INVALID,
                home_invalid_reason="missing_substitution",
                home_invalid_since=states[1].boundary,
            ),
        )
    features = FeatureDataset(target.game.game_id, states, ())
    old = make_reference().attempt
    ft = replace(
        old.input_token,
        possession_snapshot_id=pa.snapshot_id,
        parse_revision_id=revision,
        possession_input_token_sha256=pt.sha256,
        possession_output_sha256=ph,
        lineup_snapshot_id=la.snapshot_id,
        lineup_revision_id=lr,
        lineup_input_token_sha256=lt.sha256,
        lineup_output_sha256=lh,
    )
    sh, plh, fh = (
        state_output_checksum(features.states),
        player_output_checksum(()),
        feature_output_checksum(features),
    )
    fa = replace(
        old,
        input_token=ft,
        feature_revision_id=ft.feature_revision_id,
        expected_state_row_count=2,
        expected_state_output_sha256=sh,
        expected_player_row_count=0,
        expected_player_output_sha256=plh,
        expected_feature_output_sha256=fh,
        expected_issue_codes=features.issues,
        result_status=features.status,
    )
    reference = CurrentFeatureSnapshotReference(fa, 2, sh, 0, plh, fh)
    work, reader, snapshots = AsyncMock(), AsyncMock(), AsyncMock()
    work.resolve_target.return_value = target
    reader.read_target.return_value = rows
    work.resolve_current.return_value = CurrentFeatureSnapshotResolution(
        FeaturePublicationState.INVALID if invalid else FeaturePublicationState.CURRENT, reference
    )
    snapshots.read_snapshot.return_value = features
    return (
        PublishedModelFeatureSource(
            work,
            reader,
            snapshots,
            versions=FeatureVersions(),
            normalizer=NBAPlayByPlayV3PossessionNormalizer(),
        ),
        work,
        reader,
        snapshots,
    )


async def test_training_read_rechecks_reference_and_never_claims() -> None:
    source, work, reader, _ = make_model_source()
    ref = await source.resolve(work.resolve_target.return_value.game.game_id)
    inputs = await source.read_training(ref)
    assert inputs.period_ends[0].period == 4
    assert inputs.period_ends[0].home_possessions == 1
    assert inputs.period_ends[0].home_score == 2
    assert reader.read_target.await_count == 2
    work.claim_game.assert_not_called()
    work.start_run.assert_not_called()


async def test_empty_regulation_period_preserves_cumulative_counters() -> None:
    source, work, _, _ = make_model_source(possession_period=3)
    ref = await source.resolve(work.resolve_target.return_value.game.game_id)
    inputs = await source.read_training(ref)
    assert inputs.period_ends[0].home_score == 2
    assert inputs.period_ends[0].home_possessions == 1


async def test_invalid_snapshot_keeps_valid_prefix_and_prediction_reads_no_endpoints() -> None:
    source, work, _, _ = make_model_source(invalid=True)
    ref = await source.resolve(work.resolve_target.return_value.game.game_id)
    source._normalizer = Mock(wraps=NBAPlayByPlayV3PossessionNormalizer())
    source._normalizer.version = "nba-pbp-v2"
    features = await source.read_features(ref)
    assert features.states[0].is_valid and not features.states[1].is_valid
    source._normalizer.normalize.assert_not_called()


@pytest.mark.parametrize("status", [FeaturePublicationState.STALE, FeaturePublicationState.ABSENT])
async def test_stale_or_absent_fail_safely(status: FeaturePublicationState) -> None:
    source, work, _, _ = make_model_source()
    work.resolve_current.return_value = CurrentFeatureSnapshotResolution(status)
    with pytest.raises(ModelingError):
        await source.resolve(work.resolve_target.return_value.game.game_id)


async def test_changed_possession_or_pbp_ref_between_resolve_and_read_rejects() -> None:
    source, work, _, _ = make_model_source()
    game_id = work.resolve_target.return_value.game.game_id
    ref = await source.resolve(game_id)
    current = work.resolve_current.return_value.reference
    work.resolve_current.return_value = CurrentFeatureSnapshotResolution(
        FeaturePublicationState.CURRENT,
        replace(current, attempt=replace(current.attempt, snapshot_id=uuid4_value(999))),
    )
    with pytest.raises(ModelingError, match="model_source_changed"):
        await source.read_training(ref)


async def test_feature_score_clock_or_count_mismatch_rejects() -> None:
    source, work, _, snapshots = make_model_source()
    ref = await source.resolve(work.resolve_target.return_value.game.game_id)
    original = snapshots.read_snapshot.return_value
    changed = replace(original.states[1], home_score=3, score_diff=3)
    snapshots.read_snapshot.return_value = FeatureDataset(
        original.game_id, (original.states[0], changed), ()
    )
    with pytest.raises(ModelingError, match="model_source_mismatch"):
        await source.read_training(ref)


async def test_unsupported_configured_normalizer_rejects() -> None:
    source, work, _, _ = make_model_source()
    source._normalizer = Mock(version="nba-pbp-v999")
    with pytest.raises(ModelingError, match="model_normalizer_incompatible"):
        await source.resolve(work.resolve_target.return_value.game.game_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"home_score": 3, "score_diff": 3},
        {"home_possessions": 2},
        {"seconds_remaining_period_ms": 50_000},
    ],
)
async def test_self_consistent_snapshot_hash_cannot_hide_state_possession_mismatch(
    changes: dict[str, object],
) -> None:
    source, work, _, snapshots = make_model_source()
    original = snapshots.read_snapshot.return_value
    changed = replace(original.states[1], **changes)
    features = FeatureDataset(original.game_id, (original.states[0], changed), ())
    snapshots.read_snapshot.return_value = features
    old = work.resolve_current.return_value.reference
    sh, fh = state_output_checksum(features.states), feature_output_checksum(features)
    attempt = replace(
        old.attempt, expected_state_output_sha256=sh, expected_feature_output_sha256=fh
    )
    reference = replace(old, attempt=attempt, state_output_sha256=sh, feature_output_sha256=fh)
    work.resolve_current.return_value = CurrentFeatureSnapshotResolution(
        FeaturePublicationState.CURRENT, reference
    )
    ref = await source.resolve(original.game_id)
    with pytest.raises(ModelingError, match="model_source_mismatch"):
        await source.read_training(ref)


async def test_end_score_mismatch_and_missing_endpoint_reject() -> None:
    for mode in ("wrong_score", "no_end"):
        source, work, _, _ = make_model_source()
        ref = await source.resolve(work.resolve_target.return_value.game.game_id)
        normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(
            work.resolve_target.return_value.pbp_snapshot
        )
        end = replace(normalized.actions[-1], checkpoint_home_score=3)
        actions = (
            (*normalized.actions[:-1], end) if mode == "wrong_score" else normalized.actions[:-1]
        )
        source._normalizer = Mock(version="nba-pbp-v2")
        source._normalizer.normalize.return_value = replace(normalized, actions=actions)
        with pytest.raises(ModelingError, match="model_source_mismatch"):
            await source.read_training(ref)
