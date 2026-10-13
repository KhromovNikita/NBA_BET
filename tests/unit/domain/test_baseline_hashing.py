import json
from pathlib import Path
from uuid import UUID

import pytest

from nba_live_totals.domain.baseline_hashing import (
    baseline_config_projection,
    baseline_config_sha256,
    baseline_outcome_bytes,
)
from nba_live_totals.domain.baselines import BaselineConfig, DeterministicBaselineCalculator
from nba_live_totals.domain.canonical import canonical_json_bytes
from tests.unit.domain.baseline_helpers import make_state


def test_default_config_has_frozen_canonical_identity() -> None:
    assert (
        baseline_config_sha256(BaselineConfig())
        == "a1bc560d77dadccd4e7198fb2181d739581c6281100a2d9d761ab4ae6f0e878e"
    )
    assert canonical_json_bytes(baseline_config_projection(BaselineConfig())) == (
        b'{"formula_version":"baseline-formulas-v1","history_window":20,'
        b'"output_schema_version":"baseline-jsonl-v1","pace_prior_combined_possessions":40,'
        b'"ppp_prior_possessions":20,"scale":1000000,"schema":"baseline-config-v1"}'
    )


def outcome(snapshot: int = 1, config: BaselineConfig | None = None) -> bytes:
    config = config or BaselineConfig()
    row = DeterministicBaselineCalculator().calculate_state(make_state(), config)[0]
    return baseline_outcome_bytes(
        feature_snapshot_id=UUID(int=snapshot),
        feature_revision_id="a" * 64,
        feature_output_sha256="b" * 64,
        row=row,
        config=config,
    )


def test_operational_identity_changes_bytes_not_semantic_revision() -> None:
    first, second = json.loads(outcome()), json.loads(outcome(2))
    assert first["baseline_revision_sha256"] == second["baseline_revision_sha256"]
    assert first["feature_snapshot_id"] != second["feature_snapshot_id"]
    assert first["home_ppp"] == first["away_ppp"] == 1_100_000
    assert first["pace"] == 102_000_000 and first["home_ppp_live_weight"] is None
    assert b"player" not in outcome()
    assert outcome().endswith(b"\n") and b"\r" not in outcome()
    assert (
        json.loads(outcome(config=BaselineConfig(history_window=5)))["baseline_revision_sha256"]
        != first["baseline_revision_sha256"]
    )


def test_invalid_source_hash_rejects() -> None:
    row = DeterministicBaselineCalculator().calculate_state(make_state(), BaselineConfig())[0]
    with pytest.raises(ValueError):
        baseline_outcome_bytes(
            feature_snapshot_id=UUID(int=1),
            feature_revision_id="bad",
            feature_output_sha256="b" * 64,
            row=row,
            config=BaselineConfig(),
        )


def test_historical_outcome_matches_literal_golden_bytes() -> None:
    path = Path(__file__).parents[2] / "fixtures/nba/baselines/default_historical.expected.jsonl"
    assert outcome() == path.read_bytes()
