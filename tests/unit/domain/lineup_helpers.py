"""Focused builders for lineup-domain unit tests."""

from __future__ import annotations

from datetime import date

from nba_live_totals.domain.games import GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import (
    LineupReconstructionContext,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    TeamLineupState,
    TeamRoster,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, GameContext

GAME_ID = GameId("0022500123")
SHA256 = "a" * 64
HOME_PLAYERS = frozenset({101, 102, 103, 104, 105})
AWAY_PLAYERS = frozenset({201, 202, 203, 204, 205})


def make_game_context(**overrides: object) -> GameContext:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "season": SeasonId("2025-26"),
        "season_type": SeasonType.REGULAR_SEASON,
        "game_date": date(2026, 1, 2),
        "status": GameStatus.FINAL,
        "home_team_id": 10,
        "away_team_id": 20,
        "home_score": 110,
        "away_score": 108,
    }
    values.update(overrides)
    return GameContext(**values)  # type: ignore[arg-type]


def make_versions(**overrides: object) -> LineupVersions:
    values: dict[str, object] = {
        "normalizer_version": "nba-pbp-v1",
        "reconstructor_version": "causal-v1",
        "rules_version": "nba-2025-v1",
        "output_schema_version": "lineups-v1",
    }
    values.update(overrides)
    return LineupVersions(**values)  # type: ignore[arg-type]


def make_roster(team_id: int = 10, **overrides: object) -> TeamRoster:
    players = HOME_PLAYERS if team_id == 10 else AWAY_PLAYERS
    values: dict[str, object] = {
        "team_id": team_id,
        "roster_player_ids": players | {team_id * 100 + 6},
        "eligible_player_ids": players | {team_id * 100 + 6},
        "starter_player_ids": players,
    }
    values.update(overrides)
    return TeamRoster(**values)  # type: ignore[arg-type]


def make_state(player_ids: frozenset[int] = HOME_PLAYERS, **overrides: object) -> TeamLineupState:
    values: dict[str, object] = {
        "status": "valid",
        "player_ids": player_ids,
        "invalid_reason": None,
        "invalid_since": None,
    }
    values.update(overrides)
    return TeamLineupState(**values)  # type: ignore[arg-type]


def make_invalid_state(
    *,
    reason: str = "invalid_initial_lineup",
    boundary: EventBoundary | None = None,
) -> TeamLineupState:
    return TeamLineupState(
        status=LineupStatus.INVALID,
        player_ids=frozenset(),
        invalid_reason=reason,
        invalid_since=boundary or EventBoundary(1, BoundarySide.BEFORE),
    )


def make_segment(**overrides: object) -> LineupSegment:
    values: dict[str, object] = {
        "segment_number": 1,
        "start_boundary": EventBoundary(1, BoundarySide.BEFORE),
        "end_boundary": EventBoundary(2, BoundarySide.AFTER),
        "start_period": 1,
        "end_period": 1,
        "start_seconds_remaining_period_ms": 720_000,
        "end_seconds_remaining_period_ms": 700_000,
        "home_state": make_state(HOME_PLAYERS),
        "away_state": make_state(AWAY_PLAYERS),
    }
    values.update(overrides)
    return LineupSegment(**values)  # type: ignore[arg-type]


def make_reconstruction_context(**overrides: object) -> LineupReconstructionContext:
    values: dict[str, object] = {
        "game": make_game_context(),
        "versions": make_versions(),
        "box_score_document_sha256": SHA256,
        "pbp_document_sha256": "b" * 64,
        "canonical_player_box_sha256": "c" * 64,
        "canonical_event_sha256": "d" * 64,
        "game_context_sha256": "e" * 64,
        "lineup_revision_id": "f" * 64,
    }
    values.update(overrides)
    return LineupReconstructionContext(**values)  # type: ignore[arg-type]
