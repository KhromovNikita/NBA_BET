"""Causal, fixed-point inputs for offline possession simulations."""

import re
from dataclasses import dataclass

from nba_live_totals.domain.baselines import MAX_INT64, bounded_integer
from nba_live_totals.domain.features import FEATURE_SCALE, FeatureStateKind
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.model_features import require_sha256
from nba_live_totals.domain.model_predictions import PredictedRate, StateModelPrediction
from nba_live_totals.domain.model_targets import horizon_period
from nba_live_totals.domain.possessions import EventBoundary

SIMULATION_SCALE = FEATURE_SCALE
SIMULATION_POLICY_VERSION = "possession-simulation-v1"
SIMULATION_ENGINE_VERSION = "numpy-poisson-multinomial-v1"
SIMULATION_SEED_POLICY_VERSION = "state-pcg64-seed-v1"
SIMULATION_SUMMARY_POLICY_VERSION = "exact-simulation-summary-v1"
DEFAULT_SCORE_PRIOR_WEIGHTS = (460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)
MAX_SCORE = MAX_INT64 // SIMULATION_SCALE


def validate_score_prior(weights: tuple[int, ...]) -> None:
    if type(weights) is not tuple or not 3 <= len(weights) <= 33:
        raise ValueError("simulation prior requires consecutive outcomes 0..K, K2..32")
    for weight in weights:
        bounded_integer(weight, minimum=1, maximum=SIMULATION_SCALE)
    if sum(weights) != SIMULATION_SCALE:
        raise ValueError("simulation prior mass must equal the scale")


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    simulations: int = 10000
    seed: int = 42
    batch_size: int = 1024
    max_overtimes: int = 10
    max_possessions_per_horizon: int = 10000
    score_prior_weights: tuple[int, ...] = DEFAULT_SCORE_PRIOR_WEIGHTS
    policy_version: str = SIMULATION_POLICY_VERSION

    def __post_init__(self) -> None:
        bounded_integer(self.simulations, minimum=1, maximum=1_000_000)
        bounded_integer(self.seed, maximum=2**64 - 1)
        bounded_integer(self.batch_size, minimum=1, maximum=min(65536, self.simulations))
        bounded_integer(self.max_overtimes, minimum=1, maximum=20)
        bounded_integer(self.max_possessions_per_horizon, minimum=1, maximum=1_000_000)
        validate_score_prior(self.score_prior_weights)
        if self.policy_version != SIMULATION_POLICY_VERSION:
            raise ValueError("unsupported simulation policy")


@dataclass(frozen=True, slots=True)
class SimulationQuery:
    lines_scaled: tuple[int, ...] = ()
    quantiles_scaled: tuple[int, ...] = (50000, 500000, 950000)
    policy_version: str = SIMULATION_SUMMARY_POLICY_VERSION

    def __post_init__(self) -> None:
        for values, minimum, maximum_length, maximum_value in (
            (self.lines_scaled, 0, 64, MAX_INT64),
            (self.quantiles_scaled, 1, 32, SIMULATION_SCALE),
        ):
            if type(values) is not tuple or not minimum <= len(values) <= maximum_length:
                raise ValueError("invalid simulation query size or type")
            for value in values:
                bounded_integer(value, maximum=maximum_value)
            if tuple(sorted(set(values))) != values:
                raise ValueError("simulation query values must be sorted and unique")
        if any(value % (SIMULATION_SCALE // 2) for value in self.lines_scaled):
            raise ValueError("total lines must use integer or half points")
        if self.policy_version != SIMULATION_SUMMARY_POLICY_VERSION:
            raise ValueError("unsupported simulation summary policy")


@dataclass(frozen=True, slots=True)
class SimulationRuntime:
    python_version: str
    numpy_version: str
    system: str
    machine: str
    engine_version: str = SIMULATION_ENGINE_VERSION

    def __post_init__(self) -> None:
        for value in (self.python_version, self.numpy_version, self.system, self.machine):
            if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_.+\-]{1,128}", value) is None:
                raise ValueError("invalid simulation runtime identity")
        if self.engine_version != SIMULATION_ENGINE_VERSION:
            raise ValueError("unsupported simulation engine")


@dataclass(frozen=True, slots=True)
class SimulationFrame:
    game_id: GameId
    state_number: int
    possession_number: int
    state_kind: FeatureStateKind
    boundary: EventBoundary
    period: int
    seconds_remaining_game_ms: int
    seconds_remaining_period_ms: int
    home_score: int
    away_score: int
    is_valid: bool
    pace: PredictedRate
    home_ppp: PredictedRate
    away_ppp: PredictedRate
    input_sha256: str
    model_revision_sha256: str
    model_artifact_sha256: str
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.game_id, GameId)
            or not isinstance(self.state_kind, FeatureStateKind)
            or not isinstance(self.boundary, EventBoundary)
            or type(self.is_valid) is not bool
        ):
            raise ValueError("invalid simulation state identity")
        for value in (
            self.state_number,
            self.possession_number,
            self.period,
            self.seconds_remaining_game_ms,
            self.seconds_remaining_period_ms,
            self.home_score,
            self.away_score,
        ):
            bounded_integer(value)
        if self.home_score + self.away_score > MAX_SCORE:
            raise ValueError("simulation current score exceeds scaled Int64 capacity")
        pregame = self.state_kind is FeatureStateKind.PREGAME
        if (
            pregame != (self.state_number == self.possession_number == 0)
            or self.state_number != self.possession_number
        ):
            raise ValueError("simulation state numbers contradict state kind")
        if pregame:
            if (self.period, self.seconds_remaining_game_ms, self.seconds_remaining_period_ms) != (
                0,
                2_880_000,
                720_000,
            ):
                raise ValueError("invalid simulation pregame clocks")
        elif (
            self.period < 1
            or self.seconds_remaining_period_ms > (720_000 if self.period <= 4 else 300_000)
            or self.seconds_remaining_game_ms
            != self.seconds_remaining_period_ms + 720_000 * max(4 - self.period, 0)
        ):
            raise ValueError("inconsistent simulation clocks")
        for digest in (self.input_sha256, self.model_revision_sha256, self.model_artifact_sha256):
            require_sha256(digest)
        reason = (
            "invalid_feature_state"
            if not self.is_valid
            else ("no_future_time" if self.seconds_remaining_game_ms == 0 else None)
        )
        for rate in (self.pace, self.home_ppp, self.away_ppp):
            if not isinstance(rate, PredictedRate) or rate.unavailable_reason != reason:
                raise ValueError("simulation rates contradict state availability")
        if reason is not None:
            if self.unavailable_reason != reason:
                raise ValueError("simulation availability contradicts feature state")
        elif self.unavailable_reason not in {
            None,
            "ppp_outside_score_support",
            "pace_outside_runtime_limit",
        }:
            raise ValueError("invalid simulation availability reason")

    @property
    def is_available(self) -> bool:
        return self.unavailable_reason is None


def make_simulation_frame(
    prediction: StateModelPrediction,
    config: SimulationConfig,
) -> SimulationFrame:
    if not isinstance(prediction, StateModelPrediction) or not isinstance(config, SimulationConfig):
        raise ValueError("simulation requires typed prediction and config")
    state = prediction.state
    horizon_period(state)
    reason: str | None = None
    if not state.is_valid:
        reason = "invalid_feature_state"
    elif not state.seconds_remaining_game_ms:
        reason = "no_future_time"
    else:
        home, away, pace = (
            prediction.home_ppp.value_scaled,
            prediction.away_ppp.value_scaled,
            prediction.pace.value_scaled,
        )
        if home is None or away is None or pace is None:
            raise ValueError("missing rates on an available simulation state")
        if max(home, away) > (len(config.score_prior_weights) - 1) * SIMULATION_SCALE:
            reason = "ppp_outside_score_support"
        elif pace > 1000 * SIMULATION_SCALE:
            reason = "pace_outside_runtime_limit"
    return SimulationFrame(
        game_id=state.game_id,
        state_number=state.state_number,
        possession_number=state.possession_number,
        state_kind=state.state_kind,
        boundary=state.boundary,
        period=state.period,
        seconds_remaining_game_ms=state.seconds_remaining_game_ms,
        seconds_remaining_period_ms=state.seconds_remaining_period_ms,
        home_score=state.home_score,
        away_score=state.away_score,
        is_valid=state.is_valid,
        pace=prediction.pace,
        home_ppp=prediction.home_ppp,
        away_ppp=prediction.away_ppp,
        input_sha256=prediction.input_sha256,
        model_revision_sha256=prediction.model_revision_sha256,
        model_artifact_sha256=prediction.model_artifact_sha256,
        unavailable_reason=reason,
    )
