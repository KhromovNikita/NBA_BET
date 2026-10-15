"""Whole trajectories retain unknown mass, OT semantics and bounded per-state RNG."""

from __future__ import annotations

import math
from dataclasses import replace
from fractions import Fraction
from importlib import util

import pytest

from nba_live_totals.application.simulation.errors import SimulationError, SimulationStopped
from nba_live_totals.domain.simulation import (
    MAX_SCORE,
    SimulationConfig,
    SimulationStatus,
    make_simulation_frame,
)
from nba_live_totals.infrastructure.simulation.numpy_engine import (
    NumpySimulationEngine,
    SimulationRng,
    make_numpy_rng,
)
from tests.unit.domain.simulation_helpers import make_simulation_prediction
from tests.unit.infrastructure.simulation.simulation_rng_fakes import (
    MonotoneSimulationRng,
    ScriptedSimulationRng,
)

pytestmark = pytest.mark.skipif(
    util.find_spec("numpy") is None, reason="optional ML extra required"
)


def atom_config(*, n: int = 1, batch: int = 1, ots: int = 2, cap: int = 10000) -> SimulationConfig:
    return SimulationConfig(
        simulations=n,
        batch_size=batch,
        max_overtimes=ots,
        max_possessions_per_horizon=cap,
        score_prior_weights=(250000, 500000, 250000),
    )


@pytest.mark.parametrize("period", [4, 5])
def test_residual_horizon_then_two_new_overtimes_use_frozen_rates(period: int) -> None:
    config = atom_config()
    frame = make_simulation_frame(
        make_simulation_prediction(
            period=period,
            home_score=0,
            away_score=0,
            home_ppp_scaled=2000000,
            away_ppp_scaled=2000000,
        ),
        config,
    )
    rng = ScriptedSimulationRng(((0,), (2,), (1,)))
    outcome = NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert outcome.status is SimulationStatus.COMPLETE
    assert outcome.home_histogram == ((4, 1),) and outcome.away_histogram == ((2, 1),)
    assert outcome.total_histogram == ((6, 1),) and outcome.new_ot_counts == (0, 0, 1)
    assert rng.calls == ["poisson", "bits", "home", "away"] * 3
    assert rng.lambdas == pytest.approx([25 / 6, 125 / 6, 125 / 6])


@pytest.mark.parametrize("tie", [False, True])
def test_zero_pace_never_draws_and_tied_paths_remain_unknown(tie: bool) -> None:
    config = atom_config(n=3, batch=2)
    frame = make_simulation_frame(
        make_simulation_prediction(pace_scaled=0, home_score=10, away_score=10 if tie else 9),
        config,
    )
    rng = ScriptedSimulationRng(())
    outcome = NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert rng.calls == [] and outcome.executed == 3
    if tie:
        assert outcome.status is SimulationStatus.INCOMPLETE
        assert (outcome.resolved, outcome.unresolved) == (0, 3)
        assert outcome.total_histogram == () and outcome.unresolved_counts == (
            ("overtime_limit", 3),
        )
        assert outcome.new_ot_counts == (0, 0, 0)
    else:
        assert outcome.status is SimulationStatus.COMPLETE
        assert outcome.total_histogram == ((19, 3),) and outcome.new_ot_counts == (3, 0, 0)


def test_possession_and_overtime_caps_have_mutually_exclusive_mass() -> None:
    config = atom_config(n=4, batch=4, ots=1, cap=2)
    frame = make_simulation_frame(
        make_simulation_prediction(
            home_score=0,
            away_score=0,
            home_ppp_scaled=2000000,
            away_ppp_scaled=2000000,
        ),
        config,
    )
    rng = ScriptedSimulationRng(((1, 2, 99, 0), (99, 0)))
    outcome = NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert (outcome.requested, outcome.executed, outcome.resolved, outcome.unresolved) == (
        4,
        4,
        1,
        3,
    )
    assert outcome.unresolved_counts == (("overtime_limit", 1), ("possession_limit", 2))
    assert outcome.total_histogram == ((2, 1),) and outcome.new_ot_counts == (1, 0)
    assert rng.sizes == [4, 2]


def test_zero_ppp_still_draws_possessions_but_never_adds_points() -> None:
    config = atom_config(n=2, batch=2)
    frame = make_simulation_frame(
        make_simulation_prediction(home_ppp_scaled=0, away_ppp_scaled=0), config
    )
    rng = ScriptedSimulationRng(((0, 100),))
    outcome = NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert outcome.total_histogram == ((19, 2),) and outcome.status is SimulationStatus.COMPLETE
    assert rng.calls == ["poisson", "bits", "home", "away"]


def test_active_overtime_paths_keep_original_order_instead_of_score_sort() -> None:
    config = atom_config(n=3, batch=3, ots=1)
    frame = make_simulation_frame(
        make_simulation_prediction(
            home_score=0,
            away_score=0,
            home_ppp_scaled=2000000,
            away_ppp_scaled=2000000,
        ),
        config,
    )
    rng = ScriptedSimulationRng(((2, 1, 0), (1, 3)), ((1, 1, 1), (0, 1)))
    outcome = NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert outcome.total_histogram == ((2, 1), (6, 2))
    assert outcome.home_histogram == ((2, 2), (4, 1))
    assert outcome.away_histogram == ((0, 1), (2, 1), (4, 1))
    assert outcome.new_ot_counts == (1, 2)


def test_partial_last_batch_uses_one_continuous_full_digest_stream() -> None:
    config = atom_config(n=5, batch=2)
    frame = make_simulation_frame(
        make_simulation_prediction(home_ppp_scaled=2000000, away_ppp_scaled=2000000), config
    )
    rng = MonotoneSimulationRng()
    seeds: list[int] = []

    def factory(seed: int) -> MonotoneSimulationRng:
        seeds.append(seed)
        return rng

    outcome = NumpySimulationEngine(rng_factory=factory).simulate(
        frame, config, stop_requested=lambda: False
    )
    assert seeds == [int(outcome.seed_sha256, 16)] and seeds[0] > 2**64
    assert rng.sizes == [2, 2, 1]
    assert outcome.total_histogram == ((19, 1), (21, 1), (23, 1), (25, 1), (27, 1))


def test_unavailable_state_does_not_construct_rng_or_shift_another_state() -> None:
    config = atom_config(n=20, batch=7)
    frame = make_simulation_frame(make_simulation_prediction(), config)
    unavailable = make_simulation_frame(make_simulation_prediction(remaining_ms=0), config)
    seeds: list[int] = []

    def factory(seed: int) -> SimulationRng:
        seeds.append(seed)
        return make_numpy_rng(seed)

    engine = NumpySimulationEngine(rng_factory=factory)
    first = engine.simulate(frame, config, stop_requested=lambda: False)
    skipped = engine.simulate(unavailable, config, stop_requested=lambda: False)
    second = engine.simulate(frame, config, stop_requested=lambda: False)
    assert first == second and len(seeds) == 2 and seeds[0] == seeds[1]
    assert skipped.status is SimulationStatus.UNAVAILABLE and skipped.executed == 0


@pytest.mark.parametrize("between", ["batches", "overtimes"])
def test_stop_is_checked_between_batches_and_overtime_stages(between: str) -> None:
    config = atom_config(n=2 if between == "batches" else 1)
    frame = make_simulation_frame(
        make_simulation_prediction(
            home_score=0 if between == "overtimes" else 10,
            away_score=0 if between == "overtimes" else 9,
            home_ppp_scaled=2000000,
            away_ppp_scaled=2000000,
        ),
        config,
    )
    rng = ScriptedSimulationRng(((0,),))
    with pytest.raises(SimulationStopped):
        NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
            frame, config, stop_requested=lambda: bool(rng.calls)
        )
    assert rng.calls == ["poisson", "bits", "home", "away"]


def test_histogram_budget_aborts_instead_of_dropping_completed_bins() -> None:
    config = atom_config(n=100001, batch=65536, cap=1000000)
    frame = make_simulation_frame(
        make_simulation_prediction(home_ppp_scaled=2000000, away_ppp_scaled=2000000), config
    )
    rng = MonotoneSimulationRng()
    with pytest.raises(SimulationError, match="simulation_histogram_limit"):
        NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
            frame, config, stop_requested=lambda: False
        )


def test_observed_overflow_propagates_as_operational_failure() -> None:
    config = atom_config()
    frame = make_simulation_frame(
        make_simulation_prediction(
            home_score=MAX_SCORE,
            away_score=0,
            home_ppp_scaled=2000000,
            away_ppp_scaled=2000000,
        ),
        config,
    )
    rng = ScriptedSimulationRng(((1,),))
    with pytest.raises(SimulationError, match="simulation_numeric_overflow"):
        NumpySimulationEngine(rng_factory=lambda _: rng).simulate(
            frame, config, stop_requested=lambda: False
        )


def test_native_total_matches_compound_poisson_mean_and_variance_with_five_sigma_bounds() -> None:
    n, lam = 100000, 200
    config = SimulationConfig(simulations=n, batch_size=4096, max_overtimes=1)
    frame = make_simulation_frame(
        make_simulation_prediction(
            period=0,
            remaining_ms=2880000,
            home_score=1000000,
            away_score=0,
        ),
        config,
    )
    engine = NumpySimulationEngine()
    outcome = engine.simulate(frame, config, stop_requested=lambda: False)
    assert outcome.status is SimulationStatus.COMPLETE and outcome.resolved == n
    histogram = outcome.total_histogram
    assert histogram is not None
    observed_mean = Fraction(sum((x - 1000000) * c for x, c in histogram), n)
    observed_variance = (
        Fraction(sum((x - 1000000) ** 2 * c for x, c in histogram), n) - observed_mean**2
    ) * Fraction(n, n - 1)
    weights = (460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)

    def raw(power: int) -> Fraction:
        return Fraction(sum(k**power * w for k, w in enumerate(weights)), 1000000)

    expected_mean, variance = lam * raw(1), lam * raw(2)
    mean_tolerance = 5 * math.sqrt(float(variance / n))
    variance_tolerance = 5 * math.sqrt(
        float((lam * raw(4) + (2 + Fraction(2, n - 1)) * variance**2) / n)
    )
    assert abs(float(observed_mean - expected_mean)) < mean_tolerance
    assert abs(float(observed_variance - variance)) < variance_tolerance
    assert engine.simulate(frame, config, stop_requested=lambda: False) == outcome
    changed = replace(config, seed=43)
    assert (
        engine.simulate(frame, changed, stop_requested=lambda: False).total_histogram != histogram
    )
