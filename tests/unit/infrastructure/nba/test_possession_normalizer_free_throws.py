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


def _turnover(*, team_id: int = HOME_TEAM_ID) -> JsonObject:
    return _action(teamId=team_id, actionType="Turnover", subType="Bad Pass")


def _score_prefix(*, home_score: int, away_score: int) -> list[JsonObject]:
    current_home = 0
    current_away = 0
    actions: list[JsonObject] = []

    def shot_values(total: int) -> list[int]:
        if total % 3 == 1:
            return [2, 2, *([3] * ((total - 4) // 3))]
        return [2] * (total % 3 // 2) + [3] * (total // 3)

    for team_id, values in (
        (HOME_TEAM_ID, shot_values(home_score)),
        (AWAY_TEAM_ID, shot_values(away_score)),
    ):
        for value in values:
            if team_id == HOME_TEAM_ID:
                current_home += value
            else:
                current_away += value
            actions.append(
                _action(
                    teamId=team_id,
                    personId=101 if team_id == HOME_TEAM_ID else 201,
                    actionType="Made Shot",
                    subType="Jump Shot",
                    description="",
                    scoreHome=str(current_home),
                    scoreAway=str(current_away),
                    shotResult="Made",
                    isFieldGoal=1,
                    pointsTotal=current_home + current_away,
                    shotValue=value,
                )
            )
    return actions


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


@pytest.mark.parametrize(
    ("foul_sub_type", "foul_kind"),
    [("Shooting", "shooting"), ("Block", "block")],
)
def test_and_one_links_the_made_field_goal_to_the_shooting_or_block_foul(
    foul_sub_type: str, foul_kind: str
) -> None:
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
    foul = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Foul",
        subType=foul_sub_type,
        scoreHome="2",
    )
    free_throw = _free_throw("Free Throw 1 of 1", made=True, home_score=3)

    normalized = _normalize(field_goal, foul, free_throw)

    trip_id = "ft:0022500123:3:1"
    assert normalized.actions[0].signals == (FieldGoalAttempt(HOME_TEAM_ID, 2, True, True),)
    assert normalized.actions[1].signals == (
        Foul(AWAY_TEAM_ID, foul_kind, HOME_TEAM_ID, related_sequence_number=1),
    )
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


def test_live_shaped_transition_take_foul_links_ordinary_one_shot_trip() -> None:
    prefix = _score_prefix(home_score=11, away_score=14)
    while len(prefix) < 72:
        prefix.append(
            _action(
                actionType="Timeout",
                subType="Official",
                scoreHome="11",
                scoreAway="14",
            )
        )
    foul = _action(
        clock="PT08M00.00S",
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Foul",
        subType="Transition Take",
        description="",
        scoreHome="11",
        scoreAway="14",
        location="h",
        pointsTotal=25,
    )
    free_throw = _free_throw(
        "Free Throw 1 of 1",
        made=True,
        home_score=11,
        away_score=15,
        team_id=AWAY_TEAM_ID,
        clock="PT08M00.00S",
    )

    normalized = _normalize(*prefix, foul, free_throw)

    trip_id = "ft:0022500123:74:1"
    assert normalized.actions[72].signals == (
        Foul(
            HOME_TEAM_ID,
            "transition_take",
            AWAY_TEAM_ID,
            related_sequence_number=74,
        ),
    )
    assert normalized.actions[73].signals == (
        FreeThrowTripStart(
            trip_id,
            AWAY_TEAM_ID,
            FreeThrowKind.TRANSITION_TAKE,
            1,
            ResumeDisposition.RETAIN_TEAM,
            AWAY_TEAM_ID,
        ),
        FreeThrowAttempt(trip_id, 1, 1, True, 1),
        FreeThrowTripEnd(trip_id, ResumeDisposition.RETAIN_TEAM, AWAY_TEAM_ID),
    )


@pytest.mark.parametrize("points_total", [0, None, 25])
def test_transition_take_foul_links_missed_ordinary_one_shot_trip(
    points_total: int | None,
) -> None:
    prefix = _score_prefix(home_score=11, away_score=14)
    foul = _action(
        clock="PT08M00.00S",
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Foul",
        subType="Transition Take",
        description="",
        scoreHome="11",
        scoreAway="14",
        location="h",
        pointsTotal=25,
    )
    free_throw = _free_throw(
        "Free Throw 1 of 1",
        made=False,
        home_score=11,
        away_score=14,
        team_id=AWAY_TEAM_ID,
        clock="PT08M00.00S",
    )
    free_throw["pointsTotal"] = points_total

    normalized = _normalize(*prefix, foul, free_throw)

    free_throw_sequence = len(prefix) + 2
    trip_id = f"ft:0022500123:{free_throw_sequence}:1"
    assert normalized.actions[-2].signals == (
        Foul(
            HOME_TEAM_ID,
            "transition_take",
            AWAY_TEAM_ID,
            related_sequence_number=free_throw_sequence,
        ),
    )
    assert normalized.actions[-1].signals == (
        FreeThrowTripStart(
            trip_id,
            AWAY_TEAM_ID,
            FreeThrowKind.TRANSITION_TAKE,
            1,
            ResumeDisposition.RETAIN_TEAM,
            AWAY_TEAM_ID,
        ),
        FreeThrowAttempt(trip_id, 1, 1, False, 0),
        FreeThrowTripEnd(trip_id, ResumeDisposition.RETAIN_TEAM, AWAY_TEAM_ID),
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"scoreHome": None},
        {"scoreAway": None},
        {"scoreHome": None, "scoreAway": None},
        {"scoreHome": "12"},
        {"scoreAway": "15"},
        {"pointsTotal": 24},
        {"pointsTotal": 26},
    ],
)
def test_transition_take_missed_ordinary_one_shot_requires_exact_unchanged_score(
    updates: dict[str, object],
) -> None:
    foul = _action(
        clock="PT08M00.00S",
        teamId=HOME_TEAM_ID,
        actionType="Foul",
        subType="Transition Take",
        scoreHome="11",
        scoreAway="14",
        pointsTotal=25,
    )
    free_throw = _free_throw(
        "Free Throw 1 of 1",
        made=False,
        home_score=11,
        away_score=14,
        team_id=AWAY_TEAM_ID,
        clock="PT08M00.00S",
    )
    free_throw.update(cast(JsonObject, updates))

    normalized = _normalize(*_score_prefix(home_score=11, away_score=14), foul, free_throw)

    assert normalized.actions[-1].signals == (Unsupported(control_relevant=True),)


@pytest.mark.parametrize("points_total", [0, None, 25])
def test_explicit_transition_take_miss_uses_same_checkpoint_contract(
    points_total: int | None,
) -> None:
    free_throw = _free_throw(
        "Free Throw Transition Take 1 of 1",
        made=False,
        home_score=11,
        away_score=14,
        team_id=AWAY_TEAM_ID,
    )
    free_throw["pointsTotal"] = points_total

    normalized = _normalize(*_score_prefix(home_score=11, away_score=14), free_throw)

    start = cast(FreeThrowTripStart, normalized.actions[-1].signals[0])
    end = cast(FreeThrowTripEnd, normalized.actions[-1].signals[-1])
    assert start.free_throw_kind is FreeThrowKind.TRANSITION_TAKE
    assert start.resume_disposition is ResumeDisposition.RETAIN_TEAM
    assert start.resume_team == AWAY_TEAM_ID
    assert end == FreeThrowTripEnd(
        start.trip_id,
        ResumeDisposition.RETAIN_TEAM,
        AWAY_TEAM_ID,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"scoreHome": None},
        {"scoreAway": None},
        {"scoreHome": "12"},
        {"scoreAway": "15"},
        {"pointsTotal": 24},
        {"pointsTotal": 26},
    ],
)
def test_explicit_transition_take_miss_rejects_invalid_checkpoint_contract(
    updates: dict[str, object],
) -> None:
    free_throw = _free_throw(
        "Free Throw Transition Take 1 of 1",
        made=False,
        home_score=11,
        away_score=14,
        team_id=AWAY_TEAM_ID,
    )
    free_throw.update(cast(JsonObject, updates))

    normalized = _normalize(*_score_prefix(home_score=11, away_score=14), free_throw)

    assert normalized.issues[-1].code == "invalid_score_delta"
    assert normalized.issues[-1].severity.value == "error"


@pytest.mark.parametrize(
    "scenario",
    [
        "missing-foul",
        "wrong-team",
        "ambiguous-foul",
        "clock-mismatch",
        "intervening-barrier",
        "foul-checkpoint-conflict",
        "free-throw-score-conflict",
    ],
)
def test_transition_take_ordinary_one_shot_linkage_fails_closed(scenario: str) -> None:
    foul = _action(
        clock="PT10M00.00S",
        teamId=HOME_TEAM_ID,
        actionType="Foul",
        subType="Transition Take",
    )
    free_throw = _free_throw(
        "Free Throw 1 of 1",
        made=True,
        away_score=1,
        team_id=AWAY_TEAM_ID,
        clock="PT10M00.00S",
    )
    actions: list[JsonObject] = [foul, free_throw]
    if scenario == "missing-foul":
        actions = [free_throw]
    elif scenario == "wrong-team":
        foul["teamId"] = AWAY_TEAM_ID
    elif scenario == "ambiguous-foul":
        actions = [foul, deepcopy(foul), free_throw]
    elif scenario == "clock-mismatch":
        foul["clock"] = "PT10M01.00S"
    elif scenario == "intervening-barrier":
        actions = [
            foul,
            _action(
                clock="PT10M00.00S",
                actionType="Timeout",
                subType="Regular",
                scoreHome="1",
            ),
            free_throw,
        ]
    elif scenario == "foul-checkpoint-conflict":
        foul["scoreHome"] = "1"
    else:
        free_throw["scoreAway"] = "0"
        free_throw["pointsTotal"] = 0

    normalized = _normalize(*actions)

    assert normalized.actions[-1].signals == (Unsupported(control_relevant=True),)


def test_and_one_uses_post_field_goal_score_when_target_checkpoints_are_absent() -> None:
    prior_baskets = [
        _action(
            clock=f"PT11M{59 - index:02d}.00S",
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Made Shot",
            subType="Layup",
            shotResult="Made",
            isFieldGoal=1,
            shotValue=2,
            scoreHome=str((index + 1) * 2),
            scoreAway="0",
            pointsTotal=(index + 1) * 2,
        )
        for index in range(4)
    ]
    target = _action(
        clock="PT10M00.00S",
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Made Shot",
        subType="Layup",
        shotResult="Made",
        isFieldGoal=1,
        shotValue=2,
        scoreHome=None,
        scoreAway=None,
        pointsTotal=10,
    )
    foul = _action(
        clock="PT10M00.00S",
        teamId=AWAY_TEAM_ID,
        actionType="Foul",
        subType="Shooting",
        scoreHome="10",
        scoreAway="0",
    )
    free_throw = _free_throw(
        "Free Throw 1 of 1",
        made=True,
        home_score=11,
        clock="PT10M00.00S",
    )

    normalized = _normalize(*prior_baskets, target, foul, free_throw)

    assert normalized.actions[4].signals == (FieldGoalAttempt(HOME_TEAM_ID, 2, True, True),)
    assert normalized.actions[5].signals == (
        Foul(AWAY_TEAM_ID, "shooting", HOME_TEAM_ID, related_sequence_number=5),
    )
    assert cast(FreeThrowTripStart, normalized.actions[6].signals[0]).free_throw_kind is (
        FreeThrowKind.AND_ONE
    )


def test_and_one_rejects_an_ambiguous_pair_of_qualifying_fouls() -> None:
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
    shooting_foul = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Foul",
        subType="Shooting",
        scoreHome="2",
    )
    block_foul = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Foul",
        subType="Block",
        scoreHome="2",
    )
    free_throw = _free_throw("Free Throw 1 of 1", made=True, home_score=3)

    normalized = _normalize(field_goal, shooting_foul, block_foul, free_throw)

    assert normalized.actions[3].signals == (Unsupported(control_relevant=True),)
    assert all(
        cast(Foul, normalized.actions[index].signals[0]).related_sequence_number is None
        for index in (1, 2)
    )


def test_and_one_rejects_a_foul_by_the_shooting_team() -> None:
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
    wrong_team_foul = _action(
        teamId=HOME_TEAM_ID,
        actionType="Foul",
        subType="Shooting",
        scoreHome="2",
    )
    free_throw = _free_throw("Free Throw 1 of 1", made=True, home_score=3)

    normalized = _normalize(field_goal, wrong_team_foul, free_throw)

    assert normalized.actions[1].signals == (Foul(HOME_TEAM_ID, "shooting", AWAY_TEAM_ID),)
    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)


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


def test_final_missed_attempt_closes_only_after_its_defensive_lane_retry() -> None:
    first = _free_throw("Free Throw 1 of 2", made=False)
    voided_final = _free_throw("Free Throw 2 of 2", made=False)
    violation = _action(teamId=AWAY_TEAM_ID, actionType="Violation", subType="Lane Violation")
    retry = _free_throw("Free Throw 2 of 2", made=True, home_score=1)

    normalized = _normalize(first, voided_final, violation, retry)

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[1].signals == (FreeThrowAttempt(trip_id, 2, 2, False, 0),)
    assert normalized.actions[2].signals == (Violation(AWAY_TEAM_ID, ViolationDisposition.RETRY),)
    assert normalized.actions[3].signals == (
        FreeThrowAttempt(trip_id, 2, 2, True, 1),
        FreeThrowTripEnd(trip_id, ResumeDisposition.OPPONENT_INBOUND),
    )


def test_retained_special_final_miss_closes_only_after_its_defensive_lane_retry() -> None:
    voided = _free_throw("Free Throw Flagrant 1 of 1", made=False)
    violation = _action(teamId=AWAY_TEAM_ID, actionType="Violation", subType="Lane Violation")
    retry = _free_throw("Free Throw Flagrant 1 of 1", made=True, home_score=1)

    normalized = _normalize(voided, violation, retry)

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[0].signals == (
        FreeThrowTripStart(
            trip_id,
            HOME_TEAM_ID,
            FreeThrowKind.FLAGRANT,
            1,
            ResumeDisposition.RETAIN_TEAM,
            HOME_TEAM_ID,
        ),
        FreeThrowAttempt(trip_id, 1, 1, False, 0),
    )
    assert normalized.actions[1].signals == (Violation(AWAY_TEAM_ID, ViolationDisposition.RETRY),)
    assert normalized.actions[2].signals == (
        FreeThrowAttempt(trip_id, 1, 1, True, 1),
        FreeThrowTripEnd(trip_id, ResumeDisposition.RETAIN_TEAM, HOME_TEAM_ID),
    )


def test_same_clock_ordinary_trips_keep_their_own_final_resolution() -> None:
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        _free_throw("Free Throw 2 of 2", made=True, home_score=1),
        _free_throw("Free Throw 1 of 2", made=True, home_score=2),
        _free_throw("Free Throw 2 of 2", made=False, home_score=2),
    )

    first_id = "ft:0022500123:1:1"
    second_id = "ft:0022500123:3:1"
    assert (
        cast(FreeThrowTripStart, normalized.actions[0].signals[0]).resume_disposition
        is ResumeDisposition.OPPONENT_INBOUND
    )
    assert normalized.actions[1].signals[-1] == FreeThrowTripEnd(
        first_id, ResumeDisposition.OPPONENT_INBOUND
    )
    assert (
        cast(FreeThrowTripStart, normalized.actions[2].signals[0]).resume_disposition
        is ResumeDisposition.LIVE_REBOUND
    )
    assert normalized.actions[3].signals[-1] == FreeThrowTripEnd(
        second_id, ResumeDisposition.LIVE_REBOUND
    )


@pytest.mark.parametrize(
    "barrier",
    [
        _turnover(),
        _action(actionType="Timeout", subType="Regular", clock="PT09M59.00S"),
    ],
    ids=["turnover", "clock-change"],
)
def test_retained_special_trip_does_not_continue_across_a_procedure_boundary(
    barrier: JsonObject,
) -> None:
    normalized = _normalize(
        _free_throw("Free Throw Flagrant 1 of 2", made=False),
        barrier,
        _free_throw("Free Throw Flagrant 2 of 2", made=True, home_score=1),
    )

    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unresolved_free_throw_trip" and issue.sequence_number == 1
        for issue in normalized.issues
    )


def test_ft_placeholder_allows_safe_administration_before_the_next_attempt() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    timeout = _action(actionType="Timeout", subType="Regular")

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        timeout,
        _free_throw("Free Throw 2 of 2", made=True, home_score=1),
    )

    assert cast(Rebound, normalized.actions[1].signals[0]).disposition is (
        ReboundDisposition.FT_PLACEHOLDER
    )


def test_impossible_checkpoint_timeout_blocks_ft_placeholder_lookahead() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    corrupted_timeout = _action(
        actionType="Timeout",
        subType="Regular",
        scoreHome="3",
        scoreAway="0",
    )

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        corrupted_timeout,
        _free_throw("Free Throw 2 of 2", made=True, home_score=1),
    )

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)
    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)


def test_ft_placeholder_supports_an_explicit_same_ordinal_retry() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    violation = _action(teamId=AWAY_TEAM_ID, actionType="Violation", subType="Lane Violation")

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        violation,
        _free_throw("Free Throw 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    trip_id = "ft:0022500123:1:1"
    assert cast(Rebound, normalized.actions[1].signals[0]).disposition is (
        ReboundDisposition.FT_PLACEHOLDER
    )
    assert normalized.actions[2].signals == (Violation(AWAY_TEAM_ID, ViolationDisposition.RETRY),)
    assert normalized.actions[3].signals == (FreeThrowAttempt(trip_id, 1, 2, True, 1),)


def test_unsupported_violation_blocks_ft_placeholder_lookahead() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    unsupported_violation = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Violation",
        subType="Jump Ball Violation",
    )

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        unsupported_violation,
        _free_throw("Free Throw 2 of 2", made=True, home_score=1),
    )

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)
    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)


def test_non_neutral_lane_violation_cannot_authorize_an_ft_placeholder_retry() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    invalid_lane_violation = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Violation",
        subType="Lane Violation",
        shotResult="Made",
    )

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        invalid_lane_violation,
        _free_throw("Free Throw 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)
    assert normalized.actions[2].signals == (Unsupported(control_relevant=True),)


def test_control_barrier_stops_ft_placeholder_lookahead() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        rebound,
        _turnover(),
        _free_throw("Free Throw 2 of 2", made=True, home_score=1),
    )

    assert cast(Rebound, normalized.actions[1].signals[0]).disposition is (
        ReboundDisposition.AWARD_OPPONENT
    )


def test_team_rebound_after_a_final_miss_remains_a_live_control_award() -> None:
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    away_field_goal = _action(
        teamId=AWAY_TEAM_ID,
        personId=201,
        actionType="Missed Shot",
        subType="Jump Shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
        scoreHome="1",
        scoreAway="0",
        pointsTotal=0,
    )

    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=False, home_score=1),
        rebound,
        away_field_goal,
    )

    assert cast(Rebound, normalized.actions[2].signals[0]).disposition is (
        ReboundDisposition.RETAIN_TEAM
    )


@pytest.mark.parametrize(
    "actions",
    [
        (
            _free_throw("Free Throw 1 of 3", made=False),
            _free_throw("Free Throw 3 of 3", made=True, home_score=1),
        ),
        (
            _free_throw("Free Throw 1 of 2", made=False),
            _free_throw("Free Throw 1 of 2", made=True, home_score=1),
            _free_throw("Free Throw 2 of 2", made=True, home_score=2),
        ),
    ],
    ids=["ordinal-gap", "repetition-without-violation"],
)
def test_ordinary_ordinal_missing_or_repeated_ordinal(actions: tuple[JsonObject, ...]) -> None:
    normalized = _normalize(*actions)

    assert any(
        action.signals == (Unsupported(control_relevant=True),) for action in normalized.actions[1:]
    )
    assert any(
        issue.code in {"unknown_control_action", "unresolved_free_throw_trip"}
        for issue in normalized.issues
    )


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("subType", "Free Throw 4 of 4"),
        ("description", "Shooter Free Throw 1 of 2"),
        ("description", "MISS Shooter Free Throw 1 of 2 trailing"),
    ],
)
def test_malformed_free_throw_form_fails_closed(field: str, malformed_value: str) -> None:
    free_throw = _free_throw("Free Throw 1 of 2", made=False)
    free_throw[field] = malformed_value

    normalized = _normalize(free_throw)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert normalized.issues[0].code in {
        "unknown_control_action",
        "unresolved_free_throw_trip",
    }


@pytest.mark.parametrize(
    ("sub_types", "kind", "resume"),
    [
        (("Free Throw Technical",), FreeThrowKind.TECHNICAL, ResumeDisposition.RESUME_INTERRUPTED),
        (("Free Throw Flagrant 1 of 1",), FreeThrowKind.FLAGRANT, ResumeDisposition.RETAIN_TEAM),
        (
            ("Free Throw Clear Path 1 of 2", "Free Throw Clear Path 2 of 2"),
            FreeThrowKind.CLEAR_PATH,
            ResumeDisposition.RETAIN_TEAM,
        ),
        (
            ("Free Throw Away From Play 1 of 1",),
            FreeThrowKind.AWAY_FROM_PLAY,
            ResumeDisposition.RETAIN_TEAM,
        ),
        (
            ("Free Throw Transition Take 1 of 1",),
            FreeThrowKind.TRANSITION_TAKE,
            ResumeDisposition.RETAIN_TEAM,
        ),
    ],
)
def test_special_final_miss_uses_its_non_live_resume_disposition(
    sub_types: tuple[str, ...],
    kind: FreeThrowKind,
    resume: ResumeDisposition,
) -> None:
    actions = [
        _free_throw(
            sub_type,
            made=index < len(sub_types) - 1,
            home_score=min(index + 1, len(sub_types) - 1),
        )
        for index, sub_type in enumerate(sub_types)
    ]

    normalized = _normalize(*actions)

    start = cast(FreeThrowTripStart, normalized.actions[0].signals[0])
    end = cast(FreeThrowTripEnd, normalized.actions[-1].signals[-1])
    assert start.free_throw_kind is kind
    assert start.resume_disposition is resume
    assert end.resolution is resume


def test_two_attempt_technical_trip_resumes_the_parent_at_the_same_depth() -> None:
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        _free_throw("Free Throw Technical 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw Technical 2 of 2", made=False, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    parent_id = "ft:0022500123:1:1"
    technical_id = "ft:0022500123:2:2"
    assert (
        cast(FreeThrowTripStart, normalized.actions[1].signals[0]).interrupted_trip_id == parent_id
    )
    assert normalized.actions[2].signals[-1] == FreeThrowTripEnd(
        technical_id, ResumeDisposition.RESUME_INTERRUPTED
    )
    assert normalized.actions[3].signals[-1] == FreeThrowTripEnd(
        parent_id, ResumeDisposition.OPPONENT_INBOUND
    )


def test_double_lane_violation_retry_continues_the_same_trip() -> None:
    violation = _action(actionType="Violation", subType="Double Lane Violation")
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=False),
        violation,
        _free_throw("Free Throw 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=True, home_score=2),
    )

    trip_id = "ft:0022500123:1:1"
    assert normalized.actions[1].signals == (Violation(None, ViolationDisposition.RETRY),)
    assert normalized.actions[2].signals == (FreeThrowAttempt(trip_id, 1, 2, True, 1),)


def test_double_lane_violation_jump_ball_is_the_trip_resolution() -> None:
    violation = _action(actionType="Violation", subType="Double Lane Violation")
    jump_ball = _action(
        actionType="Jump Ball",
        subType="",
        description="Jump Ball Alpha vs. Beta: Tip to Gamma",
    )
    normalized = _normalize(
        _free_throw("Free Throw 1 of 2", made=True, home_score=1),
        _free_throw("Free Throw 2 of 2", made=False, home_score=1),
        violation,
        jump_ball,
    )

    trip_id = "ft:0022500123:1:1"
    assert (
        cast(FreeThrowTripStart, normalized.actions[0].signals[0]).resume_disposition
        is ResumeDisposition.JUMP_BALL
    )
    assert normalized.actions[1].signals[-1] == FreeThrowTripEnd(
        trip_id, ResumeDisposition.JUMP_BALL
    )
    assert normalized.actions[2].signals == (Violation(None, ViolationDisposition.JUMP_BALL),)
