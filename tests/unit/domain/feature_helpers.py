"""Small valid fixtures for feature-domain behavior tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from nba_live_totals.domain.features import FeatureDependency
from nba_live_totals.domain.games import Game, GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession

GAME_ID = GameId("0022500001")
OTHER_GAME_ID = GameId("0022500002")
DIGEST = "a" * 64


def make_game(*, game_id: GameId = GAME_ID) -> Game:
    return Game(
        game_id=game_id,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2025, 10, 20),
        started_at=datetime(2025, 10, 20, 23, tzinfo=UTC),
        home_team_id=10,
        away_team_id=20,
        home_score=100,
        away_score=90,
        status=GameStatus.FINAL,
        arena_name=None,
        arena_city=None,
        arena_state=None,
    )


def make_dependency(**overrides: object) -> FeatureDependency:
    values = dict(
        dependency_game_id=GAME_ID,
        started_at=datetime(2025, 10, 20, 23, tzinfo=UTC),
        game_context_sha256=DIGEST,
        applied_box_score_document_id=UUID(int=1),
        applied_box_score_sha256=DIGEST,
        canonical_team_box_sha256=DIGEST,
        canonical_player_box_sha256=DIGEST,
        possession_snapshot_id=UUID(int=2),
        parse_revision_id=DIGEST,
        possession_input_token_sha256=DIGEST,
        possession_output_sha256=DIGEST,
        ordinal=0,
        inclusion_reasons=("home_team_window",),
    )
    values.update(overrides)
    return FeatureDependency(**values)  # type: ignore[arg-type]


def make_possession(**overrides: object) -> Possession:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "possession_number": 1,
        "offense_team_id": 10,
        "defense_team_id": 20,
        "offense_possession_number": 1,
        "period": 1,
        "start_boundary": EventBoundary(1, BoundarySide.BEFORE),
        "end_boundary": EventBoundary(3, BoundarySide.AFTER),
        "start_seconds_remaining_period_ms": 720_000,
        "end_seconds_remaining_period_ms": 700_000,
        "seconds_remaining_game_ms": 2_860_000,
        "home_score_before": 0,
        "away_score_before": 0,
        "home_score": 2,
        "away_score": 0,
        "score_diff": 2,
        "offense_points": 2,
        "defense_points": 0,
        "home_possessions": 1,
        "away_possessions": 0,
        "end_reason": "made_field_goal",
        "source_event_count": 3,
    }
    values.update(overrides)
    return Possession(**values)  # type: ignore[arg-type]
