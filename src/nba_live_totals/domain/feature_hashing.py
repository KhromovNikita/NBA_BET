"""Canonical projections for deterministic Phase 5 feature identities."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from nba_live_totals.domain.canonical import (
    CanonicalObject,
    CanonicalValue,
    canonical_json_sha256,
)
from nba_live_totals.domain.features import (
    FeatureDataset,
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureVersions,
    GameStateFeature,
    HistoricalPlayerBox,
    HistoricalTeamBox,
    PlayerPregameFeatures,
)
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import EventBoundary

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TEAM_BOX_FIELDS = (
    "seconds_played",
    "field_goals_made",
    "field_goals_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "rebounds_offensive",
    "rebounds_defensive",
    "rebounds_total",
    "assists",
    "turnovers",
    "points",
)


def _require_sha256(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_game_id(value: object) -> GameId:
    if not isinstance(value, GameId):
        raise TypeError("game_id must be GameId")
    return value


def _require_uuid(value: object, *, field_name: str) -> UUID:
    if type(value) is not UUID:
        raise TypeError(f"{field_name} must be UUID")
    return value


def _utc_timestamp(value: object, *, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()


def _versions_projection(versions: FeatureVersions) -> CanonicalObject:
    if not isinstance(versions, FeatureVersions):
        raise TypeError("versions must be FeatureVersions")
    return {
        "join_version": versions.join_version,
        "formula_version": versions.formula_version,
        "event_normalizer_version": versions.event_normalizer_version,
        "output_schema_version": versions.output_schema_version,
        "player_output_schema_version": versions.player_output_schema_version,
    }


def _team_box_row(row: HistoricalTeamBox) -> CanonicalObject:
    projection: CanonicalObject = {
        "game_id": row.game_id.value,
        "team_id": row.team_id,
    }
    for name in _TEAM_BOX_FIELDS:
        projection[name] = cast(int | None, getattr(row, name))
    return projection


def canonical_team_boxes_projection(rows: Iterable[HistoricalTeamBox]) -> CanonicalObject:
    """Project exactly the formula-relevant canonical historical team-box subset."""
    snapshot = tuple(rows)
    if any(not isinstance(row, HistoricalTeamBox) for row in snapshot):
        raise TypeError("canonical team boxes must be HistoricalTeamBox values")
    if not snapshot:
        raise ValueError("canonical team boxes must not be empty")
    ordered = tuple(sorted(snapshot, key=lambda row: row.team_id))
    if any(row.game_id != ordered[0].game_id for row in ordered[1:]):
        raise ValueError("canonical team boxes must belong to one game")
    if len({(row.game_id, row.team_id) for row in ordered}) != len(ordered):
        raise ValueError("duplicate canonical team-box logical key")
    return {
        "schema": "canonical-feature-team-box-v1",
        "teams": [_team_box_row(row) for row in ordered],
    }


def canonical_team_boxes_checksum(rows: Iterable[HistoricalTeamBox]) -> str:
    return canonical_json_sha256(canonical_team_boxes_projection(rows))


def _player_box_row(row: HistoricalPlayerBox) -> CanonicalObject:
    projection: CanonicalObject = {
        "game_id": row.game_id.value,
        "team_id": row.team_id,
        "player_id": row.player_id,
        "played": row.played,
    }
    for name in _TEAM_BOX_FIELDS:
        projection[name] = cast(int | None, getattr(row, name))
    return projection


def canonical_player_boxes_projection(rows: Iterable[HistoricalPlayerBox]) -> CanonicalObject:
    """Project exactly the formula-relevant canonical historical player-box subset."""
    snapshot = tuple(rows)
    if any(not isinstance(row, HistoricalPlayerBox) for row in snapshot):
        raise TypeError("canonical player boxes must be HistoricalPlayerBox values")
    if not snapshot:
        raise ValueError("canonical player boxes must not be empty")
    ordered = tuple(sorted(snapshot, key=lambda row: (row.team_id, row.player_id)))
    if any(row.game_id != ordered[0].game_id for row in ordered[1:]):
        raise ValueError("canonical player boxes must belong to one game")
    if len({(row.game_id, row.team_id, row.player_id) for row in ordered}) != len(ordered):
        raise ValueError("duplicate canonical player-box logical key")
    return {
        "schema": "canonical-feature-player-box-v1",
        "players": [_player_box_row(row) for row in ordered],
    }


def canonical_player_boxes_checksum(rows: Iterable[HistoricalPlayerBox]) -> str:
    return canonical_json_sha256(canonical_player_boxes_projection(rows))


def _dependency_row(row: FeatureDependency, *, operational: bool) -> CanonicalObject:
    projection: CanonicalObject = {
        "dependency_game_id": row.dependency_game_id.value,
        "started_at": _utc_timestamp(row.started_at, field_name="dependency start time"),
        "game_context_sha256": row.game_context_sha256,
        "applied_box_score_sha256": row.applied_box_score_sha256,
        "canonical_team_box_sha256": row.canonical_team_box_sha256,
        "canonical_player_box_sha256": row.canonical_player_box_sha256,
        "parse_revision_id": row.parse_revision_id,
        "possession_output_sha256": row.possession_output_sha256,
        "ordinal": row.ordinal,
        "inclusion_reasons": list(sorted(row.inclusion_reasons)),
    }
    if operational:
        projection.update(
            {
                "applied_box_score_document_id": str(row.applied_box_score_document_id),
                "possession_snapshot_id": str(row.possession_snapshot_id),
                "possession_input_token_sha256": row.possession_input_token_sha256,
            }
        )
    return projection


def dependency_input_projection(manifest: FeatureDependencyManifest) -> CanonicalObject:
    """Project the exact operational dependency manifest."""
    if not isinstance(manifest, FeatureDependencyManifest):
        raise TypeError("manifest must be FeatureDependencyManifest")
    return {
        "schema": "dependency-input-v1",
        "dependencies": [_dependency_row(row, operational=True) for row in manifest.dependencies],
    }


def dependency_input_checksum(manifest: FeatureDependencyManifest) -> str:
    return canonical_json_sha256(dependency_input_projection(manifest))


def dependency_revision_projection(manifest: FeatureDependencyManifest) -> CanonicalObject:
    """Project content-only dependencies without operational materialization identities."""
    if not isinstance(manifest, FeatureDependencyManifest):
        raise TypeError("manifest must be FeatureDependencyManifest")
    return {
        "schema": "dependency-revision-v1",
        "dependencies": [_dependency_row(row, operational=False) for row in manifest.dependencies],
    }


def dependency_revision_checksum(manifest: FeatureDependencyManifest) -> str:
    return canonical_json_sha256(dependency_revision_projection(manifest))


def feature_input_token_projection(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    possession_snapshot_id: UUID,
    parse_revision_id: str,
    possession_input_token_sha256: str,
    possession_output_sha256: str,
    lineup_snapshot_id: UUID,
    lineup_revision_id: str,
    lineup_input_token_sha256: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_input_sha256: str,
    versions: FeatureVersions,
) -> CanonicalObject:
    """Project target and history operational freshness for a feature claim."""
    _require_game_id(game_id)
    _require_uuid(possession_snapshot_id, field_name="possession_snapshot_id")
    _require_uuid(lineup_snapshot_id, field_name="lineup_snapshot_id")
    if not isinstance(lineup_result_status, LineupStatus):
        raise TypeError("lineup_result_status must be LineupStatus")
    for field_name, value in (
        ("game_context_sha256", game_context_sha256),
        ("parse_revision_id", parse_revision_id),
        ("possession_input_token_sha256", possession_input_token_sha256),
        ("possession_output_sha256", possession_output_sha256),
        ("lineup_revision_id", lineup_revision_id),
        ("lineup_input_token_sha256", lineup_input_token_sha256),
        ("lineup_output_sha256", lineup_output_sha256),
        ("dependency_input_sha256", dependency_input_sha256),
    ):
        _require_sha256(value, field_name=field_name)
    projection: CanonicalObject = {
        "schema": "feature-input-token-v1",
        "game_id": game_id.value,
        "target_started_at": _utc_timestamp(target_started_at, field_name="target_started_at"),
        "game_context_sha256": game_context_sha256,
        "possession_snapshot_id": str(possession_snapshot_id),
        "parse_revision_id": parse_revision_id,
        "possession_input_token_sha256": possession_input_token_sha256,
        "possession_output_sha256": possession_output_sha256,
        "lineup_snapshot_id": str(lineup_snapshot_id),
        "lineup_revision_id": lineup_revision_id,
        "lineup_input_token_sha256": lineup_input_token_sha256,
        "lineup_output_sha256": lineup_output_sha256,
        "lineup_result_status": lineup_result_status.value,
        "dependency_input_sha256": dependency_input_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def feature_input_token_checksum(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    possession_snapshot_id: UUID,
    parse_revision_id: str,
    possession_input_token_sha256: str,
    possession_output_sha256: str,
    lineup_snapshot_id: UUID,
    lineup_revision_id: str,
    lineup_input_token_sha256: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_input_sha256: str,
    versions: FeatureVersions,
) -> str:
    return canonical_json_sha256(
        feature_input_token_projection(
            game_id=game_id,
            target_started_at=target_started_at,
            game_context_sha256=game_context_sha256,
            possession_snapshot_id=possession_snapshot_id,
            parse_revision_id=parse_revision_id,
            possession_input_token_sha256=possession_input_token_sha256,
            possession_output_sha256=possession_output_sha256,
            lineup_snapshot_id=lineup_snapshot_id,
            lineup_revision_id=lineup_revision_id,
            lineup_input_token_sha256=lineup_input_token_sha256,
            lineup_output_sha256=lineup_output_sha256,
            lineup_result_status=lineup_result_status,
            dependency_input_sha256=dependency_input_sha256,
            versions=versions,
        )
    )


def feature_revision_projection(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    parse_revision_id: str,
    possession_output_sha256: str,
    lineup_revision_id: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_revision_sha256: str,
    versions: FeatureVersions,
) -> CanonicalObject:
    """Project the semantic feature revision, independent of materialization UUIDs."""
    _require_game_id(game_id)
    if not isinstance(lineup_result_status, LineupStatus):
        raise TypeError("lineup_result_status must be LineupStatus")
    for field_name, value in (
        ("game_context_sha256", game_context_sha256),
        ("parse_revision_id", parse_revision_id),
        ("possession_output_sha256", possession_output_sha256),
        ("lineup_revision_id", lineup_revision_id),
        ("lineup_output_sha256", lineup_output_sha256),
        ("dependency_revision_sha256", dependency_revision_sha256),
    ):
        _require_sha256(value, field_name=field_name)
    projection: CanonicalObject = {
        "schema": "feature-revision-v1",
        "game_id": game_id.value,
        "target_started_at": _utc_timestamp(target_started_at, field_name="target_started_at"),
        "game_context_sha256": game_context_sha256,
        "parse_revision_id": parse_revision_id,
        "possession_output_sha256": possession_output_sha256,
        "lineup_revision_id": lineup_revision_id,
        "lineup_output_sha256": lineup_output_sha256,
        "lineup_result_status": lineup_result_status.value,
        "dependency_revision_sha256": dependency_revision_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def feature_revision_id(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    parse_revision_id: str,
    possession_output_sha256: str,
    lineup_revision_id: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_revision_sha256: str,
    versions: FeatureVersions,
) -> str:
    return canonical_json_sha256(
        feature_revision_projection(
            game_id=game_id,
            target_started_at=target_started_at,
            game_context_sha256=game_context_sha256,
            parse_revision_id=parse_revision_id,
            possession_output_sha256=possession_output_sha256,
            lineup_revision_id=lineup_revision_id,
            lineup_output_sha256=lineup_output_sha256,
            lineup_result_status=lineup_result_status,
            dependency_revision_sha256=dependency_revision_sha256,
            versions=versions,
        )
    )


def _semantic_value(value: object) -> CanonicalValue:
    if isinstance(value, GameId):
        return value.value
    if isinstance(value, EventBoundary):
        return {"sequence_number": value.sequence_number, "side": value.side.value}
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _semantic_value(getattr(value, item.name)) for item in fields(value)}
    if type(value) is tuple:
        return tuple(_semantic_value(item) for item in cast(tuple[object, ...], value))
    return cast(CanonicalValue, value)


def _state_row(row: GameStateFeature) -> CanonicalObject:
    return {
        item.name: _semantic_value(getattr(row, item.name)) for item in fields(GameStateFeature)
    }


def state_output_projection(states: Iterable[GameStateFeature]) -> CanonicalObject:
    """Project all semantic state fields in canonical state-number order."""
    snapshot = tuple(states)
    if any(not isinstance(row, GameStateFeature) for row in snapshot):
        raise TypeError("state rows must be GameStateFeature values")
    if not snapshot:
        raise ValueError("state rows must not be empty")
    ordered = tuple(sorted(snapshot, key=lambda row: row.state_number))
    if any(row.game_id != ordered[0].game_id for row in ordered[1:]):
        raise ValueError("state rows must belong to one game")
    if len({row.state_number for row in ordered}) != len(ordered):
        raise ValueError("duplicate state numbers")
    if tuple(row.state_number for row in ordered) != tuple(range(len(ordered))):
        raise ValueError("state numbers must be contiguous from zero")
    return {
        "schema": "feature-state-output-v1",
        "rows": [_state_row(row) for row in ordered],
    }


def state_output_checksum(states: Iterable[GameStateFeature]) -> str:
    return canonical_json_sha256(state_output_projection(states))


def _player_row(row: PlayerPregameFeatures) -> CanonicalObject:
    return {
        item.name: _semantic_value(getattr(row, item.name))
        for item in fields(PlayerPregameFeatures)
    }


def player_output_projection(players: Iterable[PlayerPregameFeatures]) -> CanonicalObject:
    """Project all semantic player fields in canonical numeric player order."""
    snapshot = tuple(players)
    if any(not isinstance(row, PlayerPregameFeatures) for row in snapshot):
        raise TypeError("player rows must be PlayerPregameFeatures values")
    ordered = tuple(sorted(snapshot, key=lambda row: row.player_id))
    if ordered and any(row.game_id != ordered[0].game_id for row in ordered[1:]):
        raise ValueError("player rows must belong to one game")
    if len({row.player_id for row in ordered}) != len(ordered):
        raise ValueError("duplicate player IDs")
    return {
        "schema": "feature-player-output-v1",
        "rows": [_player_row(row) for row in ordered],
    }


def player_output_checksum(players: Iterable[PlayerPregameFeatures]) -> str:
    return canonical_json_sha256(player_output_projection(players))


def feature_output_projection(dataset: FeatureDataset) -> CanonicalObject:
    """Project the bounded semantic identities and status of both feature tables."""
    if not isinstance(dataset, FeatureDataset):
        raise TypeError("dataset must be FeatureDataset")
    return {
        "schema": "feature-output-v1",
        "state_output_sha256": state_output_checksum(dataset.states),
        "state_row_count": len(dataset.states),
        "player_output_sha256": player_output_checksum(dataset.players),
        "player_row_count": len(dataset.players),
        "result_status": dataset.status.value,
        "issue_codes": list(dataset.issues),
    }


def feature_output_checksum(dataset: FeatureDataset) -> str:
    return canonical_json_sha256(feature_output_projection(dataset))


__all__ = [
    "canonical_player_boxes_checksum",
    "canonical_player_boxes_projection",
    "canonical_team_boxes_checksum",
    "canonical_team_boxes_projection",
    "dependency_input_checksum",
    "dependency_input_projection",
    "dependency_revision_checksum",
    "dependency_revision_projection",
    "feature_input_token_checksum",
    "feature_input_token_projection",
    "feature_output_checksum",
    "feature_output_projection",
    "feature_revision_id",
    "feature_revision_projection",
    "player_output_checksum",
    "player_output_projection",
    "state_output_checksum",
    "state_output_projection",
]
