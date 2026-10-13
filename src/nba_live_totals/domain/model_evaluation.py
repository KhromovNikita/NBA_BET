"""Exact streaming metrics with per-game balance and matched baseline cohorts."""

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction

from nba_live_totals.domain.baselines import BaselineMethod, bounded_integer
from nba_live_totals.domain.canonical import CanonicalObject
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition


def _round_fraction(value: Fraction) -> int:
    whole, remainder = divmod(value.numerator, value.denominator)
    return bounded_integer(whole + (2 * remainder >= value.denominator))


def round_sqrt_fraction(value: Fraction) -> int:
    if not isinstance(value, Fraction) or value < 0:
        raise ValueError("RMSE requires a nonnegative exact fraction")
    k = math.isqrt(value.numerator // value.denominator)
    return bounded_integer(k + (4 * value.numerator >= value.denominator * (2 * k + 1) ** 2))


@dataclass(frozen=True, slots=True)
class MetricRecord:
    game_id: GameId
    target_scaled: int
    prediction_scaled: int
    was_clipped_to_zero: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId) or type(self.was_clipped_to_zero) is not bool:
            raise ValueError("invalid metric identity or clipping flag")
        bounded_integer(self.target_scaled)
        bounded_integer(self.prediction_scaled)
        if self.was_clipped_to_zero and self.prediction_scaled:
            raise ValueError("clipped prediction must be zero")


@dataclass(frozen=True, slots=True)
class MetricScores:
    row_count: int
    game_count: int
    mae_scaled: int | None
    rmse_scaled: int | None
    game_balanced_mae_scaled: int | None
    game_balanced_rmse_scaled: int | None
    negative_prediction_count: int
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        bounded_integer(self.row_count)
        bounded_integer(self.game_count, maximum=self.row_count)
        bounded_integer(self.negative_prediction_count, maximum=self.row_count)
        values = (
            self.mae_scaled,
            self.rmse_scaled,
            self.game_balanced_mae_scaled,
            self.game_balanced_rmse_scaled,
        )
        if not self.row_count:
            if (
                any(value is not None for value in values)
                or self.unavailable_reason != "no_comparable_rows"
            ):
                raise ValueError("empty metrics require unavailable values")
        elif not self.game_count or self.unavailable_reason is not None:
            raise ValueError("nonempty metrics require games and available values")
        else:
            for value in values:
                bounded_integer(value)


@dataclass(slots=True)
class _MetricAccumulator:
    # Only one exact accumulator per game, never all state records.
    games: dict[GameId, tuple[int, int, int, int]] = field(default_factory=dict)

    def append(self, record: MetricRecord) -> None:
        if not isinstance(record, MetricRecord):
            raise ValueError("expected a typed metric record")
        count, absolute, squared, negatives = self.games.get(record.game_id, (0, 0, 0, 0))
        error = abs(record.target_scaled - record.prediction_scaled)
        self.games[record.game_id] = (
            count + 1,
            absolute + error,
            squared + error * error,
            negatives + record.was_clipped_to_zero,
        )

    def scores(self) -> MetricScores:
        games = len(self.games)
        if not games:
            return MetricScores(0, 0, None, None, None, None, 0, "no_comparable_rows")
        rows = sum(value[0] for value in self.games.values())
        absolute = sum(value[1] for value in self.games.values())
        squared = sum(value[2] for value in self.games.values())
        balanced_absolute = (
            sum((Fraction(v[1], v[0]) for v in self.games.values()), Fraction()) / games
        )
        balanced_squared = (
            sum((Fraction(v[2], v[0]) for v in self.games.values()), Fraction()) / games
        )
        return MetricScores(
            rows,
            games,
            _round_fraction(Fraction(absolute, rows)),
            round_sqrt_fraction(Fraction(squared, rows)),
            _round_fraction(balanced_absolute),
            round_sqrt_fraction(balanced_squared),
            sum(value[3] for value in self.games.values()),
            None,
        )


def score_metrics(records: Iterable[MetricRecord]) -> MetricScores:
    accumulator = _MetricAccumulator()
    for record in records:
        accumulator.append(record)
    return accumulator.scores()


class EvaluationScope(StrEnum):
    ALL = "all"
    REGULATION = "regulation"
    OT = "ot"


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    game_id: GameId
    period: int
    target_scaled: int | None
    prediction: PredictedRate
    baseline_values: tuple[int | None, int | None, int | None]
    exclusion_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId) or not isinstance(self.prediction, PredictedRate):
            raise ValueError("invalid evaluation row identity or prediction")
        bounded_integer(self.period)
        if type(self.baseline_values) is not tuple or len(self.baseline_values) != 3:
            raise ValueError("expected three ordered baseline values")
        for value in (*self.baseline_values, self.target_scaled):
            if value is not None:
                bounded_integer(value)
        if self.exclusion_reason is None:
            if self.target_scaled is None or self.prediction.value_scaled is None:
                raise ValueError("eligible evaluation requires target and prediction")
        elif self.exclusion_reason not in {
            "invalid_feature_state",
            "no_future_time",
            "no_future_possessions",
            "temporal_embargo",
        }:
            raise ValueError("unsupported evaluation exclusion")


@dataclass(frozen=True, slots=True)
class MatchedComparison:
    method: BaselineMethod
    model: MetricScores
    baseline: MetricScores

    def __post_init__(self) -> None:
        if (
            not isinstance(self.method, BaselineMethod)
            or not isinstance(self.model, MetricScores)
            or not isinstance(self.baseline, MetricScores)
        ):
            raise ValueError("invalid matched comparison")
        if (self.model.row_count, self.model.game_count) != (
            self.baseline.row_count,
            self.baseline.game_count,
        ):
            raise ValueError("matched comparisons require identical cohorts")


@dataclass(frozen=True, slots=True)
class EvaluationCohort:
    head: ModelHead
    partition: TemporalPartition
    scope: EvaluationScope
    total_row_count: int
    total_game_count: int
    excluded_counts: tuple[tuple[str, int], ...]
    model: MetricScores
    comparisons: tuple[MatchedComparison, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.head, ModelHead)
            or self.partition not in (TemporalPartition.VALIDATION, TemporalPartition.TEST)
            or not isinstance(self.partition, TemporalPartition)
            or not isinstance(self.scope, EvaluationScope)
        ):
            raise ValueError("invalid held-out evaluation cohort")
        bounded_integer(self.total_row_count)
        bounded_integer(self.total_game_count, maximum=self.total_row_count)
        if (
            not isinstance(self.model, MetricScores)
            or self.model.row_count > self.total_row_count
            or self.model.game_count > self.total_game_count
        ):
            raise ValueError("model coverage exceeds total cohort")
        if type(self.excluded_counts) is not tuple or len(
            {reason for reason, _ in self.excluded_counts}
        ) != len(self.excluded_counts):
            raise ValueError("invalid exclusion counters")
        for reason, count in self.excluded_counts:
            if reason not in {
                "invalid_feature_state",
                "no_future_time",
                "no_future_possessions",
                "temporal_embargo",
            }:
                raise ValueError("invalid exclusion reason")
            bounded_integer(count, minimum=1)
        if (
            sum(count for _, count in self.excluded_counts) + self.model.row_count
            != self.total_row_count
        ):
            raise ValueError("incomplete cohort coverage")
        if (
            type(self.comparisons) is not tuple
            or any(not isinstance(c, MatchedComparison) for c in self.comparisons)
            or tuple(c.method for c in self.comparisons) != tuple(BaselineMethod)
        ):
            raise ValueError("expected three ordered matched comparisons")
        if any(
            c.model.row_count > self.model.row_count or c.model.game_count > self.model.game_count
            for c in self.comparisons
        ):
            raise ValueError("matched comparison exceeds eligible cohort")


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    cohorts: tuple[EvaluationCohort, ...]

    def __post_init__(self) -> None:
        if type(self.cohorts) is not tuple or any(
            not isinstance(c, EvaluationCohort) for c in self.cohorts
        ):
            raise ValueError("expected immutable typed evaluation cohorts")
        if len({(c.head, c.partition, c.scope) for c in self.cohorts}) != len(self.cohorts):
            raise ValueError("duplicate evaluation cohort")


@dataclass(slots=True)
class _CohortAccumulator:
    model: _MetricAccumulator = field(default_factory=_MetricAccumulator)
    matched_models: tuple[_MetricAccumulator, ...] = field(
        default_factory=lambda: tuple(_MetricAccumulator() for _ in BaselineMethod)
    )
    baselines: tuple[_MetricAccumulator, ...] = field(
        default_factory=lambda: tuple(_MetricAccumulator() for _ in BaselineMethod)
    )
    total_rows: int = 0
    games: set[GameId] = field(default_factory=set)
    excluded: Counter[str] = field(default_factory=Counter)

    def append(self, row: EvaluationRow) -> None:
        self.total_rows += 1
        self.games.add(row.game_id)
        if row.exclusion_reason is not None:
            self.excluded[row.exclusion_reason] += 1
            return
        # EvaluationRow has already checked eligible availability.
        assert row.target_scaled is not None and row.prediction.value_scaled is not None
        model = MetricRecord(
            row.game_id,
            row.target_scaled,
            row.prediction.value_scaled,
            row.prediction.was_clipped_to_zero,
        )
        self.model.append(model)
        for index, value in enumerate(row.baseline_values):
            if value is not None:
                self.matched_models[index].append(model)
                self.baselines[index].append(MetricRecord(row.game_id, row.target_scaled, value))

    def finish(
        self, head: ModelHead, partition: TemporalPartition, scope: EvaluationScope
    ) -> EvaluationCohort:
        return EvaluationCohort(
            head,
            partition,
            scope,
            self.total_rows,
            len(self.games),
            tuple(sorted(self.excluded.items())),
            self.model.scores(),
            tuple(
                MatchedComparison(method, model.scores(), baseline.scores())
                for method, model, baseline in zip(
                    BaselineMethod, self.matched_models, self.baselines, strict=True
                )
            ),
        )


def evaluate_rows(
    head: ModelHead, partition: TemporalPartition, rows: Iterable[EvaluationRow]
) -> tuple[EvaluationCohort, ...]:
    accumulators = {scope: _CohortAccumulator() for scope in EvaluationScope}
    for row in rows:
        if not isinstance(row, EvaluationRow):
            raise ValueError("expected a typed evaluation row")
        accumulators[EvaluationScope.ALL].append(row)
        scope = EvaluationScope.REGULATION if row.period <= 4 else EvaluationScope.OT
        accumulators[scope].append(row)
    return tuple(
        accumulator.finish(head, partition, scope) for scope, accumulator in accumulators.items()
    )


def _scores_projection(scores: MetricScores) -> CanonicalObject:
    return {
        "row_count": scores.row_count,
        "game_count": scores.game_count,
        "mae_scaled": scores.mae_scaled,
        "rmse_scaled": scores.rmse_scaled,
        "game_balanced_mae_scaled": scores.game_balanced_mae_scaled,
        "game_balanced_rmse_scaled": scores.game_balanced_rmse_scaled,
        "negative_prediction_count": scores.negative_prediction_count,
        "unavailable_reason": scores.unavailable_reason,
    }


def evaluation_projection(report: EvaluationReport) -> CanonicalObject:
    if not isinstance(report, EvaluationReport):
        raise ValueError("expected a typed evaluation report")
    return {
        "version": "model-evaluation-v1",
        "scale": 1_000_000,
        "chronological_holdout_limitation": "canonical_sources_have_no_wall_clock_finished_at",
        "cohorts": [
            {
                "head": c.head.value,
                "partition": c.partition.value,
                "scope": c.scope.value,
                "total_row_count": c.total_row_count,
                "total_game_count": c.total_game_count,
                "excluded_counts": dict(c.excluded_counts),
                "model": _scores_projection(c.model),
                "comparisons": [
                    {
                        "method": m.method.value,
                        "model": _scores_projection(m.model),
                        "baseline": _scores_projection(m.baseline),
                    }
                    for m in c.comparisons
                ],
            }
            for c in report.cohorts
        ],
    }
