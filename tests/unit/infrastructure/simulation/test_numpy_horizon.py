"""Controlled possession counts expose ordering, parity and native-boundary errors."""

from __future__ import annotations

import subprocess
import sys
from importlib import util
from typing import TYPE_CHECKING

import pytest

from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.domain.simulation import MAX_SCORE, SimulationConfig, make_simulation_frame
from nba_live_totals.domain.simulation_pmf import ScorePmf, build_score_pmf
from nba_live_totals.infrastructure.simulation.numpy_engine import (
    capture_simulation_runtime,
    make_numpy_rng,
    native_pmf,
    sample_horizon,
)
from tests.unit.domain.simulation_helpers import make_simulation_prediction

if TYPE_CHECKING:
    from nba_live_totals.infrastructure.simulation.numpy_engine import (
        FloatArray,
        HorizonBatch,
        IntArray,
    )

pytestmark = pytest.mark.skipif(
    util.find_spec("numpy") is None, reason="optional ML extra required"
)


class AtomRng:
    def __init__(
        self,
        counts: tuple[int, ...] = (0, 1, 2, 3),
        bits: tuple[int, ...] = (1, 1, 0, 0),
        *,
        fault: str = "",
    ) -> None:
        self.counts, self.bits, self.fault = counts, bits, fault
        self.calls: list[str] = []
        self.lambdas: list[float] = []
        self.team_counts: list[tuple[int, ...]] = []

    def poisson(self, lam: float, size: int) -> IntArray:
        import numpy as np

        self.calls.append("poisson")
        self.lambdas.append(lam)
        if self.fault == "poisson_negative":
            return np.array([-1] * size, dtype=np.int64)
        if self.fault == "poisson_float":
            return np.array(self.counts, dtype=np.float64)
        if self.fault == "poisson_shape":
            return np.zeros((size, 1), dtype=np.int64)
        return np.array(self.counts, dtype=np.int64)

    def first_side_bits(self, size: int) -> IntArray:
        import numpy as np

        self.calls.append("bits")
        if self.fault == "bits":
            return np.full(size, 2, dtype=np.int64)
        return np.array(self.bits[:size], dtype=np.int64)

    def multinomial(self, n: IntArray, pvals: FloatArray) -> IntArray:
        import numpy as np

        self.calls.append("home" if len(self.team_counts) % 2 == 0 else "away")
        self.team_counts.append(tuple(int(x) for x in n))
        if self.fault == "matrix_shape":
            return np.zeros((len(n), 2), dtype=np.int64)
        if self.fault == "matrix_float":
            return np.zeros((len(n), len(pvals)), dtype=np.float64)
        result = np.zeros((len(n), len(pvals)), dtype=np.int64)
        result[:, int(np.argmax(pvals))] = n
        if self.fault == "matrix_mass":
            result[:, 0] += 1
        if self.fault == "matrix_huge":
            result[:, 0] = 2**63 - 1
        return result


def horizon(
    rng: AtomRng,
    *,
    pace: int = 100000000,
    cap: int = 10000,
    home_score: int = 10,
    away_score: int = 9,
    ppp: int = 2000000,
) -> HorizonBatch:
    import numpy as np

    config = SimulationConfig(
        simulations=4,
        batch_size=4,
        max_possessions_per_horizon=cap,
        score_prior_weights=(250000, 500000, 250000),
    )
    frame = make_simulation_frame(
        make_simulation_prediction(
            pace_scaled=pace,
            home_ppp_scaled=ppp,
            away_ppp_scaled=ppp,
            home_score=home_score,
            away_score=away_score,
        ),
        config,
    )
    pmf = build_score_pmf(ppp, config.score_prior_weights)
    return sample_horizon(
        rng,
        frame,
        config,
        pmf,
        pmf,
        np.full(len(rng.counts), home_score, dtype=np.int64),
        np.full(len(rng.counts), away_score, dtype=np.int64),
        60000,
    )


def test_alternating_possessions_keep_odd_extra_side_and_n_zero_rows() -> None:
    rng = AtomRng()
    result = horizon(rng)
    assert result.home_scores.tolist() == [10, 12, 12, 12]
    assert result.away_scores.tolist() == [9, 9, 11, 13]
    assert result.possession_limited.tolist() == [False] * 4
    assert rng.calls == ["poisson", "bits", "home", "away"]
    assert rng.team_counts == [(0, 1, 1, 1), (0, 0, 1, 2)]


def test_possession_limit_is_unresolved_and_never_clamped_or_resampled() -> None:
    rng = AtomRng()
    result = horizon(rng, cap=2)
    assert result.home_scores.tolist() == [10, 12, 12, 10]
    assert result.away_scores.tolist() == [9, 9, 11, 9]
    assert result.possession_limited.tolist() == [False, False, False, True]
    assert rng.team_counts == [(0, 1, 1), (0, 0, 1)]
    huge = horizon(AtomRng((2**63 - 1,), (1,)), cap=2)
    assert huge.possession_limited.tolist() == [True]


def test_zero_pace_skips_all_draws_and_leaves_scores_unchanged() -> None:
    rng = AtomRng()
    result = horizon(rng, pace=0)
    assert result.home_scores.tolist() == [10] * 4
    assert result.away_scores.tolist() == [9] * 4
    assert rng.calls == []


def test_rate_and_duration_use_combined_possessions_per_48_minutes() -> None:
    import numpy as np

    config = SimulationConfig(simulations=4, batch_size=4)
    frame = make_simulation_frame(
        make_simulation_prediction(period=0, remaining_ms=2880000, home_score=0, away_score=0),
        config,
    )
    rng = AtomRng()
    pmf = build_score_pmf(1141111, config.score_prior_weights)
    sample_horizon(
        rng,
        frame,
        config,
        pmf,
        pmf,
        np.zeros(4, dtype=np.int64),
        np.zeros(4, dtype=np.int64),
        2880000,
    )
    assert rng.lambdas == [200.0]


def test_modal_last_projection_remaps_scored_outcomes_to_original_order() -> None:
    rng = AtomRng((1,), (1,))
    result = horizon(rng, ppp=1)
    assert result.home_scores.tolist() == [10]
    assert result.away_scores.tolist() == [9]


@pytest.mark.parametrize(
    "pmf",
    [
        ScorePmf((999999, 1, 0), 1),
        ScorePmf((500000, 500000, 0), 500000),
        build_score_pmf(31999999, (999968,) + (1,) * 32),
    ],
)
def test_native_pmf_keeps_large_residual_and_nonnegative_finite_probabilities(
    pmf: ScorePmf,
) -> None:
    import math

    native = native_pmf(pmf)
    mode = min(range(len(pmf.counts)), key=lambda k: (-pmf.counts[k], k))
    assert native.outcome_order[-1] == mode
    assert native.probabilities[-1] >= 1 / len(pmf.counts) - 1e-15
    assert all(math.isfinite(float(p)) and 0 <= p <= 1 for p in native.probabilities)
    assert math.isclose(math.fsum(native.probabilities), 1, abs_tol=1e-15)
    if pmf.target_scaled == 1:
        assert native.outcome_order == (1, 2, 0)


@pytest.mark.parametrize(
    "fault",
    [
        "poisson_negative",
        "poisson_float",
        "poisson_shape",
        "bits",
        "matrix_shape",
        "matrix_float",
        "matrix_mass",
        "matrix_huge",
    ],
)
def test_malformed_native_arrays_fail_before_unsafe_arithmetic(fault: str) -> None:
    with pytest.raises(SimulationError, match="simulation_native_error"):
        horizon(AtomRng(fault=fault))


def test_observed_score_overflow_fails_before_histogram_or_publication() -> None:
    with pytest.raises(SimulationError, match="simulation_numeric_overflow"):
        horizon(AtomRng((1,), (1,)), home_score=MAX_SCORE, away_score=0)


def test_actual_pcg64_and_array_n_multinomial_repeat_within_runtime() -> None:
    import numpy as np

    first, second = make_numpy_rng(2**255 + 42), make_numpy_rng(2**255 + 42)
    assert np.array_equal(first.poisson(200.0, 8), second.poisson(200.0, 8))
    assert np.array_equal(first.first_side_bits(8), second.first_side_bits(8))
    n = np.array([0, 1, 2], dtype=np.int64)
    p = native_pmf(ScorePmf((250000, 500000, 250000), 1000000)).probabilities
    a, b = first.multinomial(n, p), second.multinomial(n, p)
    assert a.shape == (3, 3) and a.dtype == np.int64
    assert a.sum(axis=1).tolist() == [0, 1, 2] and np.array_equal(a, b)
    assert capture_simulation_runtime().numpy_version == np.__version__


def test_import_is_native_free_and_missing_extra_has_safe_code() -> None:
    code = """
import sys
from importlib.abc import MetaPathFinder
class Blocked(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'catboost'}:
            raise ModuleNotFoundError('blocked native extra')
sys.meta_path.insert(0, Blocked())
import nba_live_totals.domain.simulation
import nba_live_totals.application.simulation.ports
from nba_live_totals.infrastructure.simulation.numpy_engine import capture_simulation_runtime
from nba_live_totals.application.simulation.errors import SimulationError
assert 'numpy' not in sys.modules and 'catboost' not in sys.modules
try:
    capture_simulation_runtime()
except SimulationError as error:
    assert error.code == 'ml_dependencies_missing'
else:
    raise AssertionError('missing extra was silently substituted')
print('lazy-ok')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "lazy-ok"
