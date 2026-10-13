"""Exact future rates for regulation or the currently known overtime period."""

from dataclasses import dataclass
from enum import StrEnum

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureStateKind,
    GameStateFeature,
    scaled_ratio,
)
from nba_live_totals.domain.games import Game, GameStatus
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, Possession

LABEL_VERSION = "remaining-horizon-labels-v1"
_REASONS = frozenset({"invalid_feature_state", "no_future_time", "no_future_possessions"})


class ModelHead(StrEnum):
    PACE = "pace"
    HOME_PPP = "home_ppp"
    AWAY_PPP = "away_ppp"


@dataclass(frozen=True, slots=True)
class HeadTarget:
    value_scaled: int | None
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        if self.value_scaled is None:
            if self.unavailable_reason not in _REASONS:
                raise ValueError("unavailable target requires a supported reason")
        else:
            bounded_integer(self.value_scaled)
            if self.unavailable_reason is not None:
                raise ValueError("available target cannot carry an unavailable reason")


@dataclass(frozen=True, slots=True)
class RemainingTargets:
    pace: HeadTarget
    home_ppp: HeadTarget
    away_ppp: HeadTarget

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, HeadTarget) for value in (self.pace, self.home_ppp, self.away_ppp)
        ):
            raise ValueError("remaining targets require typed head targets")


@dataclass(frozen=True, slots=True)
class HorizonEnd:
    game_id: GameId
    period: int
    boundary: EventBoundary
    home_score: int
    away_score: int
    home_possessions: int
    away_possessions: int

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId):
            raise ValueError("invalid endpoint game ID")
        bounded_integer(self.period, minimum=4)
        if (
            not isinstance(self.boundary, EventBoundary)
            or self.boundary.side is not BoundarySide.AFTER
        ):
            raise ValueError("endpoint requires an AFTER boundary")
        for value in (
            self.home_score,
            self.away_score,
            self.home_possessions,
            self.away_possessions,
        ):
            bounded_integer(value)


def horizon_period(state: GameStateFeature) -> int:
    if not isinstance(state, GameStateFeature):
        raise ValueError("expected a feature state")
    for value in (state.period, state.seconds_remaining_game_ms, state.seconds_remaining_period_ms):
        bounded_integer(value)
    if state.state_kind is FeatureStateKind.PREGAME:
        if (state.period, state.seconds_remaining_game_ms, state.seconds_remaining_period_ms) != (
            0,
            2_880_000,
            720_000,
        ):
            raise ValueError("invalid pregame clocks")
        return 4
    if state.period < 1:
        raise ValueError("after-possession state requires a positive period")
    length = 720_000 if state.period <= 4 else 300_000
    expected = state.seconds_remaining_period_ms + 720_000 * max(4 - state.period, 0)
    if state.seconds_remaining_period_ms > length or state.seconds_remaining_game_ms != expected:
        raise ValueError("inconsistent feature clocks")
    return max(4, state.period)


def calculate_remaining_targets(state: GameStateFeature, endpoint: HorizonEnd) -> RemainingTargets:
    selected = horizon_period(state)
    if not isinstance(endpoint, HorizonEnd):
        raise ValueError("expected a horizon endpoint")
    if endpoint.game_id != state.game_id or endpoint.period != selected:
        raise ValueError("endpoint identity or horizon mismatch")
    if endpoint.boundary < state.boundary:
        raise ValueError("endpoint precedes the feature state")
    deltas = tuple(
        getattr(endpoint, name) - bounded_integer(getattr(state, name))
        for name in ("home_score", "away_score", "home_possessions", "away_possessions")
    )
    if any(value < 0 for value in deltas):
        raise ValueError("negative future score or possession count")
    if endpoint.boundary == state.boundary and any(deltas):
        raise ValueError("equal boundaries require equal cumulative totals")
    if not state.is_valid or state.seconds_remaining_game_ms == 0:
        reason = "invalid_feature_state" if not state.is_valid else "no_future_time"
        missing = HeadTarget(None, reason)
        return RemainingTargets(missing, missing, missing)
    home_points, away_points, home_count, away_count = deltas
    pace = scaled_ratio(2_880_000 * (home_count + away_count), 2 * state.seconds_remaining_game_ms)
    return RemainingTargets(
        HeadTarget(pace, None),
        HeadTarget(scaled_ratio(home_points, home_count), None)
        if home_count
        else HeadTarget(None, "no_future_possessions"),
        HeadTarget(scaled_ratio(away_points, away_count), None)
        if away_count
        else HeadTarget(None, "no_future_possessions"),
    )


@dataclass(frozen=True, slots=True)
class TrainingGameInput:
    game: Game
    features: FeatureDataset
    possessions: tuple[Possession, ...]
    period_ends: tuple[HorizonEnd, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.game, Game) or self.game.status is not GameStatus.FINAL:
            raise ValueError("training input requires a final game")
        if (
            not isinstance(self.features, FeatureDataset)
            or self.features.game_id != self.game.game_id
        ):
            raise ValueError("training feature identity mismatch")
        if type(self.possessions) is not tuple or type(self.period_ends) is not tuple:
            raise ValueError("training rows must be immutable tuples")
        states = self.features.states
        if len(states) != len(self.possessions) + 1:
            raise ValueError("one feature state is required for every possession")
        pregame = states[0]
        horizon_period(pregame)
        if any(
            getattr(pregame, name)
            for name in ("home_score", "away_score", "home_possessions", "away_possessions")
        ):
            raise ValueError("pregame totals must be zero")
        previous = pregame
        for number, (state, possession) in enumerate(
            zip(states[1:], self.possessions, strict=True), 1
        ):
            if not isinstance(possession, Possession) or possession.game_id != self.game.game_id:
                raise ValueError("possession identity mismatch")
            if possession.possession_number != number or state.boundary != possession.end_boundary:
                raise ValueError("possession number or feature boundary mismatch")
            horizon_period(state)
            for name in (
                "period",
                "seconds_remaining_game_ms",
                "home_score",
                "away_score",
                "home_possessions",
                "away_possessions",
                "score_diff",
            ):
                if getattr(state, name) != getattr(possession, name):
                    raise ValueError(f"feature/possession {name} mismatch")
            if state.seconds_remaining_period_ms != possession.end_seconds_remaining_period_ms:
                raise ValueError("feature/possession period clock mismatch")
            if (possession.home_score_before, possession.away_score_before) != (
                previous.home_score,
                previous.away_score,
            ):
                raise ValueError("possession score chain mismatch")
            home = possession.offense_team_id == self.game.home_team_id
            expected_teams = (
                (self.game.home_team_id, self.game.away_team_id)
                if home
                else (self.game.away_team_id, self.game.home_team_id)
            )
            if (possession.offense_team_id, possession.defense_team_id) != expected_teams:
                raise ValueError("possession team mismatch")
            if (state.home_possessions, state.away_possessions) != (
                previous.home_possessions + home,
                previous.away_possessions + (not home),
            ) or possession.offense_possession_number != (
                state.home_possessions if home else state.away_possessions
            ):
                raise ValueError("possession counter chain mismatch")
            if state.boundary <= previous.boundary or state.period < previous.period:
                raise ValueError("possession chronology mismatch")
            previous = state
        if not self.period_ends or any(not isinstance(end, HorizonEnd) for end in self.period_ends):
            raise ValueError("training input requires typed horizon endpoints")
        periods = tuple(end.period for end in self.period_ends)
        if periods != tuple(range(4, max(4, previous.period) + 1)):
            raise ValueError("missing, duplicate, or unordered horizon endpoints")
        last_boundary: EventBoundary | None = None
        for end in self.period_ends:
            if end.game_id != self.game.game_id or (
                last_boundary is not None and end.boundary <= last_boundary
            ):
                raise ValueError("endpoint identity or chronology mismatch")
            prefix = [row for row in self.possessions if row.end_boundary <= end.boundary]
            if any(row.period > end.period for row in prefix) or any(
                row.period <= end.period and row.end_boundary > end.boundary
                for row in self.possessions
            ):
                raise ValueError("endpoint crosses a period boundary")
            totals = (
                (0, 0, 0, 0)
                if not prefix
                else tuple(
                    getattr(prefix[-1], name)
                    for name in ("home_score", "away_score", "home_possessions", "away_possessions")
                )
            )
            if (
                end.home_score,
                end.away_score,
                end.home_possessions,
                end.away_possessions,
            ) != totals:
                raise ValueError("endpoint cumulative totals mismatch")
            last_boundary = end.boundary
        final = self.period_ends[-1]
        if (final.home_score, final.away_score) != (self.game.home_score, self.game.away_score):
            raise ValueError("final endpoint does not match canonical game scores")
