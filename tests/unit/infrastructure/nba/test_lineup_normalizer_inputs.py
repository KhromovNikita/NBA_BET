"""Structural and starter contracts for the Phase 4 NBA normalizer."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import NoReturn, cast

import pytest

from nba_live_totals.application.lineups import GameLineupInputSnapshot, LineupNormalizationError
from nba_live_totals.domain import (
    BoundarySide,
    GameId,
    JsonValue,
    PlayByPlayEvent,
    canonical_json_checksum,
)
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupPlayerBox,
    LineupReconstructionContext,
    LineupStatus,
    LineupVersions,
)
from nba_live_totals.infrastructure.nba import lineup_normalizer as lineup_normalizer_module
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


@pytest.mark.parametrize("document_kind", ["box", "pbp"])
@pytest.mark.parametrize("identity", ["source", "endpoint", "resource_key"])
def test_each_document_identity_is_validated_independently(
    document_kind: str, identity: str
) -> None:
    snapshot = make_snapshot()
    is_box = document_kind == "box"
    original = snapshot.box_score_source_document if is_box else snapshot.pbp_source_document
    payload = deepcopy(original.payload)
    endpoint = "boxscoretraditionalv3" if is_box else "playbyplayv3"
    resource_key = "box_score:0022500123" if is_box else "play_by_play:0022500123"
    values = {
        "source": "injected-source-marker",
        "endpoint": "injected-endpoint-marker",
        "resource_key": "injected-resource-marker",
    }
    source = document(
        payload,
        endpoint=values[identity] if identity == "endpoint" else endpoint,
        resource_key=values[identity] if identity == "resource_key" else resource_key,
        source=values[identity] if identity == "source" else "nba.com",
    )
    if is_box:
        object.__setattr__(snapshot, "box_score_source_document", source)
        object.__setattr__(snapshot, "applied_box_score_sha256", source.checksum_sha256)
    else:
        object.__setattr__(snapshot, "pbp_source_document", source)
        object.__setattr__(snapshot, "applied_pbp_sha256", source.checksum_sha256)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    expected = (
        "input_resource_not_applied"
        if identity == "resource_key"
        else "input_document_identity_mismatch"
    )
    assert caught.value.code == expected
    assert "injected" not in repr(caught.value)


@pytest.mark.parametrize("document_kind", ["box", "pbp"])
def test_raw_document_game_identity_is_independent(document_kind: str) -> None:
    snapshot = make_snapshot()
    is_box = document_kind == "box"
    original = snapshot.box_score_source_document if is_box else snapshot.pbp_source_document
    payload = deepcopy(original.payload)
    if is_box:
        box = payload["boxScoreTraditional"]
        assert isinstance(box, dict)
        box["gameId"] = "0022500999"
        endpoint, resource_key = "boxscoretraditionalv3", "box_score:0022500123"
    else:
        game = payload["game"]
        assert isinstance(game, dict)
        game["gameId"] = "0022500999"
        endpoint, resource_key = "playbyplayv3", "play_by_play:0022500123"
    source = document(payload, endpoint=endpoint, resource_key=resource_key)
    if is_box:
        object.__setattr__(snapshot, "box_score_source_document", source)
        object.__setattr__(snapshot, "applied_box_score_sha256", source.checksum_sha256)
    else:
        object.__setattr__(snapshot, "pbp_source_document", source)
        object.__setattr__(snapshot, "applied_pbp_sha256", source.checksum_sha256)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_document_identity_mismatch"


def test_box_team_context_identity_does_not_touch_pbp_document() -> None:
    snapshot = make_snapshot()
    payload = deepcopy(snapshot.box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    box["homeTeamId"] = 999
    source = document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
    )
    object.__setattr__(snapshot, "box_score_source_document", source)
    object.__setattr__(snapshot, "applied_box_score_sha256", source.checksum_sha256)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_document_identity_mismatch"
    assert snapshot.pbp_source_document.resource_key == "play_by_play:0022500123"


@pytest.mark.parametrize("document_kind", ["box", "pbp"])
def test_payload_recomputation_and_applied_hash_checks_are_independent(document_kind: str) -> None:
    snapshot = make_snapshot()
    is_box = document_kind == "box"
    original = snapshot.box_score_source_document if is_box else snapshot.pbp_source_document
    payload = deepcopy(original.payload)
    if is_box:
        payload["payload_marker"] = "injected-payload-marker"
        source = document(
            payload, endpoint="boxscoretraditionalv3", resource_key="box_score:0022500123"
        )
        object.__setattr__(source, "checksum_sha256", original.checksum_sha256)
        object.__setattr__(snapshot, "box_score_source_document", source)
        object.__setattr__(snapshot, "applied_box_score_sha256", original.checksum_sha256)
    else:
        payload["payload_marker"] = "injected-payload-marker"
        source = document(payload, endpoint="playbyplayv3", resource_key="play_by_play:0022500123")
        object.__setattr__(source, "checksum_sha256", original.checksum_sha256)
        object.__setattr__(snapshot, "pbp_source_document", source)
        object.__setattr__(snapshot, "applied_pbp_sha256", original.checksum_sha256)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_document_checksum_mismatch"
    assert "injected" not in repr(caught.value)


def test_pbp_applied_hash_mismatch_is_independent_of_document_integrity() -> None:
    snapshot = make_snapshot()
    alternate_hash = "f" * 64
    assert alternate_hash != snapshot.pbp_source_document.checksum_sha256
    object.__setattr__(snapshot, "applied_pbp_sha256", alternate_hash)

    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code == "input_document_checksum_mismatch"
    assert canonical_json_checksum(snapshot.pbp_source_document.payload) == (
        snapshot.pbp_source_document.checksum_sha256
    )


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


@pytest.mark.parametrize(
    ("field_name", "alternate"),
    [
        ("game_id", GameId("0022500999")),
        ("sequence_number", 99),
        ("action_number", 99),
        ("action_id", 99),
        ("period", 2),
        ("clock", "PT11M00.00S"),
        ("seconds_remaining_period_ms", 660_000),
        ("team_id", 20),
        ("person_id", 999),
        ("action_type", "3pt"),
        ("sub_type", "Layup"),
        ("description", "canonical-field-marker"),
        ("home_score", 7),
        ("away_score", 7),
        ("shot_result", "Missed"),
        ("shot_distance", 1),
        ("legacy_x", 1),
        ("legacy_y", 1),
        ("location", "v"),
        ("is_field_goal", False),
        ("points_total", 1),
        ("video_available", False),
    ],
)
def test_every_canonical_pbp_field_is_cross_checked(field_name: str, alternate: object) -> None:
    snapshot = make_snapshot()
    original = snapshot.canonical_events[1]
    changed = replace(original, **{field_name: alternate})  # type: ignore[arg-type]
    assert isinstance(changed, PlayByPlayEvent)
    object.__setattr__(
        snapshot,
        "canonical_events",
        (snapshot.canonical_events[0], changed, snapshot.canonical_events[2]),
    )
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


def test_duplicate_raw_player_id_across_teams_is_rejected() -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    away = box["awayTeam"]
    assert isinstance(home, dict) and isinstance(away, dict)
    home_players = home["players"]
    away_players = away["players"]
    assert isinstance(home_players, list) and isinstance(away_players, list)
    away_players.append(deepcopy(home_players[0]))
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


def test_raw_and_canonical_cross_team_duplicate_is_rejected_after_scalar_match() -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    away = box["awayTeam"]
    assert isinstance(home, dict) and isinstance(away, dict)
    home_players = home["players"]
    away_players = away["players"]
    assert isinstance(home_players, list) and isinstance(away_players, list)
    duplicate_raw = deepcopy(home_players[0])
    away_players.append(duplicate_raw)
    source = document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
    )
    baseline = make_snapshot()
    duplicate_box = LineupPlayerBox(
        game_id=baseline.game.game_id,
        team_id=20,
        player_id=101,
        position="C",
        starter=True,
        played=True,
        comment=None,
    )
    canonical = tuple(
        sorted(
            (*baseline.canonical_player_boxes, duplicate_box),
            key=lambda row: (row.team_id, row.player_id),
        )
    )
    broken = make_snapshot()
    object.__setattr__(broken, "canonical_player_boxes", canonical)
    object.__setattr__(broken, "box_score_source_document", source)
    object.__setattr__(broken, "applied_box_score_sha256", source.checksum_sha256)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"


def test_canonical_cross_team_duplicate_is_rejected_by_global_id_guard() -> None:
    snapshot = make_snapshot()
    duplicate = LineupPlayerBox(
        game_id=snapshot.game.game_id,
        team_id=20,
        player_id=101,
        position="C",
        starter=True,
        played=True,
        comment=None,
    )
    canonical = tuple(
        sorted(
            (*snapshot.canonical_player_boxes, duplicate),
            key=lambda row: (row.team_id, row.player_id),
        )
    )
    broken = make_snapshot(canonical_player_boxes=canonical)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"


def test_duplicate_canonical_player_id_across_teams_is_rejected() -> None:
    snapshot = make_snapshot()
    duplicate = LineupPlayerBox(
        game_id=snapshot.game.game_id,
        team_id=20,
        player_id=101,
        position="C",
        starter=True,
        played=True,
        comment=None,
    )
    boxes = tuple(
        sorted(
            (*snapshot.canonical_player_boxes, duplicate),
            key=lambda row: (row.team_id, row.player_id),
        )
    )
    broken = make_snapshot(canonical_player_boxes=boxes)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"


@pytest.mark.parametrize("version", [True, 1.0])
def test_box_and_pbp_versions_require_exact_integer_one(version: object) -> None:
    for document_kind in ("box", "pbp"):
        payload = deepcopy(
            make_snapshot().box_score_source_document.payload
            if document_kind == "box"
            else make_snapshot().pbp_source_document.payload
        )
        meta = payload["meta"]
        assert isinstance(meta, dict)
        meta["version"] = cast(JsonValue, version)
        if document_kind == "box":
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
        else:
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
        assert caught.value.code == "unsupported_document_schema"


def test_invalid_lineup_event_envelope_is_safe() -> None:
    payload = deepcopy(make_snapshot().pbp_source_document.payload)
    game = payload["game"]
    assert isinstance(game, dict)
    actions = game["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    action["period"] = 65_536
    source = document(
        payload,
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
    )
    snapshot = make_snapshot(pbp_payload=payload)
    broken = replace(
        snapshot,
        pbp_source_document=source,
        applied_pbp_sha256=source.checksum_sha256,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "unsupported_document_schema"


def test_raw_action_reordering_is_rejected_by_positional_identity() -> None:
    payload = deepcopy(make_snapshot().pbp_source_document.payload)
    game = payload["game"]
    assert isinstance(game, dict)
    actions = game["actions"]
    assert isinstance(actions, list)
    actions[0], actions[1] = actions[1], actions[0]
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
    assert caught.value.code == "raw_canonical_event_mismatch"


def test_duplicate_provider_action_numbers_remain_bound_to_raw_positions() -> None:
    payload = deepcopy(make_snapshot().pbp_source_document.payload)
    game = payload["game"]
    assert isinstance(game, dict)
    actions = game["actions"]
    assert isinstance(actions, list)
    first, second = actions[0], actions[1]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["actionNumber"] = first["actionNumber"]
    source = document(
        payload,
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
    )
    snapshot = make_snapshot(pbp_payload=payload)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        replace(
            snapshot,
            pbp_source_document=source,
            applied_pbp_sha256=source.checksum_sha256,
        )
    )
    assert [event.sequence_number for event in normalized.events] == [1, 2, 3]


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
    snapshot = make_snapshot(box_payload=payload)
    broken = replace(
        snapshot,
        box_score_source_document=source,
        applied_box_score_sha256=source.checksum_sha256,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_roster_mismatch"


def test_legacy_nonempty_position_starter_signal_is_supported() -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    for team_key in ("homeTeam", "awayTeam"):
        team = box[team_key]
        assert isinstance(team, dict)
        players = team["players"]
        assert isinstance(players, list)
        for player in players:
            assert isinstance(player, dict)
            player.pop("starter", None)
            player.pop("played", None)
    source = document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
    )
    snapshot = make_snapshot(box_payload=payload)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        replace(
            snapshot,
            box_score_source_document=source,
            applied_box_score_sha256=source.checksum_sha256,
        )
    )
    assert normalized.home_roster.starter_player_ids == frozenset({101})
    assert normalized.away_roster.starter_player_ids == frozenset({201})


@pytest.mark.parametrize("position", [None, "", "G"])
@pytest.mark.parametrize("comment", [None, "", "DNP - Coach's Decision"])
def test_position_and_comment_normalization_matrix(
    position: str | None, comment: str | None
) -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    assert isinstance(home, dict)
    players = home["players"]
    assert isinstance(players, list)
    player = players[0]
    assert isinstance(player, dict)
    player["position"] = position
    player["comment"] = comment
    source = document(
        payload, endpoint="boxscoretraditionalv3", resource_key="box_score:0022500123"
    )
    snapshot = make_snapshot(box_payload=payload)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        replace(
            snapshot,
            box_score_source_document=source,
            applied_box_score_sha256=source.checksum_sha256,
        )
    )
    canonical_row = next(row for row in snapshot.canonical_player_boxes if row.player_id == 101)
    assert canonical_row.position == (position or None)
    assert canonical_row.comment == (comment or None)
    assert (101 in normalized.home_roster.eligible_player_ids) is (
        canonical_row.played and canonical_row.comment is None
    )


@pytest.mark.parametrize(
    ("played", "comment", "eligible"),
    [
        (True, None, True),
        (True, "", True),
        (True, "DNP", False),
        (False, None, False),
        (False, "DNP", False),
    ],
)
def test_played_and_comment_eligibility_is_exact(
    played: bool, comment: str | None, eligible: bool
) -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    assert isinstance(home, dict)
    players = home["players"]
    assert isinstance(players, list)
    player = players[0]
    assert isinstance(player, dict)
    player["played"] = "1" if played else "0"
    player["comment"] = comment
    source = document(
        payload, endpoint="boxscoretraditionalv3", resource_key="box_score:0022500123"
    )
    snapshot = make_snapshot(box_payload=payload)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(
        replace(
            snapshot,
            box_score_source_document=source,
            applied_box_score_sha256=source.checksum_sha256,
        )
    )
    assert (101 in normalized.home_roster.eligible_player_ids) is eligible


def test_normalizer_does_not_construct_or_join_global_players(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot()

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("global Player construction is forbidden")

    monkeypatch.setattr("nba_live_totals.domain.games.Player", forbidden)
    monkeypatch.setattr("nba_live_totals.domain.Player", forbidden)
    monkeypatch.setattr("nba_live_totals.infrastructure.nba.mappers.box_score.Player", forbidden)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert normalized.home_roster.roster_player_ids == frozenset({101, 102})


def _reconstruction_context(
    snapshot: GameLineupInputSnapshot,
) -> LineupReconstructionContext:
    assert hasattr(snapshot, "game_context_sha256")
    return LineupReconstructionContext(
        game=snapshot.game,
        versions=LineupVersions("normalizer", "reconstructor", "rules", "schema"),
        box_score_document_sha256=snapshot.applied_box_score_sha256,
        pbp_document_sha256=snapshot.applied_pbp_sha256,
        canonical_player_box_sha256=snapshot.canonical_player_box_sha256,
        canonical_event_sha256=snapshot.canonical_event_sha256,
        game_context_sha256=snapshot.game_context_sha256,
        lineup_revision_id="a" * 64,
    )


@pytest.mark.parametrize("starter_count", [0, 6])
def test_non_five_starters_are_normalized_then_marked_invalid_by_domain(
    starter_count: int,
) -> None:
    payload = deepcopy(make_snapshot().box_score_source_document.payload)
    box = payload["boxScoreTraditional"]
    assert isinstance(box, dict)
    home = box["homeTeam"]
    away = box["awayTeam"]
    assert isinstance(home, dict) and isinstance(away, dict)
    players = home["players"]
    away_players = away["players"]
    assert isinstance(players, list) and isinstance(away_players, list)
    away_template = deepcopy(away_players[0])
    assert isinstance(away_template, dict)
    for player_id in range(401, 405):
        away_clone = deepcopy(away_template)
        assert isinstance(away_clone, dict)
        away_clone["personId"] = player_id
        away_clone["starter"] = "1"
        away_players.append(away_clone)
    if starter_count == 6:
        template = deepcopy(players[0])
        assert isinstance(template, dict)
        for player_id in range(301, 306):
            clone = deepcopy(template)
            assert isinstance(clone, dict)
            clone["personId"] = player_id
            clone["starter"] = "1"
            players.append(clone)
    else:
        for player in players:
            assert isinstance(player, dict)
            player["starter"] = "0"
            player["position"] = ""
    source = document(
        payload, endpoint="boxscoretraditionalv3", resource_key="box_score:0022500123"
    )
    snapshot = make_snapshot(box_payload=payload)
    object.__setattr__(snapshot, "box_score_source_document", source)
    object.__setattr__(snapshot, "applied_box_score_sha256", source.checksum_sha256)
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert len(normalized.home_roster.starter_player_ids) == starter_count
    result = DeterministicLineupReconstructor().reconstruct(
        _reconstruction_context(snapshot), normalized
    )
    assert result.status is LineupStatus.INVALID
    assert result.issues == (
        LineupIssue(
            team_id=snapshot.game.home_team_id,
            reason="invalid_initial_lineup",
            sequence_number=1,
            boundary_side=BoundarySide.BEFORE,
        ),
    )


@pytest.mark.parametrize("failure", ["source", "description", "payload"])
def test_structural_error_messages_never_leak_provider_values(failure: str) -> None:
    snapshot = make_snapshot()
    marker = "injected-secret-marker"
    if failure == "source":
        source = document(
            snapshot.box_score_source_document.payload,
            endpoint="boxscoretraditionalv3",
            resource_key="box_score:0022500123",
            source=marker,
        )
        object.__setattr__(snapshot, "box_score_source_document", source)
        object.__setattr__(snapshot, "applied_box_score_sha256", source.checksum_sha256)
    elif failure == "description":
        payload = deepcopy(snapshot.pbp_source_document.payload)
        game = payload["game"]
        assert isinstance(game, dict)
        actions = game["actions"]
        assert isinstance(actions, list)
        action = actions[0]
        assert isinstance(action, dict)
        action["description"] = marker
        source = document(payload, endpoint="playbyplayv3", resource_key="play_by_play:0022500123")
        object.__setattr__(snapshot, "pbp_source_document", source)
        object.__setattr__(snapshot, "applied_pbp_sha256", source.checksum_sha256)
    else:
        payload = deepcopy(snapshot.box_score_source_document.payload)
        payload[marker] = marker
        source = document(
            payload, endpoint="boxscoretraditionalv3", resource_key="box_score:0022500123"
        )
        object.__setattr__(source, "checksum_sha256", snapshot.applied_box_score_sha256)
        object.__setattr__(snapshot, "box_score_source_document", source)
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    assert caught.value.code in {
        "input_document_identity_mismatch",
        "input_document_checksum_mismatch",
        "unsupported_document_schema",
        "raw_canonical_event_mismatch",
    }
    assert str(caught.value) == caught.value.code
    assert marker not in repr(caught.value)


def test_supplemental_raw_action_identity_check_catches_mapper_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot()
    payload = deepcopy(snapshot.pbp_source_document.payload)
    game = payload["game"]
    assert isinstance(game, dict)
    actions = game["actions"]
    assert isinstance(actions, list)
    first = actions[0]
    assert isinstance(first, dict)
    first["actionNumber"] = 999
    source = document(
        payload,
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
    )
    broken = replace(
        snapshot,
        pbp_source_document=source,
        applied_pbp_sha256=source.checksum_sha256,
    )
    monkeypatch.setattr(
        lineup_normalizer_module,
        "parse_play_by_play",
        lambda _document: snapshot.canonical_events,
    )
    with pytest.raises(LineupNormalizationError) as caught:
        NBAPlayByPlayV3LineupNormalizer().normalize(broken)
    assert caught.value.code == "raw_canonical_event_mismatch"
