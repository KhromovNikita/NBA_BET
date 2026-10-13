import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.features import LiveFeatures
from nba_live_totals.domain.model_features import (
    FeatureColumn,
    ModelFeatureVector,
    model_feature_descriptor,
    model_input_sha256,
    project_model_features,
)
from tests.unit.domain.baseline_helpers import make_state
from tests.unit.domain.feature_helpers import OTHER_GAME_ID

GOLDEN = Path(__file__).parents[2] / "fixtures/nba/modeling/default_projection.expected.json"


def test_projection_matches_independent_literal_golden() -> None:
    golden = json.loads(GOLDEN.read_bytes())
    descriptor = model_feature_descriptor(BaselineConfig())
    assert len(descriptor.columns) == 96
    assert [{"name": c.name, "divisor": c.divisor} for c in descriptor.columns] == golden["columns"]
    assert descriptor.version == golden["version"]
    assert descriptor.baseline_config_sha256 == golden["baseline_config_sha256"]
    assert descriptor.sha256 == golden["sha256"]
    assert descriptor.columns[0].name == "period"
    assert descriptor.columns[-1].name == "baseline.shrinkage.away_ppp_live_weight"
    assert all(
        "player_id" not in c.name and "state_number" not in c.name for c in descriptor.columns
    )


def test_projection_maps_scaled_signed_bool_and_null_values() -> None:
    state = make_state(offense_difference=-12_500_000)
    assert state.home_pregame is not None
    state = replace(state, home_pregame=replace(state.home_pregame, back_to_back=True, rest_days=0))
    descriptor = model_feature_descriptor(BaselineConfig())
    vector = project_model_features(state, BaselineConfig())
    mapping = dict(zip((c.name for c in descriptor.columns), vector.values, strict=True))
    assert mapping["offense_difference"] == -12_500_000
    assert mapping["home_pregame.back_to_back"] == 1
    assert mapping["home_pregame.rest_days"] == 0
    assert mapping["home_lineup.lineup_avg_minutes"] is None
    assert mapping["baseline.historical.pace"] == 102_000_000
    assert mapping["baseline.shrinkage.home_ppp_live_weight"] == 500_000
    missing = project_model_features(
        replace(state, home_pregame=None, home_lineup=None, live=None), BaselineConfig()
    )
    absent = dict(zip((c.name for c in descriptor.columns), missing.values, strict=True))
    assert all(
        v is None
        for n, v in absent.items()
        if n.startswith(("home_pregame.", "home_lineup.", "live.", "baseline."))
    )


def test_projection_is_causal_and_configuration_is_part_of_identity() -> None:
    state = make_state()
    vector = project_model_features(state, BaselineConfig())
    changed_identity = replace(state, game_id=OTHER_GAME_ID, state_number=41, possession_number=41)
    assert project_model_features(changed_identity, BaselineConfig()) == vector
    other = project_model_features(state, BaselineConfig(ppp_prior_possessions=40))
    assert other.descriptor_sha256 != vector.descriptor_sha256
    assert model_input_sha256(other) != model_input_sha256(vector)
    changed_live = project_model_features(replace(state, live=LiveFeatures()), BaselineConfig())
    assert model_input_sha256(changed_live) != model_input_sha256(vector)
    canonical = json.dumps(
        {"descriptor_sha256": vector.descriptor_sha256, "values": vector.values},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert model_input_sha256(vector) == hashlib.sha256(canonical).hexdigest()


def test_projection_contracts_reject_invalid_values() -> None:
    for value in (True, 1.2, "1", 2**63, -(2**63) - 1):
        with pytest.raises(ValueError):
            ModelFeatureVector("a" * 64, (value,) * 96)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ModelFeatureVector("a" * 64, (0,))
    with pytest.raises(ValueError):
        FeatureColumn("game_id", 0)
