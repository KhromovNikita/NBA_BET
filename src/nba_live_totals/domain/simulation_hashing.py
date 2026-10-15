"""Explicit identities keep queries and operational provenance out of state RNG."""

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_sha256
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_features import require_sha256
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.simulation import (
    SIMULATION_POLICY_VERSION,
    SIMULATION_SEED_POLICY_VERSION,
    SimulationConfig,
    SimulationFrame,
    SimulationQuery,
    SimulationRuntime,
)
from nba_live_totals.domain.simulation_pmf import SCORE_PMF_POLICY_VERSION

SIMULATION_SCHEMA_VERSION = "simulation-results-jsonl-v1"


def simulation_config_projection(config: SimulationConfig) -> CanonicalObject:
    if not isinstance(config, SimulationConfig):
        raise ValueError("expected simulation configuration")
    return {
        "policy_version": config.policy_version,
        "simulations": config.simulations,
        "seed": config.seed,
        "batch_size": config.batch_size,
        "max_overtimes": config.max_overtimes,
        "max_possessions_per_horizon": config.max_possessions_per_horizon,
        "score_prior_weights": list(config.score_prior_weights),
    }


def simulation_query_projection(query: SimulationQuery) -> CanonicalObject:
    if not isinstance(query, SimulationQuery):
        raise ValueError("expected simulation query")
    return {
        "policy_version": query.policy_version,
        "lines_scaled": list(query.lines_scaled),
        "quantiles_scaled": list(query.quantiles_scaled),
    }


def simulation_runtime_projection(runtime: SimulationRuntime) -> CanonicalObject:
    if not isinstance(runtime, SimulationRuntime):
        raise ValueError("expected simulation runtime")
    return {
        "python_version": runtime.python_version,
        "numpy_version": runtime.numpy_version,
        "system": runtime.system,
        "machine": runtime.machine,
        "engine_version": runtime.engine_version,
    }


def _rate_projection(rate: PredictedRate) -> CanonicalObject:
    return {
        "value_scaled": rate.value_scaled,
        "unavailable_reason": rate.unavailable_reason,
        "was_clipped_to_zero": rate.was_clipped_to_zero,
    }


def simulation_frame_projection(frame: SimulationFrame) -> CanonicalObject:
    if not isinstance(frame, SimulationFrame):
        raise ValueError("expected causal simulation frame")
    return {
        "game_id": frame.game_id.value,
        "state_number": frame.state_number,
        "possession_number": frame.possession_number,
        "state_kind": frame.state_kind.value,
        "boundary": {
            "sequence_number": frame.boundary.sequence_number,
            "side": frame.boundary.side.value,
        },
        "period": frame.period,
        "seconds_remaining_game_ms": frame.seconds_remaining_game_ms,
        "seconds_remaining_period_ms": frame.seconds_remaining_period_ms,
        "home_score": frame.home_score,
        "away_score": frame.away_score,
        "is_valid": frame.is_valid,
        "pace": _rate_projection(frame.pace),
        "home_ppp": _rate_projection(frame.home_ppp),
        "away_ppp": _rate_projection(frame.away_ppp),
        "input_sha256": frame.input_sha256,
        "model_revision_sha256": frame.model_revision_sha256,
        "model_artifact_sha256": frame.model_artifact_sha256,
        "unavailable_reason": frame.unavailable_reason,
    }


def simulation_config_sha256(config: SimulationConfig) -> str:
    return canonical_json_sha256(simulation_config_projection(config))


def simulation_query_sha256(query: SimulationQuery) -> str:
    return canonical_json_sha256(simulation_query_projection(query))


def state_seed_sha256(
    frame: SimulationFrame,
    config: SimulationConfig,
    runtime: SimulationRuntime,
) -> str:
    return canonical_json_sha256(
        {
            "seed_policy_version": SIMULATION_SEED_POLICY_VERSION,
            "base_seed": config.seed,
            "config_sha256": simulation_config_sha256(config),
            "runtime": simulation_runtime_projection(runtime),
            "frame": simulation_frame_projection(frame),
            "score_pmf_policy_version": SCORE_PMF_POLICY_VERSION,
        }
    )


def simulation_revision_sha256(
    frame: SimulationFrame,
    config: SimulationConfig,
    runtime: SimulationRuntime,
) -> str:
    return canonical_json_sha256(
        {
            "simulation_policy_version": SIMULATION_POLICY_VERSION,
            "state_seed_sha256": state_seed_sha256(frame, config, runtime),
        }
    )


def simulation_run_revision(
    selectors: CanonicalObject,
    config: SimulationConfig,
    query: SimulationQuery,
    runtime: SimulationRuntime,
    semantic_sources: tuple[CanonicalObject, ...],
    semantic_body_sha256: str,
) -> str:
    require_sha256(semantic_body_sha256)
    if type(selectors) is not dict or set(selectors) != {"seasons", "game_id", "state_number"}:
        raise ValueError("simulation selectors cannot contain operational fields")
    seasons = selectors["seasons"]
    if type(seasons) not in {tuple, list} or not seasons:
        raise ValueError("simulation run requires seasons")
    if not isinstance(seasons, tuple | list):
        raise ValueError("invalid simulation season sequence")
    season_ids: list[str] = []
    for season in seasons:
        if type(season) is not str:
            raise ValueError("invalid simulation season")
        SeasonId(season)
        season_ids.append(season)
    if season_ids != sorted(set(season_ids)):
        raise ValueError("simulation seasons must be sorted unique")
    game_id, state_number = selectors["game_id"], selectors["state_number"]
    if game_id is not None:
        if type(game_id) is not str or len(seasons) != 1:
            raise ValueError("simulation game requires a single season")
        GameId(game_id)
    if state_number is not None:
        bounded_integer(state_number)
        if game_id is None:
            raise ValueError("simulation state requires a game")
    if type(semantic_sources) is not tuple or any(type(s) is not dict for s in semantic_sources):
        raise ValueError("simulation semantic sources must be immutable typed projections")
    return canonical_json_sha256(
        {
            "schema_version": SIMULATION_SCHEMA_VERSION,
            "selectors": selectors,
            "config": simulation_config_projection(config),
            "query": simulation_query_projection(query),
            "runtime": simulation_runtime_projection(runtime),
            "semantic_sources": semantic_sources,
            "semantic_body_sha256": semantic_body_sha256,
        }
    )
