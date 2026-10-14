"""Small chronological, nonconstant head cohorts with real domain labels."""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from nba_live_totals.application.modeling.models import HeadRows
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TemporalPartition, build_training_rows
from tests.unit.application.modeling_fakes import make_dataset_request, remap_training_input
from tests.unit.domain.modeling_helpers import make_training_input


def tiny_head_rows(partition: TemporalPartition, head: ModelHead) -> HeadRows:
    first = make_training_input()
    examples = {
        TemporalPartition.TRAIN: (
            first,
            remap_training_input(first, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC)),
        ),
        TemporalPartition.VALIDATION: (
            remap_training_input(first, GameId("0022500003"), datetime(2025, 11, 5, tzinfo=UTC)),
        ),
        TemporalPartition.TEST: (
            remap_training_input(first, GameId("0022500004"), datetime(2025, 12, 5, tzinfo=UTC)),
        ),
    }[partition]
    split = make_dataset_request(Path("unused.jsonl")).split
    index = tuple(ModelHead).index(head)
    rows = tuple(
        row
        for example in examples
        for row in build_training_rows(example, split, BaselineConfig())
        if row.eligibility[index].eligible
    )
    return HeadRows(
        partition, head, len(rows), Counter(row.game_id for row in rows), lambda: iter(rows)
    )
