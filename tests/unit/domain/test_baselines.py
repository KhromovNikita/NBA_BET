from dataclasses import FrozenInstanceError

import pytest

from nba_live_totals.domain.baselines import BaselineConfig, BaselineMetric


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
