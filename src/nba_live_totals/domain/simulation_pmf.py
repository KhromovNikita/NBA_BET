"""Exponential tilt projected to an exactly normalized fixed-point scoring PMF."""

import math
from dataclasses import dataclass

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.simulation import SIMULATION_SCALE, validate_score_prior

SCORE_PMF_POLICY_VERSION = "tilted-integer-score-pmf-v1"


@dataclass(frozen=True, slots=True)
class ScorePmf:
    counts: tuple[int, ...]
    target_scaled: int
    policy_version: str = SCORE_PMF_POLICY_VERSION

    def __post_init__(self) -> None:
        if type(self.counts) is not tuple or not 3 <= len(self.counts) <= 33:
            raise ValueError("score PMF requires outcomes 0..K, K2..32")
        bounded_integer(self.target_scaled, maximum=self.support_max * SIMULATION_SCALE)
        for count in self.counts:
            bounded_integer(count, maximum=SIMULATION_SCALE)
        if (
            sum(self.counts) != SIMULATION_SCALE
            or sum(k * c for k, c in enumerate(self.counts)) != self.target_scaled
        ):
            raise ValueError("score PMF mass or mean mismatch")
        if self.policy_version != SCORE_PMF_POLICY_VERSION:
            raise ValueError("unsupported score PMF policy")

    @property
    def support_max(self) -> int:
        return len(self.counts) - 1


def _tilted_probabilities(weights: tuple[int, ...], beta: float) -> tuple[float, ...]:
    logs = tuple(math.log(weight) + beta * k for k, weight in enumerate(weights))
    largest = max(logs)
    values = tuple(math.exp(value - largest) for value in logs)
    normalizer = math.fsum(values)
    if not math.isfinite(normalizer) or normalizer <= 0:
        raise ValueError("score tilt requires finite positive normalization")
    return tuple(value / normalizer for value in values)


def _project_pmf(probabilities: tuple[float, ...], target_scaled: int) -> tuple[int, ...]:
    if (
        type(probabilities) is not tuple
        or not 3 <= len(probabilities) <= 33
        or any(
            type(p) is not float or not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities
        )
        or not math.isclose(math.fsum(probabilities), 1, rel_tol=0, abs_tol=1e-12)
    ):
        raise ValueError("invalid score projection normalization")
    bounded_integer(target_scaled, maximum=(len(probabilities) - 1) * SIMULATION_SCALE)
    scaled = tuple(p * SIMULATION_SCALE for p in probabilities)
    counts = [math.floor(p) for p in scaled]
    remaining = SIMULATION_SCALE - sum(counts)
    if not 0 <= remaining <= len(counts):
        raise ValueError("invalid score projection remainder")
    order = sorted(range(len(counts)), key=lambda k: (-(scaled[k] - counts[k]), k))
    for k in order[:remaining]:
        counts[k] += 1
    residual = target_scaled - sum(k * c for k, c in enumerate(counts))
    if abs(residual) > 1024:
        raise ValueError("score mean repair exceeds limit")
    for _ in range(abs(residual)):
        candidates = range(len(counts) - 1) if residual > 0 else range(len(counts) - 1, 0, -1)
        source = next((k for k in candidates if counts[k]), None)
        if source is None:
            raise ValueError("score mean repair is impossible")
        counts[source] -= 1
        counts[source + (1 if residual > 0 else -1)] += 1
    result = tuple(counts)
    ScorePmf(result, target_scaled)
    return result


def build_score_pmf(target_scaled: int, prior_weights: tuple[int, ...]) -> ScorePmf:
    validate_score_prior(prior_weights)
    maximum = (len(prior_weights) - 1) * SIMULATION_SCALE
    bounded_integer(target_scaled, maximum=maximum)
    if target_scaled in {0, maximum}:
        outcome = 0 if target_scaled == 0 else len(prior_weights) - 1
        return ScorePmf(
            tuple(SIMULATION_SCALE if k == outcome else 0 for k in range(len(prior_weights))),
            target_scaled,
        )
    if target_scaled == sum(k * w for k, w in enumerate(prior_weights)):
        return ScorePmf(prior_weights, target_scaled)
    lower, upper = -64.0, 64.0
    target = target_scaled / SIMULATION_SCALE

    def mean(beta: float) -> float:
        return math.fsum(k * p for k, p in enumerate(_tilted_probabilities(prior_weights, beta)))

    if not mean(lower) <= target <= mean(upper):
        raise ValueError("score target is outside numeric tilt bracket")
    for _ in range(80):
        midpoint = (lower + upper) / 2
        if mean(midpoint) <= target:
            lower = midpoint
        else:
            upper = midpoint
    probabilities = _tilted_probabilities(prior_weights, (lower + upper) / 2)
    return ScorePmf(_project_pmf(probabilities, target_scaled), target_scaled)
