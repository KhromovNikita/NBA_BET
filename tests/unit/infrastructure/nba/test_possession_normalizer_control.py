"""NBA PlayByPlayV3 control-event normalizer behavior tests."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from nba_live_totals.domain import (
    ActionConsumption,
    Administrative,
    Foul,
    Goaltending,
    GoaltendingSide,
    JsonObject,
    JumpBall,
    NormalizedGameActions,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer

from .possession_helpers import AWAY_TEAM_ID, HOME_TEAM_ID, load_json_fixture, make_snapshot


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


def _safe_timeout(*, clock: str) -> JsonObject:
    return _action(clock=clock, actionType="Timeout", subType="Regular")


def _missed_field_goal(
    *,
    action_number: int,
    clock: str,
    team_id: int = HOME_TEAM_ID,
    shot_value: int = 2,
) -> JsonObject:
    return _action(
        actionNumber=action_number,
        clock=clock,
        teamId=team_id,
        personId=101,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="",
        scoreHome=None,
        scoreAway=None,
        shotResult="Missed",
        isFieldGoal=1,
        pointsTotal=0,
        shotValue=shot_value,
    )


def _inherited_block(*, action_number: int, clock: str, shot_value: int = 2) -> JsonObject:
    return _action(
        actionNumber=action_number,
        clock=clock,
        teamId=AWAY_TEAM_ID,
        personId=202,
        actionType="",
        subType="",
        description="Defender BLOCK (1 BLK)",
        scoreHome=None,
        scoreAway=None,
        shotResult="",
        isFieldGoal=0,
        pointsTotal=0,
        shotValue=shot_value,
    )


def _missed_free_throw(
    *,
    sub_type: str,
    team_id: int = HOME_TEAM_ID,
    clock: str = "PT10M00.00S",
) -> JsonObject:
    return _action(
        clock=clock,
        teamId=team_id,
        personId=101,
        actionType="Free Throw",
        subType=sub_type,
        description=f"MISS Player {sub_type}",
    )


def test_player_rebound_uses_structured_identity_and_counter_delta_only_as_annotation() -> None:
    first = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Rebound",
        subType="Unknown",
        description="Player REBOUND (Off:0 Def:1)",
    )
    second = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Rebound",
        subType="Normal Rebound",
        description="Player REBOUND (Off:1 Def:1)",
    )

    normalized = _normalize(first, second)

    assert normalized.actions[0].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.PLAYER,
            provider_side=ProviderReboundSide.DEFENSIVE,
            disposition=ReboundDisposition.NORMAL_LIVE,
        ),
    )
    assert normalized.actions[1].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.PLAYER,
            provider_side=ProviderReboundSide.OFFENSIVE,
            disposition=ReboundDisposition.NORMAL_LIVE,
        ),
    )


@pytest.mark.parametrize(
    ("team_id", "person_id", "description"),
    [
        (0, HOME_TEAM_ID, "Names Never Select The Team Rebound"),
        (None, AWAY_TEAM_ID, "Different Display Name Rebound"),
    ],
)
def test_team_rebound_uses_person_team_identity_and_bounded_following_control(
    team_id: int | None,
    person_id: int,
    description: str,
) -> None:
    rebound = _action(
        teamId=team_id,
        personId=person_id,
        actionType="Rebound",
        subType="Unknown",
        description=description,
    )
    following_team = person_id
    following = _action(
        teamId=following_team,
        personId=999,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="structured shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, following)

    assert normalized.actions[0].signals == (
        Rebound(
            rebounding_team=person_id,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.RETAIN_TEAM,
        ),
    )


def test_team_rebound_marks_opponent_award_from_next_structured_control_team() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Normal Rebound",
        description="Untrusted Name Rebound",
    )
    following = _action(
        teamId=AWAY_TEAM_ID,
        personId=202,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="secret-opponent-award",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, following)

    assert normalized.actions[0].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.AWARD_OPPONENT,
        ),
    )


def test_unresolved_team_rebound_award_fails_closed_without_using_description_name() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="secret-name Rebound",
    )

    normalized = _normalize(rebound)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert [(issue.code, issue.sequence_number) for issue in normalized.issues] == [
        ("unknown_control_action", 1)
    ]
    assert "secret-name" not in repr(normalized)


def test_team_rebound_between_nonfinal_miss_and_next_attempt_is_ft_placeholder() -> None:
    first = _missed_free_throw(sub_type="Free Throw 1 of 2")
    rebound = _action(
        teamId=0,
        personId=AWAY_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Display Team Rebound",
    )
    second = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Free Throw",
        subType="Free Throw 2 of 2",
        description="Player Free Throw 2 of 2 (2 PTS)",
    )

    normalized = _normalize(first, rebound, second)

    assert normalized.actions[1].signals == (
        Rebound(
            rebounding_team=AWAY_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.FT_PLACEHOLDER,
        ),
    )
    assert normalized.actions[1].consumption is ActionConsumption.ANNOTATION


@pytest.mark.parametrize(
    ("team_id", "person_id", "description"),
    [
        (0, 404, "Unknown Team Rebound"),
        (HOME_TEAM_ID, HOME_TEAM_ID, "Home REBOUND (Off:1 Def:0)"),
        (None, 404, "Unknown Team Rebound"),
        (HOME_TEAM_ID, 0, "Player REBOUND (Off:1 Def:0)"),
    ],
)
def test_rebound_rejects_noncanonical_structured_identity(
    team_id: int | None,
    person_id: int,
    description: str,
) -> None:
    normalized = _normalize(
        _action(
            teamId=team_id,
            personId=person_id,
            actionType="Rebound",
            subType="Unknown",
            description=description,
        )
    )

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


@pytest.mark.parametrize(
    ("sub_type", "turnover_kind"),
    [
        ("bad pass", "bad_pass"),
        ("lost ball", "lost_ball"),
        ("traveling", "traveling"),
        ("offensive foul turnover", "offensive_foul_turnover"),
        ("double dribble", "double_dribble"),
        ("discontinue dribble", "discontinue_dribble"),
        ("3 second violation", "three_second_violation"),
        ("5 second violation", "five_second_violation"),
        ("8 second violation", "eight_second_violation"),
        ("shot clock violation", "shot_clock_violation"),
        ("shot clock", "shot_clock"),
        ("inbound turnover", "inbound_turnover"),
        ("backcourt turnover", "backcourt_turnover"),
        ("backcourt", "backcourt"),
        ("lane violation", "lane_violation"),
        ("kicked ball violation", "kicked_ball_violation"),
        ("palming", "palming"),
        ("5 second inbound", "five_second_inbound"),
        ("step out of bounds", "step_out_of_bounds"),
        ("out of bounds lost ball turnover", "out_of_bounds_lost_ball_turnover"),
        ("out of bounds - bad pass turnover", "out_of_bounds_bad_pass_turnover"),
    ],
)
def test_turnover_exact_allowlist_maps_to_bounded_kinds(
    sub_type: str,
    turnover_kind: str,
) -> None:
    normalized = _normalize(
        _action(
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType=" Turnover ",
            subType=f" {sub_type.upper()} ",
            description="turnover-secret",
        )
    )

    assert normalized.actions[0].signals == (
        Turnover(
            committing_team=HOME_TEAM_ID,
            gained_by=AWAY_TEAM_ID,
            turnover_kind=turnover_kind,
        ),
    )


@pytest.mark.parametrize("sub_type", ["bad pass extra", "travel", "5 seconds", ""])
def test_turnover_near_matches_fail_closed(sub_type: str) -> None:
    normalized = _normalize(
        _action(
            teamId=HOME_TEAM_ID,
            actionType="Turnover",
            subType=sub_type,
            description="near-match-secret",
        )
    )

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert "near-match-secret" not in repr(normalized)


@pytest.mark.parametrize(
    ("sub_type", "foul_kind", "shooting_team"),
    [
        ("personal", "personal", None),
        ("shooting", "shooting", AWAY_TEAM_ID),
        ("loose ball", "loose_ball", None),
        ("intentional", "intentional", None),
        ("away from play", "away_from_play", None),
        ("clear path", "clear_path", None),
        ("double technical", "double_technical", None),
        ("technical", "technical", None),
        ("flagrant 1", "flagrant_1", None),
        ("flagrant type 1", "flagrant_type_1", None),
        ("flagrant 2", "flagrant_2", None),
        ("flagrant type 2", "flagrant_type_2", None),
        ("defense 3 second", "defense_three_second", None),
        ("taunting", "taunting", None),
        ("excess timeout", "excess_timeout", None),
        ("block", "block", None),
        ("personal take", "personal_take", None),
        ("shooting block", "shooting_block", AWAY_TEAM_ID),
    ],
)
def test_foul_exact_allowlist_is_administrative_context(
    sub_type: str,
    foul_kind: str,
    shooting_team: int | None,
) -> None:
    normalized = _normalize(
        _action(
            teamId=HOME_TEAM_ID,
            personId=101,
            actionType="Foul",
            subType=sub_type,
            description="foul-secret",
        )
    )

    assert normalized.actions[0].signals == (
        Foul(
            committing_team=HOME_TEAM_ID,
            foul_kind=foul_kind,
            shooting_team=shooting_team,
            related_sequence_number=None,
        ),
    )
    assert normalized.actions[0].consumption is ActionConsumption.ADMINISTRATIVE


@pytest.mark.parametrize("sub_type", ["personal foul", "flagrant", "blocking", "unknown"])
def test_foul_near_matches_fail_closed(sub_type: str) -> None:
    normalized = _normalize(_action(teamId=HOME_TEAM_ID, actionType="Foul", subType=sub_type))

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


@pytest.mark.parametrize("sub_type", ["offensive", "offensive charge", "charge"])
def test_offensive_foul_links_to_unique_same_clock_offensive_turnover(sub_type: str) -> None:
    foul = _action(
        teamId=HOME_TEAM_ID,
        actionType="Foul",
        subType=sub_type,
        description="offensive-foul-secret",
    )
    turnover = _action(
        teamId=HOME_TEAM_ID,
        actionType="Turnover",
        subType="Offensive Foul Turnover",
        description="linked-turnover-secret",
    )

    normalized = _normalize(foul, turnover)

    assert normalized.actions[0].signals == (
        Foul(
            committing_team=HOME_TEAM_ID,
            foul_kind=sub_type.replace(" ", "_"),
            shooting_team=None,
            related_sequence_number=2,
        ),
    )
    assert "offensive-foul-secret" not in repr(normalized)


def test_unlinked_or_ambiguous_offensive_foul_is_an_error_without_guessing() -> None:
    foul = _action(teamId=HOME_TEAM_ID, actionType="Foul", subType="Offensive")
    one = _action(
        teamId=HOME_TEAM_ID,
        actionType="Turnover",
        subType="Offensive Foul Turnover",
    )
    two = deepcopy(one)
    orphan = _normalize(foul)
    ambiguous = _normalize(foul, one, two)

    assert orphan.actions[0].signals == (
        Foul(HOME_TEAM_ID, "offensive", related_sequence_number=None),
    )
    assert ambiguous.actions[0].signals == (
        Foul(HOME_TEAM_ID, "offensive", related_sequence_number=None),
    )
    assert any(issue.code == "unknown_control_action" for issue in orphan.issues)
    assert any(issue.code == "unknown_control_action" for issue in ambiguous.issues)


def test_steal_links_to_unique_preceding_same_clock_opponent_turnover() -> None:
    turnover = _action(
        actionNumber=77,
        teamId=HOME_TEAM_ID,
        actionType="Turnover",
        subType="Bad Pass",
    )
    steal = _action(
        actionNumber=77,
        teamId=AWAY_TEAM_ID,
        personId=202,
        actionType="",
        subType="",
        description="Defender STEAL (1 STL)",
    )

    normalized = _normalize(turnover, steal)

    assert normalized.actions[1].signals == (
        StealAnnotation(team=AWAY_TEAM_ID, related_sequence_number=1),
    )
    assert normalized.actions[1].consumption is ActionConsumption.ANNOTATION


@pytest.mark.parametrize("case", ["orphan", "wrong_clock", "same_team", "ambiguous"])
def test_orphan_or_ambiguous_steal_remains_annotation_and_never_switches_control(case: str) -> None:
    steal = _action(
        teamId=AWAY_TEAM_ID,
        actionType="",
        subType="",
        description="Defender STEAL (2 STL)",
    )
    actions: list[JsonObject]
    if case == "orphan":
        actions = [steal]
    else:
        turnover = _action(
            teamId=HOME_TEAM_ID if case != "same_team" else AWAY_TEAM_ID,
            actionType="Turnover",
            subType="Lost Ball",
            clock="PT4M59.00S" if case == "wrong_clock" else "PT10M00.00S",
        )
        actions = [turnover, steal]
        if case == "ambiguous":
            actions.insert(1, deepcopy(turnover))

    normalized = _normalize(*actions)
    annotation = normalized.actions[-1]

    assert annotation.signals == (StealAnnotation(team=AWAY_TEAM_ID, related_sequence_number=None),)
    assert annotation.consumption is ActionConsumption.ANNOTATION
    assert any(
        issue.code == "orphan_steal_annotation"
        and issue.sequence_number == annotation.sequence_number
        for issue in normalized.issues
    )


@pytest.mark.parametrize("intervening_kind", ["missed_shot", "unknown_turnover", "unknown_action"])
def test_steal_does_not_link_through_an_intervening_control_event(
    intervening_kind: str,
) -> None:
    turnover = _action(
        teamId=HOME_TEAM_ID,
        actionType="Turnover",
        subType="Bad Pass",
    )
    if intervening_kind == "missed_shot":
        intervening = _action(
            teamId=AWAY_TEAM_ID,
            personId=202,
            actionType="Missed Shot",
            subType="Jump Shot",
            description="structured intervening shot",
            shotResult="Missed",
            isFieldGoal=1,
            shotValue=2,
        )
    elif intervening_kind == "unknown_turnover":
        intervening = _action(
            teamId=AWAY_TEAM_ID,
            actionType="Turnover",
            subType="unlisted turnover subtype",
        )
    else:
        intervening = _action(
            teamId=AWAY_TEAM_ID,
            actionType="unlisted control action",
            subType="unlisted control subtype",
        )
    steal = _action(
        teamId=AWAY_TEAM_ID,
        personId=202,
        actionType="",
        subType="",
        description="Defender STEAL (3 STL)",
    )

    normalized = _normalize(turnover, intervening, steal)

    assert normalized.actions[2].signals == (
        StealAnnotation(team=AWAY_TEAM_ID, related_sequence_number=None),
    )
    assert any(
        issue.code == "orphan_steal_annotation" and issue.sequence_number == 3
        for issue in normalized.issues
    )


@pytest.mark.parametrize("barrier_kind", ["player_rebound", "unknown_control", "jump_ball"])
def test_team_rebound_does_not_look_through_an_intervening_control_barrier(
    barrier_kind: str,
) -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Team Rebound",
    )
    if barrier_kind == "player_rebound":
        barrier = _action(
            teamId=AWAY_TEAM_ID,
            personId=202,
            actionType="Rebound",
            subType="Normal Rebound",
            description="Player REBOUND (Off:1 Def:0)",
        )
    elif barrier_kind == "unknown_control":
        barrier = _action(
            teamId=AWAY_TEAM_ID,
            actionType="unlisted control action",
            subType="unlisted control subtype",
        )
    else:
        barrier = _action(
            teamId=0,
            personId=0,
            actionType="Jump Ball",
            subType="",
            description="Jump Ball A vs. B: Tip to C",
        )
    following = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="structured following shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, barrier, following)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


def test_team_rebound_can_look_through_an_explicit_safe_administrative_action() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Team Rebound",
    )
    timeout = _action(actionType="Timeout", subType="Regular")
    following = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="structured following shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, timeout, following)

    assert normalized.actions[0].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.RETAIN_TEAM,
        ),
    )


def test_live_shaped_inherited_blocks_and_cross_clock_team_rebounds_are_structural() -> None:
    prefix = [_safe_timeout(clock=f"PT11M{59 - index:02d}.00S") for index in range(16)]
    miss_17 = _missed_field_goal(action_number=170, clock="PT10M12.00S")
    block_18 = _inherited_block(action_number=170, clock="PT10M12.00S")
    rebound_19 = _action(
        clock="PT10M12.00S",
        teamId=None,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Home Rebound",
    )
    control_20 = _missed_field_goal(action_number=200, clock="PT10M09.00S")
    filler_21 = _safe_timeout(clock="PT10M08.00S")
    miss_22 = _missed_field_goal(action_number=220, clock="PT10M07.00S")
    block_23 = _inherited_block(action_number=220, clock="PT10M07.00S")
    rebound_24 = _action(
        clock="PT10M04.00S",
        teamId=None,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Home Rebound",
    )
    foul_25 = _action(
        clock="PT9M59.00S",
        teamId=AWAY_TEAM_ID,
        personId=202,
        actionType="Foul",
        subType="Personal",
        description="",
    )
    control_26 = _missed_field_goal(action_number=260, clock="PT9M53.00S")

    normalized = _normalize(
        *prefix,
        miss_17,
        block_18,
        rebound_19,
        control_20,
        filler_21,
        miss_22,
        block_23,
        rebound_24,
        foul_25,
        control_26,
    )

    assert normalized.actions[17].signals == (Administrative("block"),)
    assert normalized.actions[18].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.RETAIN_TEAM,
        ),
    )
    assert normalized.actions[22].signals == (Administrative("block"),)
    assert normalized.actions[23].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.RETAIN_TEAM,
        ),
    )
    assert not any(issue.severity.value == "error" for issue in normalized.issues)


def test_live_shaped_sequence_four_block_links_only_to_immediate_sequence_three_miss() -> None:
    normalized = _normalize(
        _safe_timeout(clock="PT11M59.00S"),
        _safe_timeout(clock="PT11M58.00S"),
        _missed_field_goal(action_number=30, clock="PT11M57.00S"),
        _inherited_block(action_number=30, clock="PT11M57.00S"),
    )

    assert normalized.actions[3].signals == (Administrative("block"),)
    assert not any(issue.severity.value == "error" for issue in normalized.issues)


@pytest.mark.parametrize(
    ("target", "updates"),
    [
        ("block", {"shotValue": 3}),
        ("block", {"actionNumber": 31}),
        ("block", {"clock": "PT11M56.00S"}),
        ("block", {"pointsTotal": 1}),
        ("block", {"scoreHome": "0"}),
        ("block", {"scoreAway": "0"}),
        ("block", {"shotResult": "Missed"}),
        ("block", {"isFieldGoal": 1}),
        ("miss", {"shotResult": "Made", "actionType": "Made Shot"}),
        ("miss", {"actionType": "Timeout", "subType": "Regular", "isFieldGoal": 0}),
    ],
)
def test_inherited_block_shot_value_fails_closed_without_exact_missed_fg_pair(
    target: str,
    updates: dict[str, object],
) -> None:
    miss = _missed_field_goal(action_number=30, clock="PT11M57.00S")
    block = _inherited_block(action_number=30, clock="PT11M57.00S")
    (miss if target == "miss" else block).update(cast(JsonObject, updates))

    normalized = _normalize(miss, block)

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unknown_control_action" and issue.sequence_number == 2
        for issue in normalized.issues
    )


@pytest.mark.parametrize("case", ["no_pair", "not_immediate", "out_of_range_value"])
def test_inherited_block_without_one_exact_predecessor_fails_closed(case: str) -> None:
    block = _inherited_block(action_number=30, clock="PT11M57.00S")
    if case == "no_pair":
        actions = [block]
    elif case == "not_immediate":
        actions = [
            _missed_field_goal(action_number=30, clock="PT11M57.00S"),
            _safe_timeout(clock="PT11M57.00S"),
            block,
        ]
    else:
        block["shotValue"] = 4
        actions = [_missed_field_goal(action_number=30, clock="PT11M57.00S"), block]

    normalized = _normalize(*actions)

    assert normalized.actions[-1].signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unknown_control_action"
        and issue.sequence_number == normalized.actions[-1].sequence_number
        for issue in normalized.issues
    )


@pytest.mark.parametrize(
    "barrier_kind",
    ["period_boundary", "player_rebound", "jump_ball", "unknown", "invalid_fg", "team_rebound"],
)
def test_cross_clock_team_rebound_scan_stops_at_any_control_barrier(barrier_kind: str) -> None:
    rebound = _action(
        clock="PT10M12.00S",
        teamId=None,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Home Rebound",
    )
    if barrier_kind == "period_boundary":
        barrier = _action(
            period=2,
            clock="PT12M00.00S",
            actionType="period",
            subType="start",
        )
    elif barrier_kind == "player_rebound":
        barrier = _action(
            clock="PT10M10.00S",
            teamId=AWAY_TEAM_ID,
            personId=202,
            actionType="Rebound",
            subType="Normal Rebound",
            description="Player REBOUND (Off:0 Def:1)",
        )
    elif barrier_kind == "jump_ball":
        barrier = _action(
            clock="PT10M10.00S",
            actionType="Jump Ball",
            subType="",
            description="Jump Ball A vs. B: Tip to C",
        )
    elif barrier_kind == "unknown":
        barrier = _action(
            clock="PT10M10.00S",
            actionType="Unknown Control",
            subType="Unknown",
        )
    elif barrier_kind == "invalid_fg":
        barrier = _missed_field_goal(action_number=2, clock="PT10M10.00S")
        barrier["shotValue"] = 4
    else:
        barrier = _action(
            clock="PT10M10.00S",
            teamId=None,
            personId=AWAY_TEAM_ID,
            actionType="Rebound",
            subType="Unknown",
            description="Away Rebound",
        )
    following = _missed_field_goal(action_number=3, clock="PT10M09.00S")

    normalized = _normalize(rebound, barrier, following)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


def test_team_rebound_does_not_award_from_an_invalid_turnover() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Team Rebound",
    )
    invalid_turnover = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Turnover",
        subType="Bad Pass",
        pointsTotal=3,
    )

    normalized = _normalize(rebound, invalid_turnover)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


def test_team_rebound_can_look_through_a_valid_instant_replay() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Team Rebound",
    )
    replay = _action(actionType="Instant Replay", subType="Replay Center")
    following = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="structured following shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, replay, following)

    assert normalized.actions[0].signals == (
        Rebound(
            rebounding_team=HOME_TEAM_ID,
            actor=ReboundActor.TEAM,
            provider_side=ProviderReboundSide.UNKNOWN,
            disposition=ReboundDisposition.RETAIN_TEAM,
        ),
    )


def test_team_rebound_does_not_look_through_a_correction_relevant_replay() -> None:
    rebound = _action(
        teamId=0,
        personId=HOME_TEAM_ID,
        actionType="Rebound",
        subType="Unknown",
        description="Team Rebound",
    )
    replay = _action(
        actionType="Instant Replay",
        subType="Replay Center",
        scoreHome="2",
        pointsTotal=0,
    )
    following = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Missed Shot",
        subType="Jump Shot",
        description="structured following shot",
        shotResult="Missed",
        isFieldGoal=1,
        shotValue=2,
    )

    normalized = _normalize(rebound, replay, following)

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


def test_jump_ball_accepts_only_anchored_shape_and_never_maps_names_to_winner() -> None:
    jump = _action(
        teamId=HOME_TEAM_ID,
        personId=101,
        actionType="Jump Ball",
        subType="Recovered",
        description="Jump Ball Home Jumper vs. Away Jumper: Tip to Home Recipient",
    )

    normalized = _normalize(jump)

    assert normalized.actions[0].signals == (JumpBall(winner_team=None),)
    assert normalized.actions[0].consumption is ActionConsumption.TRANSITION


@pytest.mark.parametrize(
    "updates",
    [
        {"description": "prefix Jump Ball A vs. B: Tip to C"},
        {"description": "Jump Ball A vs B: Tip to C"},
        {"description": "Jump Ball A vs. B: Tip to C: trailing"},
        {"subType": "Tip"},
        {"teamId": 404},
    ],
)
def test_jump_ball_near_matches_or_unknown_identity_fail_closed(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "teamId": HOME_TEAM_ID,
        "actionType": "Jump Ball",
        "subType": "",
        "description": "Jump Ball A vs. B: Tip to C",
    }
    values.update(updates)
    normalized = _normalize(_action(**values))

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


@pytest.mark.parametrize("sub_type", ["delay of game", "kicked ball", "kicked ball violation"])
def test_retained_defensive_violations_are_exact_annotations(sub_type: str) -> None:
    normalized = _normalize(
        _action(
            teamId=AWAY_TEAM_ID,
            actionType="Violation",
            subType=sub_type,
            description="violation-secret",
        )
    )

    assert normalized.actions[0].signals == (
        Violation(team=AWAY_TEAM_ID, disposition=ViolationDisposition.RETAIN_OFFENSE),
    )
    assert normalized.actions[0].consumption is ActionConsumption.ANNOTATION


def test_defensive_goaltending_uses_exact_opponent_score_delta() -> None:
    goaltend = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Violation",
        subType="Defensive Goaltending",
        scoreHome="2",
        scoreAway="0",
        pointsTotal=2,
        description="goaltend-secret",
    )

    normalized = _normalize(goaltend)

    assert normalized.actions[0].signals == (
        Goaltending(
            team=AWAY_TEAM_ID,
            side=GoaltendingSide.DEFENSIVE,
            awarded_points=2,
        ),
    )
    assert normalized.actions[0].checkpoint_combined_score == 2


@pytest.mark.parametrize(
    ("score_home", "score_away", "points_total"),
    [("1", "0", 1), ("0", "2", 2), ("3", "0", 2), ("2", "0", 0)],
)
def test_defensive_goaltending_rejects_non_opponent_or_inexact_score_delta(
    score_home: str,
    score_away: str,
    points_total: int,
) -> None:
    normalized = _normalize(
        _action(
            teamId=AWAY_TEAM_ID,
            actionType="Violation",
            subType="Defensive Goaltending",
            scoreHome=score_home,
            scoreAway=score_away,
            pointsTotal=points_total,
        )
    )

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)


def test_offensive_goaltending_emits_score_effect_before_turnover() -> None:
    normalized = _normalize(
        _action(
            teamId=HOME_TEAM_ID,
            actionType="Turnover",
            subType="Offensive Goaltending",
            description="offensive-goaltend-secret",
        )
    )

    assert normalized.actions[0].signals == (
        Goaltending(HOME_TEAM_ID, GoaltendingSide.OFFENSIVE, 0),
        Turnover(HOME_TEAM_ID, AWAY_TEAM_ID, "offensive_goaltending"),
    )


def test_defensive_lane_violation_is_retry_only_with_same_ordinal_attempt() -> None:
    first = _missed_free_throw(sub_type="Free Throw 1 of 2")
    violation = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Violation",
        subType="Lane Violation",
    )
    retry = _missed_free_throw(sub_type="Free Throw 1 of 2")

    normalized = _normalize(first, violation, retry)

    assert normalized.actions[1].signals == (
        Violation(team=AWAY_TEAM_ID, disposition=ViolationDisposition.RETRY),
    )


@pytest.mark.parametrize(
    ("first_updates", "retry_updates"),
    [
        ({}, {"description": "unanchored retry description"}),
        ({"teamId": 404}, {"teamId": 404}),
        ({}, {"shotResult": "Made"}),
        ({}, {"pointsTotal": 3}),
    ],
)
def test_lane_violation_retry_rejects_invalid_free_throw_context(
    first_updates: dict[str, object],
    retry_updates: dict[str, object],
) -> None:
    first = _missed_free_throw(sub_type="Free Throw 1 of 2")
    first.update(cast(JsonObject, first_updates))
    violation = _action(
        teamId=AWAY_TEAM_ID,
        actionType="Violation",
        subType="Lane Violation",
    )
    retry = _missed_free_throw(sub_type="Free Throw 1 of 2")
    retry.update(cast(JsonObject, retry_updates))

    normalized = _normalize(first, violation, retry)

    assert normalized.actions[1].signals == (Unsupported(control_relevant=True),)


def test_offensive_lane_violation_requires_final_miss_and_turnover_pair() -> None:
    final_miss = _missed_free_throw(sub_type="Free Throw 2 of 2")
    violation = _action(
        teamId=HOME_TEAM_ID,
        actionType="Violation",
        subType="Lane Violation",
    )
    turnover = _action(
        teamId=HOME_TEAM_ID,
        actionType="Turnover",
        subType="Lane Violation",
    )

    normalized = _normalize(final_miss, violation, turnover)

    assert normalized.actions[1].signals == (
        Violation(team=HOME_TEAM_ID, disposition=ViolationDisposition.OPPONENT_AWARD),
    )


@pytest.mark.parametrize("next_kind", ["retry", "jump"])
def test_double_lane_violation_requires_explicit_retry_or_jump(next_kind: str) -> None:
    first = _missed_free_throw(sub_type="Free Throw 1 of 2")
    violation = _action(
        teamId=0,
        personId=0,
        actionType="Violation",
        subType="Double Lane Violation",
    )
    if next_kind == "retry":
        following = _missed_free_throw(sub_type="Free Throw 1 of 2")
        disposition = ViolationDisposition.RETRY
    else:
        following = _action(
            teamId=0,
            personId=0,
            actionType="Jump Ball",
            subType="",
            description="Jump Ball A vs. B: Tip to C",
        )
        disposition = ViolationDisposition.JUMP_BALL

    normalized = _normalize(first, violation, following)

    assert normalized.actions[1].signals == (Violation(team=None, disposition=disposition),)


def test_jump_ball_violation_requires_following_explicit_jump_ball() -> None:
    violation = _action(
        teamId=HOME_TEAM_ID,
        actionType="Violation",
        subType="Jump Ball Violation",
    )
    jump = _action(
        teamId=0,
        personId=0,
        actionType="Jump Ball",
        subType="",
        description="Jump Ball A vs. B: Tip to C",
    )

    normalized = _normalize(violation, jump)

    assert normalized.actions[0].signals == (
        Violation(team=HOME_TEAM_ID, disposition=ViolationDisposition.JUMP_BALL),
    )


@pytest.mark.parametrize("sub_type", ["double lane violation", "jump ball violation"])
def test_jump_dispositions_reject_malformed_following_jump_ball(sub_type: str) -> None:
    if sub_type == "double lane violation":
        preceding = _missed_free_throw(sub_type="Free Throw 1 of 2")
        violation = _action(
            teamId=0,
            personId=0,
            actionType="Violation",
            subType=sub_type,
        )
    else:
        preceding = _action(
            teamId=HOME_TEAM_ID,
            actionType="Violation",
            subType=sub_type,
        )
        violation = None
    malformed_jump = _action(
        teamId=0,
        personId=0,
        actionType="Jump Ball",
        subType="Tip",
        description="not an anchored jump ball",
    )

    normalized = (
        _normalize(preceding, violation, malformed_jump)
        if violation is not None
        else _normalize(preceding, malformed_jump)
    )
    violation_index = 1 if violation is not None else 0

    assert normalized.actions[violation_index].signals == (Unsupported(control_relevant=True),)


@pytest.mark.parametrize(
    "sub_type",
    [
        "coach challenge support ruling",
        "coach challenge overturn ruling",
        "support ruling",
        "overturn ruling",
        "replay center",
        "review",
        "",
    ],
)
def test_replay_exact_forms_are_administrative_when_score_and_control_are_unchanged(
    sub_type: str,
) -> None:
    normalized = _normalize(
        _action(
            actionType="Instant Replay",
            subType=sub_type,
            description="replay-secret",
        )
    )

    assert normalized.actions[0].signals == (Administrative("instant_replay"),)


def test_unexplained_replay_score_correction_fails_closed_without_raw_text() -> None:
    normalized = _normalize(
        _action(
            actionType="Instant Replay",
            subType="Overturn Ruling",
            scoreHome="2",
            scoreAway="0",
            pointsTotal=0,
            description="retroactive-secret",
        )
    )

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
    assert "retroactive-secret" not in repr(normalized)


@pytest.mark.parametrize(
    ("sub_type", "actions"),
    [
        ("lane violation", []),
        ("double lane violation", []),
        ("jump ball violation", []),
        ("delay of games", []),
    ],
)
def test_ambiguous_violation_dispositions_fail_closed(
    sub_type: str,
    actions: list[JsonObject],
) -> None:
    normalized = _normalize(
        _action(teamId=HOME_TEAM_ID, actionType="Violation", subType=sub_type),
        *actions,
    )

    assert normalized.actions[0].signals == (Unsupported(control_relevant=True),)
