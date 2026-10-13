"""Provider-neutral immutable baseline estimates in fixed-point units."""

from dataclasses import dataclass
from enum import StrEnum

from nba_live_totals.domain.features import FEATURE_SCALE, GameStateFeature

BASELINE_SCALE = FEATURE_SCALE
BASELINE_FORMULA_VERSION = "baseline-formulas-v1"
BASELINE_OUTPUT_SCHEMA_VERSION = "baseline-jsonl-v1"
MAX_INT64 = 2**63 - 1


def bounded_integer(value: object, *, minimum: int = 0, maximum: int = MAX_INT64) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid baseline integer")
    return value


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    history_window: int = 20
    ppp_prior_possessions: int = 20
    pace_prior_combined_possessions: int = 40

    def __post_init__(self) -> None:
        if type(self.history_window) is not int or self.history_window not in {5, 10, 20}:
            raise ValueError("invalid baseline history window")
        bounded_integer(self.ppp_prior_possessions, minimum=1)
        bounded_integer(self.pace_prior_combined_possessions, minimum=1)


class BaselineMethod(StrEnum):
    HISTORICAL = "historical"
    LIVE = "live"
    SHRINKAGE = "shrinkage"


class BaselineStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class BaselineUnavailableReason(StrEnum):
    MISSING_HISTORICAL_INPUTS = "missing_historical_inputs"
    NO_LIVE_OBSERVATIONS = "no_live_observations"
    MISSING_LIVE_INPUTS = "missing_live_inputs"
    INVALID_FEATURE_STATE = "invalid_feature_state"


@dataclass(frozen=True, slots=True)
class BaselineMetric:
    value: int | None
    reason: BaselineUnavailableReason | None
    live_weight: int | None = None

    def __post_init__(self) -> None:
        if self.value is None:
            if (
                not isinstance(self.reason, BaselineUnavailableReason)
                or self.live_weight is not None
            ):
                raise ValueError("unavailable baseline metric requires reason and no weight")
        else:
            bounded_integer(self.value)
            if self.reason is not None:
                raise ValueError("available baseline metric cannot carry a reason")
        if self.live_weight is not None:
            bounded_integer(self.live_weight, maximum=BASELINE_SCALE)


@dataclass(frozen=True, slots=True)
class StateBaseline:
    state: GameStateFeature
    method: BaselineMethod
    history_window: int
    home_games_used: int
    away_games_used: int
    pace: BaselineMetric
    home_ppp: BaselineMetric
    away_ppp: BaselineMetric

    def __post_init__(self) -> None:
        if not isinstance(self.state, GameStateFeature) or not isinstance(
            self.method, BaselineMethod
        ):
            raise ValueError("invalid baseline state or method")
        BaselineConfig(history_window=self.history_window)
        bounded_integer(self.home_games_used, maximum=self.history_window)
        bounded_integer(self.away_games_used, maximum=self.history_window)
        for metric in (self.pace, self.home_ppp, self.away_ppp):
            if not isinstance(metric, BaselineMetric):
                raise ValueError("invalid baseline metric")
            if self.method is BaselineMethod.SHRINKAGE:
                if metric.value is not None and metric.live_weight is None:
                    raise ValueError("available shrinkage metric requires weight")
            elif metric.live_weight is not None:
                raise ValueError("only shrinkage metrics carry weights")

    @property
    def status(self) -> BaselineStatus:
        count = sum(
            metric.value is not None for metric in (self.pace, self.home_ppp, self.away_ppp)
        )
        return (
            BaselineStatus.AVAILABLE
            if count == 3
            else BaselineStatus.PARTIAL
            if count
            else BaselineStatus.UNAVAILABLE
        )
