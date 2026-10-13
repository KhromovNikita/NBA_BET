"""Hand-scored, provider-neutral examples for offline modeling tests."""

from dataclasses import replace

from nba_live_totals.domain.features import FeatureDataset, GameStateFeature
from nba_live_totals.domain.model_targets import HorizonEnd, TrainingGameInput
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession
from tests.unit.domain.baseline_helpers import make_state
from tests.unit.domain.feature_helpers import GAME_ID, make_game, make_possession


def make_target_case() -> tuple[GameStateFeature, HorizonEnd]:
    state = make_state(
        state_number=2,
        period=4,
        seconds_remaining_game_ms=60_000,
        seconds_remaining_period_ms=60_000,
        home_score=2,
        away_score=1,
        score_diff=1,
        home_possessions=1,
        away_possessions=1,
    )
    return state, HorizonEnd(GAME_ID, 4, EventBoundary(100, BoundarySide.AFTER), 7, 4, 4, 3)


def make_training_input() -> TrainingGameInput:
    possessions: list[Possession] = []
    states = [make_state(state_number=0, seconds_remaining_period_ms=720_000)]
    home_score = away_score = home_count = away_count = 0
    previous_clock = 80_000
    for number, (points, clock) in enumerate(
        zip(
            (2, 1, 2, 1, 1, 2, 2), (70_000, 60_000, 50_000, 40_000, 30_000, 20_000, 0), strict=True
        ),
        start=1,
    ):
        home = number % 2 == 1
        before_home, before_away = home_score, away_score
        if home:
            home_score += points
            home_count += 1
        else:
            away_score += points
            away_count += 1
        boundary = EventBoundary(2 * number, BoundarySide.AFTER)
        possession = make_possession(
            possession_number=number,
            offense_team_id=10 if home else 20,
            defense_team_id=20 if home else 10,
            offense_possession_number=home_count if home else away_count,
            period=4,
            start_boundary=EventBoundary(2 * number - 1, BoundarySide.BEFORE),
            end_boundary=boundary,
            start_seconds_remaining_period_ms=previous_clock,
            end_seconds_remaining_period_ms=clock,
            seconds_remaining_game_ms=clock,
            home_score_before=before_home,
            away_score_before=before_away,
            home_score=home_score,
            away_score=away_score,
            score_diff=home_score - away_score,
            offense_points=points,
            defense_points=0,
            home_possessions=home_count,
            away_possessions=away_count,
        )
        possessions.append(possession)
        states.append(
            make_state(
                state_number=number,
                boundary=boundary,
                period=4,
                seconds_remaining_game_ms=clock,
                seconds_remaining_period_ms=clock,
                home_score=home_score,
                away_score=away_score,
                score_diff=home_score - away_score,
                home_possessions=home_count,
                away_possessions=away_count,
            )
        )
        previous_clock = clock
    endpoint = HorizonEnd(GAME_ID, 4, EventBoundary(100, BoundarySide.AFTER), 7, 4, 4, 3)
    return TrainingGameInput(
        replace(make_game(), home_score=7, away_score=4),
        FeatureDataset(GAME_ID, tuple(states), ()),
        tuple(possessions),
        (endpoint,),
    )
