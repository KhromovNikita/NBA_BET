"""Golden vectors for canonical Phase 5 feature identities."""

from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum,
    canonical_player_boxes_projection,
    canonical_team_boxes_checksum,
    canonical_team_boxes_projection,
    dependency_input_checksum,
    dependency_input_projection,
    dependency_revision_checksum,
    dependency_revision_projection,
    feature_input_token_checksum,
    feature_input_token_projection,
    feature_output_checksum,
    feature_output_projection,
    feature_revision_id,
    feature_revision_projection,
    player_output_checksum,
    player_output_projection,
    state_output_checksum,
    state_output_projection,
)
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureStateKind,
    FeatureVersions,
    GameStateFeature,
    HistoricalPlayerBox,
    HistoricalTeamBox,
    LineupAggregateFeatures,
    LiveFeatures,
    PlayerPregameFeatures,
    TeamPregameFeatures,
)
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary

GAME_ID = GameId("0022500123")
OTHER_GAME_ID = GameId("0022500124")
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
BOX_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
POSSESSION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
LINEUP_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def make_team_box(*, team_id: int, **overrides: object) -> HistoricalTeamBox:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "team_id": team_id,
        "started_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        "game_date": date(2025, 1, 2),
        "is_home": team_id == 10,
        "seconds_played": 14_400,
        "field_goals_made": 40,
        "field_goals_attempted": 80,
        "three_pointers_made": 12,
        "three_pointers_attempted": 30,
        "free_throws_made": 18,
        "free_throws_attempted": 20,
        "rebounds_offensive": 9,
        "rebounds_defensive": 31,
        "rebounds_total": 40,
        "assists": 25,
        "turnovers": 11,
        "points": 110,
    }
    values.update(overrides)
    return HistoricalTeamBox(**values)  # type: ignore[arg-type]


def make_player_box(*, team_id: int, player_id: int, **overrides: object) -> HistoricalPlayerBox:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "team_id": team_id,
        "player_id": player_id,
        "played": True,
        "seconds_played": 1_800,
        "field_goals_made": 5,
        "field_goals_attempted": 11,
        "three_pointers_made": 2,
        "three_pointers_attempted": 5,
        "free_throws_made": 3,
        "free_throws_attempted": 4,
        "rebounds_offensive": 1,
        "rebounds_defensive": 4,
        "rebounds_total": 5,
        "assists": 6,
        "turnovers": 2,
        "points": 15,
    }
    values.update(overrides)
    return HistoricalPlayerBox(**values)  # type: ignore[arg-type]


def make_dependency(*, ordinal: int, **overrides: object) -> FeatureDependency:
    values: dict[str, object] = {
        "dependency_game_id": GameId(f"00225001{ordinal + 20:02d}"),
        "started_at": datetime(2025, 1, ordinal + 1, 3, 4, 5, tzinfo=UTC),
        "game_context_sha256": SHA_A,
        "applied_box_score_document_id": UUID(int=ordinal + 1),
        "applied_box_score_sha256": SHA_B,
        "canonical_team_box_sha256": SHA_C,
        "canonical_player_box_sha256": SHA_D,
        "possession_snapshot_id": UUID(int=ordinal + 11),
        "parse_revision_id": SHA_E,
        "possession_input_token_sha256": SHA_F,
        "possession_output_sha256": SHA_A,
        "ordinal": ordinal,
        "inclusion_reasons": ("player_history", "home_team_window"),
    }
    values.update(overrides)
    return FeatureDependency(**values)  # type: ignore[arg-type]


def make_manifest() -> FeatureDependencyManifest:
    return FeatureDependencyManifest(
        (
            make_dependency(ordinal=0),
            make_dependency(
                ordinal=1,
                game_context_sha256=SHA_B,
                applied_box_score_document_id=UUID(int=2),
                possession_snapshot_id=UUID(int=12),
                possession_input_token_sha256=SHA_A,
                inclusion_reasons=("away_team_window",),
            ),
        )
    )


def make_player(*, player_id: int, team_id: int, **overrides: object) -> PlayerPregameFeatures:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "player_id": player_id,
        "team_id": team_id,
        "is_home": team_id == 10,
        "games_used": 7,
        "seconds_played": 12_345,
        "estimated_possessions": 400_001,
        "minutes_avg": 1_763_571,
        "points_per_100": 2_200_000,
        "off_rating": 1_100_000,
        "def_rating": 1_050_000,
        "usage_rate": 225_000,
        "assist_rate": 180_000,
        "turnover_rate": 90_000,
        "three_point_attempt_rate": 410_000,
        "three_point_percentage": 375_000,
        "free_throw_rate": 260_000,
        "rebound_rate": 120_000,
        "pace_context": 995_000,
    }
    values.update(overrides)
    return PlayerPregameFeatures(**values)  # type: ignore[arg-type]


def make_team_pregame(team_id: int) -> TeamPregameFeatures:
    return TeamPregameFeatures(
        team_id=team_id,
        is_home=team_id == 10,
        games_used_5=5,
        games_used_10=8,
        games_used_20=8,
        off_rating_5=1_100_001,
        off_rating_10=1_090_002,
        off_rating_20=None,
        def_rating_5=1_020_003,
        def_rating_10=1_030_004,
        def_rating_20=None,
        pace_5=980_005,
        pace_10=990_006,
        pace_20=None,
        rest_days=2,
        back_to_back=False,
    )


def make_lineup(team_id: int, player_ids: tuple[int, ...]) -> LineupAggregateFeatures:
    return LineupAggregateFeatures(
        team_id=team_id,
        player_ids=player_ids,
        profiled_player_count=5,
        lineup_avg_off_rating=1_111_001,
        lineup_avg_def_rating=999_002,
        lineup_avg_usage_rate=222_003,
        lineup_avg_three_point_attempt_rate=333_004,
        lineup_avg_free_throw_rate=444_005,
        lineup_avg_minutes=1_800_006,
        lineup_avg_pace_context=1_000_007,
    )


def make_live() -> LiveFeatures:
    values: dict[str, int | None] = {}
    next_value = 101
    for item in fields(LiveFeatures):
        if item.name.endswith("possessions_used"):
            values[item.name] = 3
        else:
            values[item.name] = next_value
            next_value += 1
    return LiveFeatures(**values)  # type: ignore[arg-type]


def make_states() -> tuple[GameStateFeature, ...]:
    home_ids = (101, 102, 103, 104, 105)
    away_ids = (201, 202, 203, 204, 205)
    pregame = GameStateFeature(
        game_id=GAME_ID,
        state_number=0,
        state_kind=FeatureStateKind.PREGAME,
        possession_number=0,
        boundary=EventBoundary(1, BoundarySide.BEFORE),
        home_player_ids=home_ids,
        away_player_ids=away_ids,
        seconds_remaining_game_ms=2_880_000,
        seconds_remaining_period_ms=720_000,
        home_pregame=make_team_pregame(10),
        away_pregame=make_team_pregame(20),
        home_lineup=make_lineup(10, home_ids),
        away_lineup=make_lineup(20, away_ids),
        live=LiveFeatures(),
        offense_difference=112,
        defense_difference=-31,
        pace_difference=7,
    )
    invalid = GameStateFeature(
        game_id=GAME_ID,
        state_number=1,
        state_kind=FeatureStateKind.AFTER_POSSESSION,
        possession_number=1,
        boundary=EventBoundary(3, BoundarySide.AFTER),
        home_lineup_status=LineupStatus.INVALID,
        away_lineup_status=LineupStatus.VALID,
        home_player_ids=(),
        away_player_ids=away_ids,
        home_invalid_reason="unknown_substitution",
        home_invalid_since=EventBoundary(2, BoundarySide.AFTER),
        period=1,
        seconds_remaining_game_ms=2_860_000,
        seconds_remaining_period_ms=700_000,
        home_score=2,
        away_score=0,
        score_diff=2,
        home_possessions=1,
        away_possessions=0,
        home_pregame=make_team_pregame(10),
        away_pregame=make_team_pregame(20),
        home_lineup=LineupAggregateFeatures(10, ()),
        away_lineup=make_lineup(20, away_ids),
        live=make_live(),
    )
    return pregame, invalid


def make_dataset() -> FeatureDataset:
    return FeatureDataset(
        GAME_ID,
        make_states(),
        (make_player(player_id=201, team_id=20), make_player(player_id=101, team_id=10)),
        ("source_quality", "unknown_substitution", "source_quality"),
    )


def test_team_box_projection_pins_formula_subset_order_nulls_and_golden_vector() -> None:
    team_10 = make_team_box(team_id=10)
    team_2 = make_team_box(
        team_id=2,
        seconds_played=None,
        field_goals_made=None,
        points=99,
        is_home=False,
    )
    projection = canonical_team_boxes_projection((team_10, team_2))
    expected = (
        b'{"schema":"canonical-feature-team-box-v1","teams":['
        b'{"assists":25,"field_goals_attempted":80,"field_goals_made":null,'
        b'"free_throws_attempted":20,"free_throws_made":18,"game_id":"0022500123",'
        b'"points":99,"rebounds_defensive":31,"rebounds_offensive":9,"rebounds_total":40,'
        b'"seconds_played":null,"team_id":2,"three_pointers_attempted":30,'
        b'"three_pointers_made":12,"turnovers":11},'
        b'{"assists":25,"field_goals_attempted":80,"field_goals_made":40,'
        b'"free_throws_attempted":20,"free_throws_made":18,"game_id":"0022500123",'
        b'"points":110,"rebounds_defensive":31,"rebounds_offensive":9,"rebounds_total":40,'
        b'"seconds_played":14400,"team_id":10,"three_pointers_attempted":30,'
        b'"three_pointers_made":12,"turnovers":11}]}'
    )

    expected_sha256 = "e61e1f8df7fc522a69dc6e1d5c4d50bb831686bf3d6ebbabe62c00301d3bfa6f"
    assert hashlib.sha256(expected).hexdigest() == expected_sha256
    assert canonical_json_bytes(projection) == expected
    assert canonical_team_boxes_checksum((team_10, team_2)) == expected_sha256
    assert canonical_team_boxes_checksum((team_2, team_10)) == expected_sha256
    metadata_only = replace(
        team_10,
        started_at=team_10.started_at + timedelta(days=50),
        game_date=date(2025, 9, 9),
        is_home=not team_10.is_home,
    )
    assert canonical_team_boxes_checksum((team_2, metadata_only)) == expected_sha256


def test_player_box_projection_pins_formula_subset_order_nulls_and_golden_vector() -> None:
    player_10 = make_player_box(team_id=10, player_id=2)
    player_2 = make_player_box(
        team_id=2,
        player_id=99,
        played=False,
        seconds_played=None,
        field_goals_made=None,
        points=None,
    )
    projection = canonical_player_boxes_projection((player_10, player_2))
    expected = (
        b'{"players":[{"assists":6,"field_goals_attempted":11,"field_goals_made":null,'
        b'"free_throws_attempted":4,"free_throws_made":3,"game_id":"0022500123",'
        b'"played":false,"player_id":99,"points":null,"rebounds_defensive":4,'
        b'"rebounds_offensive":1,"rebounds_total":5,"seconds_played":null,"team_id":2,'
        b'"three_pointers_attempted":5,"three_pointers_made":2,"turnovers":2},'
        b'{"assists":6,"field_goals_attempted":11,"field_goals_made":5,'
        b'"free_throws_attempted":4,"free_throws_made":3,"game_id":"0022500123",'
        b'"played":true,"player_id":2,"points":15,"rebounds_defensive":4,'
        b'"rebounds_offensive":1,"rebounds_total":5,"seconds_played":1800,"team_id":10,'
        b'"three_pointers_attempted":5,"three_pointers_made":2,"turnovers":2}],'
        b'"schema":"canonical-feature-player-box-v1"}'
    )

    expected_sha256 = "51db64e90826d886bd78c21443e73ccba9845b4fd2b5675454c38b7b4f32c735"
    assert hashlib.sha256(expected).hexdigest() == expected_sha256
    assert canonical_json_bytes(projection) == expected
    assert canonical_player_boxes_checksum((player_10, player_2)) == expected_sha256
    assert canonical_player_boxes_checksum((player_2, player_10)) == expected_sha256


@pytest.mark.parametrize(
    "projector, rows",
    [
        (
            canonical_team_boxes_projection,
            (make_team_box(team_id=10), make_team_box(team_id=10)),
        ),
        (
            canonical_player_boxes_projection,
            (
                make_player_box(team_id=10, player_id=1),
                make_player_box(team_id=10, player_id=1),
            ),
        ),
    ],
)
def test_box_projections_reject_duplicate_logical_keys(projector: Any, rows: object) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        projector(rows)


def test_box_projections_reject_mixed_games_and_non_domain_rows() -> None:
    with pytest.raises(ValueError, match="one game"):
        canonical_team_boxes_projection(
            (make_team_box(team_id=10), make_team_box(team_id=20, game_id=OTHER_GAME_ID))
        )
    with pytest.raises(TypeError, match="HistoricalPlayerBox"):
        canonical_player_boxes_projection((object(),))  # type: ignore[arg-type]


def test_dependency_projections_pin_operational_and_semantic_golden_vectors() -> None:
    manifest = make_manifest()
    operational = dependency_input_projection(manifest)
    semantic = dependency_revision_projection(manifest)
    operational_expected = (
        b'{"dependencies":[{"applied_box_score_document_id":"00000000-0000-0000-0000-000000000001",'
        b'"applied_box_score_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"canonical_player_box_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_team_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"dependency_game_id":"0022500120","game_context_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"inclusion_reasons":["home_team_window","player_history"],"ordinal":0,'
        b'"parse_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"possession_input_token_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
        b'"possession_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"possession_snapshot_id":"00000000-0000-0000-0000-00000000000b",'
        b'"started_at":"2025-01-01T03:04:05+00:00"},'
        b'{"applied_box_score_document_id":"00000000-0000-0000-0000-000000000002",'
        b'"applied_box_score_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"canonical_player_box_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_team_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"dependency_game_id":"0022500121","game_context_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"inclusion_reasons":["away_team_window"],"ordinal":1,'
        b'"parse_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"possession_input_token_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"possession_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"possession_snapshot_id":"00000000-0000-0000-0000-00000000000c",'
        b'"started_at":"2025-01-02T03:04:05+00:00"}],"schema":"dependency-input-v1"}'
    )
    semantic_expected = (
        b'{"dependencies":[{"applied_box_score_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"canonical_player_box_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_team_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"dependency_game_id":"0022500120","game_context_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"inclusion_reasons":["home_team_window","player_history"],"ordinal":0,'
        b'"parse_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"possession_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"started_at":"2025-01-01T03:04:05+00:00"},'
        b'{"applied_box_score_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"canonical_player_box_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_team_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"dependency_game_id":"0022500121","game_context_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"inclusion_reasons":["away_team_window"],"ordinal":1,'
        b'"parse_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"possession_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"started_at":"2025-01-02T03:04:05+00:00"}],"schema":"dependency-revision-v1"}'
    )

    assert canonical_json_bytes(operational) == operational_expected
    assert canonical_json_bytes(semantic) == semantic_expected
    assert dependency_input_checksum(manifest) == (
        "ac6e7ef16bf08b2b282f7c4710ba79f7854a67cd197cf41c9dbee7cb8825db2c"
    )
    assert dependency_revision_checksum(manifest) == (
        "bc89a994b1a0025ba177a66bfefaa102e928903918eca530b2387944b226b6df"
    )


def test_dependency_materialization_changes_input_but_not_revision() -> None:
    original = make_manifest()
    first, second = original.dependencies
    rematerialized = FeatureDependencyManifest(
        (
            replace(
                first,
                applied_box_score_document_id=UUID(int=100),
                possession_snapshot_id=UUID(int=101),
                possession_input_token_sha256=SHA_B,
            ),
            second,
        )
    )
    assert dependency_input_checksum(rematerialized) != dependency_input_checksum(original)
    assert dependency_revision_checksum(rematerialized) == dependency_revision_checksum(original)


def token_args() -> dict[str, object]:
    return {
        "game_id": GAME_ID,
        "target_started_at": datetime(2025, 10, 20, 23, 1, 2, 345678, tzinfo=UTC),
        "game_context_sha256": SHA_A,
        "possession_snapshot_id": POSSESSION_ID,
        "parse_revision_id": SHA_B,
        "possession_input_token_sha256": SHA_C,
        "possession_output_sha256": SHA_D,
        "lineup_snapshot_id": LINEUP_ID,
        "lineup_revision_id": SHA_E,
        "lineup_input_token_sha256": SHA_F,
        "lineup_output_sha256": SHA_A,
        "lineup_result_status": LineupStatus.INVALID,
        "dependency_input_sha256": SHA_B,
        "versions": FeatureVersions(),
    }


def revision_args() -> dict[str, object]:
    values = token_args()
    return {
        key: value
        for key, value in values.items()
        if key
        not in {
            "possession_snapshot_id",
            "possession_input_token_sha256",
            "lineup_snapshot_id",
            "lineup_input_token_sha256",
            "dependency_input_sha256",
        }
    } | {"dependency_revision_sha256": SHA_C}


def revision_args_from_token(values: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if key
        not in {
            "possession_snapshot_id",
            "possession_input_token_sha256",
            "lineup_snapshot_id",
            "lineup_input_token_sha256",
            "dependency_input_sha256",
        }
    } | {"dependency_revision_sha256": SHA_C}


def test_feature_token_and_revision_pin_cutoff_versions_and_golden_vectors() -> None:
    token = feature_input_token_projection(**token_args())  # type: ignore[arg-type]
    revision = feature_revision_projection(**revision_args())  # type: ignore[arg-type]
    token_expected = (
        b'{"dependency_input_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"event_normalizer_version":"nba-feature-events-v1",'
        b'"formula_version":"feature-formulas-v1",'
        b'"game_context_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"game_id":"0022500123","join_version":"feature-join-v1",'
        b'"lineup_input_token_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
        b'"lineup_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"lineup_result_status":"invalid",'
        b'"lineup_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"lineup_snapshot_id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc",'
        b'"output_schema_version":"game-state-features-v1",'
        b'"parse_revision_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"player_output_schema_version":"player-pregame-features-v1",'
        b'"possession_input_token_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"possession_output_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"possession_snapshot_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",'
        b'"schema":"feature-input-token-v1","target_started_at":"2025-10-20T23:01:02.345678+00:00"}'
    )
    revision_expected = (
        b'{"dependency_revision_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"event_normalizer_version":"nba-feature-events-v1",'
        b'"formula_version":"feature-formulas-v1",'
        b'"game_context_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"game_id":"0022500123","join_version":"feature-join-v1",'
        b'"lineup_output_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"lineup_result_status":"invalid",'
        b'"lineup_revision_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"output_schema_version":"game-state-features-v1",'
        b'"parse_revision_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"player_output_schema_version":"player-pregame-features-v1",'
        b'"possession_output_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"schema":"feature-revision-v1","target_started_at":"2025-10-20T23:01:02.345678+00:00"}'
    )

    assert canonical_json_bytes(token) == token_expected
    assert canonical_json_bytes(revision) == revision_expected
    assert feature_input_token_checksum(**token_args()) == (  # type: ignore[arg-type]
        "c6a4b3e17bbc7a53d7d2616873fd7461c85ec330dc9716092ab8fd0da59ef77e"
    )
    assert feature_revision_id(**revision_args()) == (  # type: ignore[arg-type]
        "10f284291422ba0719557421c3416cca11dc6dee6a7e1a4b9b6fcd4776ea49fc"
    )


def test_target_materialization_changes_token_but_not_revision() -> None:
    original_token = token_args()
    rematerialized = dict(original_token)
    rematerialized.update(
        possession_snapshot_id=UUID(int=900),
        possession_input_token_sha256=SHA_D,
        lineup_snapshot_id=UUID(int=901),
        lineup_input_token_sha256=SHA_C,
        dependency_input_sha256=SHA_E,
    )
    assert feature_input_token_checksum(**rematerialized) != feature_input_token_checksum(  # type: ignore[arg-type]
        **original_token  # type: ignore[arg-type]
    )
    original_revision = feature_revision_id(
        **revision_args_from_token(original_token)  # type: ignore[arg-type]
    )
    rematerialized_revision = feature_revision_id(
        **revision_args_from_token(rematerialized)  # type: ignore[arg-type]
    )
    assert rematerialized_revision == original_revision


def test_feature_revision_changes_for_cutoff_content_status_and_every_version() -> None:
    base = revision_args()
    original = feature_revision_id(**base)  # type: ignore[arg-type]
    changes: tuple[tuple[str, object], ...] = (
        ("game_id", OTHER_GAME_ID),
        ("target_started_at", cast(datetime, base["target_started_at"]) + timedelta(seconds=1)),
        ("game_context_sha256", SHA_F),
        ("parse_revision_id", SHA_F),
        ("possession_output_sha256", SHA_F),
        ("lineup_revision_id", SHA_F),
        ("lineup_output_sha256", SHA_F),
        ("lineup_result_status", LineupStatus.VALID),
        ("dependency_revision_sha256", SHA_F),
    )
    for name, value in changes:
        changed = dict(base)
        changed[name] = value
        assert feature_revision_id(**changed) != original  # type: ignore[arg-type]
    for item in fields(FeatureVersions):
        changed = dict(base)
        changed["versions"] = replace(FeatureVersions(), **{item.name: "changed-v1"})
        assert feature_revision_id(**changed) != original  # type: ignore[arg-type]


def test_state_output_projects_every_semantic_field_and_pins_hash() -> None:
    states = make_states()
    projection = state_output_projection(reversed(states))
    rows = cast(list[dict[str, object]], projection["rows"])

    assert projection["schema"] == "feature-state-output-v1"
    assert [row["state_number"] for row in rows] == [0, 1]
    assert set(rows[1]) == {item.name for item in fields(GameStateFeature)}
    assert set(cast(dict[str, object], rows[1]["home_pregame"])) == {
        item.name for item in fields(TeamPregameFeatures)
    }
    assert set(cast(dict[str, object], rows[1]["home_lineup"])) == {
        item.name for item in fields(LineupAggregateFeatures)
    }
    assert set(cast(dict[str, object], rows[1]["live"])) == {
        item.name for item in fields(LiveFeatures)
    }
    assert rows[1]["home_invalid_reason"] == "unknown_substitution"
    assert rows[1]["home_invalid_since"] == {"sequence_number": 2, "side": "after"}
    assert rows[1]["home_player_ids"] == ()
    assert state_output_checksum(states) == (
        "d3f428297943a58b8d9df8435b5754bfeb391c706cec576e9ce51e23e5c69f46"
    )
    assert state_output_checksum(reversed(states)) == (
        "d3f428297943a58b8d9df8435b5754bfeb391c706cec576e9ce51e23e5c69f46"
    )


def test_player_output_projects_every_semantic_field_and_pins_hash() -> None:
    players = (make_player(player_id=201, team_id=20), make_player(player_id=101, team_id=10))
    projection = player_output_projection(players)
    rows = cast(list[dict[str, object]], projection["rows"])

    assert projection["schema"] == "feature-player-output-v1"
    assert [row["player_id"] for row in rows] == [101, 201]
    assert set(rows[0]) == {item.name for item in fields(PlayerPregameFeatures)}
    assert player_output_checksum(players) == (
        "cf658a3ee83ee0ee1fa38224e2945b18bc1626205c3cba7ff7d16a5ec279908d"
    )
    assert player_output_checksum(reversed(players)) == (
        "cf658a3ee83ee0ee1fa38224e2945b18bc1626205c3cba7ff7d16a5ec279908d"
    )


def test_output_projections_reject_mixed_duplicate_or_noncontiguous_rows() -> None:
    state_0, state_1 = make_states()
    with pytest.raises(ValueError, match="duplicate"):
        state_output_projection((state_0, state_0))
    with pytest.raises(ValueError, match="contiguous"):
        state_output_projection((state_1,))
    with pytest.raises(ValueError, match="one game"):
        state_output_projection((state_0, replace(state_1, game_id=OTHER_GAME_ID)))
    player = make_player(player_id=101, team_id=10)
    with pytest.raises(ValueError, match="duplicate"):
        player_output_projection((player, player))
    with pytest.raises(ValueError, match="one game"):
        player_output_projection((player, replace(player, game_id=OTHER_GAME_ID)))


def test_combined_output_contains_only_semantic_hashes_counts_status_and_issues() -> None:
    dataset = make_dataset()
    projection = feature_output_projection(dataset)

    assert projection == {
        "schema": "feature-output-v1",
        "state_output_sha256": "d3f428297943a58b8d9df8435b5754bfeb391c706cec576e9ce51e23e5c69f46",
        "state_row_count": 2,
        "player_output_sha256": "cf658a3ee83ee0ee1fa38224e2945b18bc1626205c3cba7ff7d16a5ec279908d",
        "player_row_count": 2,
        "result_status": "invalid",
        "issue_codes": ["unknown_substitution", "source_quality"],
    }
    assert feature_output_checksum(dataset) == (
        "d92edf967bbfafc1d33b666d1d19026c5a438fc76378ea5683bbf86861c3d92d"
    )
    assert (
        not {
            "snapshot_id",
            "attempt_id",
            "generation",
            "insert_version",
            "created_at",
            "updated_at",
        }
        & projection.keys()
    )


def test_hashing_revalidates_domain_arguments_and_canonical_guard_rejects_numbers() -> None:
    with pytest.raises(TypeError, match="FeatureDependencyManifest"):
        dependency_input_projection(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="FeatureVersions"):
        feature_revision_projection(**(revision_args() | {"versions": object()}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="SHA-256"):
        feature_input_token_projection(**(token_args() | {"game_context_sha256": "A" * 64}))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="strict canonical JSON"):
        canonical_json_bytes({"metric": 1.5})
    with pytest.raises(TypeError, match="strict canonical JSON"):
        canonical_json_bytes({"metric": Decimal("1.5")})
