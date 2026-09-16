"""NBA V1 substitution classification contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

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
        firstName="Two",
        familyName="Player",
        starter="1",
        position="G",
        played="1",
        comment="",
        statistics=deepcopy(player["statistics"]),
    )
    for player_id, first_name, family_name, starter in (
        (103, "Three", "Player", "1"),
        (104, "Four", "Player", "1"),
        (105, "Five", "Player", "1"),
        (106, "Bench", "Six", "0"),
    ):
        clone = deepcopy(player)
        clone.update(
            personId=player_id,
            firstName=first_name,
            familyName=family_name,
            starter=starter,
            position="G" if starter == "1" else "",
            played="1",
            comment="",
        )
        home["players"].append(clone)
    away = box["boxScoreTraditional"]["awayTeam"]
    template = deepcopy(away["players"][0])
    for player_id in (202, 203, 204, 205):
        clone = deepcopy(template)
        clone.update(personId=player_id, firstName=f"Away{player_id}", familyName="Player")
        away["players"].append(clone)
    pbp = cast(Any, deepcopy(fixture("lineups/play_by_play_v1.json")))
    action = pbp["game"]["actions"][0]
    action.update(
        actionType=action_type,
        subType=subtype,
        personId=101,
        playerName="Starter One",
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


def test_malformed_substitution_is_safe_and_does_not_leak_description() -> None:
    marker = "raw-provider-secret"
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        _substitution_snapshot(description=marker)
    )
    transition = normalized.events[0].transition
    assert transition == InvalidTeamTransition(10, "unsupported_substitution_form")
    assert marker not in repr(transition)
