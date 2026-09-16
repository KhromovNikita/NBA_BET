"""NBA V1 substitution classification contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from nba_live_totals.domain.lineups import InvalidTeamTransition, NoLineupChange, Substitute
from nba_live_totals.infrastructure.nba.lineup_normalizer import (
    NBAPlayByPlayV3LineupNormalizer,
)

from .lineup_helpers import fixture, make_snapshot


def _substitution_snapshot(
    *,
    action_type: str = "Substitution",
    subtype: str = "out",
    description: str = "SUB: Bench Six FOR Starter One",
    eligible_collision: bool = False,
    ambiguous_collision: bool = False,
    person_id: int = 101,
    player_name: str = "Starter One",
    team_id: int = 10,
    incoming_first_name: str = "Bench",
    incoming_family_name: str = "Six",
    incoming_played: str = "1",
    incoming_comment: str = "",
    incoming_only_on_away_team: bool = False,
) -> Any:
    box = cast(Any, deepcopy(fixture("lineups/box_score_v1.json")))
    home = box["boxScoreTraditional"]["homeTeam"]
    player = home["players"][0]
    player["personId"] = 101
    player["firstName"] = "Starter"
    player["familyName"] = "One"
    player["playerName"] = "Starter One"
    player["position"] = "G"
    player["starter"] = "1"
    player["played"] = "1"
    player["comment"] = ""
    existing_bench = home["players"][1]
    existing_bench.update(
        firstName="Bench" if eligible_collision or ambiguous_collision else "Two",
        familyName="Six" if eligible_collision or ambiguous_collision else "Player",
        starter="0" if eligible_collision else "1",
        position="" if eligible_collision else "G",
        played="0" if eligible_collision else "1",
        comment="DNP - Coach's Decision" if eligible_collision else "",
        statistics={} if eligible_collision else deepcopy(player["statistics"]),
    )
    for player_id, first_name, family_name, starter in (
        (
            103,
            "Bench" if ambiguous_collision else "Three",
            "Six" if ambiguous_collision else "Player",
            "1",
        ),
        (104, "Four", "Player", "1"),
        (105, "Five", "Player", "1"),
        (106, incoming_first_name, incoming_family_name, "0"),
    ):
        clone = deepcopy(player)
        clone.update(
            personId=player_id,
            firstName=first_name,
            familyName=family_name,
            starter=starter,
            position="G" if starter == "1" else "",
            played=incoming_played if player_id == 106 else "1",
            comment=incoming_comment if player_id == 106 else "",
        )
        home["players"].append(clone)
    away = box["boxScoreTraditional"]["awayTeam"]
    template = deepcopy(away["players"][0])
    for player_id in (202, 203, 204, 205):
        clone = deepcopy(template)
        clone.update(
            personId=player_id,
            firstName="Bench"
            if incoming_only_on_away_team and player_id == 202
            else f"Away{player_id}",
            familyName="Six" if incoming_only_on_away_team and player_id == 202 else "Player",
        )
        away["players"].append(clone)
    pbp = cast(Any, deepcopy(fixture("lineups/play_by_play_v1.json")))
    action = pbp["game"]["actions"][0]
    action.update(
        actionType=action_type,
        subType=subtype,
        teamId=team_id,
        personId=person_id,
        playerName=player_name,
        description=description,
    )
    return make_snapshot(box_payload=box, pbp_payload=pbp, include_substitutions=True)


def test_supported_substitution_emits_id_only_transition() -> None:
    snapshot = _substitution_snapshot()
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert isinstance(normalized.events[0].transition, Substitute)
    assert normalized.events[0].transition == Substitute(10, 101, 106)


def test_non_substitution_and_administrative_events_are_no_change() -> None:
    snapshot = _substitution_snapshot(action_type="timeout", subtype="", description="timeout")
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert isinstance(normalized.events[0].transition, NoLineupChange)


def test_in_subtype_is_unsupported_even_when_provider_accepts_it() -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(_substitution_snapshot(subtype="in"))
    assert normalized.events[0].transition == InvalidTeamTransition(
        10, "unsupported_substitution_form"
    )


def test_one_eligible_incoming_wins_over_same_alias_for_dnp_player() -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(eligible_collision=True)
    )
    assert normalized.events[0].transition == Substitute(10, 101, 106)


def test_multiple_eligible_incoming_aliases_are_ambiguous() -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(ambiguous_collision=True)
    )
    assert normalized.events[0].transition == InvalidTeamTransition(10, "ambiguous_incoming_player")


def test_malformed_substitution_is_safe_and_does_not_leak_description() -> None:
    marker = "raw-provider-secret"
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description=marker)
    )
    transition = normalized.events[0].transition
    assert transition == InvalidTeamTransition(10, "unsupported_substitution_form")
    assert marker not in repr(transition)


def test_outgoing_identity_failures_are_safe_and_localized() -> None:
    cases = (
        (dict(person_id=0), "missing_outgoing_player_id", 10),
        (dict(person_id=201, player_name="LeBron James"), "roster_team_mismatch", 10),
        (dict(player_name="Not Starter"), "outgoing_label_mismatch", 10),
        (dict(description="SUB: Bench Six FOR Two Player"), "outgoing_label_mismatch", 10),
        (dict(team_id=0), "unknown_substitution_team", None),
    )
    for values, code, team_id in cases:
        normalized = NBAPlayByPlayV3LineupNormalizer().normalize(_substitution_snapshot(**values))
        assert normalized.events[0].transition == InvalidTeamTransition(team_id, code)


def test_incoming_unknown_and_fullmatch_junk_are_safe() -> None:
    unknown = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description="SUB: Nobody FOR Starter One")
    )
    assert unknown.events[0].transition == InvalidTeamTransition(10, "unknown_incoming_player")

    junk = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description="prefix SUB: Bench Six FOR Starter One")
    )
    assert junk.events[0].transition == InvalidTeamTransition(10, "unsupported_substitution_form")


def test_unique_dnp_incoming_is_ineligible() -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(incoming_played="0", incoming_comment="DNP - Coach's Decision")
    )
    assert normalized.events[0].transition == InvalidTeamTransition(
        10, "ineligible_incoming_player"
    )


def test_incoming_label_only_on_the_other_team_is_localized_unknown() -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(
            incoming_first_name="Home",
            incoming_family_name="Reserve",
            incoming_only_on_away_team=True,
        )
    )
    assert normalized.events[0].transition == InvalidTeamTransition(10, "unknown_incoming_player")


@pytest.mark.parametrize(
    "action_type, subtype", [("  sUbStItUtIoN  ", ""), ("SUBSTITUTION", " Out ")]
)
def test_trimmed_casefolded_action_type_accepts_empty_and_out_subtypes(
    action_type: str, subtype: str
) -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(action_type=action_type, subtype=subtype)
    )
    assert normalized.events[0].transition == Substitute(10, 101, 106)


@pytest.mark.parametrize(
    "description",
    (
        "SUB: \uff22\uff45\uff4e\uff43\uff48\u3000\uff33\uff49\uff58 FOR Starter One",
        "SUB: Bench   Six FOR Starter One",
        "SUB: B.e,nch Six FOR Starter One",
    ),
)
def test_finite_aliases_accept_nfkc_spaces_and_dot_comma_variants(description: str) -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description=description)
    )
    assert normalized.events[0].transition == Substitute(10, 101, 106)


@pytest.mark.parametrize(
    "description, incoming_first_name, incoming_family_name",
    (
        ("SUB: N ONeal FOR Starter One", "N", "O'Neal"),
        ("SUB: Jean Pierre Dupont FOR Starter One", "Jean", "Pierre-Dupont"),
        ("SUB: Six Bench FOR Starter One", "Bench", "Six"),
    ),
)
def test_finite_aliases_reject_deletion_and_reordered_labels(
    description: str, incoming_first_name: str, incoming_family_name: str
) -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(
            description=description,
            incoming_first_name=incoming_first_name,
            incoming_family_name=incoming_family_name,
        )
    )
    assert normalized.events[0].transition == InvalidTeamTransition(10, "unknown_incoming_player")


@pytest.mark.parametrize("control", ("\t", "\n"))
def test_controls_are_rejected_before_whitespace_normalization(control: str) -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description=f"SUB: Bench{control}Six FOR Starter One")
    )
    assert normalized.events[0].transition == InvalidTeamTransition(
        10, "unsupported_substitution_form"
    )


@pytest.mark.parametrize(
    "label_length, expected_transition",
    [
        (256, Substitute(10, 101, 106)),
        (257, InvalidTeamTransition(10, "unsupported_substitution_form")),
    ],
)
def test_captured_labels_enforce_the_exact_256_character_limit(
    label_length: int, expected_transition: Substitute | InvalidTeamTransition
) -> None:
    incoming_first_name = "I"
    incoming_family_name = "x" * (label_length - len(incoming_first_name) - 1)
    label = f"{incoming_first_name} {incoming_family_name}"
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(
            description=f"SUB: {label} FOR Starter One",
            incoming_first_name=incoming_first_name,
            incoming_family_name=incoming_family_name,
        )
    )
    assert normalized.events[0].transition == expected_transition


@pytest.mark.parametrize(
    "description, expected_code",
    (
        ("leading junk SUB: Bench Six FOR Starter One", "unsupported_substitution_form"),
        ("SUB: Bench Six FOR Starter One trailing junk", "outgoing_label_mismatch"),
    ),
)
def test_fullmatch_rejects_leading_and_trailing_junk_with_safe_codes(
    description: str, expected_code: str
) -> None:
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description=description)
    )
    assert normalized.events[0].transition == InvalidTeamTransition(10, expected_code)


def test_future_events_preserve_normalized_prefix_and_add_one_envelope() -> None:
    baseline_snapshot = _substitution_snapshot()
    baseline = NBAPlayByPlayV3LineupNormalizer().normalize(baseline_snapshot)
    box = cast(Any, deepcopy(baseline_snapshot.box_score_source_document.serializable_payload()))
    pbp = cast(Any, deepcopy(baseline_snapshot.pbp_source_document.serializable_payload()))
    appended = deepcopy(pbp["game"]["actions"][-1])
    appended.update(
        actionNumber=4,
        actionId=40,
        period=2,
        clock="PT12M00.00S",
        actionType="timeout",
        subType="",
        description="Timeout",
    )
    pbp["game"]["actions"].append(appended)

    extended = NBAPlayByPlayV3LineupNormalizer().normalize(
        make_snapshot(box_payload=box, pbp_payload=pbp, include_substitutions=True)
    )

    assert extended.events[: len(baseline.events)] == baseline.events
    assert len(extended.events) == len(baseline.events) + 1
    assert isinstance(extended.events[-1].transition, NoLineupChange)
