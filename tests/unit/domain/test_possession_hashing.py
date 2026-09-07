from dataclasses import replace
from datetime import date, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

import pytest

from nba_live_totals.domain.canonical import canonical_json_bytes, canonical_json_sha256
from nba_live_totals.domain.games import GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.play_by_play import PlayByPlayEvent
from nba_live_totals.domain.possession_hashing import (
    canonical_event_checksum,
    canonical_pbp_events_projection,
    game_context_checksum,
    game_context_projection,
    normalized_actions_checksum,
    normalized_actions_projection,
    parse_revision_id,
    parse_revision_projection,
    possession_input_token_checksum,
    possession_input_token_projection,
    possession_output_checksum,
    possession_output_projection,
)
from nba_live_totals.domain.possessions import (
    ActionConsumption,
    Administrative,
    BoundarySide,
    ControlAward,
    EventBoundary,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowKind,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    GameContext,
    Goaltending,
    GoaltendingSide,
    JumpBall,
    NormalizedAction,
    NormalizedGameActions,
    PeriodEnd,
    PeriodStart,
    Possession,
    PossessionEndReason,
    PossessionSignal,
    PossessionVersions,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    ResumeDisposition,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
)

GAME_ID = GameId("0022500123")
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def as_object(value: object) -> dict[str, object]:
    assert type(value) is dict
    return cast(dict[str, object], value)


def as_array(value: object) -> list[object]:
    assert type(value) is list
    return cast(list[object], value)


def make_game_context() -> GameContext:
    return GameContext(
        game_id=GAME_ID,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=10,
        away_team_id=20,
        home_score=110,
        away_score=108,
    )


def make_versions() -> PossessionVersions:
    return PossessionVersions(
        normalizer_version="nba-pbp-v1",
        parser_version="analytical-v1",
        rules_version="nba-2025-v1",
        output_schema_version="possessions-v1",
    )


def make_event(sequence_number: int = 1) -> PlayByPlayEvent:
    return PlayByPlayEvent(
        game_id=GAME_ID,
        sequence_number=sequence_number,
        action_number=7,
        action_id=None,
        period=1,
        clock="PT11M59.00S",
        seconds_remaining_period_ms=719_000,
        team_id=10,
        person_id=None,
        action_type="Jump Ball",
        sub_type="",
        description="Café 🏀",
        home_score=None,
        away_score=None,
        shot_result=None,
        shot_distance=None,
        legacy_x=None,
        legacy_y=None,
        location=None,
        is_field_goal=False,
        points_total=0,
        video_available=False,
    )


def make_normalized_actions() -> NormalizedGameActions:
    return NormalizedGameActions(
        game_id=GAME_ID,
        actions=(
            NormalizedAction(
                game_id=GAME_ID,
                sequence_number=1,
                provider_action_number=7,
                provider_action_id=None,
                period=1,
                clock_ms=719_000,
                actor_team_id=10,
                actor_person_id=None,
                checkpoint_home_score=None,
                checkpoint_away_score=None,
                checkpoint_combined_score=None,
                consumption=ActionConsumption.TRANSITION,
                signals=(
                    FieldGoalAttempt(
                        team=10,
                        value=2,
                        made=True,
                        and_one_candidate=False,
                    ),
                ),
            ),
        ),
    )


def make_possession() -> Possession:
    return Possession(
        game_id=GAME_ID,
        possession_number=1,
        offense_team_id=10,
        defense_team_id=20,
        offense_possession_number=1,
        period=1,
        start_boundary=EventBoundary(1, BoundarySide.BEFORE),
        end_boundary=EventBoundary(1, BoundarySide.AFTER),
        start_seconds_remaining_period_ms=720_000,
        end_seconds_remaining_period_ms=719_000,
        seconds_remaining_game_ms=2_879_000,
        home_score_before=0,
        away_score_before=0,
        home_score=2,
        away_score=0,
        score_diff=2,
        offense_points=2,
        defense_points=0,
        home_possessions=1,
        away_possessions=0,
        end_reason=PossessionEndReason.MADE_FIELD_GOAL,
        source_event_count=1,
    )


def test_strict_canonical_encoder_matches_unicode_golden_vector() -> None:
    projection = {"b": [True, None, 2], "a": "Café 🏀"}

    encoded = canonical_json_bytes(projection)

    assert encoded == '{"a":"Café 🏀","b":[true,null,2]}'.encode()
    assert not encoded.startswith(b"\xef\xbb\xbf")
    assert not encoded.endswith((b"\n", b"\r", b" "))
    assert canonical_json_sha256(projection) == (
        "e96bb88b7b8b40fdde3b618cf689fe690659af59f599b35875d9eb3ec990fa92"
    )


def test_strict_canonical_encoder_has_exact_json_type_semantics() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == canonical_json_bytes({"a": 1, "b": 2})
    assert canonical_json_bytes({"a": [1, 2]}) != canonical_json_bytes({"a": [2, 1]})
    assert (
        len(
            {
                canonical_json_sha256({"value": 1}),
                canonical_json_sha256({"value": True}),
                canonical_json_sha256({"value": "1"}),
            }
        )
        == 3
    )
    assert canonical_json_bytes({"value": None}) == b'{"value":null}'


class ExampleEnum(StrEnum):
    VALUE = "value"


@pytest.mark.parametrize(
    "unsupported",
    [
        1.0,
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        date(2026, 1, 2),
        datetime(2026, 1, 2),
        ExampleEnum.VALUE,
        {1: "non-string key"},
    ],
)
def test_strict_canonical_encoder_rejects_non_contract_types(unsupported: object) -> None:
    with pytest.raises(TypeError, match="canonical JSON"):
        canonical_json_bytes({"value": unsupported})


def test_game_context_projection_matches_spec_golden_vector() -> None:
    projection = game_context_projection(make_game_context())

    assert projection == {
        "schema": "possession-game-context-v1",
        "game_id": "0022500123",
        "season": "2025-26",
        "season_type": "regular_season",
        "game_date": "2026-01-02",
        "status": "final",
        "home_team_id": 10,
        "away_team_id": 20,
        "home_score": 110,
        "away_score": 108,
    }
    assert game_context_checksum(make_game_context()) == (
        "6a31d3483e743e09df8c5fcb1d405d6bcd1a4d0a0ae7476bef0a947ff2d29e34"
    )


def test_canonical_event_projection_has_only_the_spec_fields_and_explicit_nulls() -> None:
    root = canonical_pbp_events_projection((make_event(),))
    event_projection = as_object(as_array(root["events"])[0])

    assert list(event_projection) == [
        "game_id",
        "sequence_number",
        "action_number",
        "action_id",
        "period",
        "clock",
        "seconds_remaining_period_ms",
        "team_id",
        "person_id",
        "action_type",
        "sub_type",
        "description",
        "home_score",
        "away_score",
        "shot_result",
        "shot_distance",
        "legacy_x",
        "legacy_y",
        "location",
        "is_field_goal",
        "points_total",
        "video_available",
    ]
    assert event_projection["action_id"] is None
    assert event_projection["shot_result"] is None
    assert "shot_value" not in event_projection
    assert canonical_event_checksum((make_event(2), make_event(1))) == canonical_event_checksum(
        (make_event(1), make_event(2))
    )
    assert canonical_event_checksum((make_event(),)) == (
        "559ae3b2c9e6460be045024401ae1b935dca631be94705f3a82a41f76ff1807f"
    )


def test_canonical_event_projection_rejects_invalid_or_incoherent_streams() -> None:
    with pytest.raises(TypeError, match="PlayByPlayEvent"):
        canonical_pbp_events_projection((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        canonical_pbp_events_projection(())
    with pytest.raises(ValueError, match="contiguous"):
        canonical_pbp_events_projection((make_event(2),))
    with pytest.raises(ValueError, match="one game"):
        canonical_pbp_events_projection(
            (
                make_event(1),
                replace(make_event(2), game_id=GameId("0022500124")),
            )
        )


def test_normalized_projection_uses_signal_discriminator_and_exact_envelope() -> None:
    projection = normalized_actions_projection(make_normalized_actions())
    action = as_object(as_array(projection["actions"])[0])

    assert projection["schema"] == "normalized-actions-v1"
    assert projection["game_id"] == "0022500123"
    assert set(action) == {
        "game_id",
        "sequence_number",
        "provider_action_number",
        "provider_action_id",
        "period",
        "clock_ms",
        "actor_team_id",
        "actor_person_id",
        "checkpoint_home_score",
        "checkpoint_away_score",
        "checkpoint_combined_score",
        "consumption",
        "signals",
    }
    assert action["provider_action_id"] is None
    assert action["signals"] == [
        {
            "signal_type": "field_goal_attempt",
            "team": 10,
            "value": 2,
            "made": True,
            "and_one_candidate": False,
        }
    ]
    assert normalized_actions_checksum(make_normalized_actions()) == normalized_actions_checksum(
        make_normalized_actions()
    )
    assert normalized_actions_checksum(make_normalized_actions()) == (
        "190ff8239cf9e113006b6f9f6fbeaede3b1dc0ccc1421b52774bc0a09e35693c"
    )


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (PeriodStart(), {"signal_type": "period_start"}),
        (PeriodEnd(), {"signal_type": "period_end"}),
        (
            Rebound(
                rebounding_team=10,
                actor=ReboundActor.PLAYER,
                provider_side=ProviderReboundSide.OFFENSIVE,
                disposition=ReboundDisposition.NORMAL_LIVE,
            ),
            {
                "signal_type": "rebound",
                "rebounding_team": 10,
                "actor": "player",
                "provider_side": "offensive",
                "disposition": "normal_live",
            },
        ),
        (
            Turnover(committing_team=10, gained_by=None, turnover_kind="bad_pass"),
            {
                "signal_type": "turnover",
                "committing_team": 10,
                "gained_by": None,
                "turnover_kind": "bad_pass",
            },
        ),
        (
            StealAnnotation(team=20, related_sequence_number=None),
            {
                "signal_type": "steal_annotation",
                "team": 20,
                "related_sequence_number": None,
            },
        ),
        (
            Foul(
                committing_team=20,
                foul_kind="shooting",
                shooting_team=10,
                related_sequence_number=None,
            ),
            {
                "signal_type": "foul",
                "committing_team": 20,
                "foul_kind": "shooting",
                "shooting_team": 10,
                "related_sequence_number": None,
            },
        ),
        (
            FreeThrowTripStart(
                trip_id="ft:0022500123:1:0",
                shooting_team=10,
                free_throw_kind=FreeThrowKind.TECHNICAL,
                attempts=1,
                resume_disposition=ResumeDisposition.RESUME_INTERRUPTED,
                resume_team=None,
                interrupted_trip_id=None,
            ),
            {
                "signal_type": "free_throw_trip_start",
                "trip_id": "ft:0022500123:1:0",
                "shooting_team": 10,
                "free_throw_kind": "technical",
                "attempts": 1,
                "resume_disposition": "resume_interrupted",
                "resume_team": None,
                "interrupted_trip_id": None,
            },
        ),
        (
            FreeThrowAttempt(
                trip_id="ft:0022500123:1:0",
                attempt=1,
                total=1,
                made=True,
                awarded_points=1,
            ),
            {
                "signal_type": "free_throw_attempt",
                "trip_id": "ft:0022500123:1:0",
                "attempt": 1,
                "total": 1,
                "made": True,
                "awarded_points": 1,
            },
        ),
        (
            FreeThrowTripEnd(
                trip_id="ft:0022500123:1:0",
                resolution=ResumeDisposition.RESUME_INTERRUPTED,
                resume_team=None,
            ),
            {
                "signal_type": "free_throw_trip_end",
                "trip_id": "ft:0022500123:1:0",
                "resolution": "resume_interrupted",
                "resume_team": None,
            },
        ),
        (JumpBall(), {"signal_type": "jump_ball", "winner_team": None}),
        (
            Violation(team=None, disposition=ViolationDisposition.JUMP_BALL),
            {"signal_type": "violation", "team": None, "disposition": "jump_ball"},
        ),
        (
            Goaltending(team=20, side=GoaltendingSide.DEFENSIVE, awarded_points=2),
            {
                "signal_type": "goaltending",
                "team": 20,
                "side": "defensive",
                "awarded_points": 2,
            },
        ),
        (
            ControlAward(team=10, reason="opening_control"),
            {"signal_type": "control_award", "team": 10, "reason": "opening_control"},
        ),
        (
            Administrative(administrative_kind="timeout"),
            {"signal_type": "administrative", "administrative_kind": "timeout"},
        ),
        (
            Unsupported(control_relevant=True),
            {"signal_type": "unsupported", "control_relevant": True},
        ),
    ],
)
def test_normalized_projection_covers_the_closed_signal_vocabulary(
    signal: PossessionSignal, expected: dict[str, object]
) -> None:
    normalized = make_normalized_actions()
    action = replace(normalized.actions[0], signals=(signal,))
    projection = normalized_actions_projection(replace(normalized, actions=(action,)))
    action_projection = as_object(as_array(projection["actions"])[0])

    assert action_projection["signals"] == [expected]


def test_revision_and_input_token_include_only_content_and_lineage_fields() -> None:
    versions = make_versions()
    document_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    revision = parse_revision_projection(
        game_id=GAME_ID,
        pbp_document_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    )
    token = possession_input_token_projection(
        game_id=GAME_ID,
        applied_pbp_document_id=document_id,
        applied_pbp_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    )

    assert revision == {
        "schema": "possession-parse-revision-v1",
        "game_id": "0022500123",
        "pbp_document_sha256": SHA_A,
        "canonical_event_sha256": SHA_B,
        "game_context_sha256": SHA_C,
        "normalizer_version": "nba-pbp-v1",
        "parser_version": "analytical-v1",
        "rules_version": "nba-2025-v1",
        "output_schema_version": "possessions-v1",
    }
    assert "applied_pbp_document_id" not in revision
    assert token == {
        "schema": "possession-input-token-v1",
        "game_id": "0022500123",
        "applied_pbp_document_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "applied_pbp_sha256": SHA_A,
        "canonical_event_sha256": SHA_B,
        "game_context_sha256": SHA_C,
        "normalizer_version": "nba-pbp-v1",
        "parser_version": "analytical-v1",
        "rules_version": "nba-2025-v1",
        "output_schema_version": "possessions-v1",
    }
    assert parse_revision_id(
        game_id=GAME_ID,
        pbp_document_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    ) == parse_revision_id(
        game_id=GAME_ID,
        pbp_document_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    )
    assert (
        parse_revision_id(
            game_id=GAME_ID,
            pbp_document_sha256=SHA_A,
            canonical_event_sha256=SHA_B,
            game_context_sha256=SHA_C,
            versions=versions,
        )
        == "5478e87925502e348f3adabfba32101e2b61e1db2dcbf4d76f4a1f81af6b7441"
    )
    assert possession_input_token_checksum(
        game_id=GAME_ID,
        applied_pbp_document_id=document_id,
        applied_pbp_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    ) != possession_input_token_checksum(
        game_id=GAME_ID,
        applied_pbp_document_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        applied_pbp_sha256=SHA_A,
        canonical_event_sha256=SHA_B,
        game_context_sha256=SHA_C,
        versions=versions,
    )
    assert (
        possession_input_token_checksum(
            game_id=GAME_ID,
            applied_pbp_document_id=document_id,
            applied_pbp_sha256=SHA_A,
            canonical_event_sha256=SHA_B,
            game_context_sha256=SHA_C,
            versions=versions,
        )
        == "125a5e7d3c2dbad868a9d7c885e84bf097746b48add6b6940b26faa0f45fb295"
    )


def test_output_projection_contains_only_typed_content_fields_in_possession_order() -> None:
    game = make_game_context()
    first = make_possession()
    second = replace(
        first,
        possession_number=2,
        offense_team_id=20,
        defense_team_id=10,
        start_boundary=EventBoundary(2, BoundarySide.BEFORE),
        end_boundary=EventBoundary(2, BoundarySide.AFTER),
        offense_points=0,
        home_possessions=1,
        away_possessions=1,
    )
    projection = possession_output_projection(
        parse_revision_id=SHA_A,
        game=game,
        possessions=(second, first),
    )

    assert projection["schema"] == "possession-output-v1"
    assert projection["parse_revision_id"] == SHA_A
    rows = [as_object(row) for row in as_array(projection["rows"])]
    assert [row["possession_number"] for row in rows] == [1, 2]
    assert list(rows[0]) == [
        "game_id",
        "season",
        "season_start_year",
        "season_type",
        "game_date",
        "home_team_id",
        "away_team_id",
        "possession_number",
        "offense_possession_number",
        "period",
        "offense_team_id",
        "defense_team_id",
        "start_sequence_number",
        "start_boundary_side",
        "end_sequence_number",
        "end_boundary_side",
        "start_seconds_remaining_period_ms",
        "end_seconds_remaining_period_ms",
        "seconds_remaining_game_ms",
        "home_score_before",
        "away_score_before",
        "home_score",
        "away_score",
        "score_diff",
        "offense_points",
        "defense_points",
        "home_possessions",
        "away_possessions",
        "end_reason",
        "source_event_count",
    ]
    assert "snapshot_id" not in rows[0]
    assert "output_sha256" not in rows[0]
    assert possession_output_checksum(
        parse_revision_id=SHA_A,
        game=game,
        possessions=(second, first),
    ) == possession_output_checksum(
        parse_revision_id=SHA_A,
        game=game,
        possessions=(first, second),
    )
    assert (
        possession_output_checksum(
            parse_revision_id=SHA_A,
            game=game,
            possessions=(first,),
        )
        == "ad98acf59befe3f51946fcfe7a5a55e208fab5245d89f840efe62be67a6260d9"
    )


def test_output_projection_rejects_invalid_or_empty_possession_streams() -> None:
    with pytest.raises(TypeError, match="Possession"):
        possession_output_projection(
            parse_revision_id=SHA_A,
            game=make_game_context(),
            possessions=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="empty"):
        possession_output_projection(
            parse_revision_id=SHA_A,
            game=make_game_context(),
            possessions=(),
        )
