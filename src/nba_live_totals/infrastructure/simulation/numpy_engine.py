"""Batched possession sampling; native imports happen only at execution boundaries."""

from __future__ import annotations

import importlib
import math
import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.domain.baselines import MAX_INT64, bounded_integer
from nba_live_totals.domain.simulation import (
    MAX_SCORE,
    SIMULATION_SCALE,
    SimulationConfig,
    SimulationFrame,
    SimulationRuntime,
)
from nba_live_totals.domain.simulation_pmf import ScorePmf

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
