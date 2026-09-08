"""NBA PlayByPlayV3 free-throw procedure normalization behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from nba_live_totals.domain import (
    ActionConsumption,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowKind,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    JsonObject,
    NormalizedGameActions,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    ResumeDisposition,
    Unsupported,
    Violation,
    ViolationDisposition,
    normalized_actions_checksum,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer

from .possession_helpers import AWAY_TEAM_ID, HOME_TEAM_ID, load_json_fixture, make_snapshot

FREE_THROW_GOLDEN_SHA256 = cast(
    str,
    load_json_fixture("pbp_v3_normalizer_free_throws_v1.expected.json")["sha256"],
)


def _fixture_actions(payload: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], cast(JsonObject, payload["game"])["actions"])


def _action(**updates: object) -> JsonObject:
    payload = load_json_fixture("pbp_v3_normalizer_control_v1.json")
    action = deepcopy(_fixture_actions(payload)[0])
    action.pop("actionNumber")
    action.pop("actionId")
    action.update(cast(JsonObject, updates))
    return action


def _normalize(*actions: JsonObject) -> NormalizedGameActions:
    payload = load_json_fixture("pbp_v3_normalizer_control_v1.json")
    for sequence_number, action in enumerate(actions, start=1):
        action.setdefault("actionNumber", sequence_number)
        action.setdefault("actionId", sequence_number * 10)
    cast(JsonObject, payload["game"])["actions"] = list(actions)
    return NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))


def _free_throw(
    sub_type: str,
    *,
    made: bool,
    home_score: int = 0,
    away_score: int = 0,
    team_id: int = HOME_TEAM_ID,
    player_points: int = 71,
    clock: str = "PT10M00.00S",
) -> JsonObject:
    description = (
        f"Shooter {sub_type} ({player_points} PTS)" if made else f"MISS Shooter {sub_type}"
    )
    return _action(
        clock=clock,
        teamId=team_id,
        personId=101 if team_id == HOME_TEAM_ID else 201,
        actionType="Free Throw",
        subType=sub_type,
        description=description,
        scoreHome=str(home_score),
        scoreAway=str(away_score),
        pointsTotal=home_score + away_score if made else 0,
    )


def test_ordinary_trip_emits_ordered_start_attempt_end_and_uses_game_score_not_player_points() -> (
    None
):
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        _free_throw("Free Throw 2 of 2", made=True, home_score=1, player_points=99),
    )

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[0].signals == (
        FreeThrowTripStart(
            trip_id,
            HOME_TEAM_ID,
            FreeThrowKind.ORDINARY,
            2,
            ResumeDisposition.OPPONENT_INBOUND,
        ),
        FreeThrowAttempt(trip_id, 1, 2, False, 0),
    )
    assert normalized.actions[1].signals == (
        FreeThrowAttempt(trip_id, 2, 2, True, 1),
        FreeThrowTripEnd(trip_id, ResumeDisposition.OPPONENT_INBOUND),
    )
    assert normalized.actions[1].checkpoint_combined_score == 1
    assert normalized.actions[0].consumption is ActionConsumption.TRANSITION


def test_three_shot_ordinary_trip_keeps_all_ordinals_and_final_miss_live() -> None:
    normalized = _normalize(
        _free_throw("Free Throw 1 of 3", made=True, home_score=1),
        _free_throw("Free Throw 2 of 3", made=True, home_score=2),
        _free_throw("Free Throw 3 of 3", made=False, home_score=2),
    )

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[0].signals[0] == FreeThrowTripStart(
        trip_id,
        HOME_TEAM_ID,
        FreeThrowKind.ORDINARY,
        3,
        ResumeDisposition.LIVE_REBOUND,
    )
    assert [
        cast(
            FreeThrowAttempt, action.signals[-2 if action is normalized.actions[-1] else -1]
        ).attempt
        for action in normalized.actions
    ] == [1, 2, 3]
    assert normalized.actions[-1].signals[-1] == FreeThrowTripEnd(
        trip_id, ResumeDisposition.LIVE_REBOUND
    )


@pytest.mark.parametrize(
    ("sub_types", "kind"),
    [
        (("Free Throw Technical",), FreeThrowKind.TECHNICAL),
        (("Free Throw Technical 1 of 2", "Free Throw Technical 2 of 2"), FreeThrowKind.TECHNICAL),
        (("Free Throw Flagrant 1 of 1",), FreeThrowKind.FLAGRANT),
        (("Free Throw Flagrant 1 of 2", "Free Throw Flagrant 2 of 2"), FreeThrowKind.FLAGRANT),
        (
            (
                "Free Throw Flagrant 1 of 3",
                "Free Throw Flagrant 2 of 3",
                "Free Throw Flagrant 3 of 3",
            ),
            FreeThrowKind.FLAGRANT,
        ),
        (
            ("Free Throw Clear Path 1 of 2", "Free Throw Clear Path 2 of 2"),
            FreeThrowKind.CLEAR_PATH,
        ),
        (("Free Throw Away From Play 1 of 1",), FreeThrowKind.AWAY_FROM_PLAY),
        (("Free Throw Transition Take 1 of 1",), FreeThrowKind.TRANSITION_TAKE),
    ],
)
def test_closed_special_subtype_matrix_emits_the_documented_kind_and_resume(
    sub_types: tuple[str, ...], kind: FreeThrowKind
) -> None:
    actions = [
        _free_throw(sub_type, made=True, home_score=index + 1, player_points=30 + index)
        for index, sub_type in enumerate(sub_types)
    ]

    normalized = _normalize(*actions)

    trip_id = "ft:0022500123:1:1"
    start = cast(FreeThrowTripStart, normalized.actions[0].signals[0])
    end = cast(FreeThrowTripEnd, normalized.actions[-1].signals[-1])
    assert start.free_throw_kind is kind
    assert start.attempts == len(sub_types)
    if kind is FreeThrowKind.TECHNICAL:
        assert start.resume_disposition is ResumeDisposition.RESUME_INTERRUPTED
        assert end.resolution is ResumeDisposition.RESUME_INTERRUPTED
    else:
        assert start.resume_disposition is ResumeDisposition.RETAIN_TEAM
        assert start.resume_team == HOME_TEAM_ID
        assert end.resolution is ResumeDisposition.RETAIN_TEAM
        assert end.resume_team == HOME_TEAM_ID
    assert [
        cast(
            FreeThrowAttempt, action.signals[-2 if action is normalized.actions[-1] else -1]
        ).attempt
        for action in normalized.actions
    ] == list(range(1, len(sub_types) + 1))
    assert all(
        action.checkpoint_combined_score == index + 1
        for index, action in enumerate(normalized.actions)
    )
    assert trip_id in repr(normalized)


def test_and_one_requires_the_structural_made_field_goal_and_shooting_foul_link() -> None:
    field_goal = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Made Shot",
        subType="Layup",
        shotResult="Made",
        isFieldGoal=1,
        shotValue=2,
        scoreHome="2",
        scoreAway="0",
        pointsTotal=2,
    )
    foul = _action(teamId=AWAY_TEAM_ID, actionType="Foul", subType="Shooting")
    free_throw = _free_throw("Free Throw 1 of 1", made=True, home_score=3)

    normalized = _normalize(field_goal, foul, free_throw)

    trip_id = "ft:0022500123:3:1"
    assert normalized.actions[0].signals == (FieldGoalAttempt(HOME_TEAM_ID, 2, True, True),)
    assert normalized.actions[1].signals == (Foul(AWAY_TEAM_ID, "shooting", HOME_TEAM_ID),)
    assert normalized.actions[2].signals == (
        FreeThrowTripStart(
            trip_id,
            HOME_TEAM_ID,
            FreeThrowKind.AND_ONE,
            1,
            ResumeDisposition.OPPONENT_INBOUND,
        ),
        FreeThrowAttempt(trip_id, 1, 1, True, 1),
        FreeThrowTripEnd(trip_id, ResumeDisposition.OPPONENT_INBOUND),
    )


def test_unlinked_or_unanchored_free_throws_fail_closed_without_provider_text() -> None:
    unlinked = _free_throw("Free Throw 1 of 1", made=True, home_score=1)
    malformed = _free_throw("Free Throw 1 of 2", made=False)
    malformed["description"] = "prefix MISS Shooter Free Throw 1 of 2"

    normalized = _normalize(unlinked, malformed)

    assert all(
        action.signals == (Unsupported(control_relevant=True),) for action in normalized.actions
    )
    assert "prefix MISS" not in repr(normalized)
    assert all(
        issue.code == "unknown_control_action"
        for issue in normalized.issues
        if issue.sequence_number
    )


def test_only_technical_trips_may_interrupt_an_ordinary_trip_with_deterministic_nested_ids() -> (
    None
):
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        _free_throw("Free Throw Technical", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    parent_id = "ft:0022500123:1:1"
    technical_id = "ft:0022500123:2:2"
    assert normalized.actions[1].signals == (
        FreeThrowTripStart(
            technical_id,
            HOME_TEAM_ID,
            FreeThrowKind.TECHNICAL,
            1,
            ResumeDisposition.RESUME_INTERRUPTED,
            interrupted_trip_id=parent_id,
        ),
        FreeThrowAttempt(technical_id, 1, 1, True, 1),
        FreeThrowTripEnd(technical_id, ResumeDisposition.RESUME_INTERRUPTED),
    )
    assert normalized.actions[2].signals[-1] == FreeThrowTripEnd(
        parent_id, ResumeDisposition.OPPONENT_INBOUND
    )
    assert normalized_actions_checksum(normalized) == FREE_THROW_GOLDEN_SHA256


def test_nontechnical_trip_cannot_interrupt_an_open_ordinary_trip() -> None:
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        _free_throw("Free Throw Flagrant 1 of 1", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unknown_control_action" and issue.sequence_number == 2
        for issue in normalized.issues
    )


def test_lane_retry_preserves_ordinal_with_an_explicit_follow_up() -> None:
    first = _free_throw("Free Throw 1 of 2", made=False)
    violation = _action(teamId=AWAY_TEAM_ID, actionType="Violation", subType="Lane Violation")
    retry = _free_throw("Free Throw 1 of 2", made=False)
    final = _free_throw("Free Throw 2 of 2", made=True, home_score=1)

    normalized = _normalize(first, violation, retry, final)

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[1].signals == (Violation(AWAY_TEAM_ID, ViolationDisposition.RETRY),)
    assert normalized.actions[2].signals == (FreeThrowAttempt(trip_id, 1, 2, False, 0),)
    assert normalized.actions[3].signals[-1] == FreeThrowTripEnd(
        trip_id, ResumeDisposition.OPPONENT_INBOUND
    )


def test_forfeited_final_miss_uses_only_the_explicit_offensive_lane_award() -> None:
    first = _free_throw("Free Throw 1 of 2", made=True, home_score=1)
    final_miss = _free_throw("Free Throw 2 of 2", made=False, home_score=1)
    violation = _action(teamId=HOME_TEAM_ID, actionType="Violation", subType="Lane Violation")
    award = _action(teamId=HOME_TEAM_ID, actionType="Turnover", subType="Lane Violation")

    normalized = _normalize(first, final_miss, violation, award)

    trip_id = "ft:0022500123:1:1"
    assert cast(FreeThrowTripStart, normalized.actions[0].signals[0]).resume_disposition is (
        ResumeDisposition.OPPONENT_INBOUND
    )
    assert normalized.actions[1].signals[-1] == FreeThrowTripEnd(
        trip_id, ResumeDisposition.OPPONENT_INBOUND
    )
    assert normalized.actions[2].signals == (
        Violation(HOME_TEAM_ID, ViolationDisposition.OPPONENT_AWARD),
    )


def test_incomplete_ordinary_trip_reports_the_bounded_unresolved_trip_issue() -> None:
    normalized = _normalize(_free_throw("Free Throw 1 of 2", made=False))

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert [(issue.code, issue.sequence_number) for issue in normalized.issues] == [
        ("unresolved_free_throw_trip", 1),
    ]


def test_orphan_noninitial_attempt_is_unsupported_without_a_synthetic_trip() -> None:
    normalized = _normalize(_free_throw("Free Throw 2 of 2", made=True, home_score=1))

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert [(issue.code, issue.sequence_number) for issue in normalized.issues] == [
        ("unknown_control_action", 1),
    ]


def test_ordinary_trip_does_not_link_across_a_same_clock_control_barrier() -> None:
    first = _free_throw("Free Throw 1 of 2", made=False)
    turnover = _action(teamId=HOME_TEAM_ID, actionType="Turnover", subType="Bad Pass")
    final = _free_throw("Free Throw 2 of 2", made=True, home_score=1)

    normalized = _normalize(first, turnover, final)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unresolved_free_throw_trip" and issue.sequence_number == 1
        for issue in normalized.issues
    )


def test_team_rebound_between_special_nonfinal_attempts_is_a_placeholder() -> None:
    first = _free_throw("Free Throw Flagrant 1 of 2", made=False)
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    final = _free_throw("Free Throw Flagrant 2 of 2", made=True, home_score=1)

    normalized = _normalize(first, rebound, final)

    assert normalized.actions[1].signals == (
        Rebound(
            rebounding_team=AWAY_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.FT_PLACEHOLDER,
        ),
    )
