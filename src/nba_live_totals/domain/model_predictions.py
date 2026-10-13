"""One fixed postprocessing policy for persisted rates and evaluation."""

import math
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.features import GameStateFeature
from nba_live_totals.domain.model_features import require_sha256
from nba_live_totals.domain.model_targets import horizon_period

PREDICTION_POLICY_VERSION = "nonnegative-rmse-rates-v1"
PREDICTION_SCHEMA_VERSION = "model-predictions-jsonl-v1"


@dataclass(frozen=True, slots=True)
class PredictedRate:
    value_scaled: int | None
    unavailable_reason: str | None
    was_clipped_to_zero: bool

    def __post_init__(self) -> None:
        if type(self.was_clipped_to_zero) is not bool:
            raise ValueError("clipping flag must be an exact boolean")
        if self.value_scaled is None:
            if (
                self.unavailable_reason not in {"invalid_feature_state", "no_future_time"}
                or self.was_clipped_to_zero
            ):
                raise ValueError("invalid unavailable prediction")
        else:
            bounded_integer(self.value_scaled)
            if self.unavailable_reason is not None or (
                self.was_clipped_to_zero and self.value_scaled != 0
            ):
                raise ValueError("invalid available prediction")


def encode_model_rate(value: float) -> PredictedRate:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("model rate must be a finite float")
    if value < 0:
        return PredictedRate(0, None, True)
    exact = Fraction(Decimal(str(value))) * 1_000_000
    whole, remainder = divmod(exact.numerator, exact.denominator)
    return PredictedRate(whole + (2 * remainder >= exact.denominator), None, False)


@dataclass(frozen=True, slots=True)
class StateModelPrediction:
    state: GameStateFeature
    model_revision_sha256: str
    model_artifact_sha256: str
    input_sha256: str
    pace: PredictedRate
    home_ppp: PredictedRate
    away_ppp: PredictedRate

    def __post_init__(self) -> None:
        horizon_period(self.state)
        for digest in (self.model_revision_sha256, self.model_artifact_sha256, self.input_sha256):
            require_sha256(digest)
        reason = (
            "invalid_feature_state"
            if not self.state.is_valid
            else ("no_future_time" if not self.state.seconds_remaining_game_ms else None)
        )
        for rate in (self.pace, self.home_ppp, self.away_ppp):
            if not isinstance(rate, PredictedRate) or rate.unavailable_reason != reason:
                raise ValueError("prediction availability contradicts current state")
