"""Explicit canonical baseline projections; operational identities stay separate."""

import re
from uuid import UUID

from nba_live_totals.domain.baselines import (
    BASELINE_FORMULA_VERSION,
    BASELINE_OUTPUT_SCHEMA_VERSION,
    BASELINE_SCALE,
    BaselineConfig,
    StateBaseline,
)
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.identifiers import GameId


def baseline_config_projection(config: BaselineConfig) -> CanonicalObject:
    if not isinstance(config, BaselineConfig):
        raise ValueError("invalid baseline config")
    return {
        "schema": "baseline-config-v1",
        "formula_version": BASELINE_FORMULA_VERSION,
        "output_schema_version": BASELINE_OUTPUT_SCHEMA_VERSION,
        "scale": BASELINE_SCALE,
        "history_window": config.history_window,
        "ppp_prior_possessions": config.ppp_prior_possessions,
        "pace_prior_combined_possessions": config.pace_prior_combined_possessions,
    }


def baseline_config_sha256(config: BaselineConfig) -> str:
    return canonical_json_sha256(baseline_config_projection(config))


def baseline_revision_sha256(
    *,
    game_id: GameId,
    feature_revision_id: str,
    feature_output_sha256: str,
    row: StateBaseline,
    config: BaselineConfig,
) -> str:
    if game_id != row.state.game_id or row.history_window != config.history_window:
        raise ValueError("baseline identity mismatch")
    for digest in (feature_revision_id, feature_output_sha256):
        if type(digest) is not str or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid baseline source digest")
    return canonical_json_sha256(
        {
            "schema": "baseline-revision-v1",
            "game_id": game_id.value,
            "feature_revision_id": feature_revision_id,
            "feature_output_sha256": feature_output_sha256,
            "state_number": row.state.state_number,
            "boundary_sequence_number": row.state.boundary.sequence_number,
            "boundary_side": row.state.boundary.side.value,
            "method": row.method.value,
            "config_sha256": baseline_config_sha256(config),
            "formula_version": BASELINE_FORMULA_VERSION,
            "output_schema_version": BASELINE_OUTPUT_SCHEMA_VERSION,
        }
    )


def baseline_outcome_projection(
    *,
    feature_snapshot_id: UUID,
    feature_revision_id: str,
    feature_output_sha256: str,
    row: StateBaseline,
    config: BaselineConfig,
) -> CanonicalObject:
    if type(feature_snapshot_id) is not UUID:
        raise ValueError("invalid baseline snapshot identity")
    state = row.state
    output: CanonicalObject = {
        "record_type": "baseline",
        "game_id": state.game_id.value,
        "state_number": state.state_number,
        "state_kind": state.state_kind.value,
        "boundary_sequence_number": state.boundary.sequence_number,
        "boundary_side": state.boundary.side.value,
        "method": row.method.value,
        "feature_snapshot_id": str(feature_snapshot_id),
        "baseline_revision_sha256": baseline_revision_sha256(
            game_id=state.game_id,
            feature_revision_id=feature_revision_id,
            feature_output_sha256=feature_output_sha256,
            row=row,
            config=config,
        ),
        "period": state.period,
        "seconds_remaining_game_ms": state.seconds_remaining_game_ms,
        "seconds_remaining_period_ms": state.seconds_remaining_period_ms,
        "home_score": state.home_score,
        "away_score": state.away_score,
        "status": row.status.value,
        "home_games_used": row.home_games_used,
        "away_games_used": row.away_games_used,
        "home_possessions": state.home_possessions,
        "away_possessions": state.away_possessions,
        "combined_possessions": state.home_possessions + state.away_possessions,
    }
    for name in ("pace", "home_ppp", "away_ppp"):
        metric = getattr(row, name)
        output[name] = metric.value
        output[f"{name}_unavailable_reason"] = (
            None if metric.reason is None else metric.reason.value
        )
        output[f"{name}_live_weight"] = metric.live_weight
    return output


def baseline_outcome_bytes(
    *,
    feature_snapshot_id: UUID,
    feature_revision_id: str,
    feature_output_sha256: str,
    row: StateBaseline,
    config: BaselineConfig,
) -> bytes:
    return (
        canonical_json_bytes(
            baseline_outcome_projection(
                feature_snapshot_id=feature_snapshot_id,
                feature_revision_id=feature_revision_id,
                feature_output_sha256=feature_output_sha256,
                row=row,
                config=config,
            )
        )
        + b"\n"
    )
