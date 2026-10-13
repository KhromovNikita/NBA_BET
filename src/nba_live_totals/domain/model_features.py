"""Frozen numeric projection shared by training and inference."""

import re
from dataclasses import dataclass

from nba_live_totals.domain.baseline_hashing import baseline_config_sha256
from nba_live_totals.domain.baselines import (
    MAX_INT64,
    BaselineConfig,
    DeterministicBaselineCalculator,
    bounded_integer,
)
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_sha256
from nba_live_totals.domain.features import GameStateFeature

PROJECTION_VERSION = "model-feature-projection-v1"


def require_sha256(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("expected a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class FeatureColumn:
    name: str
    divisor: int

    def __post_init__(self) -> None:
        if type(self.name) is not str or re.fullmatch(r"[a-z][a-z0-9_.]{0,127}", self.name) is None:
            raise ValueError("invalid feature column name")
        if type(self.divisor) is not int or self.divisor not in (1, 1_000_000):
            raise ValueError("unsupported feature divisor")


# Literal and versioned: new domain fields never silently enter the model.
_COLUMNS = (
    FeatureColumn("period", 1),
    FeatureColumn("seconds_remaining_game_ms", 1),
    FeatureColumn("seconds_remaining_period_ms", 1),
    FeatureColumn("home_score", 1),
    FeatureColumn("away_score", 1),
    FeatureColumn("score_diff", 1),
    FeatureColumn("home_possessions", 1),
    FeatureColumn("away_possessions", 1),
    FeatureColumn("offense_difference", 1_000_000),
    FeatureColumn("defense_difference", 1_000_000),
    FeatureColumn("pace_difference", 1_000_000),
    FeatureColumn("home_pregame.games_used_5", 1),
    FeatureColumn("home_pregame.games_used_10", 1),
    FeatureColumn("home_pregame.games_used_20", 1),
    FeatureColumn("home_pregame.off_rating_5", 1_000_000),
    FeatureColumn("home_pregame.off_rating_10", 1_000_000),
    FeatureColumn("home_pregame.off_rating_20", 1_000_000),
    FeatureColumn("home_pregame.def_rating_5", 1_000_000),
    FeatureColumn("home_pregame.def_rating_10", 1_000_000),
    FeatureColumn("home_pregame.def_rating_20", 1_000_000),
    FeatureColumn("home_pregame.pace_5", 1_000_000),
    FeatureColumn("home_pregame.pace_10", 1_000_000),
    FeatureColumn("home_pregame.pace_20", 1_000_000),
    FeatureColumn("home_pregame.rest_days", 1),
    FeatureColumn("home_pregame.back_to_back", 1),
    FeatureColumn("away_pregame.games_used_5", 1),
    FeatureColumn("away_pregame.games_used_10", 1),
    FeatureColumn("away_pregame.games_used_20", 1),
    FeatureColumn("away_pregame.off_rating_5", 1_000_000),
    FeatureColumn("away_pregame.off_rating_10", 1_000_000),
    FeatureColumn("away_pregame.off_rating_20", 1_000_000),
    FeatureColumn("away_pregame.def_rating_5", 1_000_000),
    FeatureColumn("away_pregame.def_rating_10", 1_000_000),
    FeatureColumn("away_pregame.def_rating_20", 1_000_000),
    FeatureColumn("away_pregame.pace_5", 1_000_000),
    FeatureColumn("away_pregame.pace_10", 1_000_000),
    FeatureColumn("away_pregame.pace_20", 1_000_000),
    FeatureColumn("away_pregame.rest_days", 1),
    FeatureColumn("away_pregame.back_to_back", 1),
    FeatureColumn("home_lineup.profiled_player_count", 1),
    FeatureColumn("home_lineup.lineup_avg_off_rating", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_def_rating", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_usage_rate", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_three_point_attempt_rate", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_free_throw_rate", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_minutes", 1_000_000),
    FeatureColumn("home_lineup.lineup_avg_pace_context", 1_000_000),
    FeatureColumn("away_lineup.profiled_player_count", 1),
    FeatureColumn("away_lineup.lineup_avg_off_rating", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_def_rating", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_usage_rate", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_three_point_attempt_rate", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_free_throw_rate", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_minutes", 1_000_000),
    FeatureColumn("away_lineup.lineup_avg_pace_context", 1_000_000),
    FeatureColumn("live.current_pace", 1_000_000),
    FeatureColumn("live.pace_last_5", 1_000_000),
    FeatureColumn("live.pace_last_10", 1_000_000),
    FeatureColumn("live.pace_last_20", 1_000_000),
    FeatureColumn("live.pace_last_5_possessions_used", 1),
    FeatureColumn("live.pace_last_10_possessions_used", 1),
    FeatureColumn("live.pace_last_20_possessions_used", 1),
    FeatureColumn("live.home_ppp", 1_000_000),
    FeatureColumn("live.away_ppp", 1_000_000),
    FeatureColumn("live.home_ppp_last_5", 1_000_000),
    FeatureColumn("live.home_ppp_last_10", 1_000_000),
    FeatureColumn("live.home_ppp_last_20", 1_000_000),
    FeatureColumn("live.away_ppp_last_5", 1_000_000),
    FeatureColumn("live.away_ppp_last_10", 1_000_000),
    FeatureColumn("live.away_ppp_last_20", 1_000_000),
    FeatureColumn("live.home_ppp_last_5_possessions_used", 1),
    FeatureColumn("live.home_ppp_last_10_possessions_used", 1),
    FeatureColumn("live.home_ppp_last_20_possessions_used", 1),
    FeatureColumn("live.away_ppp_last_5_possessions_used", 1),
    FeatureColumn("live.away_ppp_last_10_possessions_used", 1),
    FeatureColumn("live.away_ppp_last_20_possessions_used", 1),
    FeatureColumn("live.home_three_point_attempt_rate", 1_000_000),
    FeatureColumn("live.away_three_point_attempt_rate", 1_000_000),
    FeatureColumn("live.home_free_throw_rate", 1_000_000),
    FeatureColumn("live.away_free_throw_rate", 1_000_000),
    FeatureColumn("live.home_turnover_rate", 1_000_000),
    FeatureColumn("live.away_turnover_rate", 1_000_000),
    FeatureColumn("live.home_offensive_rebound_rate", 1_000_000),
    FeatureColumn("live.away_offensive_rebound_rate", 1_000_000),
    FeatureColumn("baseline.historical.pace", 1_000_000),
    FeatureColumn("baseline.historical.home_ppp", 1_000_000),
    FeatureColumn("baseline.historical.away_ppp", 1_000_000),
    FeatureColumn("baseline.live.pace", 1_000_000),
    FeatureColumn("baseline.live.home_ppp", 1_000_000),
    FeatureColumn("baseline.live.away_ppp", 1_000_000),
    FeatureColumn("baseline.shrinkage.pace", 1_000_000),
    FeatureColumn("baseline.shrinkage.home_ppp", 1_000_000),
    FeatureColumn("baseline.shrinkage.away_ppp", 1_000_000),
    FeatureColumn("baseline.shrinkage.pace_live_weight", 1_000_000),
    FeatureColumn("baseline.shrinkage.home_ppp_live_weight", 1_000_000),
    FeatureColumn("baseline.shrinkage.away_ppp_live_weight", 1_000_000),
)


@dataclass(frozen=True, slots=True)
class FeatureProjectionDescriptor:
    version: str
    columns: tuple[FeatureColumn, ...]
    baseline_config_sha256: str

    def __post_init__(self) -> None:
        if self.version != PROJECTION_VERSION:
            raise ValueError("unsupported projection version")
        if (
            type(self.columns) is not tuple
            or len(self.columns) != 96
            or any(not isinstance(c, FeatureColumn) for c in self.columns)
        ):
            raise ValueError("expected 96 typed feature columns")
        if len({c.name for c in self.columns}) != 96:
            raise ValueError("duplicate feature column")
        require_sha256(self.baseline_config_sha256)

    def projection(self) -> CanonicalObject:
        return {
            "version": self.version,
            "columns": [{"name": c.name, "divisor": c.divisor} for c in self.columns],
            "baseline_config_sha256": self.baseline_config_sha256,
        }

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.projection())


@dataclass(frozen=True, slots=True)
class ModelFeatureVector:
    descriptor_sha256: str
    values: tuple[int | None, ...]

    def __post_init__(self) -> None:
        require_sha256(self.descriptor_sha256)
        if type(self.values) is not tuple or len(self.values) != 96:
            raise ValueError("expected exactly 96 immutable feature values")
        for value in self.values:
            if value is not None:
                bounded_integer(value, minimum=-MAX_INT64 - 1)


def model_feature_descriptor(config: BaselineConfig) -> FeatureProjectionDescriptor:
    return FeatureProjectionDescriptor(PROJECTION_VERSION, _COLUMNS, baseline_config_sha256(config))


def project_model_features(
    state: GameStateFeature, baseline_config: BaselineConfig
) -> ModelFeatureVector:
    if not isinstance(state, GameStateFeature):
        raise ValueError("expected a typed feature state")
    descriptor = model_feature_descriptor(baseline_config)
    baseline_values: dict[str, int | None] = {}
    if state.home_pregame is not None and state.away_pregame is not None and state.live is not None:
        for row in DeterministicBaselineCalculator().calculate_state(state, baseline_config):
            for head in ("pace", "home_ppp", "away_ppp"):
                metric = getattr(row, head)
                baseline_values[f"baseline.{row.method.value}.{head}"] = metric.value
                if row.method.value == "shrinkage":
                    baseline_values[f"baseline.shrinkage.{head}_live_weight"] = metric.live_weight
    values: list[int | None] = []
    for column in descriptor.columns:
        if column.name.startswith("baseline."):
            value = baseline_values.get(column.name)
        elif "." in column.name:
            parent, attribute = column.name.split(".", 1)
            nested = getattr(state, parent)
            value = None if nested is None else getattr(nested, attribute)
            if attribute == "back_to_back" and value is not None:
                value = int(value)
        else:
            value = getattr(state, column.name)
        values.append(value)
    return ModelFeatureVector(descriptor.sha256, tuple(values))


def model_input_sha256(vector: ModelFeatureVector) -> str:
    if not isinstance(vector, ModelFeatureVector):
        raise ValueError("expected a model feature vector")
    return canonical_json_sha256(
        {
            "descriptor_sha256": vector.descriptor_sha256,
            "values": vector.values,
        }
    )
