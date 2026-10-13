from fractions import Fraction

from nba_live_totals.domain.baselines import BaselineMethod
from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.model_evaluation import (
    EvaluationReport,
    EvaluationRow,
    MetricRecord,
    evaluate_rows,
    evaluation_projection,
    round_sqrt_fraction,
    score_metrics,
)
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition
from tests.unit.domain.feature_helpers import GAME_ID, OTHER_GAME_ID


def test_exact_metrics_and_game_balance() -> None:
    scores = score_metrics((MetricRecord(GAME_ID, 0, 0), MetricRecord(OTHER_GAME_ID, 0, 2_000_000)))
    assert scores.mae_scaled == 1_000_000
    assert scores.rmse_scaled == 1_414_214
    unbalanced = score_metrics(
        (
            MetricRecord(GAME_ID, 0, 0),
            MetricRecord(GAME_ID, 0, 0),
            MetricRecord(OTHER_GAME_ID, 0, 3_000_000),
        )
    )
    assert unbalanced.mae_scaled == 1_000_000
    assert unbalanced.game_balanced_mae_scaled == 1_500_000
    assert unbalanced.game_balanced_rmse_scaled == 2_121_320
    empty = score_metrics(())
    assert empty.row_count == empty.game_count == 0
    assert empty.rmse_scaled is None and empty.unavailable_reason == "no_comparable_rows"


def test_rmse_half_step_is_exact_even_arbitrarily_close() -> None:
    assert round_sqrt_fraction(Fraction(1, 4)) == 1
    assert round_sqrt_fraction(Fraction(1, 4) - Fraction(1, 10**100)) == 0
    assert round_sqrt_fraction(Fraction(25, 4)) == 3
    assert round_sqrt_fraction(Fraction(25, 4) - Fraction(1, 10**100)) == 2


def test_partial_baseline_comparison_reweights_matched_games() -> None:
    rows = (
        EvaluationRow(GAME_ID, 4, 0, PredictedRate(0, None, False), (0, None, 0), None),
        EvaluationRow(GAME_ID, 4, 0, PredictedRate(0, None, False), (None, None, None), None),
        EvaluationRow(
            OTHER_GAME_ID,
            4,
            0,
            PredictedRate(3_000_000, None, False),
            (1_000_000, None, 1_000_000),
            None,
        ),
    )
    cohorts = evaluate_rows(ModelHead.HOME_PPP, TemporalPartition.TEST, iter(rows))
    full, regulation, ot = cohorts
    assert full.model.mae_scaled == 1_000_000
    historical, live, _ = full.comparisons
    assert historical.method is BaselineMethod.HISTORICAL
    assert historical.model.row_count == historical.baseline.row_count == 2
    assert historical.model.game_balanced_mae_scaled == 1_500_000
    assert historical.baseline.game_balanced_mae_scaled == 500_000
    assert live.model.unavailable_reason == "no_comparable_rows"
    assert regulation.total_row_count == 3
    assert ot.total_row_count == 0 and ot.model.unavailable_reason == "no_comparable_rows"
    assert b'"scale":1000000' in canonical_json_bytes(
        evaluation_projection(EvaluationReport(cohorts))
    )


def test_ot_coverage_and_negative_count_are_explicit() -> None:
    rows = (
        EvaluationRow(GAME_ID, 5, 100, PredictedRate(0, None, True), (None, None, None), None),
        EvaluationRow(
            GAME_ID,
            5,
            None,
            PredictedRate(None, "no_future_time", False),
            (None, None, None),
            "no_future_time",
        ),
    )
    _, regulation, ot = evaluate_rows(ModelHead.PACE, TemporalPartition.VALIDATION, rows)
    assert regulation.total_row_count == 0
    assert ot.total_row_count == 2 and ot.model.row_count == 1
    assert ot.excluded_counts == (("no_future_time", 1),)
    assert ot.model.negative_prediction_count == 1
