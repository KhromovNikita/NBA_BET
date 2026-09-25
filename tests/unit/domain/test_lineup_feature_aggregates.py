"""Exactly five target profiles form component-wise lineup means."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nba_live_totals.domain.features import (
    LineupAggregateFeatures,
    PlayerPregameFeatures,
    build_lineup_aggregate_features,
)
from tests.unit.domain.feature_helpers import GAME_ID


def _profile(player_id: int, **updates: object) -> PlayerPregameFeatures:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "player_id": player_id,
        "team_id": 10,
        "is_home": True,
        "games_used": 1,
        "seconds_played": 60,
        "off_rating": player_id,
        "def_rating": player_id + 10,
        "usage_rate": player_id + 20,
        "three_point_attempt_rate": player_id + 30,
        "free_throw_rate": player_id + 40,
        "minutes_avg": player_id + 50,
        "pace_context": player_id + 60,
    }
    values.update(updates)
    return PlayerPregameFeatures(**values)  # type: ignore[arg-type]


def test_five_profiles_are_averaged_independently_with_half_away_rounding() -> None:
    profiles = tuple(_profile(player_id) for player_id in range(101, 106))

    result = build_lineup_aggregate_features(10, reversed(range(101, 106)), reversed(profiles))

    assert result == LineupAggregateFeatures(
        team_id=10,
        player_ids=(101, 102, 103, 104, 105),
        profiled_player_count=5,
        lineup_avg_off_rating=103,
        lineup_avg_def_rating=113,
        lineup_avg_usage_rate=123,
        lineup_avg_three_point_attempt_rate=133,
        lineup_avg_free_throw_rate=143,
        lineup_avg_minutes=153,
        lineup_avg_pace_context=163,
    )
    assert not hasattr(result, "lineup_pair_synergy")


def test_one_null_component_nulls_only_that_lineup_mean() -> None:
    profiles = tuple(_profile(player_id) for player_id in range(101, 106))
    profiles = (*profiles[:2], replace(profiles[2], usage_rate=None), *profiles[3:])

    result = build_lineup_aggregate_features(10, range(101, 106), profiles)

    assert result.profiled_player_count == 5
    assert result.lineup_avg_usage_rate is None
    assert result.lineup_avg_off_rating == 103
    assert result.lineup_avg_three_point_attempt_rate == 133


def test_lineup_mean_rounds_the_final_fraction_instead_of_each_profile() -> None:
    profiles = tuple(
        _profile(player_id, off_rating=3 if player_id == 105 else 0)
        for player_id in range(101, 106)
    )

    result = build_lineup_aggregate_features(10, range(101, 106), profiles)

    assert result.lineup_avg_off_rating == 1


@pytest.mark.parametrize(
    ("player_ids", "profiles", "message"),
    [
        ((101, 102, 103, 104), tuple(_profile(i) for i in range(101, 105)), "five"),
        ((101, 102, 103, 104, 104), tuple(_profile(i) for i in range(101, 106)), "distinct"),
        ((101, 102, 103, 104, 105), tuple(_profile(i) for i in range(101, 105)), "matching"),
        (
            (101, 102, 103, 104, 105),
            (*tuple(_profile(i) for i in range(101, 106)), _profile(106)),
            "matching",
        ),
        (
            (101, 102, 103, 104, 105),
            (*tuple(_profile(i) for i in range(101, 105)), _profile(105, team_id=20)),
            "team",
        ),
    ],
)
def test_lineup_requires_exactly_one_matching_profile_for_each_distinct_player(
    player_ids: tuple[int, ...],
    profiles: tuple[PlayerPregameFeatures, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_lineup_aggregate_features(10, player_ids, profiles)
