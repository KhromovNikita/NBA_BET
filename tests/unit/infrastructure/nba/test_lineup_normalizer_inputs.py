"""Structural and starter contracts for the Phase 4 NBA normalizer."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from nba_live_totals.application.lineups import LineupNormalizationError
from nba_live_totals.infrastructure.nba.lineup_normalizer import (
    NBAPlayByPlayV3LineupNormalizer,
)

from .lineup_helpers import document, make_snapshot


def test_normalize_builds_rosters_and_positioned_no_change_events() -> None:
    snapshot = make_snapshot()

    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)

    assert normalized.game_id == snapshot.game.game_id
    assert normalized.home_roster.roster_player_ids == frozenset({101, 102})
    assert normalized.away_roster.roster_player_ids == frozenset({201})
    assert normalized.home_roster.eligible_player_ids == frozenset({101})
    assert normalized.home_roster.starter_player_ids == frozenset({101})
    assert [event.sequence_number for event in normalized.events] == [1, 2, 3]
    assert [event.period for event in normalized.events] == [1, 1, 1]
    assert all(type(event.transition).__name__ == "NoLineupChange" for event in normalized.events)


@pytest.mark.parametrize("field", ["source", "endpoint"])
def test_document_identity_is_checked_per_resource(field: str) -> None:
    snapshot = make_snapshot()
    box_payload = deepcopy(snapshot.box_score_source_document.payload)
    box = document(
        box_payload,
        endpoint="not-nba" if field == "endpoint" else "boxscoretraditionalv3",
        resource_key="box_score:0022500123",
        source="not-nba" if field == "source" else "nba.com",
    )
    broken = replace(snapshot, box_score_source_document=box)

    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "input_document_identity_mismatch"


def test_document_resource_and_checksum_are_independent() -> None:
    snapshot = make_snapshot()
    wrong_resource = document(
        snapshot.box_score_source_document.payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:other",
    )
    object.__setattr__(snapshot, "box_score_source_document", wrong_resource)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_resource_not_applied"

    snapshot = make_snapshot()
    object.__setattr__(snapshot.box_score_source_document, "checksum_sha256", "a" * 64)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_document_checksum_mismatch"


def test_pbp_cardinality_and_every_canonical_field_are_cross_checked() -> None:
    parsed = make_snapshot().canonical_events
    snapshot = make_snapshot(canonical_events=parsed[:-1])
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "raw_canonical_cardinality_mismatch"

    parsed = make_snapshot().canonical_events
    changed = replace(parsed[1], description="different")
    snapshot = make_snapshot(canonical_events=(parsed[0], changed, parsed[2]))
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "raw_canonical_event_mismatch"


def test_duplicate_raw_players_are_rejected_without_global_player_join() -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    assert isinstance(home, dict)
    players = home["players"]
    assert isinstance(players, list)
    players.append(deepcopy(players[0]))
    source = document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
    )
    snapshot = make_snapshot()
    broken = replace(
        snapshot,
        box_score_source_document=source,
        applied_box_score_sha256=source.checksum_sha256,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"


def test_empty_completed_pbp_is_safe_failure() -> None:
    payload = deepcopy(make_snapshot().pbp_source_document.payload)
    game = payload["game"]
    assert isinstance(game, dict)
    game["actions"] = []
    source = document(
        payload,
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
    )
    snapshot = make_snapshot()
    broken = replace(
        snapshot,
        pbp_source_document=source,
        applied_pbp_sha256=source.checksum_sha256,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "empty_completed_game_pbp"


def test_legacy_position_and_explicit_starter_conflict_are_enforced() -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    team = box["homeTeam"]
    assert isinstance(team, dict)
    players = team["players"]
    assert isinstance(players, list)
    player = players[0]
    assert isinstance(player, dict)
    player["starter"] = "0"
    source = document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
    )
    snapshot = make_snapshot()
    broken = replace(
        snapshot,
        box_score_source_document=source,
        applied_box_score_sha256=source.checksum_sha256,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"
