"""Behavior tests for immutable, provider-neutral feature contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from nba_live_totals.domain.features import (
    FEATURE_SCALE,
    FeatureCalculationInput,
    FeatureDataset,
    FeatureDependencyManifest,
    FeatureResultStatus,
    FeatureStatDelta,
    FeatureStateKind,
    FeatureVersions,
    GameStateFeature,
    HistoricalPlayerBox,
    HistoricalTeamBox,
    LineupAggregateFeatures,
    NormalizedFeatureEvents,
    PlayerPregameFeatures,
    TeamPregameFeatures,
    scaled_ratio,
)
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.feature_helpers import (
    DIGEST,
    GAME_ID,
    OTHER_GAME_ID,
    make_dependency,
    make_game,
)


def test_scaled_ratio_rounds_half_away_from_zero_without_float_math() -> None:
    assert FEATURE_SCALE == 1_000_000
    assert scaled_ratio(1, 2) == 500_000
    assert scaled_ratio(-1, 2) == -500_000
    assert scaled_ratio(1, 0) is None
    assert scaled_ratio(0, 7) == 0
    assert scaled_ratio(1, 128, multiplier=8) == 62_500
    assert scaled_ratio(1, 2_000_000) == 1
    assert scaled_ratio(-1, 2_000_000) == -1
    assert scaled_ratio(1, -2_000_000) == -1
    assert scaled_ratio(10**30, 3) == 333333333333333333333333333333333333


@pytest.mark.parametrize("bad", [True, 1.0, Decimal("1")])
def test_scaled_ratio_rejects_non_exact_integers(bad: object) -> None:
    with pytest.raises(ValueError, match="integer"):
        scaled_ratio(bad, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        scaled_ratio(1, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        scaled_ratio(1, 2, multiplier=bad)  # type: ignore[arg-type]


def test_versions_have_the_five_safe_defaults_and_reject_unsafe_values() -> None:
    versions = FeatureVersions()
    assert tuple(getattr(versions, field.name) for field in fields(versions)) == (
        "feature-join-v1",
        "feature-formulas-v1",
        "nba-feature-events-v1",
        "game-state-features-v1",
        "player-pregame-features-v1",
    )
    with pytest.raises(ValueError, match="version"):
        FeatureVersions(formula_version="A bad version")
    with pytest.raises(FrozenInstanceError):
        versions.join_version = "changed"  # type: ignore[misc]
    assert not hasattr(versions, "__dict__")


def test_historical_boxes_preserve_nulls_and_reject_noninteger_stats() -> None:
    team = HistoricalTeamBox(
        game_id=GAME_ID,
        team_id=10,
        started_at=datetime(2025, 10, 20, 23, tzinfo=UTC),
        game_date=date(2025, 10, 20),
        is_home=True,
        seconds_played=None,
    )
    player = HistoricalPlayerBox(
        game_id=GAME_ID, team_id=10, player_id=101, played=False, seconds_played=None
    )
    assert team.seconds_played is None
    assert player.seconds_played is None
    assert not any(
        "percentage" in field.name or "plus_minus" in field.name for field in fields(team)
    )
    with pytest.raises(ValueError, match="team ID"):
        replace(team, team_id=True)
    with pytest.raises(ValueError, match="player ID"):
        HistoricalPlayerBox(game_id=GAME_ID, team_id=10, player_id=0, played=True)
    with pytest.raises(ValueError, match="field goals attempted"):
        replace(team, field_goals_attempted=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="played"):
        HistoricalPlayerBox(game_id=GAME_ID, team_id=10, player_id=101, played=1)  # type: ignore[arg-type]


def test_dependency_validates_all_hashes_uuid_and_canonical_manifest_order() -> None:
    later = make_dependency(dependency_game_id=OTHER_GAME_ID, ordinal=1)
    first = make_dependency()
    manifest = FeatureDependencyManifest((later, first))
    assert manifest.dependencies == (first, later)
    assert manifest.dependencies[0].applied_box_score_document_id == UUID(int=1)
    for field_name in (
        "game_context_sha256",
        "applied_box_score_sha256",
        "canonical_team_box_sha256",
        "canonical_player_box_sha256",
        "parse_revision_id",
        "possession_input_token_sha256",
        "possession_output_sha256",
    ):
        with pytest.raises(ValueError, match="SHA-256"):
            make_dependency(**{field_name: DIGEST.upper()})
    with pytest.raises(ValueError, match="UUID"):
        make_dependency(possession_snapshot_id="not-a-uuid")
    with pytest.raises(ValueError, match="duplicate"):
        FeatureDependencyManifest((first, first))
    with pytest.raises(ValueError, match="ordinal"):
        FeatureDependencyManifest((make_dependency(ordinal=1),))
    with pytest.raises(ValueError, match="inclusion"):
        make_dependency(inclusion_reasons=("unknown",))


def test_stat_deltas_require_after_boundary_and_nonnegative_integer_counts() -> None:
    delta = FeatureStatDelta(
        sequence_number=1, boundary=EventBoundary(1, BoundarySide.AFTER), team_id=None
    )
    assert delta.field_goal_attempts == 0
    assert NormalizedFeatureEvents(GAME_ID, [delta]).deltas == (delta,)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="AFTER"):
        FeatureStatDelta(sequence_number=1, boundary=EventBoundary(1, BoundarySide.BEFORE))
    with pytest.raises(ValueError, match="team ID"):
        FeatureStatDelta(
            sequence_number=1, boundary=EventBoundary(1, BoundarySide.AFTER), team_id=True
        )
    with pytest.raises(ValueError, match="turnovers"):
        FeatureStatDelta(
            sequence_number=1,
            boundary=EventBoundary(1, BoundarySide.AFTER),
            turnovers=Decimal("1"),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="team ID"):
        FeatureStatDelta(
            sequence_number=1,
            boundary=EventBoundary(1, BoundarySide.AFTER),
            field_goal_attempts=1,
        )


def test_calculation_input_has_exact_six_fields_and_snapshots_iterables() -> None:
    assert tuple(field.name for field in fields(FeatureCalculationInput)) == (
        "target_game",
        "possessions",
        "lineup_segments",
        "historical_team_boxes",
        "historical_player_boxes",
        "history_possessions",
    )
    input_data = FeatureCalculationInput(make_game(), [], [], [], [], [])  # type: ignore[arg-type]
    assert tuple(input_data.possessions) == ()
    assert tuple(input_data.lineup_segments) == ()
    assert not hasattr(input_data, "__dict__")


def test_dataset_sorts_states_and_players_and_retains_earlier_invalid_status() -> None:
    invalid = GameStateFeature(
        game_id=GAME_ID,
        state_number=0,
        state_kind=FeatureStateKind.PREGAME,
        possession_number=0,
        boundary=EventBoundary(1, BoundarySide.BEFORE),
        home_lineup_status=LineupStatus.INVALID,
        away_lineup_status=LineupStatus.VALID,
        home_invalid_reason="lineup_invalid",
        home_invalid_since=EventBoundary(1, BoundarySide.BEFORE),
        away_player_ids=(201, 202, 203, 204, 205),
    )
    valid = GameStateFeature(
        game_id=GAME_ID,
        state_number=1,
        state_kind=FeatureStateKind.AFTER_POSSESSION,
        possession_number=1,
        boundary=EventBoundary(2, BoundarySide.AFTER),
        home_lineup_status=LineupStatus.VALID,
        away_lineup_status=LineupStatus.VALID,
        home_player_ids=(101, 102, 103, 104, 105),
        away_player_ids=(201, 202, 203, 204, 205),
    )
    players = [
        PlayerPregameFeatures(GAME_ID, 102, 10, True),
        PlayerPregameFeatures(GAME_ID, 101, 10, True),
    ]
    dataset = FeatureDataset(
        GAME_ID,
        [valid, invalid],  # type: ignore[arg-type]
        players,  # type: ignore[arg-type]
        ["lineup_invalid", "lineup_invalid"],  # type: ignore[arg-type]
    )
    players.clear()
    assert tuple(state.state_number for state in dataset.states) == (0, 1)
    assert tuple(player.player_id for player in dataset.players) == (101, 102)
    assert dataset.status is FeatureResultStatus.INVALID
    assert dataset.issues == ("lineup_invalid",)
    with pytest.raises(ValueError, match="state number"):
        FeatureDataset(GAME_ID, [valid], [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate player"):
        player = next(iter(dataset.players))
        FeatureDataset(GAME_ID, [invalid], [player, player])  # type: ignore[arg-type]


def test_issue_codes_are_unique_deterministic_and_capped_at_32() -> None:
    state = GameStateFeature(
        GAME_ID,
        0,
        FeatureStateKind.PREGAME,
        0,
        EventBoundary(1, BoundarySide.BEFORE),
        home_player_ids=(101, 102, 103, 104, 105),
        away_player_ids=(201, 202, 203, 204, 205),
    )
    codes = [f"issue_{number:02d}" for number in range(40)]
    dataset = FeatureDataset(GAME_ID, [state], [], reversed(codes))  # type: ignore[arg-type]
    assert dataset.issues == tuple(reversed(codes))[:32]
    with pytest.raises(ValueError, match="issue"):
        FeatureDataset(GAME_ID, [state], [], ["unsafe issue"])  # type: ignore[arg-type]


def test_state_requires_five_players_on_valid_side_and_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="five players"):
        GameStateFeature(
            GAME_ID, 0, FeatureStateKind.PREGAME, 0, EventBoundary(1, BoundarySide.BEFORE)
        )
    with pytest.raises(ValueError, match="invalid reason"):
        GameStateFeature(
            GAME_ID,
            0,
            FeatureStateKind.PREGAME,
            0,
            EventBoundary(1, BoundarySide.BEFORE),
            home_lineup_status=LineupStatus.INVALID,
            away_player_ids=(201, 202, 203, 204, 205),
        )


def test_feature_groups_reject_floats_and_keep_nullable_metrics() -> None:
    team = TeamPregameFeatures(team_id=10, is_home=True)
    lineup = LineupAggregateFeatures(team_id=10, player_ids=[105, 101, 104, 102, 103])  # type: ignore[arg-type]
    assert team.off_rating_5 is None
    assert lineup.player_ids == (101, 102, 103, 104, 105)
    with pytest.raises(ValueError, match="off rating"):
        TeamPregameFeatures(team_id=10, is_home=True, off_rating_5=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="player ID"):
        LineupAggregateFeatures(team_id=10, player_ids=[101, True])  # type: ignore[arg-type]
