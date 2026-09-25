"""Causal last-20 player profiles use exact aggregate formulas."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from nba_live_totals.domain.features import (
    HistoricalPlayerBox,
    HistoricalTeamBox,
    PlayerPregameFeatures,
    build_player_pregame_features,
)
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.possessions import Possession
from tests.unit.domain.feature_helpers import make_game, make_possession


def _dependency(
    number: int,
    *,
    started_at: datetime,
    player_team_id: int,
    opponent_team_id: int,
    player_seconds: int | None,
    played: bool = True,
    player_points: int | None = 20,
    player_fgm: int | None = 8,
    player_fga: int | None = 16,
    player_three_pm: int | None = 3,
    player_three_pa: int | None = 8,
    player_fta: int | None = 4,
    player_rebounds: int | None = 6,
    player_assists: int | None = 5,
    player_turnovers: int | None = 2,
    team_fgm: int | None = 40,
    team_fga: int | None = 80,
    team_fta: int | None = 20,
    team_turnovers: int | None = 10,
    team_rebounds: int | None = 40,
    team_points: int | None = 100,
    opponent_rebounds: int | None = 35,
    opponent_points: int | None = 90,
    team_possessions: int = 100,
    opponent_possessions: int = 90,
    is_home: bool = True,
    period: int = 4,
    player_id: int = 101,
) -> tuple[HistoricalPlayerBox, tuple[HistoricalTeamBox, HistoricalTeamBox], Possession]:
    game_id = GameId(f"002240{number:04d}")
    player = HistoricalPlayerBox(
        game_id=game_id,
        team_id=player_team_id,
        player_id=player_id,
        played=played,
        seconds_played=player_seconds,
        field_goals_made=player_fgm,
        field_goals_attempted=player_fga,
        three_pointers_made=player_three_pm,
        three_pointers_attempted=player_three_pa,
        free_throws_attempted=player_fta,
        rebounds_total=player_rebounds,
        assists=player_assists,
        turnovers=player_turnovers,
        points=player_points,
    )
    boxes = (
        HistoricalTeamBox(
            game_id=game_id,
            team_id=player_team_id,
            started_at=started_at,
            game_date=started_at.date(),
            is_home=is_home,
            field_goals_made=team_fgm,
            field_goals_attempted=team_fga,
            free_throws_attempted=team_fta,
            rebounds_total=team_rebounds,
            turnovers=team_turnovers,
            points=team_points,
        ),
        HistoricalTeamBox(
            game_id=game_id,
            team_id=opponent_team_id,
            started_at=started_at,
            game_date=started_at.date(),
            is_home=not is_home,
            rebounds_total=opponent_rebounds,
            points=opponent_points,
        ),
    )
    possession = make_possession(
        game_id=game_id,
        offense_team_id=player_team_id,
        defense_team_id=opponent_team_id,
        period=period,
        home_possessions=team_possessions if is_home else opponent_possessions,
        away_possessions=opponent_possessions if is_home else team_possessions,
    )
    return player, boxes, possession


def _calculate(
    *dependencies: tuple[HistoricalPlayerBox, tuple[HistoricalTeamBox, ...], Possession],
) -> PlayerPregameFeatures:
    target = replace(
        make_game(),
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
        game_date=datetime(2026, 2, 1, tzinfo=UTC).date(),
    )
    return build_player_pregame_features(
        target,
        (player for player, _, _ in dependencies),
        (box for _, boxes, _ in dependencies for box in boxes),
        (possession for _, _, possession in dependencies),
        player_id=101,
        team_id=10,
    )


def test_two_trade_appearances_pin_every_player_formula() -> None:
    first = _dependency(
        1,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        player_team_id=30,
        opponent_team_id=31,
        player_seconds=1_440,
    )
    second = _dependency(
        2,
        started_at=datetime(2026, 1, 2, tzinfo=UTC),
        player_team_id=40,
        opponent_team_id=41,
        player_seconds=720,
        player_points=10,
        player_fgm=4,
        player_fga=8,
        player_three_pm=1,
        player_three_pa=4,
        player_fta=2,
        player_rebounds=3,
        player_assists=2,
        player_turnovers=1,
        team_fgm=30,
        team_fga=70,
        team_fta=10,
        team_turnovers=8,
        team_rebounds=30,
        team_points=80,
        opponent_rebounds=50,
        opponent_points=100,
        team_possessions=80,
        opponent_possessions=100,
        is_home=False,
    )

    profile = _calculate(second, first)

    assert (profile.game_id, profile.player_id, profile.team_id, profile.is_home) == (
        make_game().game_id,
        101,
        10,
        True,
    )
    assert (profile.games_used, profile.seconds_played) == (2, 2_160)
    assert profile.estimated_possessions == 70_000_000
    assert profile.minutes_avg == 18_000_000
    assert profile.points_per_100 == 42_857_143
    assert profile.off_rating == 101_214_575
    assert profile.def_rating == 100_000_000
    assert profile.usage_rate == 423_429
    assert profile.assist_rate == 311_111
    assert profile.turnover_rate == 101_215
    assert profile.three_point_attempt_rate == 500_000
    assert profile.three_point_percentage == 333_333
    assert profile.free_throw_rate == 250_000
    assert profile.rebound_rate == 156_522
    assert profile.pace_context == 93_333_333


def test_last_twenty_and_eligibility_exclude_nonappearances_and_noncausal_rows() -> None:
    target_time = datetime(2026, 2, 1, tzinfo=UTC)
    eligible = [
        _dependency(
            number,
            started_at=target_time - timedelta(days=30 - number),
            player_team_id=30,
            opponent_team_id=31,
            player_seconds=60,
            player_points=1_000 if number == 1 else 1,
        )
        for number in range(1, 22)
    ]
    excluded = (
        _dependency(
            50,
            started_at=target_time - timedelta(days=1),
            player_team_id=30,
            opponent_team_id=31,
            player_seconds=60,
            played=False,
        ),
        _dependency(
            51,
            started_at=target_time - timedelta(days=1),
            player_team_id=30,
            opponent_team_id=31,
            player_seconds=0,
        ),
    )
    result = _calculate(*reversed(eligible), *excluded)

    assert (result.games_used, result.seconds_played) == (20, 1_200)
    assert result.points_per_100 == 48_000_000


def test_equal_time_current_future_and_unselected_rows_do_not_validate_or_leak() -> None:
    target_time = datetime(2026, 2, 1, tzinfo=UTC)
    prior = _dependency(
        1,
        started_at=target_time - timedelta(days=1),
        player_team_id=30,
        opponent_team_id=31,
        player_seconds=600,
    )
    result = _calculate(prior)
    target = replace(make_game(), started_at=target_time, game_date=target_time.date())
    excluded_player = HistoricalPlayerBox(
        game_id=target.game_id,
        team_id=99,
        player_id=101,
        played=True,
        seconds_played=99_999,
        points=99_999,
    )
    equal_id = GameId("0022400098")
    future_id = GameId("0022400099")
    excluded_boxes = (
        HistoricalTeamBox(equal_id, 99, target_time, target_time.date(), True, points=99_999),
        HistoricalTeamBox(
            future_id,
            99,
            target_time + timedelta(days=1),
            (target_time + timedelta(days=1)).date(),
            True,
            points=99_999,
        ),
    )
    excluded_players = (
        excluded_player,
        replace(excluded_player, game_id=equal_id),
        replace(excluded_player, game_id=future_id),
    )

    mutated = build_player_pregame_features(
        target,
        (prior[0], *excluded_players),
        (*prior[1], *excluded_boxes),
        (prior[2],),
        player_id=101,
        team_id=10,
    )

    assert asdict(result) == asdict(mutated)


def test_a_null_input_only_nulls_metrics_that_depend_on_it() -> None:
    dependency = _dependency(
        1,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        player_team_id=30,
        opponent_team_id=31,
        player_seconds=600,
        player_points=None,
        player_three_pm=None,
    )

    result = _calculate(dependency)

    assert result.points_per_100 is None
    assert result.off_rating is None
    assert result.three_point_percentage is None
    assert result.estimated_possessions is not None
    assert result.def_rating is not None
    assert result.usage_rate is not None
    assert result.three_point_attempt_rate == 500_000


def test_zero_history_and_zero_denominators_are_null_not_imputed_zero() -> None:
    target = make_game()
    empty = build_player_pregame_features(target, (), (), (), player_id=101, team_id=10)
    assert empty == PlayerPregameFeatures(target.game_id, 101, 10, True)

    zero = _calculate(
        _dependency(
            1,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            player_team_id=30,
            opponent_team_id=31,
            player_seconds=600,
            player_points=0,
            player_fgm=0,
            player_fga=0,
            player_three_pm=0,
            player_three_pa=0,
            player_fta=0,
            player_rebounds=0,
            player_assists=0,
            player_turnovers=0,
            team_fgm=0,
            team_fga=0,
            team_fta=0,
            team_turnovers=0,
            team_rebounds=0,
            opponent_rebounds=0,
            opponent_points=0,
            team_possessions=0,
            opponent_possessions=0,
        )
    )
    assert zero.points_per_100 is None
    assert zero.off_rating is None
    assert zero.usage_rate is None
    assert zero.three_point_attempt_rate is None
    assert zero.three_point_percentage is None
    assert zero.def_rating is None
    assert zero.pace_context == 0


def test_selected_dependency_requires_exact_coherent_team_pair_and_possessions() -> None:
    dependency = _dependency(
        1,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        player_team_id=30,
        opponent_team_id=31,
        player_seconds=600,
    )
    player, boxes, possession = dependency
    target = replace(
        make_game(),
        started_at=datetime(2026, 2, 1, tzinfo=UTC),
        game_date=datetime(2026, 2, 1, tzinfo=UTC).date(),
    )

    for bad_boxes, bad_possessions in (
        ((boxes[0],), (possession,)),
        ((boxes[0], replace(boxes[1], is_home=True)), (possession,)),
        (boxes, ()),
        (boxes, (replace(possession, possession_number=2),)),
    ):
        with pytest.raises(ValueError, match="historical game structure"):
            build_player_pregame_features(
                target,
                (player,),
                bad_boxes,
                bad_possessions,
                player_id=101,
                team_id=10,
            )

    unmatched = build_player_pregame_features(
        target,
        (replace(player, team_id=99),),
        boxes,
        (possession,),
        player_id=101,
        team_id=10,
    )
    assert unmatched.games_used == 0
