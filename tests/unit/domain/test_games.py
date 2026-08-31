from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from nba_live_totals.domain.games import (
    Game,
    GameStatus,
    Player,
    SeasonSchedule,
    SeasonType,
    Team,
)
from nba_live_totals.domain.identifiers import GameId, SeasonId


def make_game(
    *,
    game_id: GameId | None = None,
    status: GameStatus = GameStatus.FINAL,
    home_team_id: int = 1,
    away_team_id: int = 2,
    home_score: int | None = 100,
    away_score: int | None = 90,
    started_at: datetime = datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
) -> Game:
    return Game(
        game_id=game_id or GameId("0022500123"),
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=started_at.date(),
        started_at=started_at,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_score=home_score,
        away_score=away_score,
        status=status,
        arena_name="Test Arena",
        arena_city="Test City",
        arena_state="TS",
    )


def test_season_and_game_ids_validate_and_classify() -> None:
    assert SeasonId("2025-26").start_year == 2025
    assert GameId("0022500123").season_type is SeasonType.REGULAR_SEASON
    assert GameId("0042500101").season_type is SeasonType.PLAYOFFS
    assert GameId("0012500001").is_excluded
    assert GameId("0032500001").is_excluded

    with pytest.raises(ValueError, match="season"):
        SeasonId("2025-27")
    with pytest.raises(ValueError, match="game type"):
        GameId("0092500123")


def test_final_game_requires_distinct_teams_scores_and_utc() -> None:
    with pytest.raises(ValueError, match="scores"):
        make_game(status=GameStatus.FINAL, home_score=None)
    with pytest.raises(ValueError, match="distinct"):
        make_game(home_team_id=1, away_team_id=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        make_game(started_at=datetime(2026, 1, 1, 1, 0))


def test_game_normalizes_aware_started_at_to_utc() -> None:
    started_at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=7)))

    game = make_game(started_at=started_at)

    assert game.started_at == datetime(2025, 12, 31, 18, 0, tzinfo=UTC)
    assert game.started_at.tzinfo is UTC


def test_domain_models_reject_invalid_numeric_identities_and_duplicates() -> None:
    with pytest.raises(ValueError, match="positive"):
        Team(team_id=0, city="Test", name="Team", tricode="TST", slug="test-team")
    with pytest.raises(ValueError, match="positive"):
        Player(player_id=-1, first_name="Test", family_name="Player", full_name="Test Player")
    with pytest.raises(ValueError, match="positive"):
        make_game(home_team_id=0)

    team = Team(team_id=1, city="Test", name="Team", tricode="TST", slug="test-team")
    game = make_game()
    with pytest.raises(ValueError, match="duplicate"):
        SeasonSchedule(season=SeasonId("2025-26"), teams=(team,), games=(game, game))


def test_domain_models_are_immutable() -> None:
    team = Team(team_id=1, city="Test", name="Team", tricode="TST", slug="test-team")

    with pytest.raises(FrozenInstanceError):
        team.name = "Renamed"  # type: ignore[misc]
