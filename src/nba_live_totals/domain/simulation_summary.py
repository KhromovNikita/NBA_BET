"""Exact histogram statistics preserve unknown mass and integer threshold equality."""

from dataclasses import dataclass
from fractions import Fraction

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.model_evaluation import round_sqrt_fraction
from nba_live_totals.domain.simulation import (
    MAX_SCORE,
    SIMULATION_SCALE,
    Histogram,
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationQuery,
    SimulationRuntime,
    SimulationStatus,
    histogram_moments,
)
from nba_live_totals.domain.simulation_hashing import simulation_revision_sha256, state_seed_sha256
from nba_live_totals.domain.simulation_pmf import build_score_pmf


def _round_ratio(numerator: int, denominator: int) -> int:
    whole, remainder = divmod(numerator, denominator)
    return bounded_integer(whole + (2 * remainder >= denominator))


@dataclass(frozen=True, slots=True)
class MarginalSummary:
    mean_scaled: int
    stdev_scaled: int
    quantiles: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        bounded_integer(self.mean_scaled)
        bounded_integer(self.stdev_scaled)
        if type(self.quantiles) is not tuple or any(
            type(p) is not tuple or len(p) != 2 for p in self.quantiles
        ):
            raise ValueError("quantiles require immutable level/score pairs")
        SimulationQuery(quantiles_scaled=tuple(q for q, _ in self.quantiles))
        previous = -1
        for _, score in self.quantiles:
            bounded_integer(score, maximum=MAX_SCORE)
            if score < previous:
                raise ValueError("quantile scores must be nondecreasing")
            previous = score


@dataclass(frozen=True, slots=True)
class LineSummary:
    line_scaled: int
    count_under: int | None
    count_equal: int | None
    count_over: int | None
    count_unknown: int | None
    prob_under_scaled: int | None
    prob_equal_scaled: int | None
    prob_over_scaled: int | None

    def __post_init__(self) -> None:
        SimulationQuery(lines_scaled=(self.line_scaled,))
        counts = (self.count_under, self.count_equal, self.count_over, self.count_unknown)
        probabilities = (self.prob_under_scaled, self.prob_equal_scaled, self.prob_over_scaled)
        if all(c is None for c in counts):
            if any(p is not None for p in probabilities):
                raise ValueError("unavailable line cannot contain probabilities")
            return
        values = tuple(bounded_integer(c, maximum=1_000_000) for c in counts)
        population = sum(values)
        bounded_integer(population, minimum=1, maximum=1_000_000)
        if values[3]:
            if any(p is not None for p in probabilities):
                raise ValueError("incomplete line cannot contain full probabilities")
        else:
            for count, probability in zip(values[:3], probabilities, strict=True):
                bounded_integer(probability, maximum=SIMULATION_SCALE)
                if probability != _round_ratio(count * SIMULATION_SCALE, population):
                    raise ValueError("line probability disagrees with authoritative counts")


def summarize_histogram(histogram: Histogram, quantiles_scaled: tuple[int, ...]) -> MarginalSummary:
    SimulationQuery(quantiles_scaled=quantiles_scaled)
    count, first, second = histogram_moments(histogram)
    if count == 0:
        raise ValueError("empty histogram cannot define a full distribution")
    mean = Fraction(first, count)
    variance = Fraction(second, count) - mean * mean
    levels: list[tuple[int, int]] = []
    ranks = tuple(
        max(1, (q * count + SIMULATION_SCALE - 1) // SIMULATION_SCALE) for q in quantiles_scaled
    )
    index = cumulative = 0
    for score, mass in histogram:
        cumulative += mass
        while index < len(ranks) and ranks[index] <= cumulative:
            levels.append((quantiles_scaled[index], score))
            index += 1
    return MarginalSummary(
        _round_ratio(first * SIMULATION_SCALE, count),
        round_sqrt_fraction(variance * SIMULATION_SCALE**2),
        tuple(levels),
    )


def summarize_lines(outcome: SimulationOutcome, query: SimulationQuery) -> tuple[LineSummary, ...]:
    if not isinstance(outcome, SimulationOutcome) or not isinstance(query, SimulationQuery):
        raise ValueError("line summary requires typed outcome and query")
    rows: list[LineSummary] = []
    for line in query.lines_scaled:
        if outcome.status is SimulationStatus.UNAVAILABLE:
            rows.append(LineSummary(line, None, None, None, None, None, None, None))
            continue
        if outcome.total_histogram is None:
            raise ValueError("executed simulation requires total histogram")
        under = equal = over = 0
        for score, count in outcome.total_histogram:
            difference = score * SIMULATION_SCALE - line
            if difference < 0:
                under += count
            elif difference == 0:
                equal += count
            else:
                over += count
        probabilities: tuple[int | None, ...] = (
            tuple(
                _round_ratio(count * SIMULATION_SCALE, outcome.requested)
                for count in (under, equal, over)
            )
            if outcome.status is SimulationStatus.COMPLETE
            else (None, None, None)
        )
        rows.append(LineSummary(line, under, equal, over, outcome.unresolved, *probabilities))
    return tuple(rows)


def validate_simulation_outcome(outcome: SimulationOutcome, config: SimulationConfig) -> None:
    if not isinstance(outcome, SimulationOutcome) or not isinstance(config, SimulationConfig):
        raise ValueError("simulation validation requires typed outcome and config")
    if (
        outcome.requested != config.simulations
        or len(outcome.new_ot_counts) != config.max_overtimes + 1
    ):
        raise ValueError("simulation outcome population or overtime config mismatch")
    frame = outcome.frame
    expected: str | None = None
    if not frame.is_valid:
        expected = "invalid_feature_state"
    elif frame.seconds_remaining_game_ms == 0:
        expected = "no_future_time"
    else:
        home, away, pace = (
            frame.home_ppp.value_scaled,
            frame.away_ppp.value_scaled,
            frame.pace.value_scaled,
        )
        if home is None or away is None or pace is None:
            raise ValueError("available frame requires numeric rates")
        if max(home, away) > (len(config.score_prior_weights) - 1) * SIMULATION_SCALE:
            expected = "ppp_outside_score_support"
        elif pace > 1000 * SIMULATION_SCALE:
            expected = "pace_outside_runtime_limit"
    if frame.unavailable_reason != expected:
        raise ValueError("simulation frame availability contradicts configuration")
    if outcome.status is not SimulationStatus.UNAVAILABLE:
        for pmf, rate in ((outcome.home_pmf, frame.home_ppp), (outcome.away_pmf, frame.away_ppp)):
            if rate.value_scaled is None or pmf != build_score_pmf(
                rate.value_scaled, config.score_prior_weights
            ):
                raise ValueError("simulation score PMF contradicts configured scoring policy")


def make_unavailable_outcome(
    frame: SimulationFrame,
    config: SimulationConfig,
    runtime: SimulationRuntime,
) -> SimulationOutcome:
    if frame.is_available:
        raise ValueError("available frame cannot become unavailable without a reason")
    outcome = SimulationOutcome(
        frame=frame,
        status=SimulationStatus.UNAVAILABLE,
        requested=config.simulations,
        executed=0,
        resolved=0,
        unresolved=0,
        unavailable_reason=frame.unavailable_reason,
        unresolved_counts=(),
        new_ot_counts=(0,) * (config.max_overtimes + 1),
        home_pmf=None,
        away_pmf=None,
        seed_sha256=state_seed_sha256(frame, config, runtime),
        simulation_revision_sha256=simulation_revision_sha256(frame, config, runtime),
        home_histogram=None,
        away_histogram=None,
        total_histogram=None,
    )
    validate_simulation_outcome(outcome, config)
    return outcome
