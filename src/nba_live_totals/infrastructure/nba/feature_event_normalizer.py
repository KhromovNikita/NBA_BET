"""NBA PlayByPlayV3 normalization into provider-neutral feature deltas."""

from __future__ import annotations

from dataclasses import fields
from typing import cast

from nba_live_totals.application.features import FeatureNormalizationError
from nba_live_totals.application.possessions import GamePbpSnapshot
from nba_live_totals.domain import (
    FieldGoalAttempt,
    JsonObject,
    NormalizedAction,
    PlayByPlayEvent,
    ProviderReboundSide,
    Rebound,
    canonical_json_checksum,
)
from nba_live_totals.domain.features import FeatureStatDelta, NormalizedFeatureEvents
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers import parse_play_by_play
from nba_live_totals.infrastructure.nba.possession_normalizer import (
    NBAPlayByPlayV3PossessionNormalizer,
)

NORMALIZER_VERSION = "nba-feature-events-v1"


def _fail(code: str) -> None:
    raise FeatureNormalizationError(code)


def _actions(snapshot: GamePbpSnapshot) -> list[object]:
    document = snapshot.source_document
    if document.source != "nba.com" or document.endpoint != "playbyplayv3":
        _fail("input_document_identity_mismatch")
    if document.resource_key != f"play_by_play:{snapshot.game.game_id.value}":
        _fail("input_resource_not_applied")
    try:
        payload = document.serializable_payload()
        if (
            document.checksum_sha256 != snapshot.applied_pbp_sha256
            or canonical_json_checksum(payload) != document.checksum_sha256
        ):
            _fail("input_document_checksum_mismatch")
        if not isinstance(payload, dict):
            _fail("unsupported_document_schema")
        meta_value = payload.get("meta")
        game_value = payload.get("game")
        if not isinstance(meta_value, dict) or not isinstance(game_value, dict):
            _fail("unsupported_document_schema")
        meta = cast(JsonObject, meta_value)
        game = cast(JsonObject, game_value)
        if type(meta.get("version")) is not int or meta.get("version") != 1:
            _fail("unsupported_document_schema")
        if game.get("gameId") != snapshot.game.game_id.value:
            _fail("input_document_identity_mismatch")
        actions = game.get("actions")
        if not isinstance(actions, list):
            _fail("unsupported_document_schema")
        return cast(list[object], actions)
    except FeatureNormalizationError:
        raise
    except Exception:
        _fail("unsupported_document_schema")
    raise AssertionError("unreachable")


def _cross_check(snapshot: GamePbpSnapshot, actions: list[object]) -> tuple[PlayByPlayEvent, ...]:
    try:
        parsed = parse_play_by_play(snapshot.source_document)
    except ProviderSchemaError:
        _fail("unsupported_document_schema")
    if len(actions) != len(parsed) or len(parsed) != len(snapshot.canonical_events):
        _fail("raw_canonical_cardinality_mismatch")
    event_fields = tuple(item.name for item in fields(PlayByPlayEvent))
    for sequence_number, (raw, canonical) in enumerate(
        zip(parsed, snapshot.canonical_events, strict=True), start=1
    ):
        if (
            raw.sequence_number != sequence_number
            or canonical.sequence_number != sequence_number
            or any(getattr(raw, name) != getattr(canonical, name) for name in event_fields)
        ):
            _fail("raw_canonical_event_mismatch")
        if raw.team_id not in {
            None,
            snapshot.game.home_team_id,
            snapshot.game.away_team_id,
        }:
            _fail("raw_canonical_team_mismatch")
    return parsed


def _delta(
    normalized: NormalizedAction,
    event: PlayByPlayEvent,
) -> FeatureStatDelta:
    field_goal_attempts = 0
    three_point_attempts = 0
    free_throw_attempts = 0
    turnovers = 0
    offensive_rebounds = 0
    defensive_rebounds = 0
    team_id: int | None = None
    for signal in normalized.signals:
        if type(signal) is FieldGoalAttempt:
            team_id = signal.team
            field_goal_attempts += 1
            three_point_attempts += int(signal.value == 3)
        elif type(signal) is Rebound:
            if signal.provider_side is ProviderReboundSide.OFFENSIVE:
                team_id = signal.rebounding_team
                offensive_rebounds += 1
            elif signal.provider_side is ProviderReboundSide.DEFENSIVE:
                team_id = signal.rebounding_team
                defensive_rebounds += 1
    action_type = event.action_type.strip().casefold()
    if action_type == "free throw":
        team_id = event.team_id
        free_throw_attempts = 1
    elif action_type == "turnover":
        team_id = event.team_id
        turnovers = 1
    sequence_number = normalized.sequence_number
    return FeatureStatDelta(
        sequence_number=sequence_number,
        boundary=EventBoundary(sequence_number, BoundarySide.AFTER),
        team_id=team_id,
        field_goal_attempts=field_goal_attempts,
        three_point_attempts=three_point_attempts,
        free_throw_attempts=free_throw_attempts,
        turnovers=turnovers,
        offensive_rebounds=offensive_rebounds,
        defensive_rebounds=defensive_rebounds,
    )


class NBAPlayByPlayV3FeatureEventNormalizer:
    """Validate one immutable PBP snapshot and emit aligned stat deltas."""

    version = NORMALIZER_VERSION

    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedFeatureEvents:
        if not isinstance(snapshot, GamePbpSnapshot):
            raise ValueError("snapshot must be a GamePbpSnapshot")
        actions = _actions(snapshot)
        _cross_check(snapshot, actions)
        try:
            normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(snapshot)
            if len(normalized.actions) != len(snapshot.canonical_events):
                _fail("unsupported_document_schema")
            return NormalizedFeatureEvents(
                game_id=snapshot.game.game_id,
                deltas=tuple(
                    _delta(action, event)
                    for action, event in zip(
                        normalized.actions, snapshot.canonical_events, strict=True
                    )
                ),
            )
        except FeatureNormalizationError:
            raise
        except Exception:
            _fail("unsupported_document_schema")
        raise AssertionError("unreachable")


__all__ = ["NORMALIZER_VERSION", "NBAPlayByPlayV3FeatureEventNormalizer"]
