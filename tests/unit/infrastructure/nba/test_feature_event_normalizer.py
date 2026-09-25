"""Provider-neutral feature-event normalization behavior tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest

from nba_live_totals.application.features import FeatureNormalizationError
from nba_live_totals.application.possessions import GamePbpSnapshot
from nba_live_totals.domain import (
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3FeatureEventNormalizer
from nba_live_totals.infrastructure.nba.mappers import parse_play_by_play

GAME_ID = GameId("0022500001")
HOME_TEAM_ID = 10
AWAY_TEAM_ID = 20


def _action(sequence_number: int, **updates: object) -> JsonObject:
    value: JsonObject = {
        "actionNumber": sequence_number,
        "actionId": sequence_number * 10,
        "period": 1,
        "clock": f"PT{12 - sequence_number}M00.00S",
        "teamId": 0,
        "personId": 0,
        "actionType": "Timeout",
        "subType": "Official",
        "description": "provider text must remain private",
        "scoreHome": "0",
        "scoreAway": "0",
        "shotResult": None,
        "shotDistance": None,
        "xLegacy": None,
        "yLegacy": None,
        "location": None,
        "isFieldGoal": 0,
        "pointsTotal": 0,
        "videoAvailable": 0,
        "shotValue": None,
    }
    value.update(cast(JsonObject, updates))
    return value


def _snapshot(
    actions: list[JsonObject],
    *,
    canonical_events: tuple[PlayByPlayEvent, ...] | None = None,
) -> GamePbpSnapshot:
    payload = cast(
        JsonObject,
        {
            "meta": {"version": 1},
            "game": {"gameId": GAME_ID.value, "actions": actions},
        },
    )
    document = SourceDocument(
        source="nba.com",
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{GAME_ID.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
    events = canonical_events or parse_play_by_play(document)
    game = GameContext(
        game_id=GAME_ID,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=HOME_TEAM_ID,
        away_team_id=AWAY_TEAM_ID,
        home_score=1,
        away_score=0,
    )
    return GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=UUID("00000000-0000-4000-8000-000000000001"),
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )


def _stat_actions() -> list[JsonObject]:
    return [
        _action(1),
        _action(
            2,
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Missed Shot",
            subType="Jump Shot",
            shotResult="Missed",
            shotDistance=25,
            location="h",
            isFieldGoal=1,
            shotValue=3,
        ),
        _action(
            3,
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Free Throw",
            subType="Free Throw 1 of 1",
            shotResult="Made",
            scoreHome="1",
            pointsTotal=1,
            shotValue=1,
        ),
        _action(
            4,
            teamId=AWAY_TEAM_ID,
            personId=201,
            actionType="Turnover",
            subType="Bad Pass",
            scoreHome="1",
            description="turnover provider secret",
        ),
        _action(
            5,
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Rebound",
            subType="Normal Rebound",
            scoreHome="1",
            description="Home Player REBOUND (Off:1 Def:0)",
        ),
        _action(
            6,
            teamId=AWAY_TEAM_ID,
            personId=201,
            actionType="Rebound",
            subType="Normal Rebound",
            scoreHome="1",
            description="Away Player REBOUND (Off:0 Def:1)",
        ),
        _action(
            7,
            teamId=AWAY_TEAM_ID,
            personId=202,
            actionType="Made Shot",
            subType="Driving Layup",
            scoreHome="1",
            scoreAway="2",
            shotResult="Made",
            shotDistance=1,
            location="v",
            isFieldGoal=1,
            pointsTotal=3,
            shotValue=2,
        ),
    ]


def test_normalizer_emits_one_aligned_delta_per_raw_action() -> None:
    normalized = NBAPlayByPlayV3FeatureEventNormalizer().normalize(_snapshot(_stat_actions()))

    assert len(normalized.deltas) == 7
    assert normalized.deltas[0].boundary == EventBoundary(1, BoundarySide.AFTER)
    assert normalized.deltas[0].team_id is None
    assert normalized.deltas[0].field_goal_attempts == 0
    assert normalized.deltas[1].team_id == HOME_TEAM_ID
    assert normalized.deltas[1].field_goal_attempts == 1
    assert normalized.deltas[1].three_point_attempts == 1
    assert normalized.deltas[2].free_throw_attempts == 1
    assert normalized.deltas[3].turnovers == 1
    assert normalized.deltas[4].offensive_rebounds == 1
    assert normalized.deltas[5].defensive_rebounds == 1
    assert normalized.deltas[6].field_goal_attempts == 1
    assert normalized.deltas[6].three_point_attempts == 0


def test_rebound_deltas_reuse_phase_3_normalized_signals() -> None:
    actions = [
        _action(
            sequence_number,
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Rebound",
            subType="Normal Rebound",
            description=f"Home Player REBOUND (Off:{offensive_total} Def:0)",
        )
        for sequence_number, offensive_total in enumerate((1, 1, 0, 1), start=1)
    ]

    normalized = NBAPlayByPlayV3FeatureEventNormalizer().normalize(_snapshot(actions))

    assert [delta.offensive_rebounds for delta in normalized.deltas] == [1, 0, 0, 1]


def test_possession_normalizer_failure_is_wrapped_without_provider_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_with_provider_text(*_args: object, **_kwargs: object) -> object:
        raise ValueError("sensitive provider payload")

    monkeypatch.setattr(
        "nba_live_totals.infrastructure.nba.feature_event_normalizer."
        "NBAPlayByPlayV3PossessionNormalizer.normalize",
        fail_with_provider_text,
    )

    with pytest.raises(FeatureNormalizationError) as caught:
        NBAPlayByPlayV3FeatureEventNormalizer().normalize(_snapshot(_stat_actions()))

    assert caught.value.code == "unsupported_document_schema"
    assert str(caught.value) == "unsupported_document_schema"
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: replace(event, action_number=999),
        lambda event: replace(event, period=2),
        lambda event: replace(event, clock="PT1M01.00S"),
        lambda event: replace(event, seconds_remaining_period_ms=61_000),
        lambda event: replace(event, description="sensitive canonical drift"),
    ],
)
def test_normalizer_rejects_exact_raw_canonical_event_drift(
    mutate: Callable[[PlayByPlayEvent], PlayByPlayEvent],
) -> None:
    actions = _stat_actions()
    base = _snapshot(actions)
    changed = mutate(base.canonical_events[0])
    snapshot = _snapshot(actions, canonical_events=(changed, *base.canonical_events[1:]))

    with pytest.raises(FeatureNormalizationError) as caught:
        NBAPlayByPlayV3FeatureEventNormalizer().normalize(snapshot)

    assert caught.value.code == "raw_canonical_event_mismatch"
    assert str(caught.value) == "raw_canonical_event_mismatch"
    assert "sensitive" not in str(caught.value)


def test_normalizer_rejects_raw_canonical_cardinality_drift() -> None:
    actions = _stat_actions()
    canonical = _snapshot(actions).canonical_events[:-1]

    with pytest.raises(FeatureNormalizationError) as caught:
        NBAPlayByPlayV3FeatureEventNormalizer().normalize(
            _snapshot(actions, canonical_events=canonical)
        )

    assert caught.value.code == "raw_canonical_cardinality_mismatch"


def test_normalizer_rejects_team_outside_the_game() -> None:
    actions = [_action(1, teamId=30, personId=301)]

    with pytest.raises(FeatureNormalizationError) as caught:
        NBAPlayByPlayV3FeatureEventNormalizer().normalize(_snapshot(actions))

    assert caught.value.code == "raw_canonical_team_mismatch"


def test_normalizer_rejects_unapplied_document_identity_without_provider_text() -> None:
    snapshot = _snapshot(_stat_actions())
    original = snapshot.source_document
    mismatched = SourceDocument(
        source="other-provider",
        endpoint=original.endpoint,
        resource_key=original.resource_key,
        requested_at=original.requested_at,
        request_parameters=original.request_parameters,
        payload=original.payload,
        checksum_sha256=original.checksum_sha256,
    )
    snapshot = replace(
        snapshot,
        source_document=mismatched,
    )

    with pytest.raises(FeatureNormalizationError) as caught:
        NBAPlayByPlayV3FeatureEventNormalizer().normalize(snapshot)

    assert caught.value.code == "input_document_identity_mismatch"
    assert "provider" not in str(caught.value)
