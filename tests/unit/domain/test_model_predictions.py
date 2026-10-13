from dataclasses import replace

import pytest

from nba_live_totals.domain.model_predictions import (
    PredictedRate,
    StateModelPrediction,
    encode_model_rate,
)
from tests.unit.domain.modeling_helpers import make_target_case


def test_negative_rate_clips_but_nonfinite_never_becomes_missing() -> None:
    rate = encode_model_rate(-0.125)
    assert rate.value_scaled == 0 and rate.was_clipped_to_zero
    for value in (float("nan"), float("inf"), -float("inf"), 1e30, True):
        with pytest.raises(ValueError):
            encode_model_rate(value)
    assert encode_model_rate(0.0000005).value_scaled == 1
    assert encode_model_rate(1.6666666).value_scaled == 1_666_667
    assert encode_model_rate(-0.0) == PredictedRate(0, None, False)


def test_prediction_state_contract_checks_masks_and_hashes() -> None:
    state, _ = make_target_case()
    rate = encode_model_rate(1.5)
    prediction = StateModelPrediction(state, "a" * 64, "b" * 64, "c" * 64, rate, rate, rate)
    assert prediction.pace.value_scaled == 1_500_000
    with pytest.raises(ValueError):
        replace(prediction, input_sha256="bad")
    with pytest.raises(ValueError):
        replace(
            prediction,
            state=replace(state, seconds_remaining_game_ms=0, seconds_remaining_period_ms=0),
        )
    with pytest.raises(ValueError):
        PredictedRate(None, "no_future_time", True)
    with pytest.raises(ValueError):
        PredictedRate(1, None, True)
