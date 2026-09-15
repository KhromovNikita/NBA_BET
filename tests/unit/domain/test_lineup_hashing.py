"""Golden vectors for canonical Phase 4 lineup identities."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from uuid import UUID

from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    canonical_player_boxes_projection,
    lineup_input_token_checksum,
    lineup_input_token_projection,
    lineup_output_checksum,
    lineup_output_projection,
    lineup_revision_id,
    lineup_revision_projection,
    normalized_lineups_checksum,
    normalized_lineups_projection,
)
from nba_live_totals.domain.lineups import (
    FullLineupReset,
    InvalidTeamTransition,
    LineupIssue,
    LineupPlayerBox,
    LineupReconstructionResult,
    LineupStatus,
    NoLineupChange,
    NormalizedGameLineups,
    NormalizedLineupEvent,
    Substitute,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.lineup_helpers import (
    GAME_ID,
    make_invalid_state,
    make_reconstruction_context,
    make_roster,
    make_segment,
    make_versions,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
BOX_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PBP_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def test_canonical_player_boxes_projection_has_exact_sorted_rows_and_golden_vector() -> None:
    players = (
        LineupPlayerBox(GAME_ID, 20, 202, "", False, True, ""),
        LineupPlayerBox(GAME_ID, 10, 101, None, True, False, None),
    )
    projection = canonical_player_boxes_projection(players)

    assert projection == {
        "schema": "canonical-lineup-player-box-v1",
        "players": [
            {
                "game_id": "0022500123",
                "team_id": 10,
                "player_id": 101,
                "position": None,
                "starter": True,
                "played": False,
                "comment": None,
            },
            {
                "game_id": "0022500123",
                "team_id": 20,
                "player_id": 202,
                "position": "",
                "starter": False,
                "played": True,
                "comment": "",
            },
        ],
    }
    projected_players = cast(list[dict[str, object]], projection["players"])
    assert list(projected_players[0]) == [
        "game_id",
        "team_id",
        "player_id",
        "position",
        "starter",
        "played",
        "comment",
    ]
    expected = (
        b'{"players":[{"comment":null,"game_id":"0022500123","played":false,'
        b'"player_id":101,"position":null,"starter":true,"team_id":10},'
        b'{"comment":"","game_id":"0022500123","played":true,"player_id":202,'
        b'"position":"","starter":false,"team_id":20}],'
        b'"schema":"canonical-lineup-player-box-v1"}'
    )
    assert canonical_json_bytes(projection) == expected
    assert canonical_player_boxes_checksum(players) == (
        "c381cd6da0f80efe376805b18bd2e05ab8ea7a5d8b8af246060c144794c4f9a6"
    )


def test_revision_and_input_token_projections_have_independent_golden_vectors() -> None:
    versions = make_versions()
    revision = lineup_revision_projection(
        game_id=GAME_ID,
        box_score_document_sha256=SHA_A,
        pbp_document_sha256=SHA_B,
        canonical_player_box_sha256=SHA_C,
        canonical_event_sha256=SHA_D,
        game_context_sha256=SHA_E,
        versions=versions,
    )
    token = lineup_input_token_projection(
        game_id=GAME_ID,
        applied_box_score_document_id=BOX_ID,
        applied_box_score_sha256=SHA_A,
        applied_pbp_document_id=PBP_ID,
        applied_pbp_sha256=SHA_B,
        canonical_player_box_sha256=SHA_C,
        canonical_event_sha256=SHA_D,
        game_context_sha256=SHA_E,
        versions=versions,
    )
    revision_bytes = (
        b'{"box_score_document_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"canonical_event_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_player_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"game_context_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"game_id":"0022500123","normalizer_version":"nba-pbp-v1",'
        b'"output_schema_version":"lineups-v1",'
        b'"pbp_document_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"reconstructor_version":"causal-v1","rules_version":"nba-2025-v1",'
        b'"schema":"lineup-revision-v1"}'
    )
    token_bytes = (
        b'{"applied_box_score_document_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
        b'"applied_box_score_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"applied_pbp_document_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",'
        b'"applied_pbp_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"canonical_event_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"canonical_player_box_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"game_context_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        b'"game_id":"0022500123","normalizer_version":"nba-pbp-v1",'
        b'"output_schema_version":"lineups-v1","reconstructor_version":"causal-v1",'
        b'"rules_version":"nba-2025-v1","schema":"lineup-input-token-v1"}'
    )
    assert canonical_json_bytes(revision) == revision_bytes
    assert canonical_json_bytes(token) == token_bytes
    assert (
        lineup_revision_id(
            game_id=GAME_ID,
            box_score_document_sha256=SHA_A,
            pbp_document_sha256=SHA_B,
            canonical_player_box_sha256=SHA_C,
            canonical_event_sha256=SHA_D,
            game_context_sha256=SHA_E,
            versions=versions,
        )
        == "3a9aba32d23d912f1ec39b466232d4f1867b49e9dfbef8a60e2608d62f216da7"
    )
    assert (
        lineup_input_token_checksum(
            game_id=GAME_ID,
            applied_box_score_document_id=BOX_ID,
            applied_box_score_sha256=SHA_A,
            applied_pbp_document_id=PBP_ID,
            applied_pbp_sha256=SHA_B,
            canonical_player_box_sha256=SHA_C,
            canonical_event_sha256=SHA_D,
            game_context_sha256=SHA_E,
            versions=versions,
        )
        == "a4f1af755a7d322608675b6f9449e875a110cfe2354edb48ca29bb22e3b707f5"
    )


def test_revision_changes_for_every_content_hash_and_version_but_not_document_ids() -> None:
    def token_digest(values: dict[str, object]) -> str:
        return lineup_input_token_checksum(**values)  # type: ignore[arg-type]

    base = dict(
        game_id=GAME_ID,
        box_score_document_sha256=SHA_A,
        pbp_document_sha256=SHA_B,
        canonical_player_box_sha256=SHA_C,
        canonical_event_sha256=SHA_D,
        game_context_sha256=SHA_E,
        versions=make_versions(),
    )
    original = lineup_revision_id(**base)  # type: ignore[arg-type]
    revision = lineup_revision_projection(**base)  # type: ignore[arg-type]
    for field_name in (
        "box_score_document_sha256",
        "pbp_document_sha256",
        "canonical_player_box_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
    ):
        changed = dict(base)
        changed[field_name] = "f" * 64
        assert lineup_revision_id(**changed) != original  # type: ignore[arg-type]
    for field_name in (
        "normalizer_version",
        "reconstructor_version",
        "rules_version",
        "output_schema_version",
    ):
        changed = dict(base)
        changed["versions"] = replace(make_versions(), **{field_name: "changed-v1"})
        assert lineup_revision_id(**changed) != original  # type: ignore[arg-type]

    token_args = dict(
        game_id=GAME_ID,
        applied_box_score_document_id=BOX_ID,
        applied_box_score_sha256=SHA_A,
        applied_pbp_document_id=PBP_ID,
        applied_pbp_sha256=SHA_B,
        canonical_player_box_sha256=SHA_C,
        canonical_event_sha256=SHA_D,
        game_context_sha256=SHA_E,
        versions=make_versions(),
    )
    for field_name, value in (
        ("applied_box_score_document_id", UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")),
        ("applied_pbp_document_id", UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")),
    ):
        changed_ids = dict(token_args)
        changed_ids[field_name] = value
        assert token_digest(changed_ids) != token_digest(token_args)
    assert "applied_box_score_document_id" not in revision
    assert "applied_pbp_document_id" not in revision


def make_normalized() -> NormalizedGameLineups:
    return NormalizedGameLineups(
        GAME_ID,
        make_roster(10),
        make_roster(20),
        (
            NormalizedLineupEvent(1, 1, 720_000, NoLineupChange()),
            NormalizedLineupEvent(2, 1, 710_000, Substitute(10, 101, 106)),
            NormalizedLineupEvent(3, 1, 700_000, FullLineupReset(20, (205, 204, 203, 202, 201))),
            NormalizedLineupEvent(4, 1, 690_000, InvalidTeamTransition(None, "unknown_team")),
        ),
    )


def test_normalized_lineups_projection_sorts_all_id_sets_and_has_golden_vector() -> None:
    projection = normalized_lineups_projection(make_normalized())
    expected = (
        b'{"away_roster":{"eligible_player_ids":[201,202,203,204,205,2006],'
        b'"roster_player_ids":[201,202,203,204,205,2006],'
        b'"starter_player_ids":[201,202,203,204,205],"team_id":20},'
        b'"events":[{"period":1,"seconds_remaining_period_ms":720000,"sequence_number":1,'
        b'"transition":{"transition_type":"no_lineup_change"}},'
        b'{"period":1,"seconds_remaining_period_ms":710000,"sequence_number":2,'
        b'"transition":{"incoming_player_id":106,"outgoing_player_id":101,"team_id":10,'
        b'"transition_type":"substitute"}},'
        b'{"period":1,"seconds_remaining_period_ms":700000,"sequence_number":3,'
        b'"transition":{"player_ids":[201,202,203,204,205],"team_id":20,'
        b'"transition_type":"full_lineup_reset"}},'
        b'{"period":1,"seconds_remaining_period_ms":690000,"sequence_number":4,'
        b'"transition":{"code":"unknown_team","team_id":null,'
        b'"transition_type":"invalid_team_transition"}}],'
        b'"game_id":"0022500123","home_roster":{"eligible_player_ids":[101,102,103,104,105,1006],'
        b'"roster_player_ids":[101,102,103,104,105,1006],'
        b'"starter_player_ids":[101,102,103,104,105],"team_id":10},'
        b'"schema":"normalized-lineups-v1"}'
    )
    assert canonical_json_bytes(projection) == expected
    assert normalized_lineups_checksum(make_normalized()) == (
        "30bb085782deaf5a0faddfccd0604371712463e57f020a9d5d1cb7fb441bd7e2"
    )


def test_lineup_output_projects_ordered_semantic_segments_and_excludes_issues() -> None:
    context = make_reconstruction_context(lineup_revision_id=SHA_A)
    segment = make_segment(
        home_state=make_invalid_state(
            reason="outgoing_not_on_court",
            boundary=EventBoundary(1, BoundarySide.AFTER),
        )
    )
    result = LineupReconstructionResult(
        context,
        LineupStatus.INVALID,
        (segment,),
        (LineupIssue(10, "outgoing_not_on_court", 1, BoundarySide.AFTER),),
    )
    # The domain derives issues, so retain those while proving hashes ignore them as metadata.
    result = replace(result, issues=result.issues)
    projection = lineup_output_projection(result)
    expected = (
        b'{"lineup_revision_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"schema":"lineup-output-v1","segments":[{"away_state":{"invalid_reason":null,'
        b'"invalid_since":null,"player_ids":[201,202,203,204,205],"status":"valid"},'
        b'"end_boundary":{"sequence_number":2,"side":"after"},"end_period":1,'
        b'"end_seconds_remaining_period_ms":700000,"home_state":'
        b'{"invalid_reason":"outgoing_not_on_court","invalid_since":{"sequence_number":1,'
        b'"side":"after"},"player_ids":[],"status":"invalid"},"segment_number":1,'
        b'"start_boundary":{"sequence_number":1,"side":"before"},"start_period":1,'
        b'"start_seconds_remaining_period_ms":720000}],"status":"invalid"}'
    )
    assert canonical_json_bytes(projection) == expected
    assert "issues" not in projection
    assert not {"snapshot_id", "generation", "created_at", "output_sha256"} & projection.keys()
    assert lineup_output_checksum(result) == (
        "0dac657817233edef52e3cb50a44a2fa18cd62e5bfca4fea3eb76b4e7efb6fdb"
    )
    assert lineup_output_checksum(replace(result, issues=result.issues)) == lineup_output_checksum(
        result
    )
