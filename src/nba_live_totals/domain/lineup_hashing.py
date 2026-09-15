"""Explicit typed projections for deterministic Phase 4 lineup identities."""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from nba_live_totals.domain.canonical import CanonicalObject, CanonicalValue, canonical_json_sha256
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.lineups import (
    FullLineupReset,
    InvalidTeamTransition,
    LineupPlayerBox,
    LineupReconstructionResult,
    LineupSegment,
    LineupVersions,
    NoLineupChange,
    NormalizedGameLineups,
    Substitute,
    TeamLineupState,
    TeamRoster,
)
from nba_live_totals.domain.possession_hashing import (
    canonical_event_checksum,
    canonical_pbp_events_projection,
    game_context_checksum,
    game_context_projection,
)
from nba_live_totals.domain.possessions import EventBoundary

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _versions_projection(versions: LineupVersions) -> CanonicalObject:
    if not isinstance(versions, LineupVersions):
        raise TypeError("versions must be LineupVersions")
    return {
        "normalizer_version": versions.normalizer_version,
        "reconstructor_version": versions.reconstructor_version,
        "rules_version": versions.rules_version,
        "output_schema_version": versions.output_schema_version,
    }


def canonical_player_boxes_projection(players: Iterable[LineupPlayerBox]) -> CanonicalObject:
    """Project the exact lineup-relevant subset of canonical player box rows."""
    snapshot = tuple(players)
    if any(not isinstance(player, LineupPlayerBox) for player in snapshot):
        raise TypeError("canonical player boxes must be LineupPlayerBox values")
    if not snapshot:
        raise ValueError("canonical player boxes must not be empty")
    ordered = sorted(snapshot, key=lambda player: (player.team_id, player.player_id))
    if any(player.game_id != ordered[0].game_id for player in ordered[1:]):
        raise ValueError("canonical player boxes must belong to one game")
    return {
        "schema": "canonical-lineup-player-box-v1",
        "players": [
            {
                "game_id": player.game_id.value,
                "team_id": player.team_id,
                "player_id": player.player_id,
                "position": player.position,
                "starter": player.starter,
                "played": player.played,
                "comment": player.comment,
            }
            for player in ordered
        ],
    }


def canonical_player_boxes_checksum(players: Iterable[LineupPlayerBox]) -> str:
    return canonical_json_sha256(canonical_player_boxes_projection(players))


def lineup_revision_projection(
    *,
    game_id: GameId,
    box_score_document_sha256: str,
    pbp_document_sha256: str,
    canonical_player_box_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: LineupVersions,
) -> CanonicalObject:
    """Project the content-only lineup revision identity."""
    if not isinstance(game_id, GameId):
        raise TypeError("game_id must be GameId")
    for field_name, digest in (
        ("box-score document SHA-256", box_score_document_sha256),
        ("PBP document SHA-256", pbp_document_sha256),
        ("canonical player-box SHA-256", canonical_player_box_sha256),
        ("canonical event SHA-256", canonical_event_sha256),
        ("game context SHA-256", game_context_sha256),
    ):
        _require_sha256(digest, field_name=field_name)
    projection: CanonicalObject = {
        "schema": "lineup-revision-v1",
        "game_id": game_id.value,
        "box_score_document_sha256": box_score_document_sha256,
        "pbp_document_sha256": pbp_document_sha256,
        "canonical_player_box_sha256": canonical_player_box_sha256,
        "canonical_event_sha256": canonical_event_sha256,
        "game_context_sha256": game_context_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def lineup_revision_id(
    *,
    game_id: GameId,
    box_score_document_sha256: str,
    pbp_document_sha256: str,
    canonical_player_box_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: LineupVersions,
) -> str:
    return canonical_json_sha256(
        lineup_revision_projection(
            game_id=game_id,
            box_score_document_sha256=box_score_document_sha256,
            pbp_document_sha256=pbp_document_sha256,
            canonical_player_box_sha256=canonical_player_box_sha256,
            canonical_event_sha256=canonical_event_sha256,
            game_context_sha256=game_context_sha256,
            versions=versions,
        )
    )


def lineup_input_token_projection(
    *,
    game_id: GameId,
    applied_box_score_document_id: UUID,
    applied_box_score_sha256: str,
    applied_pbp_document_id: UUID,
    applied_pbp_sha256: str,
    canonical_player_box_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: LineupVersions,
) -> CanonicalObject:
    """Project freshness plus exact operational source-document lineage."""
    if not isinstance(game_id, GameId):
        raise TypeError("game_id must be GameId")
    if type(applied_box_score_document_id) is not UUID:
        raise TypeError("applied_box_score_document_id must be UUID")
    if type(applied_pbp_document_id) is not UUID:
        raise TypeError("applied_pbp_document_id must be UUID")
    for field_name, digest in (
        ("applied box-score SHA-256", applied_box_score_sha256),
        ("applied PBP SHA-256", applied_pbp_sha256),
        ("canonical player-box SHA-256", canonical_player_box_sha256),
        ("canonical event SHA-256", canonical_event_sha256),
        ("game context SHA-256", game_context_sha256),
    ):
        _require_sha256(digest, field_name=field_name)
    projection: CanonicalObject = {
        "schema": "lineup-input-token-v1",
        "game_id": game_id.value,
        "applied_box_score_document_id": str(applied_box_score_document_id),
        "applied_box_score_sha256": applied_box_score_sha256,
        "applied_pbp_document_id": str(applied_pbp_document_id),
        "applied_pbp_sha256": applied_pbp_sha256,
        "canonical_player_box_sha256": canonical_player_box_sha256,
        "canonical_event_sha256": canonical_event_sha256,
        "game_context_sha256": game_context_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def lineup_input_token_checksum(
    *,
    game_id: GameId,
    applied_box_score_document_id: UUID,
    applied_box_score_sha256: str,
    applied_pbp_document_id: UUID,
    applied_pbp_sha256: str,
    canonical_player_box_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: LineupVersions,
) -> str:
    return canonical_json_sha256(
        lineup_input_token_projection(
            game_id=game_id,
            applied_box_score_document_id=applied_box_score_document_id,
            applied_box_score_sha256=applied_box_score_sha256,
            applied_pbp_document_id=applied_pbp_document_id,
            applied_pbp_sha256=applied_pbp_sha256,
            canonical_player_box_sha256=canonical_player_box_sha256,
            canonical_event_sha256=canonical_event_sha256,
            game_context_sha256=game_context_sha256,
            versions=versions,
        )
    )


def _roster_projection(roster: TeamRoster) -> CanonicalObject:
    return {
        "team_id": roster.team_id,
        "roster_player_ids": tuple(sorted(roster.roster_player_ids)),
        "eligible_player_ids": tuple(sorted(roster.eligible_player_ids)),
        "starter_player_ids": tuple(sorted(roster.starter_player_ids)),
    }


def _transition_projection(transition: object) -> CanonicalObject:
    if type(transition) is Substitute:
        assert isinstance(transition, Substitute)
        return {
            "transition_type": "substitute",
            "team_id": transition.team_id,
            "outgoing_player_id": transition.outgoing_player_id,
            "incoming_player_id": transition.incoming_player_id,
        }
    if type(transition) is InvalidTeamTransition:
        assert isinstance(transition, InvalidTeamTransition)
        return {
            "transition_type": "invalid_team_transition",
            "team_id": transition.team_id,
            "code": transition.code,
        }
    if type(transition) is FullLineupReset:
        assert isinstance(transition, FullLineupReset)
        return {
            "transition_type": "full_lineup_reset",
            "team_id": transition.team_id,
            "player_ids": tuple(sorted(transition.player_ids)),
        }
    if type(transition) is NoLineupChange:
        return {"transition_type": "no_lineup_change"}
    raise TypeError("unsupported lineup transition")


def normalized_lineups_projection(normalized: NormalizedGameLineups) -> CanonicalObject:
    """Project the provider-neutral V1 lineup normalizer output."""
    if not isinstance(normalized, NormalizedGameLineups):
        raise TypeError("normalized must be NormalizedGameLineups")
    events: list[CanonicalValue] = [
        {
            "sequence_number": event.sequence_number,
            "period": event.period,
            "seconds_remaining_period_ms": event.seconds_remaining_period_ms,
            "transition": _transition_projection(event.transition),
        }
        for event in normalized.events
    ]
    return {
        "schema": "normalized-lineups-v1",
        "game_id": normalized.game_id.value,
        "home_roster": _roster_projection(normalized.home_roster),
        "away_roster": _roster_projection(normalized.away_roster),
        "events": events,
    }


def normalized_lineups_checksum(normalized: NormalizedGameLineups) -> str:
    return canonical_json_sha256(normalized_lineups_projection(normalized))


def _boundary_projection(boundary: EventBoundary | None) -> CanonicalObject | None:
    if boundary is None:
        return None
    return {"sequence_number": boundary.sequence_number, "side": boundary.side.value}


def _state_projection(state: TeamLineupState) -> CanonicalObject:
    return {
        "status": state.status.value,
        "player_ids": tuple(sorted(state.player_ids)),
        "invalid_reason": state.invalid_reason,
        "invalid_since": _boundary_projection(state.invalid_since),
    }


def _segment_projection(segment: LineupSegment) -> CanonicalObject:
    return {
        "segment_number": segment.segment_number,
        "start_boundary": _boundary_projection(segment.start_boundary),
        "end_boundary": _boundary_projection(segment.end_boundary),
        "start_period": segment.start_period,
        "end_period": segment.end_period,
        "start_seconds_remaining_period_ms": segment.start_seconds_remaining_period_ms,
        "end_seconds_remaining_period_ms": segment.end_seconds_remaining_period_ms,
        "home_state": _state_projection(segment.home_state),
        "away_state": _state_projection(segment.away_state),
    }


def lineup_output_projection(result: LineupReconstructionResult) -> CanonicalObject:
    """Project only revision, status, and ordered semantic lineup segments."""
    if not isinstance(result, LineupReconstructionResult):
        raise TypeError("result must be LineupReconstructionResult")
    ordered = sorted(result.segments, key=lambda segment: segment.segment_number)
    return {
        "schema": "lineup-output-v1",
        "lineup_revision_id": result.context.lineup_revision_id,
        "status": result.status.value,
        "segments": [_segment_projection(segment) for segment in ordered],
    }


def lineup_output_checksum(result: LineupReconstructionResult) -> str:
    return canonical_json_sha256(lineup_output_projection(result))


__all__ = [
    "canonical_event_checksum",
    "canonical_pbp_events_projection",
    "canonical_player_boxes_checksum",
    "canonical_player_boxes_projection",
    "game_context_checksum",
    "game_context_projection",
    "lineup_input_token_checksum",
    "lineup_input_token_projection",
    "lineup_output_checksum",
    "lineup_output_projection",
    "lineup_revision_id",
    "lineup_revision_projection",
    "normalized_lineups_checksum",
    "normalized_lineups_projection",
]
