"""Independent scoring goldens catch mean drift, rounding bias and guessed fallbacks."""

import math
from dataclasses import replace

import pytest

from nba_live_totals.domain import simulation_pmf
from nba_live_totals.domain.simulation import DEFAULT_SCORE_PRIOR_WEIGHTS
from nba_live_totals.domain.simulation_pmf import ScorePmf, build_score_pmf


def test_binomial_prior_tilt_has_exact_known_solution() -> None:
    weights = (250000, 500000, 250000)
    assert build_score_pmf(500000, weights).counts == (562500, 375000, 62500)
    assert build_score_pmf(1500000, weights).counts == (62500, 375000, 562500)
    assert build_score_pmf(0, weights).counts == (1000000, 0, 0)
    assert build_score_pmf(2000000, weights).counts == (0, 0, 1000000)


def test_default_prior_mean_returns_the_original_integer_shape() -> None:
    pmf = build_score_pmf(1141111, DEFAULT_SCORE_PRIOR_WEIGHTS)
    assert pmf.counts == (460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)
    assert pmf.support_max == 8


@pytest.mark.parametrize(
    "weights",
    [DEFAULT_SCORE_PRIOR_WEIGHTS, (250000, 500000, 250000), (999968,) + (1,) * 32],
)
@pytest.mark.parametrize("target", [1, 2, 17, 499999, 500001, 1000000, 1234567, 1999999])
def test_pmf_mass_and_mean_are_exact_even_near_support_edges(
    weights: tuple[int, ...],
    target: int,
) -> None:
    for value in (target, (len(weights) - 1) * 1000000 - target):
        pmf = build_score_pmf(value, weights)
        assert sum(pmf.counts) == 1000000
        assert sum(k * c for k, c in enumerate(pmf.counts)) == value
        assert all(type(c) is int and c >= 0 for c in pmf.counts)


def test_largest_remainder_ties_prefer_lower_outcome() -> None:
    assert simulation_pmf._project_pmf((0.0000005, 0.0000005, 0.999999), 1999998) == (
        1,
        0,
        999999,
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (750002, (499998, 250002, 250000)),
        (749998, (500000, 250002, 249998)),
        (751024, (498976, 251024, 250000)),
    ],
)
def test_adjacent_repair_uses_declared_direction_and_preserves_mass(
    target: int,
    expected: tuple[int, ...],
) -> None:
    assert simulation_pmf._project_pmf((0.5, 0.25, 0.25), target) == expected


def test_projection_rejects_excessive_repair_and_bad_normalization() -> None:
    with pytest.raises(ValueError, match="repair"):
        simulation_pmf._project_pmf((0.5, 0.25, 0.25), 751025)
    for probabilities in (
        (float("nan"), 0.5, 0.5),
        (float("inf"), 0.0, 0.0),
        (-0.1, 0.5, 0.6),
        (0.0, 0.0, 0.0),
        (0.1, 0.1, 0.1),
    ):
        with pytest.raises(ValueError):
            simulation_pmf._project_pmf(probabilities, 1000000)


@pytest.mark.parametrize("target", [-1, 2000001, True, 0.5])
def test_unrepresentable_target_fails_without_clamping(target: object) -> None:
    with pytest.raises(ValueError):
        build_score_pmf(target, (250000, 500000, 250000))  # type: ignore[arg-type]


@pytest.mark.parametrize("weights", [(0, 500000, 500000), (500000, 500000), (1, 1, 1)])
def test_invalid_prior_cannot_reach_numeric_tilt(weights: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        build_score_pmf(1000000, weights)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("counts", (500000, 0, 500001)),
        ("counts", (500000, False, 500000)),
        ("counts", (500000, -1, 500001)),
        ("counts", [500000, 0, 500000]),
        ("counts", (500000, 500000)),
        ("target_scaled", 999999),
        ("target_scaled", True),
        ("policy_version", "tilted-integer-score-pmf-v2"),
    ],
)
def test_persisted_pmf_cannot_lie_about_mass_mean_or_policy(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(ScorePmf((500000, 0, 500000), 1000000), **{field: value})  # type: ignore[arg-type]


def test_nonfinite_tilt_and_unbracketed_target_raise_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(math, "exp", lambda _: float("nan"))
    with pytest.raises(ValueError, match=r"finite|normalization"):
        build_score_pmf(500000, (250000, 500000, 250000))
    monkeypatch.undo()
    monkeypatch.setattr(
        simulation_pmf, "_tilted_probabilities", lambda _weights, _beta: (1.0, 0.0, 0.0)
    )
    with pytest.raises(ValueError, match="bracket"):
        build_score_pmf(500000, (250000, 500000, 250000))
