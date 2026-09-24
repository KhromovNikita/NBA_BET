"""Causal aggregate-of-counts team pregame feature behavior."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import UTC, date, datetime

import pytest

from nba_live_totals.domain import features
from nba_live_totals.domain.features import HistoricalTeamBox, TeamPregameFeatures
from nba_live_totals.domain.games import Game, GameStatus
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.possessions import Possession
from tests.unit.domain.feature_helpers import make_game, make_possession


def _history(
    number: int,
    day: int,
    *,
    team_points: int | None = 100,
    opponent_points: int | None = 80,
    team_possessions: int = 100,
    opponent_possessions: int = 100,
    is_home: bool = True,
    period: int = 4,
    hour: int = 12,
) -> tuple[tuple[HistoricalTeamBox, HistoricalTeamBox], Possession]:
    game_id = GameId(f"002250{number:04d}")
    started_at = datetime(2026, 1, day, hour, tzinfo=UTC)
    home_possessions = team_possessions if is_home else opponent_possessions
    away_possessions = opponent_possessions if is_home else team_possessions
    boxes = (
        HistoricalTeamBox(
            game_id=game_id,
            team_id=10,
            started_at=started_at,
            game_date=started_at.date(),
            is_home=is_home,
            points=team_points,
        ),
        HistoricalTeamBox(
            game_id=game_id,
            team_id=20,
            started_at=started_at,
            game_date=started_at.date(),
            is_home=not is_home,
            points=opponent_points,
        ),
    )
    possession = make_possession(
        game_id=game_id,
        period=period,
        home_possessions=home_possessions,
        away_possessions=away_possessions,
    )
    return boxes, possession


def _target(*, day: int = 23, hour: int = 12, home_team_id: int = 10) -> Game:
    return replace(
        make_game(),
        game_id=GameId("0022500099"),
        game_date=date(2026, 1, day),
        started_at=datetime(2026, 1, day, hour, tzinfo=UTC),
        home_team_id=home_team_id,
        away_team_id=20 if home_team_id == 10 else 10,
    )


def _calculate(
    target: Game,
    histories: Iterable[tuple[tuple[HistoricalTeamBox, ...], Possession]],
) -> TeamPregameFeatures:
    histories = tuple(histories)
    return features.build_team_pregame_features(
        target,
        (box for boxes, _ in histories for box in boxes),
        (possession for _, possession in histories),
        team_id=10,
    )


def test_bounded_windows_aggregate_counts_instead_of_mean_game_ratios() -> None:
    # A mean of per-game offensive ratios would be 80, 90, and 95.
    histories = [_history(number, number, is_home=number % 2 == 0) for number in range(1, 21)]
    histories.append(
        _history(
            21,
            21,
            team_points=0,
            opponent_points=0,
            team_possessions=50,
            opponent_possessions=50,
            is_home=False,
        )
    )
    result = _calculate(_target(), reversed(histories))
    assert (result.games_used_5, result.games_used_10, result.games_used_20) == (5, 10, 20)
    assert (result.off_rating_5, result.off_rating_10, result.off_rating_20) == (
        88_888_889,
        94_736_842,
        97_435_897,
    )
    assert (result.def_rating_5, result.def_rating_10, result.def_rating_20) == (
        71_111_111,
        75_789_474,
        77_948_718,
    )
    assert (result.pace_5, result.pace_10, result.pace_20) == (
        90_000_000,
        95_000_000,
        97_500_000,
    )
    assert (result.rest_days, result.back_to_back, result.is_home) == (1, False, True)


def test_incomplete_window_uses_all_games_and_completed_overtime_duration() -> None:
    histories = [
        _history(
            1, 20, team_points=90, opponent_points=80, team_possessions=50, opponent_possessions=50
        ),
        _history(
            2,
            21,
            team_points=120,
            opponent_points=100,
            team_possessions=40,
            opponent_possessions=60,
            is_home=False,
            period=5,
        ),
    ]
    result = _calculate(_target(day=22, home_team_id=20), histories)
    assert (result.games_used_5, result.games_used_10, result.games_used_20) == (2, 2, 2)
    assert (result.off_rating_5, result.def_rating_5, result.pace_5) == (
        233_333_333,
        163_636_364,
        47_524_752,
    )
    assert result.pace_10 == result.pace_20 == 47_524_752
    assert (result.rest_days, result.back_to_back, result.is_home) == (0, True, False)


def test_zero_denominator_is_absent_but_zero_numerator_is_a_real_zero() -> None:
    empty = _calculate(
        _target(),
        [
            _history(
                1, 20, team_points=0, opponent_points=0, team_possessions=0, opponent_possessions=0
            )
        ],
    )
    assert (empty.off_rating_5, empty.def_rating_5, empty.pace_5) == (None, None, 0)
    zero = _calculate(
        _target(),
        [
            _history(
                1,
                20,
                team_points=0,
                opponent_points=0,
                team_possessions=10,
                opponent_possessions=20,
            )
        ],
    )
    assert (zero.off_rating_5, zero.def_rating_5, zero.pace_5) == (0, 0, 15_000_000)


def test_null_points_make_only_the_affected_window_metric_absent() -> None:
    result = _calculate(
        _target(),
        [
            _history(
                1,
                20,
                team_points=None,
                opponent_points=80,
                team_possessions=100,
                opponent_possessions=100,
            )
        ],
    )
    assert result.off_rating_5 is None
    assert (result.def_rating_5, result.pace_5, result.games_used_5) == (
        80_000_000,
        100_000_000,
        1,
    )


def test_final_possession_cumulative_counters_drive_ratings() -> None:
    boxes, first = _history(1, 20)
    first = replace(first, home_possessions=1, away_possessions=0)
    final = replace(first, possession_number=2, home_possessions=10, away_possessions=20)
    result = features.build_team_pregame_features(_target(), boxes, [first, final], team_id=10)
    assert (result.off_rating_5, result.def_rating_5, result.pace_5) == (
        1_000_000_000,
        400_000_000,
        15_000_000,
    )


def test_no_prior_game_has_no_rest_or_rates_and_false_back_to_back() -> None:
    assert _calculate(_target(), []) == TeamPregameFeatures(team_id=10, is_home=True)


def test_same_day_prior_start_has_nonnegative_rest_without_back_to_back() -> None:
    result = _calculate(_target(day=22, hour=20), [_history(1, 22, hour=12)])
    assert (result.rest_days, result.back_to_back) == (0, False)


def test_equal_time_current_and_future_rows_cannot_change_serialized_output() -> None:
    earlier = _history(50, 20)
    equal_time = _history(1, 23, hour=12)
    current = _history(99, 22, hour=12)
    future = _history(100, 24)
    target = _target()
    baseline = _calculate(target, [earlier, equal_time, current, future])
    changed = []
    for boxes, possession in (equal_time, current, future):
        changed.append(
            (
                (
                    replace(boxes[0], points=999, is_home=not boxes[0].is_home),
                    replace(boxes[1], points=999, is_home=not boxes[1].is_home),
                ),
                replace(possession, home_possessions=999, away_possessions=999, period=8),
            )
        )
    mutated = _calculate(target, [earlier, *changed])
    assert baseline.games_used_20 == 1
    assert baseline == mutated
    assert (
        json.dumps(asdict(baseline), sort_keys=True).encode()
        == json.dumps(asdict(mutated), sort_keys=True).encode()
    )


def test_missing_or_mixed_history_structure_raises_static_value_error() -> None:
    boxes, possession = _history(1, 20)
    bad_histories = [
        ((boxes[0],), possession),
        ((boxes[0], boxes[0]), possession),
        ((boxes[0], replace(boxes[1], is_home=True)), possession),
        ((boxes[0], replace(boxes[1], started_at=datetime(2026, 1, 19, tzinfo=UTC))), possession),
    ]
    for bad_boxes, valid_possession in bad_histories:
        with pytest.raises(ValueError, match="historical game structure"):
            _calculate(_target(), [(bad_boxes, valid_possession)])
    with pytest.raises(ValueError, match="historical game structure"):
        features.build_team_pregame_features(_target(), boxes, [], team_id=10)
    with pytest.raises(ValueError, match="historical game structure"):
        features.build_team_pregame_features(_target(), boxes, [possession, possession], team_id=10)
    with pytest.raises(ValueError, match="historical game structure"):
        features.build_team_pregame_features(
            _target(), boxes, [replace(possession, possession_number=2), possession], team_id=10
        )
    with pytest.raises(ValueError, match="historical game structure"):
        features.build_team_pregame_features(
            _target(), boxes, [replace(possession, defense_team_id=30)], team_id=10
        )


def test_target_must_be_final_and_team_must_belong_to_target() -> None:
    with pytest.raises(ValueError, match="target"):
        features.build_team_pregame_features(
            replace(_target(), status=GameStatus.LIVE), [], [], team_id=10
        )
    with pytest.raises(ValueError, match="team ID"):
        features.build_team_pregame_features(_target(), [], [], team_id=30)


def test_historical_box_context_requires_utc_datetime_date_and_exact_bool() -> None:
    boxes, _ = _history(1, 20)
    assert boxes[0].started_at.tzinfo is UTC
    for changes in (
        {"started_at": datetime(2026, 1, 20, 12)},
        {"game_date": datetime(2026, 1, 20, 12, tzinfo=UTC)},
        {"is_home": 1},
    ):
        with pytest.raises(ValueError):
            replace(boxes[0], **changes)
