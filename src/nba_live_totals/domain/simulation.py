"""Causal, fixed-point inputs for offline possession simulations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from nba_live_totals.domain.baselines import MAX_INT64, bounded_integer
from nba_live_totals.domain.features import FEATURE_SCALE, FeatureStateKind
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.model_features import require_sha256
from nba_live_totals.domain.model_predictions import PredictedRate, StateModelPrediction
from nba_live_totals.domain.model_targets import horizon_period
from nba_live_totals.domain.possessions import EventBoundary

if TYPE_CHECKING:
    from nba_live_totals.domain.simulation_pmf import ScorePmf

SIMULATION_SCALE = FEATURE_SCALE
SIMULATION_POLICY_VERSION = "possession-simulation-v1"
SIMULATION_ENGINE_VERSION = "numpy-poisson-multinomial-v1"
SIMULATION_SEED_POLICY_VERSION = "state-pcg64-seed-v1"
SIMULATION_SUMMARY_POLICY_VERSION = "exact-simulation-summary-v1"
DEFAULT_SCORE_PRIOR_WEIGHTS = (460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)
MAX_SCORE = MAX_INT64 // SIMULATION_SCALE
MAX_HISTOGRAM_BINS = 100_000

type Histogram = tuple[tuple[int, int], ...]


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


class SimulationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


def histogram_moments(histogram: Histogram, *, minimum_score: int = 0) -> tuple[int, int, int]:
    """Return count/first/second moments with bounded bins and Python-integer arithmetic."""
    if type(histogram) is not tuple:
        raise ValueError("histogram must be an immutable tuple")
    if len(histogram) > MAX_HISTOGRAM_BINS:
        raise ValueError("simulation_histogram_limit")
    mass = first = second = 0
    previous = -1
    for pair in histogram:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("histogram bins require score and count")
        score, count = pair
        bounded_integer(score, minimum=minimum_score, maximum=MAX_SCORE)
        bounded_integer(count, minimum=1, maximum=1_000_000)
        if score <= previous:
            raise ValueError("histogram scores must be strictly increasing")
        previous = score
        mass += count
        first += score * count
        second += score * score * count
    if mass > 1_000_000:
        raise ValueError("histogram exceeds maximum simulation population")
    return mass, first, second


def validate_unresolved_counts(counts: tuple[tuple[str, int], ...], total: int) -> None:
    if type(counts) is not tuple:
        raise ValueError("unresolved reasons require immutable counters")
    previous = ""
    mass = 0
    for pair in counts:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("invalid unresolved counter")
        reason, count = pair
        if (
            type(reason) is not str
            or reason not in {"overtime_limit", "possession_limit"}
            or reason <= previous
        ):
            raise ValueError("unresolved reasons must be supported, sorted and unique")
        bounded_integer(count, minimum=1)
        mass += count
        previous = reason
    if mass != total:
        raise ValueError("unresolved reason mass mismatch")


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    frame: SimulationFrame
    status: SimulationStatus
    requested: int
    executed: int
    resolved: int
    unresolved: int
    unavailable_reason: str | None
    unresolved_counts: tuple[tuple[str, int], ...]
    new_ot_counts: tuple[int, ...]
    home_pmf: ScorePmf | None
    away_pmf: ScorePmf | None
    seed_sha256: str
    simulation_revision_sha256: str
    home_histogram: Histogram | None
    away_histogram: Histogram | None
    total_histogram: Histogram | None

    def __post_init__(self) -> None:
        from nba_live_totals.domain.simulation_pmf import ScorePmf

        if not isinstance(self.frame, SimulationFrame) or not isinstance(
            self.status, SimulationStatus
        ):
            raise ValueError("simulation outcome requires typed frame and status")
        bounded_integer(self.requested, minimum=1, maximum=1_000_000)
        for count in (self.executed, self.resolved, self.unresolved):
            bounded_integer(count, maximum=self.requested)
        for digest in (self.seed_sha256, self.simulation_revision_sha256):
            require_sha256(digest)
        validate_unresolved_counts(self.unresolved_counts, self.unresolved)
        if type(self.new_ot_counts) is not tuple or not 2 <= len(self.new_ot_counts) <= 21:
            raise ValueError("simulation requires counters for zero through max new OT")
        for count in self.new_ot_counts:
            bounded_integer(count, maximum=self.resolved)
        if sum(self.new_ot_counts) != self.resolved:
            raise ValueError("completed overtime mass mismatch")
        if self.status is SimulationStatus.UNAVAILABLE:
            if (
                self.frame.is_available
                or self.unavailable_reason != self.frame.unavailable_reason
                or self.executed
                or self.resolved
                or self.unresolved
                or any(
                    value is not None
                    for value in (
                        self.home_pmf,
                        self.away_pmf,
                        self.home_histogram,
                        self.away_histogram,
                        self.total_histogram,
                    )
                )
            ):
                raise ValueError("unavailable outcome cannot carry an executed population")
            return
        if (
            not self.frame.is_available
            or self.unavailable_reason is not None
            or self.executed != self.requested
            or self.resolved + self.unresolved != self.executed
            or (self.status is SimulationStatus.COMPLETE) != (self.unresolved == 0)
        ):
            raise ValueError("simulation outcome status or mass mismatch")
        for pmf, rate in (
            (self.home_pmf, self.frame.home_ppp),
            (self.away_pmf, self.frame.away_ppp),
        ):
            if not isinstance(pmf, ScorePmf) or pmf.target_scaled != rate.value_scaled:
                raise ValueError("simulation score PMF contradicts current rate")
        moments: list[tuple[int, int, int]] = []
        for histogram, score in (
            (self.home_histogram, self.frame.home_score),
            (self.away_histogram, self.frame.away_score),
            (self.total_histogram, self.frame.home_score + self.frame.away_score),
        ):
            if histogram is None:
                raise ValueError("executed simulation requires completed histograms")
            item = histogram_moments(histogram, minimum_score=score)
            if item[0] != self.resolved:
                raise ValueError("completed histogram mass mismatch")
            moments.append(item)
        if moments[2][1] != moments[0][1] + moments[1][1]:
            raise ValueError("home/away/total exact first moment mismatch")
