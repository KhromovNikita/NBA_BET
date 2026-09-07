"""Explicit typed projections for deterministic Phase 3 content identities."""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from nba_live_totals.domain.canonical import (
    CanonicalObject,
    CanonicalValue,
    canonical_json_sha256,
)
from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.play_by_play import PlayByPlayEvent
from nba_live_totals.domain.possessions import (
    Administrative,
    ControlAward,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    GameContext,
    Goaltending,
    JumpBall,
    NormalizedGameActions,
    PeriodEnd,
    PeriodStart,
    Possession,
    PossessionSignal,
    PossessionVersions,
    Rebound,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: str, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _versions_projection(versions: PossessionVersions) -> CanonicalObject:
    if not isinstance(versions, PossessionVersions):
        raise TypeError("versions must be PossessionVersions")
    return {
        "normalizer_version": versions.normalizer_version,
        "parser_version": versions.parser_version,
        "rules_version": versions.rules_version,
        "output_schema_version": versions.output_schema_version,
    }


def _event_projection(event: PlayByPlayEvent) -> CanonicalObject:
    return {
        "game_id": event.game_id.value,
        "sequence_number": event.sequence_number,
        "action_number": event.action_number,
        "action_id": event.action_id,
        "period": event.period,
        "clock": event.clock,
        "seconds_remaining_period_ms": event.seconds_remaining_period_ms,
        "team_id": event.team_id,
        "person_id": event.person_id,
        "action_type": event.action_type,
        "sub_type": event.sub_type,
        "description": event.description,
        "home_score": event.home_score,
        "away_score": event.away_score,
        "shot_result": event.shot_result,
        "shot_distance": event.shot_distance,
        "legacy_x": event.legacy_x,
        "legacy_y": event.legacy_y,
        "location": event.location,
        "is_field_goal": event.is_field_goal,
        "points_total": event.points_total,
        "video_available": event.video_available,
    }


def canonical_pbp_events_projection(events: Iterable[PlayByPlayEvent]) -> CanonicalObject:
    """Project canonical Phase 2 PBP rows in sequence order."""
    snapshot = tuple(events)
    if any(not isinstance(event, PlayByPlayEvent) for event in snapshot):
        raise TypeError("canonical events must be PlayByPlayEvent values")
    if not snapshot:
        raise ValueError("canonical events must not be empty")
    ordered = tuple(sorted(snapshot, key=lambda event: event.sequence_number))
    sequence_numbers = tuple(event.sequence_number for event in ordered)
    if sequence_numbers != tuple(range(1, len(ordered) + 1)):
        raise ValueError("canonical events must have contiguous one-based sequence numbers")
    game_id = ordered[0].game_id
    if any(event.game_id != game_id for event in ordered[1:]):
        raise ValueError("canonical events must belong to one game")
    return {
        "schema": "canonical-pbp-events-v1",
        "events": [_event_projection(event) for event in ordered],
    }


def canonical_event_checksum(events: Iterable[PlayByPlayEvent]) -> str:
    """Hash the exact canonical Phase 2 event projection."""
    return canonical_json_sha256(canonical_pbp_events_projection(events))


def game_context_projection(game: GameContext) -> CanonicalObject:
    """Project only final-game data that affects possession output."""
    if not isinstance(game, GameContext):
        raise TypeError("game must be GameContext")
    return {
        "schema": "possession-game-context-v1",
        "game_id": game.game_id.value,
        "season": game.season.value,
        "season_type": game.season_type.value,
        "game_date": game.game_date.isoformat(),
        "status": game.status.value,
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "home_score": game.home_score,
        "away_score": game.away_score,
    }


def game_context_checksum(game: GameContext) -> str:
    """Hash the exact final-game context projection."""
    return canonical_json_sha256(game_context_projection(game))


def _signal_projection(signal: PossessionSignal) -> CanonicalObject:
    if type(signal) is PeriodStart:
        return {"signal_type": "period_start"}
    if type(signal) is PeriodEnd:
        return {"signal_type": "period_end"}
    if type(signal) is FieldGoalAttempt:
        return {
            "signal_type": "field_goal_attempt",
            "team": signal.team,
            "value": signal.value,
            "made": signal.made,
            "and_one_candidate": signal.and_one_candidate,
        }
    if type(signal) is Rebound:
        return {
            "signal_type": "rebound",
            "rebounding_team": signal.rebounding_team,
            "actor": signal.actor.value,
            "provider_side": signal.provider_side.value,
            "disposition": signal.disposition.value,
        }
    if type(signal) is Turnover:
        return {
            "signal_type": "turnover",
            "committing_team": signal.committing_team,
            "gained_by": signal.gained_by,
            "turnover_kind": signal.turnover_kind,
        }
    if type(signal) is StealAnnotation:
        return {
            "signal_type": "steal_annotation",
            "team": signal.team,
            "related_sequence_number": signal.related_sequence_number,
        }
    if type(signal) is Foul:
        return {
            "signal_type": "foul",
            "committing_team": signal.committing_team,
            "foul_kind": signal.foul_kind,
            "shooting_team": signal.shooting_team,
            "related_sequence_number": signal.related_sequence_number,
        }
    if type(signal) is FreeThrowTripStart:
        return {
            "signal_type": "free_throw_trip_start",
            "trip_id": signal.trip_id,
            "shooting_team": signal.shooting_team,
            "free_throw_kind": signal.free_throw_kind.value,
            "attempts": signal.attempts,
            "resume_disposition": signal.resume_disposition.value,
            "resume_team": signal.resume_team,
            "interrupted_trip_id": signal.interrupted_trip_id,
        }
    if type(signal) is FreeThrowAttempt:
        return {
            "signal_type": "free_throw_attempt",
            "trip_id": signal.trip_id,
            "attempt": signal.attempt,
            "total": signal.total,
            "made": signal.made,
            "awarded_points": signal.awarded_points,
        }
    if type(signal) is FreeThrowTripEnd:
        return {
            "signal_type": "free_throw_trip_end",
            "trip_id": signal.trip_id,
            "resolution": signal.resolution.value,
            "resume_team": signal.resume_team,
        }
    if type(signal) is JumpBall:
        return {
            "signal_type": "jump_ball",
            "winner_team": signal.winner_team,
        }
    if type(signal) is Violation:
        return {
            "signal_type": "violation",
            "team": signal.team,
            "disposition": signal.disposition.value,
        }
    if type(signal) is Goaltending:
        return {
            "signal_type": "goaltending",
            "team": signal.team,
            "side": signal.side.value,
            "awarded_points": signal.awarded_points,
        }
    if type(signal) is ControlAward:
        return {
            "signal_type": "control_award",
            "team": signal.team,
            "reason": signal.reason,
        }
    if type(signal) is Administrative:
        return {
            "signal_type": "administrative",
            "administrative_kind": signal.administrative_kind,
        }
    if type(signal) is Unsupported:
        return {
            "signal_type": "unsupported",
            "control_relevant": signal.control_relevant,
        }
    raise TypeError("unsupported possession signal")


def normalized_actions_projection(normalized: NormalizedGameActions) -> CanonicalObject:
    """Project the provider-neutral V1 normalizer output for golden checks."""
    if not isinstance(normalized, NormalizedGameActions):
        raise TypeError("normalized must be NormalizedGameActions")
    actions: list[CanonicalValue] = []
    for action in normalized.actions:
        actions.append(
            {
                "game_id": action.game_id.value,
                "sequence_number": action.sequence_number,
                "provider_action_number": action.provider_action_number,
                "provider_action_id": action.provider_action_id,
                "period": action.period,
                "clock_ms": action.clock_ms,
                "actor_team_id": action.actor_team_id,
                "actor_person_id": action.actor_person_id,
                "checkpoint_home_score": action.checkpoint_home_score,
                "checkpoint_away_score": action.checkpoint_away_score,
                "checkpoint_combined_score": action.checkpoint_combined_score,
                "consumption": action.consumption.value,
                "signals": [_signal_projection(signal) for signal in action.signals],
            }
        )
    return {
        "schema": "normalized-actions-v1",
        "game_id": normalized.game_id.value,
        "actions": actions,
    }


def normalized_actions_checksum(normalized: NormalizedGameActions) -> str:
    """Hash a normalized action stream for golden tests."""
    return canonical_json_sha256(normalized_actions_projection(normalized))


def parse_revision_projection(
    *,
    game_id: GameId,
    pbp_document_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: PossessionVersions,
) -> CanonicalObject:
    """Project the content-only parser revision identity."""
    if not isinstance(game_id, GameId):
        raise TypeError("game_id must be GameId")
    _require_sha256(pbp_document_sha256, field_name="PBP document SHA-256")
    _require_sha256(canonical_event_sha256, field_name="canonical event SHA-256")
    _require_sha256(game_context_sha256, field_name="game context SHA-256")
    projection: CanonicalObject = {
        "schema": "possession-parse-revision-v1",
        "game_id": game_id.value,
        "pbp_document_sha256": pbp_document_sha256,
        "canonical_event_sha256": canonical_event_sha256,
        "game_context_sha256": game_context_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def parse_revision_id(
    *,
    game_id: GameId,
    pbp_document_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: PossessionVersions,
) -> str:
    """Hash a content-only parser revision projection."""
    return canonical_json_sha256(
        parse_revision_projection(
            game_id=game_id,
            pbp_document_sha256=pbp_document_sha256,
            canonical_event_sha256=canonical_event_sha256,
            game_context_sha256=game_context_sha256,
            versions=versions,
        )
    )


def possession_input_token_projection(
    *,
    game_id: GameId,
    applied_pbp_document_id: UUID,
    applied_pbp_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: PossessionVersions,
) -> CanonicalObject:
    """Project freshness plus operational PBP lineage for a build claim."""
    if not isinstance(game_id, GameId):
        raise TypeError("game_id must be GameId")
    if type(applied_pbp_document_id) is not UUID:
        raise TypeError("applied_pbp_document_id must be UUID")
    _require_sha256(applied_pbp_sha256, field_name="applied PBP SHA-256")
    _require_sha256(canonical_event_sha256, field_name="canonical event SHA-256")
    _require_sha256(game_context_sha256, field_name="game context SHA-256")
    projection: CanonicalObject = {
        "schema": "possession-input-token-v1",
        "game_id": game_id.value,
        "applied_pbp_document_id": str(applied_pbp_document_id),
        "applied_pbp_sha256": applied_pbp_sha256,
        "canonical_event_sha256": canonical_event_sha256,
        "game_context_sha256": game_context_sha256,
    }
    projection.update(_versions_projection(versions))
    return projection


def possession_input_token_checksum(
    *,
    game_id: GameId,
    applied_pbp_document_id: UUID,
    applied_pbp_sha256: str,
    canonical_event_sha256: str,
    game_context_sha256: str,
    versions: PossessionVersions,
) -> str:
    """Hash the exact input freshness and lineage projection."""
    return canonical_json_sha256(
        possession_input_token_projection(
            game_id=game_id,
            applied_pbp_document_id=applied_pbp_document_id,
            applied_pbp_sha256=applied_pbp_sha256,
            canonical_event_sha256=canonical_event_sha256,
            game_context_sha256=game_context_sha256,
            versions=versions,
        )
    )


def _possession_row_projection(game: GameContext, possession: Possession) -> CanonicalObject:
    if possession.game_id != game.game_id:
        raise ValueError("possession must belong to the projected game")
    return {
        "game_id": possession.game_id.value,
        "season": game.season.value,
        "season_start_year": game.season.start_year,
        "season_type": game.season_type.value,
        "game_date": game.game_date.isoformat(),
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "possession_number": possession.possession_number,
        "offense_possession_number": possession.offense_possession_number,
        "period": possession.period,
        "offense_team_id": possession.offense_team_id,
        "defense_team_id": possession.defense_team_id,
        "start_sequence_number": possession.start_boundary.sequence_number,
        "start_boundary_side": possession.start_boundary.side.value,
        "end_sequence_number": possession.end_boundary.sequence_number,
        "end_boundary_side": possession.end_boundary.side.value,
        "start_seconds_remaining_period_ms": possession.start_seconds_remaining_period_ms,
        "end_seconds_remaining_period_ms": possession.end_seconds_remaining_period_ms,
        "seconds_remaining_game_ms": possession.seconds_remaining_game_ms,
        "home_score_before": possession.home_score_before,
        "away_score_before": possession.away_score_before,
        "home_score": possession.home_score,
        "away_score": possession.away_score,
        "score_diff": possession.score_diff,
        "offense_points": possession.offense_points,
        "defense_points": possession.defense_points,
        "home_possessions": possession.home_possessions,
        "away_possessions": possession.away_possessions,
        "end_reason": possession.end_reason.value,
        "source_event_count": possession.source_event_count,
    }


def possession_output_projection(
    *,
    parse_revision_id: str,
    game: GameContext,
    possessions: Iterable[Possession],
) -> CanonicalObject:
    """Project the immutable analytical rows that define a published output."""
    _require_sha256(parse_revision_id, field_name="parse revision SHA-256")
    if not isinstance(game, GameContext):
        raise TypeError("game must be GameContext")
    snapshot = tuple(possessions)
    if any(not isinstance(item, Possession) for item in snapshot):
        raise TypeError("output possessions must be Possession values")
    if not snapshot:
        raise ValueError("output possessions must not be empty")
    ordered = tuple(sorted(snapshot, key=lambda item: item.possession_number))
    numbers = tuple(item.possession_number for item in ordered)
    if numbers != tuple(range(1, len(ordered) + 1)):
        raise ValueError("output possessions must be contiguous and one-based")
    return {
        "schema": "possession-output-v1",
        "parse_revision_id": parse_revision_id,
        "rows": [_possession_row_projection(game, possession) for possession in ordered],
    }


def possession_output_checksum(
    *,
    parse_revision_id: str,
    game: GameContext,
    possessions: Iterable[Possession],
) -> str:
    """Hash the exact ordered analytical possession-row projection."""
    return canonical_json_sha256(
        possession_output_projection(
            parse_revision_id=parse_revision_id,
            game=game,
            possessions=possessions,
        )
    )


__all__ = [
    "canonical_event_checksum",
    "canonical_pbp_events_projection",
    "game_context_checksum",
    "game_context_projection",
    "normalized_actions_checksum",
    "normalized_actions_projection",
    "parse_revision_id",
    "parse_revision_projection",
    "possession_input_token_checksum",
    "possession_input_token_projection",
    "possession_output_checksum",
    "possession_output_projection",
]
