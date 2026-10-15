"""Batched possession sampling; native imports happen only at execution boundaries."""

from __future__ import annotations

import importlib
import math
import platform
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from nba_live_totals.application.simulation.errors import SimulationError, SimulationStopped
from nba_live_totals.domain.baselines import MAX_INT64, bounded_integer
from nba_live_totals.domain.simulation import (
    MAX_HISTOGRAM_BINS,
    MAX_SCORE,
    SIMULATION_SCALE,
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
    SimulationStatus,
)
from nba_live_totals.domain.simulation_hashing import simulation_revision_sha256, state_seed_sha256
from nba_live_totals.domain.simulation_pmf import ScorePmf, build_score_pmf
from nba_live_totals.domain.simulation_summary import (
    make_unavailable_outcome,
    validate_simulation_outcome,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    type IntArray = NDArray[np.int64]
    type FloatArray = NDArray[np.float64]
    type BoolArray = NDArray[np.bool_]


def _require_numpy() -> None:
    try:
        importlib.import_module("numpy")
    except ImportError:
        raise SimulationError("ml_dependencies_missing") from None


class SimulationRng(Protocol):
    def poisson(self, lam: float, size: int) -> IntArray: ...
    def first_side_bits(self, size: int) -> IntArray: ...
    def multinomial(self, n: IntArray, pvals: FloatArray) -> IntArray: ...


class _NumpyRng:
    def __init__(self, generator: np.random.Generator) -> None:
        self._generator = generator

    def poisson(self, lam: float, size: int) -> IntArray:
        return self._generator.poisson(lam, size)

    def first_side_bits(self, size: int) -> IntArray:
        import numpy as np

        return self._generator.integers(0, 2, size=size, dtype=np.int64)

    def multinomial(self, n: IntArray, pvals: FloatArray) -> IntArray:
        return self._generator.multinomial(n, pvals)


def make_numpy_rng(seed: int) -> SimulationRng:
    bounded_integer(seed, maximum=2**256 - 1)
    _require_numpy()
    import numpy as np

    return _NumpyRng(np.random.Generator(np.random.PCG64(seed)))


def capture_simulation_runtime() -> SimulationRuntime:
    _require_numpy()
    import numpy as np

    return SimulationRuntime(
        platform.python_version(), np.__version__, platform.system(), platform.machine()
    )


@dataclass(frozen=True, slots=True)
class NativePmf:
    outcome_order: tuple[int, ...]
    probabilities: FloatArray


def native_pmf(pmf: ScorePmf) -> NativePmf:
    _require_numpy()
    import numpy as np

    if not isinstance(pmf, ScorePmf):
        raise SimulationError("simulation_invalid_input")
    mode = min(range(len(pmf.counts)), key=lambda k: (-pmf.counts[k], k))
    order = (*(k for k in range(len(pmf.counts)) if k != mode), mode)
    previous = tuple(pmf.counts[k] / SIMULATION_SCALE for k in order[:-1])
    probabilities = np.array((*previous, 1.0 - math.fsum(previous)), dtype=np.float64)
    if (
        not np.all(np.isfinite(probabilities))
        or np.any(probabilities < 0)
        or np.any(probabilities > 1)
    ):
        raise SimulationError("simulation_native_error")
    return NativePmf(order, probabilities)


@dataclass(frozen=True, slots=True)
class HorizonBatch:
    home_scores: IntArray
    away_scores: IntArray
    possession_limited: BoolArray


def _checked_int_array(
    value: object,
    shape: tuple[int, ...],
    *,
    minimum: int = 0,
    maximum: int = MAX_INT64,
) -> IntArray:
    import numpy as np

    if (
        not isinstance(value, np.ndarray)
        or value.shape != shape
        or value.dtype != np.dtype(np.int64)
        or np.any(value < minimum)
        or np.any(value > maximum)
    ):
        raise SimulationError("simulation_native_error")
    return value


def _sample_points(rng: SimulationRng, n: IntArray, pmf: ScorePmf, maximum: int) -> IntArray:
    import numpy as np

    native = native_pmf(pmf)
    matrix = _checked_int_array(
        rng.multinomial(n, native.probabilities), (len(n), len(pmf.counts)), maximum=maximum
    )
    if np.any(matrix.sum(axis=1, dtype=np.int64) != n):
        raise SimulationError("simulation_native_error")
    original_order = np.argsort(np.array(native.outcome_order, dtype=np.int64))
    return matrix[:, original_order] @ np.arange(len(pmf.counts), dtype=np.int64)


def sample_horizon(
    rng: SimulationRng,
    frame: SimulationFrame,
    config: SimulationConfig,
    home_pmf: ScorePmf,
    away_pmf: ScorePmf,
    home_scores: IntArray,
    away_scores: IntArray,
    remaining_ms: int,
) -> HorizonBatch:
    _require_numpy()
    import numpy as np

    try:
        bounded_integer(remaining_ms, maximum=2_880_000)
        if (
            not isinstance(frame, SimulationFrame)
            or not isinstance(config, SimulationConfig)
            or not frame.is_available
        ):
            raise ValueError("available simulation frame required")
        pace = frame.pace.value_scaled
        if pace is None or pace > 1000 * SIMULATION_SCALE:
            raise ValueError("pace outside runtime limit")
        for pmf, rate in ((home_pmf, frame.home_ppp), (away_pmf, frame.away_ppp)):
            if (
                not isinstance(pmf, ScorePmf)
                or pmf.target_scaled != rate.value_scaled
                or len(pmf.counts) != len(config.score_prior_weights)
            ):
                raise ValueError("score PMF contradicts frame or configuration")
    except ValueError:
        raise SimulationError("simulation_invalid_input") from None
    if not isinstance(home_scores, np.ndarray) or home_scores.ndim != 1:
        raise SimulationError("simulation_native_error")
    size = len(home_scores)
    if size > config.batch_size:
        raise SimulationError("simulation_native_error")
    home = _checked_int_array(
        home_scores, (size,), minimum=frame.home_score, maximum=MAX_SCORE
    ).copy()
    away = _checked_int_array(
        away_scores, (size,), minimum=frame.away_score, maximum=MAX_SCORE
    ).copy()
    if np.any(home + away > MAX_SCORE):
        raise SimulationError("simulation_numeric_overflow")
    limited = np.zeros(size, dtype=np.bool_)
    lam = pace * remaining_ms / (1_440_000 * SIMULATION_SCALE)
    if not math.isfinite(lam) or lam < 0:
        raise SimulationError("simulation_native_error")
    if lam == 0:
        return HorizonBatch(home, away, limited)
    try:
        counts = _checked_int_array(rng.poisson(lam, size), (size,))
        limited = counts > config.max_possessions_per_horizon
        admitted = ~limited
        active_counts = counts[admitted]
        bits = _checked_int_array(
            rng.first_side_bits(len(active_counts)), (len(active_counts),), maximum=1
        )
        home_n = active_counts // 2 + (active_counts % 2) * bits
        away_n = active_counts - home_n
        home[admitted] += _sample_points(rng, home_n, home_pmf, config.max_possessions_per_horizon)
        away[admitted] += _sample_points(rng, away_n, away_pmf, config.max_possessions_per_horizon)
    except ArithmeticError, ValueError, TypeError:
        raise SimulationError("simulation_native_error") from None
    if np.any(home + away > MAX_SCORE):
        raise SimulationError("simulation_numeric_overflow")
    return HorizonBatch(home, away, limited)


def _accumulate_histogram(histogram: dict[int, int], values: IntArray) -> None:
    import numpy as np

    keys, counts = np.unique(values, return_counts=True)
    for key, count in zip(keys, counts, strict=True):
        score = int(key)
        if score not in histogram and len(histogram) >= MAX_HISTOGRAM_BINS:
            raise SimulationError("simulation_histogram_limit")
        histogram[score] = histogram.get(score, 0) + int(count)


class NumpySimulationEngine:
    def __init__(self, *, rng_factory: Callable[[int], SimulationRng] | None = None) -> None:
        self._rng_factory = rng_factory if rng_factory is not None else make_numpy_rng
        self._runtime: SimulationRuntime | None = None

    def runtime(self) -> SimulationRuntime:
        if self._runtime is None:
            self._runtime = capture_simulation_runtime()
        return self._runtime

    def simulate(
        self,
        frame: SimulationFrame,
        config: SimulationConfig,
        *,
        stop_requested: Callable[[], bool],
    ) -> SimulationOutcome:
        if stop_requested():
            raise SimulationStopped()
        if not isinstance(frame, SimulationFrame) or not isinstance(config, SimulationConfig):
            raise SimulationError("simulation_invalid_input")
        runtime = self.runtime()
        if not frame.is_available:
            return make_unavailable_outcome(frame, config, runtime)
        import numpy as np

        if frame.home_ppp.value_scaled is None or frame.away_ppp.value_scaled is None:
            raise SimulationError("simulation_invalid_input")
        home_pmf = build_score_pmf(frame.home_ppp.value_scaled, config.score_prior_weights)
        away_pmf = build_score_pmf(frame.away_ppp.value_scaled, config.score_prior_weights)
        seed = state_seed_sha256(frame, config, runtime)
        rng = self._rng_factory(int.from_bytes(bytes.fromhex(seed), "big"))
        home_histogram: dict[int, int] = {}
        away_histogram: dict[int, int] = {}
        total_histogram: dict[int, int] = {}
        new_ot_counts = [0] * (config.max_overtimes + 1)
        reasons = {"overtime_limit": 0, "possession_limit": 0}
        for offset in range(0, config.simulations, config.batch_size):
            if stop_requested():
                raise SimulationStopped()
            size = min(config.batch_size, config.simulations - offset)
            home = np.full(size, frame.home_score, dtype=np.int64)
            away = np.full(size, frame.away_score, dtype=np.int64)
            active = np.arange(size, dtype=np.int64)
            for new_overtimes in range(config.max_overtimes + 1):
                if stop_requested():
                    raise SimulationStopped()
                remaining = frame.seconds_remaining_game_ms if new_overtimes == 0 else 300_000
                horizon = sample_horizon(
                    rng, frame, config, home_pmf, away_pmf, home[active], away[active], remaining
                )
                home[active], away[active] = horizon.home_scores, horizon.away_scores
                reasons["possession_limit"] += int(np.count_nonzero(horizon.possession_limited))
                admitted = active[~horizon.possession_limited]
                completed = admitted[home[admitted] != away[admitted]]
                _accumulate_histogram(home_histogram, home[completed])
                _accumulate_histogram(away_histogram, away[completed])
                _accumulate_histogram(total_histogram, home[completed] + away[completed])
                new_ot_counts[new_overtimes] += len(completed)
                active = admitted[home[admitted] == away[admitted]]
                if not len(active):
                    break
                if new_overtimes == config.max_overtimes:
                    reasons["overtime_limit"] += len(active)
        unresolved = sum(reasons.values())
        outcome = SimulationOutcome(
            frame=frame,
            status=SimulationStatus.INCOMPLETE if unresolved else SimulationStatus.COMPLETE,
            requested=config.simulations,
            executed=config.simulations,
            resolved=config.simulations - unresolved,
            unresolved=unresolved,
            unavailable_reason=None,
            unresolved_counts=tuple(
                (reason, count) for reason, count in sorted(reasons.items()) if count
            ),
            new_ot_counts=tuple(new_ot_counts),
            home_pmf=home_pmf,
            away_pmf=away_pmf,
            seed_sha256=seed,
            simulation_revision_sha256=simulation_revision_sha256(frame, config, runtime),
            home_histogram=tuple(sorted(home_histogram.items())),
            away_histogram=tuple(sorted(away_histogram.items())),
            total_histogram=tuple(sorted(total_histogram.items())),
        )
        validate_simulation_outcome(outcome, config)
        return outcome
