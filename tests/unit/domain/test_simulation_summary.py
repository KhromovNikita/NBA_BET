"""Exact moments/counts distinguish partial populations from usable probabilities."""

from dataclasses import replace

import pytest

from nba_live_totals.domain.simulation import (
    MAX_SCORE,
    SimulationConfig,
    SimulationQuery,
    SimulationRuntime,
    SimulationStatus,
    make_simulation_frame,
)
from nba_live_totals.domain.simulation_summary import (
    LineSummary,
    MarginalSummary,
    make_unavailable_outcome,
    summarize_histogram,
    summarize_lines,
    validate_simulation_outcome,
)
from tests.unit.domain.simulation_helpers import make_simulation_outcome, make_simulation_prediction


def test_exact_population_statistics_and_nearest_rank() -> None:
    result = summarize_histogram(((0, 1), (1, 1), (2, 2)), (0, 250000, 500000, 1000000))
    assert (result.mean_scaled, result.stdev_scaled) == (1250000, 829156)
    assert result.quantiles == ((0, 0), (250000, 0), (500000, 1), (1000000, 2))


def test_hand_counted_integer_and_half_lines_have_exact_equality_mass() -> None:
    outcome = make_simulation_outcome(((0, 1), (1, 1), (2, 2)))
    integer, half = summarize_lines(outcome, SimulationQuery((1000000, 1500000)))
    assert (
        integer.count_under,
        integer.count_equal,
        integer.count_over,
        integer.count_unknown,
    ) == (1, 1, 2, 0)
    assert (integer.prob_under_scaled, integer.prob_equal_scaled, integer.prob_over_scaled) == (
        250000,
        250000,
        500000,
    )
    assert (half.count_under, half.count_equal, half.count_over, half.count_unknown) == (2, 0, 2, 0)


def test_independent_probability_rounding_does_not_change_equality_mass() -> None:
    outcome = make_simulation_outcome(((0, 1), (1, 1), (2, 1)), requested=3)
    (line,) = summarize_lines(outcome, SimulationQuery((1000000,)))
    assert (line.prob_under_scaled, line.prob_equal_scaled, line.prob_over_scaled) == (333333,) * 3
    outcome = make_simulation_outcome(((0, 1), (1, 1), (2, 126)), requested=128)
    (line,) = summarize_lines(outcome, SimulationQuery((1000000,)))
    assert (line.prob_under_scaled, line.prob_equal_scaled, line.prob_over_scaled) == (
        7813,
        7813,
        984375,
    )
    assert (line.count_under, line.count_equal, line.count_over) == (1, 1, 126)


def test_unresolved_mass_is_unknown_and_never_renormalized() -> None:
    outcome = make_simulation_outcome(((2, 2),), unresolved=2)
    (line,) = summarize_lines(outcome, SimulationQuery((2000000,)))
    assert (line.count_under, line.count_equal, line.count_over, line.count_unknown) == (0, 2, 0, 2)
    assert line.prob_under_scaled is line.prob_equal_scaled is line.prob_over_scaled is None
    empty = make_simulation_outcome((), unresolved=4)
    assert empty.status is SimulationStatus.INCOMPLETE
    assert empty.total_histogram == ()
    (line,) = summarize_lines(empty, SimulationQuery((0,)))
    assert (line.count_under, line.count_equal, line.count_over, line.count_unknown) == (0, 0, 0, 4)


def test_unavailable_outcome_has_no_fake_histograms_or_line_counts() -> None:
    config = SimulationConfig(simulations=4, batch_size=4, max_overtimes=2)
    frame = make_simulation_frame(make_simulation_prediction(remaining_ms=0), config)
    outcome = make_unavailable_outcome(
        frame, config, SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")
    )
    validate_simulation_outcome(outcome, config)
    assert outcome.status is SimulationStatus.UNAVAILABLE
    assert (outcome.requested, outcome.executed, outcome.resolved, outcome.unresolved) == (
        4,
        0,
        0,
        0,
    )
    assert outcome.new_ot_counts == (0, 0, 0)
    assert outcome.home_pmf is None and outcome.away_pmf is None
    assert outcome.total_histogram is None
    (line,) = summarize_lines(outcome, SimulationQuery((0,)))
    assert line.count_under is line.count_equal is line.count_over is line.count_unknown is None
    with pytest.raises(ValueError):
        make_unavailable_outcome(
            make_simulation_frame(make_simulation_prediction(), config),
            config,
            SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64"),
        )


@pytest.mark.parametrize(
    "histogram",
    [
        (),
        ((0, 0),),
        ((0, True),),
        ((0, 1), (0, 2)),
        ((2, 1), (1, 1)),
        ((-1, 1),),
        ((MAX_SCORE + 1, 1),),
        ((0, 1000001),),
        ((0.5, 1),),
        ((0, 1, 2),),
    ],
)
def test_invalid_histograms_never_become_summaries(histogram: object) -> None:
    with pytest.raises(ValueError):
        summarize_histogram(histogram, (500000,))  # type: ignore[arg-type]


def test_histogram_bin_budget_is_explicit_not_silent_truncation() -> None:
    with pytest.raises(ValueError, match="simulation_histogram_limit"):
        summarize_histogram(tuple((n, 1) for n in range(100001)), (500000,))


def test_large_scores_use_python_integer_moments_without_int64_wrap() -> None:
    result = summarize_histogram(((MAX_SCORE - 1, 1), (MAX_SCORE, 1)), (0, 500000, 1000000))
    assert result.mean_scaled == MAX_SCORE * 1000000 - 500000
    assert result.stdev_scaled == 500000
    assert result.quantiles == ((0, MAX_SCORE - 1), (500000, MAX_SCORE - 1), (1000000, MAX_SCORE))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested", True),
        ("executed", 3),
        ("resolved", 3),
        ("unresolved", 1),
        ("status", "complete"),
        ("unavailable_reason", "no_future_time"),
        ("unresolved_counts", (("overtime_limit", 1),)),
        ("new_ot_counts", (3,) + (0,) * 10),
        ("new_ot_counts", (4,)),
        ("new_ot_counts", (False,) + (0,) * 10),
        ("home_histogram", ((2, 1), (3, 1), (5, 2))),
        ("away_histogram", ((0, 3),)),
        ("total_histogram", ((2, 4),)),
        ("total_histogram", None),
        ("home_pmf", None),
        ("seed_sha256", "bad"),
        ("simulation_revision_sha256", "bad"),
    ],
)
def test_outcome_rejects_contradictory_status_mass_or_moments(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(make_simulation_outcome(), **{field: value})  # type: ignore[arg-type]


def test_config_boundaries_are_rechecked_for_loaded_outcomes() -> None:
    outcome = make_simulation_outcome()
    config = SimulationConfig(simulations=4, batch_size=4)
    validate_simulation_outcome(outcome, config)
    for changed in (
        replace(config, simulations=5),
        replace(config, max_overtimes=2),
        replace(config, score_prior_weights=(250000, 500000, 250000)),
    ):
        with pytest.raises(ValueError):
            validate_simulation_outcome(outcome, changed)
    invalid_frame = replace(outcome.frame, unavailable_reason="pace_outside_runtime_limit")
    with pytest.raises(ValueError):
        validate_simulation_outcome(replace(outcome, frame=invalid_frame), config)


def test_line_and_marginal_dtos_reject_contradictory_derived_values() -> None:
    line = LineSummary(1000000, 1, 1, 2, 0, 250000, 250000, 500000)
    for field, value in (
        ("prob_equal_scaled", 250001),
        ("count_unknown", 1),
        ("count_equal", None),
        ("line_scaled", 250000),
    ):
        with pytest.raises(ValueError):
            replace(line, **{field: value})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MarginalSummary(1000000, -1, ((500000, 1),))
    with pytest.raises(ValueError):
        MarginalSummary(1000000, 0, ((0, 2), (1000000, 1)))
