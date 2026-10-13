"""Provider-neutral immutable baseline estimates in fixed-point units."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from nba_live_totals.domain.features import FEATURE_SCALE, FeatureDataset, GameStateFeature

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
        if count == 3:
            return BaselineStatus.AVAILABLE
        return BaselineStatus.PARTIAL if count else BaselineStatus.UNAVAILABLE


def _round(value: Fraction) -> int:
    whole, remainder = divmod(value.numerator, value.denominator)
    return bounded_integer(whole + int(2 * remainder >= value.denominator))


def _fraction(value: int | None) -> Fraction | None:
    return None if value is None else Fraction(bounded_integer(value))


def _average(a: int | None, b: int | None, denominator: int) -> Fraction | None:
    left, right = _fraction(a), _fraction(b)
    return None if left is None or right is None else (left + right) / denominator


class DeterministicBaselineCalculator:
    def calculate_state(
        self, state: GameStateFeature, config: BaselineConfig
    ) -> tuple[StateBaseline, StateBaseline, StateBaseline]:
        if not isinstance(state, GameStateFeature) or not isinstance(config, BaselineConfig):
            raise ValueError("invalid baseline input")
        home, away, live = state.home_pregame, state.away_pregame, state.live
        if home is None or away is None or live is None:
            raise ValueError("missing baseline feature objects")
        if not home.is_home or away.is_home or home.team_id == away.team_id:
            raise ValueError("invalid baseline team identities")
        hn, an = bounded_integer(state.home_possessions), bounded_integer(state.away_possessions)
        combined = bounded_integer(hn + an)
        if state.state_number == 0 and combined:
            raise ValueError("pregame baseline cannot contain possessions")
        window = config.history_window
        hg, ag = getattr(home, f"games_used_{window}"), getattr(away, f"games_used_{window}")
        priors = (
            _average(getattr(home, f"pace_{window}"), getattr(away, f"pace_{window}"), 2),
            _average(
                getattr(home, f"off_rating_{window}"), getattr(away, f"def_rating_{window}"), 200
            ),
            _average(
                getattr(away, f"off_rating_{window}"), getattr(home, f"def_rating_{window}"), 200
            ),
        )
        if not hg or not ag:
            priors = (None, None, None)
        values = tuple(_fraction(v) for v in (live.current_pace, live.home_ppp, live.away_ppp))
        ns = (combined, hn, an)
        ks = (
            config.pace_prior_combined_possessions,
            config.ppp_prior_possessions,
            config.ppp_prior_possessions,
        )
        result: list[StateBaseline] = []
        for method in BaselineMethod:
            metrics: list[BaselineMetric] = []
            for prior, value, n, k in zip(priors, values, ns, ks, strict=True):
                reason: BaselineUnavailableReason | None = None
                estimate: Fraction | None = None
                weight: int | None = None
                if not state.is_valid:
                    reason = BaselineUnavailableReason.INVALID_FEATURE_STATE
                elif method is BaselineMethod.HISTORICAL:
                    estimate = prior
                    reason = (
                        BaselineUnavailableReason.MISSING_HISTORICAL_INPUTS
                        if prior is None
                        else None
                    )
                elif method is BaselineMethod.LIVE:
                    estimate = value if n else None
                    reason = (
                        BaselineUnavailableReason.NO_LIVE_OBSERVATIONS
                        if not n
                        else BaselineUnavailableReason.MISSING_LIVE_INPUTS
                        if value is None
                        else None
                    )
                elif prior is None:
                    reason = BaselineUnavailableReason.MISSING_HISTORICAL_INPUTS
                elif not n:
                    estimate, weight = prior, 0
                elif value is None:
                    reason = BaselineUnavailableReason.MISSING_LIVE_INPUTS
                else:
                    estimate = (n * value + k * prior) / (n + k)
                    weight = _round(Fraction(BASELINE_SCALE * n, n + k))
                metrics.append(
                    BaselineMetric(None if estimate is None else _round(estimate), reason, weight)
                )
            result.append(StateBaseline(state, method, window, hg, ag, *metrics))
        return result[0], result[1], result[2]

    def calculate_dataset(
        self, dataset: FeatureDataset, config: BaselineConfig
    ) -> Iterator[StateBaseline]:
        if not isinstance(dataset, FeatureDataset) or not dataset.states:
            raise ValueError("invalid baseline dataset")
        first = dataset.states[0]
        previous = None
        for number, state in enumerate(dataset.states):
            if (
                state.state_number != number
                or state.game_id != dataset.game_id
                or (previous is not None and state.boundary <= previous)
                or state.home_pregame != first.home_pregame
                or state.away_pregame != first.away_pregame
            ):
                raise ValueError("inconsistent baseline dataset")
            yield from self.calculate_state(state, config)
            previous = state.boundary
