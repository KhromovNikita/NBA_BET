from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pytest

from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.model_training import (
    TemporalPartition,
    TemporalSplitConfig,
    TrainingConfig,
    balanced_weights,
    build_training_rows,
    classify_game,
    training_config_projection,
    training_config_sha256,
)
from tests.unit.domain.feature_helpers import GAME_ID, OTHER_GAME_ID
from tests.unit.domain.modeling_helpers import make_training_input


def test_embargo_cutoffs_and_utc_whole_game_split() -> None:
    config = TemporalSplitConfig(date(2025, 7, 1), date(2026, 7, 1))
    for instant, expected in (
        (datetime(2025, 6, 29, 23, 59, tzinfo=UTC), TemporalPartition.TRAIN),
        (datetime(2025, 6, 30, tzinfo=UTC), TemporalPartition.EMBARGO),
        (datetime(2025, 7, 1, tzinfo=UTC), TemporalPartition.VALIDATION),
        (datetime(2026, 6, 30, tzinfo=UTC), TemporalPartition.EMBARGO),
        (datetime(2026, 7, 1, tzinfo=UTC), TemporalPartition.TEST),
    ):
        assert classify_game(instant, config) is expected
        assert classify_game(instant.astimezone(timezone(timedelta(hours=3))), config) is expected
    inputs = make_training_input()
    rows = tuple(build_training_rows(inputs, config, BaselineConfig()))
    assert len(rows) == len(inputs.features.states)
    assert {row.partition for row in rows} == {TemporalPartition.VALIDATION}
    assert rows[2].future_home_points == 5
    assert rows[2].future_home_possessions == 3
    assert rows[-1].eligibility[0].reason == "no_future_time"


def test_available_embargo_labels_keep_separate_eligibility_reason() -> None:
    inputs = make_training_input()
    config = TemporalSplitConfig(date(2025, 10, 21), date(2026, 7, 1))
    row = tuple(build_training_rows(inputs, config, BaselineConfig()))[2]
    assert row.partition is TemporalPartition.EMBARGO
    assert row.targets.home_ppp.value_scaled == 1_666_667
    assert row.targets.home_ppp.unavailable_reason is None
    assert all(not e.eligible and e.reason == "temporal_embargo" for e in row.eligibility)


def test_balanced_weights_only_use_supplied_cohort() -> None:
    assert balanced_weights((GAME_ID, GAME_ID, OTHER_GAME_ID)) == (
        Fraction(3, 4),
        Fraction(3, 4),
        Fraction(3, 2),
    )
    assert balanced_weights(()) == ()
    assert balanced_weights((OTHER_GAME_ID,)) == (Fraction(1),)


def test_split_rejects_invalid_dates_and_embargo() -> None:
    for gap in (True, 0, 31, 1.0):
        with pytest.raises(ValueError):
            TemporalSplitConfig(date(2025, 1, 1), date(2026, 1, 1), gap)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TemporalSplitConfig(date(2025, 1, 1), date(2025, 1, 2))
    with pytest.raises(ValueError):
        TemporalSplitConfig(datetime(2025, 1, 1), date(2026, 1, 1))
    with pytest.raises(ValueError):
        classify_game(datetime(2025, 1, 1), TemporalSplitConfig(date(2025, 1, 1), date(2026, 1, 1)))


def test_training_config_normalization_and_fixed_native_defaults() -> None:
    first = TrainingConfig(learning_rate=Decimal("0.0300"), l2_leaf_reg=Decimal("3.000"))
    assert training_config_sha256(first) == training_config_sha256(TrainingConfig())
    projection = training_config_projection(first)
    assert projection["learning_rate"] == "0.03"
    assert projection["l2_leaf_reg"] == "3"
    assert projection["thread_count"] == 1
    assert projection["task_type"] == "CPU"
    assert projection["has_time"] is True
    assert projection["bootstrap_type"] == "No"


@pytest.mark.parametrize(
    "changes",
    [
        {"iterations": True},
        {"iterations": 0},
        {"iterations": 100_001},
        {"depth": 11},
        {"learning_rate": Decimal("NaN")},
        {"learning_rate": Decimal("Infinity")},
        {"learning_rate": Decimal("0")},
        {"learning_rate": Decimal("1.1")},
        {"l2_leaf_reg": Decimal("-1")},
        {"l2_leaf_reg": Decimal("1001")},
        {"random_seed": 2**31},
        {"early_stopping_rounds": 1001},
        {"iterations": 10},
        {"max_rows_per_partition": 10_000_001},
    ],
)
def test_invalid_config_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(TrainingConfig(), **changes)  # type: ignore[arg-type]


def test_row_contract_rejects_mismatched_label_and_mask() -> None:
    inputs = make_training_input()
    row = tuple(
        build_training_rows(
            inputs, TemporalSplitConfig(date(2025, 1, 1), date(2026, 1, 1)), BaselineConfig()
        )
    )[2]
    with pytest.raises(ValueError):
        replace(row, future_home_points=6)
    with pytest.raises(ValueError):
        replace(row, is_valid=False)
