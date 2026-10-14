"""Chronological training records, exact per-game weights and frozen CPU recipe."""

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from nba_live_totals.domain.baselines import (
    BaselineConfig,
    BaselineMethod,
    BaselineMetric,
    DeterministicBaselineCalculator,
    bounded_integer,
)
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_sha256
from nba_live_totals.domain.features import scaled_ratio
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.model_features import ModelFeatureVector, project_model_features
from nba_live_totals.domain.model_targets import (
    HeadTarget,
    RemainingTargets,
    TrainingGameInput,
    calculate_remaining_targets,
    horizon_period,
)
from nba_live_totals.domain.possessions import EventBoundary

SPLIT_VERSION = "game-temporal-split-v1"
TRAINING_VERSION = "catboost-training-v1"
_ELIGIBILITY_REASONS = frozenset(
    {"temporal_embargo", "invalid_feature_state", "no_future_time", "no_future_possessions"}
)


class TemporalPartition(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    EMBARGO = "embargo"


@dataclass(frozen=True, slots=True)
class TemporalSplitConfig:
    validation_from: date
    test_from: date
    embargo_days: int = 1

    def __post_init__(self) -> None:
        if type(self.validation_from) is not date or type(self.test_from) is not date:
            raise ValueError("split cutoffs require exact dates")
        bounded_integer(self.embargo_days, minimum=1, maximum=30)
        try:
            self.validation_from - timedelta(days=self.embargo_days)
            stop = self.test_from - timedelta(days=self.embargo_days)
        except OverflowError as error:
            raise ValueError("split date underflow") from error
        if self.validation_from >= stop:
            raise ValueError("split requires a nonempty validation interval")


def classify_game(started_at: datetime, config: TemporalSplitConfig) -> TemporalPartition:
    if not isinstance(started_at, datetime) or started_at.tzinfo is None:
        raise ValueError("game start requires a timezone-aware datetime")
    if not isinstance(config, TemporalSplitConfig):
        raise ValueError("invalid temporal split config")
    start = started_at.astimezone(UTC)
    validation = datetime.combine(config.validation_from, time(), UTC)
    test = datetime.combine(config.test_from, time(), UTC)
    gap = timedelta(days=config.embargo_days)
    if start < validation - gap:
        return TemporalPartition.TRAIN
    if start < validation:
        return TemporalPartition.EMBARGO
    if start < test - gap:
        return TemporalPartition.VALIDATION
    if start < test:
        return TemporalPartition.EMBARGO
    return TemporalPartition.TEST


def decimal_string(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("expected a finite Decimal")
    if value == 0:
        return "0"
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    if type(exponent) is not int:
        raise ValueError("expected a finite decimal exponent")
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return str(Decimal((parts.sign, tuple(digits), exponent)))


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    iterations: int = 1000
    depth: int = 6
    learning_rate: Decimal = Decimal("0.03")
    l2_leaf_reg: Decimal = Decimal("3")
    random_seed: int = 42
    early_stopping_rounds: int = 100
    max_rows_per_partition: int = 1_000_000

    def __post_init__(self) -> None:
        bounded_integer(self.iterations, minimum=1, maximum=100_000)
        bounded_integer(self.depth, minimum=1, maximum=10)
        bounded_integer(self.random_seed, maximum=2**31 - 1)
        bounded_integer(self.early_stopping_rounds, minimum=1, maximum=self.iterations)
        bounded_integer(self.max_rows_per_partition, minimum=1, maximum=10_000_000)
        for value, low, high, inclusive in (
            (self.learning_rate, Decimal(0), Decimal(1), False),
            (self.l2_leaf_reg, Decimal(0), Decimal(1000), True),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("training decimal parameters must be finite Decimals")
            if value > high or value < low or (not inclusive and value == low):
                raise ValueError("training decimal parameter out of range")


def training_config_projection(config: TrainingConfig) -> CanonicalObject:
    if not isinstance(config, TrainingConfig):
        raise ValueError("invalid training config")
    return {
        "version": TRAINING_VERSION,
        "task_type": "CPU",
        "loss_function": "RMSE",
        "eval_metric": "RMSE",
        "iterations": config.iterations,
        "depth": config.depth,
        "learning_rate": decimal_string(config.learning_rate),
        "l2_leaf_reg": decimal_string(config.l2_leaf_reg),
        "random_seed": config.random_seed,
        "thread_count": 1,
        "has_time": True,
        "boosting_type": "Plain",
        "bootstrap_type": "No",
        "random_strength": 0,
        "nan_mode": "Min",
        "use_best_model": True,
        "early_stopping_rounds": config.early_stopping_rounds,
        "allow_writing_files": False,
        "verbose": False,
        "max_rows_per_partition": config.max_rows_per_partition,
    }


def training_config_sha256(config: TrainingConfig) -> str:
    return canonical_json_sha256(training_config_projection(config))


@dataclass(frozen=True, slots=True)
class HeadEligibility:
    eligible: bool
    reason: str | None

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool or (self.eligible and self.reason is not None):
            raise ValueError("invalid eligibility mask")
        if not self.eligible and self.reason not in _ELIGIBILITY_REASONS:
            raise ValueError("excluded row requires a supported reason")


@dataclass(frozen=True, slots=True)
class TrainingBaseline:
    method: BaselineMethod
    pace: BaselineMetric
    home_ppp: BaselineMetric
    away_ppp: BaselineMetric

    def __post_init__(self) -> None:
        if not isinstance(self.method, BaselineMethod):
            raise ValueError("invalid training baseline method")
        for metric in (self.pace, self.home_ppp, self.away_ppp):
            if not isinstance(metric, BaselineMetric):
                raise ValueError("invalid training baseline metric")
            if self.method is BaselineMethod.SHRINKAGE:
                if metric.value is not None and metric.live_weight is None:
                    raise ValueError("available shrinkage requires a weight")
            elif metric.live_weight is not None:
                raise ValueError("only shrinkage can carry live weight")


def _targets_from_counts(
    valid: bool, remaining: int, hp: int, ap: int, hn: int, an: int
) -> RemainingTargets:
    if not valid or not remaining:
        missing = HeadTarget(None, "invalid_feature_state" if not valid else "no_future_time")
        return RemainingTargets(missing, missing, missing)
    return RemainingTargets(
        HeadTarget(scaled_ratio(2_880_000 * (hn + an), 2 * remaining), None),
        HeadTarget(scaled_ratio(hp, hn), None) if hn else HeadTarget(None, "no_future_possessions"),
        HeadTarget(scaled_ratio(ap, an), None) if an else HeadTarget(None, "no_future_possessions"),
    )


def _eligibility(
    targets: RemainingTargets, partition: TemporalPartition
) -> tuple[HeadEligibility, HeadEligibility, HeadEligibility]:
    masks = tuple(
        HeadEligibility(False, "temporal_embargo")
        if partition is TemporalPartition.EMBARGO
        else HeadEligibility(target.value_scaled is not None, target.unavailable_reason)
        for target in (targets.pace, targets.home_ppp, targets.away_ppp)
    )
    return masks[0], masks[1], masks[2]


@dataclass(frozen=True, slots=True)
class TrainingRow:
    game_id: GameId
    started_at: datetime
    state_number: int
    boundary: EventBoundary
    period: int
    horizon_period: int
    is_valid: bool
    partition: TemporalPartition
    remaining_time_ms: int
    vector: ModelFeatureVector
    targets: RemainingTargets
    eligibility: tuple[HeadEligibility, HeadEligibility, HeadEligibility]
    future_home_possessions: int
    future_away_possessions: int
    future_home_points: int
    future_away_points: int
    baselines: tuple[TrainingBaseline, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId) or not isinstance(self.boundary, EventBoundary):
            raise ValueError("invalid training row identity")
        if not isinstance(self.started_at, datetime) or self.started_at.tzinfo is None:
            raise ValueError("training row start requires timezone")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        if type(self.is_valid) is not bool or not isinstance(self.partition, TemporalPartition):
            raise ValueError("invalid training row status")
        for value in (
            self.state_number,
            self.period,
            self.horizon_period,
            self.remaining_time_ms,
            self.future_home_possessions,
            self.future_away_possessions,
            self.future_home_points,
            self.future_away_points,
        ):
            bounded_integer(value)
        if self.horizon_period != max(4, self.period) or self.remaining_time_ms > (
            2_880_000 if self.period <= 4 else 300_000
        ):
            raise ValueError("invalid training horizon")
        if not isinstance(self.vector, ModelFeatureVector):
            raise ValueError("invalid training vector")
        if self.vector.values[0] != self.period or self.vector.values[1] != self.remaining_time_ms:
            raise ValueError("training row diagnostics contradict projection")
        expected = _targets_from_counts(
            self.is_valid,
            self.remaining_time_ms,
            self.future_home_points,
            self.future_away_points,
            self.future_home_possessions,
            self.future_away_possessions,
        )
        if self.targets != expected or self.eligibility != _eligibility(expected, self.partition):
            raise ValueError("training targets or eligibility contradict diagnostics")
        if type(self.baselines) is not tuple or any(
            not isinstance(b, TrainingBaseline) for b in self.baselines
        ):
            raise ValueError("invalid training baselines")
        if tuple(b.method for b in self.baselines) != tuple(BaselineMethod):
            raise ValueError("training row requires ordered three baselines")


def build_training_rows(
    inputs: TrainingGameInput, split: TemporalSplitConfig, baseline_config: BaselineConfig
) -> Iterator[TrainingRow]:
    if not isinstance(inputs, TrainingGameInput):
        raise ValueError("invalid training game input")
    partition = classify_game(inputs.game.started_at, split)
    endpoints = {end.period: end for end in inputs.period_ends}
    for state in inputs.features.states:
        period = horizon_period(state)
        endpoint = endpoints[period]
        targets = calculate_remaining_targets(state, endpoint)
        baselines = tuple(
            TrainingBaseline(row.method, row.pace, row.home_ppp, row.away_ppp)
            for row in DeterministicBaselineCalculator().calculate_state(state, baseline_config)
        )
        yield TrainingRow(
            inputs.game.game_id,
            inputs.game.started_at,
            state.state_number,
            state.boundary,
            state.period,
            period,
            state.is_valid,
            partition,
            state.seconds_remaining_game_ms,
            project_model_features(state, baseline_config),
            targets,
            _eligibility(targets, partition),
            endpoint.home_possessions - state.home_possessions,
            endpoint.away_possessions - state.away_possessions,
            endpoint.home_score - state.home_score,
            endpoint.away_score - state.away_score,
            baselines,
        )


def balanced_weights(game_ids: Sequence[GameId]) -> tuple[Fraction, ...]:
    if any(not isinstance(game_id, GameId) for game_id in game_ids):
        raise ValueError("weights require typed game IDs")
    counts = Counter(game_ids)
    return tuple(Fraction(len(game_ids), len(counts) * counts[game_id]) for game_id in game_ids)
