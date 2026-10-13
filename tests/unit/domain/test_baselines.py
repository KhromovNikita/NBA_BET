from dataclasses import FrozenInstanceError, replace

import pytest

from nba_live_totals.domain.baselines import BaselineConfig, BaselineMetric
from nba_live_totals.domain.features import LiveFeatures
from nba_live_totals.domain.lineups import LineupStatus
from tests.unit.domain.baseline_helpers import make_dataset, make_state


def test_config_defaults_and_metric_invariants() -> None:
    config = BaselineConfig()
    assert (
        config.history_window,
        config.ppp_prior_possessions,
        config.pace_prior_combined_possessions,
    ) == (20, 20, 40)
    assert BaselineMetric(0, None).value == 0
    with pytest.raises(ValueError):
        BaselineMetric(None, None)
    with pytest.raises(FrozenInstanceError):
        config.history_window = 5  # type: ignore[misc]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "20"])
def test_config_rejects_invalid_strength(value: object) -> None:
    with pytest.raises(ValueError):
        BaselineConfig(ppp_prior_possessions=value)  # type: ignore[arg-type]


def test_config_rejects_invalid_window() -> None:
    with pytest.raises(ValueError):
        BaselineConfig(history_window=7)


@pytest.mark.parametrize("value", [-1, 2**63, True, 1.5])
def test_metric_rejects_invalid_value(value: object) -> None:
    with pytest.raises(ValueError):
        BaselineMetric(value, None)  # type: ignore[arg-type]


def calculate(**changes: object):  # type: ignore[no-untyped-def]
    from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

    return DeterministicBaselineCalculator().calculate_state(
        make_state(**changes), BaselineConfig()
    )


def test_baselines_use_independent_team_counts_and_exact_priors() -> None:
    h, live, mix = calculate()
    assert (h.pace.value, h.home_ppp.value, h.away_ppp.value) == (102_000_000, 1_100_000, 1_100_000)
    assert (live.pace.value, live.home_ppp.value, live.away_ppp.value) == (
        110_000_000,
        1_500_000,
        800_000,
    )
    assert (mix.pace.value, mix.home_ppp.value, mix.away_ppp.value) == (
        106_000_000,
        1_300_000,
        950_000,
    )
    assert mix.home_ppp.live_weight == mix.away_ppp.live_weight == mix.pace.live_weight == 500_000
    _, _, unequal = calculate(home_possessions=0, away_possessions=40)
    assert unequal.home_ppp.value == 1_100_000
    assert unequal.away_ppp.value == 900_000


def test_shrinkage_does_not_round_prior_before_blending() -> None:
    from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

    state = make_state(home_possessions=1)
    assert state.home_pregame is not None and state.away_pregame is not None
    state = replace(
        state,
        home_pregame=replace(state.home_pregame, off_rating_20=0),
        away_pregame=replace(state.away_pregame, def_rating_20=100),
        live=LiveFeatures(home_ppp=0),
    )
    h, _, mix = DeterministicBaselineCalculator().calculate_state(
        state, BaselineConfig(ppp_prior_possessions=1)
    )
    assert h.home_ppp.value == 1
    assert mix.home_ppp.value == 0
    assert mix.home_ppp.live_weight == 500_000


@pytest.mark.parametrize("window", [5, 10, 20])
def test_selected_window(window: int) -> None:
    from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

    rows = DeterministicBaselineCalculator().calculate_state(
        make_state(), BaselineConfig(history_window=window)
    )
    assert rows[0].home_ppp.value == 1_100_000
    assert rows[0].home_games_used == 3


def test_missing_and_zero_observations_are_explicit() -> None:
    _, live, mix = calculate(state_number=0)
    assert live.home_ppp.reason == "no_live_observations"
    assert mix.home_ppp.value == 1_100_000 and mix.home_ppp.live_weight == 0
    _, live, mix = calculate(live=LiveFeatures(home_ppp=0))
    assert live.home_ppp.value == 0
    assert live.away_ppp.reason == "missing_live_inputs"
    assert mix.away_ppp.reason == "missing_live_inputs"
    state = make_state()
    assert state.home_pregame is not None
    _, live, mix = calculate(home_pregame=replace(state.home_pregame, off_rating_20=None))
    assert live.home_ppp.value == 1_500_000
    assert mix.home_ppp.reason == "missing_historical_inputs"


def test_invalid_state_then_recovery_preserves_later_estimates() -> None:
    from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

    bad = make_state(
        state_number=1,
        home_lineup_status=LineupStatus.INVALID,
        home_player_ids=(),
        home_invalid_reason="missing_home_sub",
        home_invalid_since=make_state(state_number=1).boundary,
    )
    dataset = make_dataset(make_state(state_number=0), bad, make_state(state_number=2))
    rows = tuple(DeterministicBaselineCalculator().calculate_dataset(dataset, BaselineConfig()))
    assert len(rows) == 9
    assert all(row.pace.reason == "invalid_feature_state" for row in rows[3:6])
    assert rows[6].home_ppp.value == 1_100_000


@pytest.mark.parametrize(
    "changes", [{"home_pregame": None}, {"live": None}, {"state_number": 0, "home_possessions": 1}]
)
def test_structural_missing_inputs_reject(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        calculate(**changes)


def test_negative_and_overflow_rates_reject() -> None:
    for value in (-1, 2**63):
        with pytest.raises(ValueError):
            calculate(live=LiveFeatures(current_pace=value))


def test_later_state_mutation_preserves_earlier_numeric_outcomes() -> None:
    from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

    calc, config = DeterministicBaselineCalculator(), BaselineConfig()
    first, second = make_state(state_number=1), make_state(state_number=2)
    before = tuple(
        calc.calculate_dataset(make_dataset(make_state(state_number=0), first, second), config)
    )
    after = tuple(
        calc.calculate_dataset(
            make_dataset(make_state(state_number=0), first, replace(second, live=LiveFeatures())),
            config,
        )
    )
    assert before[:6] == after[:6]
