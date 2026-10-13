"""Independent feature-state fixtures for numerical baseline tests."""

from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureStateKind,
    GameStateFeature,
    LineupAggregateFeatures,
    LiveFeatures,
    TeamPregameFeatures,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.feature_helpers import GAME_ID


def make_state(**changes: object) -> GameStateFeature:
    home, away = tuple(range(101, 106)), tuple(range(201, 206))
    teams = []
    for team_id, is_home, off, defense, pace in (
        (10, True, 120, 110, 100),
        (20, False, 110, 100, 104),
    ):
        fields: dict[str, object] = {"team_id": team_id, "is_home": is_home}
        for window in (5, 10, 20):
            fields.update(
                {
                    f"games_used_{window}": 3,
                    f"off_rating_{window}": off * 1_000_000,
                    f"def_rating_{window}": defense * 1_000_000,
                    f"pace_{window}": pace * 1_000_000,
                }
            )
        teams.append(TeamPregameFeatures(**fields))  # type: ignore[arg-type]
    number = changes.get("state_number", 40)
    assert type(number) is int
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "state_number": number,
        "possession_number": number,
        "state_kind": FeatureStateKind.PREGAME
        if number == 0
        else FeatureStateKind.AFTER_POSSESSION,
        "boundary": EventBoundary(
            max(1, int(number) * 2), BoundarySide.BEFORE if number == 0 else BoundarySide.AFTER
        ),
        "home_player_ids": home,
        "away_player_ids": away,
        "home_pregame": teams[0],
        "away_pregame": teams[1],
        "home_lineup": LineupAggregateFeatures(10, home),
        "away_lineup": LineupAggregateFeatures(20, away),
        "home_possessions": 0 if number == 0 else 20,
        "away_possessions": 0 if number == 0 else 20,
        "period": 0 if number == 0 else 2,
        "seconds_remaining_game_ms": 2_880_000 if number == 0 else 1_400_000,
        "live": LiveFeatures()
        if number == 0
        else LiveFeatures(current_pace=110_000_000, home_ppp=1_500_000, away_ppp=800_000),
    }
    values.update(changes)
    return GameStateFeature(**values)  # type: ignore[arg-type]


def make_dataset(*states: GameStateFeature) -> FeatureDataset:
    return FeatureDataset(GAME_ID, states, ())
