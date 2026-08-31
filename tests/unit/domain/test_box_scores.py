from collections.abc import Callable
from decimal import Decimal

import pytest

from nba_live_totals.domain.box_scores import (
    BoxScore,
    PlayerBoxScore,
    TeamBoxScore,
    TraditionalStats,
)
from nba_live_totals.domain.games import Player, Team
from nba_live_totals.domain.identifiers import GameId

GAME_ID = GameId("0022500123")
HOME_TEAM = Team(10, "Home", "Team", "HOM", "home-team")
AWAY_TEAM = Team(20, "Away", "Team", "AWY", "away-team")


def make_statistics(*, points: int | None = 100) -> TraditionalStats:
    return TraditionalStats(
        seconds_played=14_400,
        field_goals_made=40,
        field_goals_attempted=80,
        field_goals_percentage=Decimal("0.5"),
        three_pointers_made=10,
        three_pointers_attempted=30,
        three_pointers_percentage=Decimal("0.333"),
        free_throws_made=10,
        free_throws_attempted=12,
        free_throws_percentage=Decimal("0.833"),
        rebounds_offensive=8,
        rebounds_defensive=35,
        rebounds_total=43,
        assists=20,
        steals=6,
        blocks=4,
        turnovers=12,
        fouls_personal=18,
        points=points,
        plus_minus=Decimal("5"),
    )


def make_team_row(team: Team, *, points: int = 100) -> TeamBoxScore:
    return TeamBoxScore(game_id=GAME_ID, team=team, statistics=make_statistics(points=points))


def make_player_row(*, player_id: int = 1, team_id: int = HOME_TEAM.team_id) -> PlayerBoxScore:
    player = Player(player_id, "Test", "Player", f"Test Player {player_id}")
    return PlayerBoxScore(
        game_id=GAME_ID,
        player=player,
        team_id=team_id,
        position="G",
        jersey_number="1",
        starter=True,
        played=True,
        comment=None,
        statistics=make_statistics(points=12),
    )


def make_box_score(
    *,
    team_rows: tuple[TeamBoxScore, ...] | None = None,
    player_rows: tuple[PlayerBoxScore, ...] | None = None,
) -> BoxScore:
    return BoxScore(
        game_id=GAME_ID,
        home_team_id=HOME_TEAM.team_id,
        away_team_id=AWAY_TEAM.team_id,
        home_score=100,
        away_score=90,
        team_rows=team_rows
        or (make_team_row(HOME_TEAM, points=100), make_team_row(AWAY_TEAM, points=90)),
        player_rows=player_rows or (make_player_row(),),
    )


def test_box_score_accepts_two_matching_team_rows_and_final_scores() -> None:
    box_score = make_box_score()

    assert box_score.home_score == 100
    assert tuple(row.team.team_id for row in box_score.team_rows) == (10, 20)


def test_box_score_copies_mutable_row_collections_before_validation() -> None:
    team_rows = [make_team_row(HOME_TEAM), make_team_row(AWAY_TEAM, points=90)]
    player_rows = [make_player_row()]

    box_score = BoxScore(
        game_id=GAME_ID,
        home_team_id=HOME_TEAM.team_id,
        away_team_id=AWAY_TEAM.team_id,
        home_score=100,
        away_score=90,
        team_rows=team_rows,  # type: ignore[arg-type]
        player_rows=player_rows,  # type: ignore[arg-type]
    )
    team_rows.clear()
    player_rows.append(make_player_row(player_id=2))

    assert isinstance(box_score.team_rows, tuple)
    assert isinstance(box_score.player_rows, tuple)
    assert tuple(row.team.team_id for row in box_score.team_rows) == (10, 20)
    assert tuple(row.player.player_id for row in box_score.player_rows) == (1,)


def test_box_score_rejects_duplicate_players() -> None:
    row = make_player_row()

    with pytest.raises(ValueError, match="duplicate player"):
        make_box_score(player_rows=(row, row))


def test_box_score_rejects_third_team_row() -> None:
    third_team = Team(30, "Third", "Team", "THD", "third-team")

    with pytest.raises(ValueError, match="exactly two"):
        make_box_score(
            team_rows=(
                make_team_row(HOME_TEAM),
                make_team_row(AWAY_TEAM, points=90),
                make_team_row(third_team),
            )
        )


def test_box_score_rejects_player_assigned_to_unknown_team() -> None:
    with pytest.raises(ValueError, match="unknown team"):
        make_box_score(player_rows=(make_player_row(team_id=30),))


def test_box_score_rejects_team_points_that_disagree_with_final_score() -> None:
    with pytest.raises(ValueError, match="points"):
        make_box_score(
            team_rows=(make_team_row(HOME_TEAM, points=99), make_team_row(AWAY_TEAM, points=90))
        )


def test_box_score_requires_both_final_scores() -> None:
    with pytest.raises(ValueError, match="final scores"):
        BoxScore(
            game_id=GAME_ID,
            home_team_id=HOME_TEAM.team_id,
            away_team_id=AWAY_TEAM.team_id,
            home_score=None,  # type: ignore[arg-type]
            away_score=90,
            team_rows=(make_team_row(HOME_TEAM), make_team_row(AWAY_TEAM, points=90)),
            player_rows=(make_player_row(),),
        )


def test_dnp_player_does_not_gain_false_zero_statistics() -> None:
    row = PlayerBoxScore(
        game_id=GameId("0022500123"),
        player=Player(1, "Bench", "Player", "Bench Player"),
        team_id=10,
        position=None,
        jersey_number="9",
        starter=False,
        played=False,
        comment="DNP - Coach's Decision",
        statistics=TraditionalStats.empty(),
    )

    assert row.statistics.points is None
    assert row.statistics.seconds_played is None


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: TraditionalStats(field_goals_made=-1), "nonnegative"),
        (lambda: TraditionalStats(field_goals_made=2, field_goals_attempted=1), "made"),
        (lambda: TraditionalStats(field_goals_percentage=Decimal("1.01")), "percentage"),
    ],
)
def test_traditional_stats_reject_invalid_numeric_values(
    factory: Callable[[], TraditionalStats], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
