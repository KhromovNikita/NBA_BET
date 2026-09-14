"""NBA PlayByPlayV3 basic possession-normalizer behavior tests."""

from __future__ import annotations

from typing import cast

import pytest

from nba_live_totals.application.possessions import PossessionNormalizer
from nba_live_totals.domain import (
    ActionConsumption,
    Administrative,
    FieldGoalAttempt,
    IssueSeverity,
    JsonObject,
    JsonValue,
    Unsupported,
    normalized_actions_checksum,
    normalized_actions_projection,
)
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer

from .possession_helpers import load_json_fixture, make_snapshot

NORMALIZED_GOLDEN_SHA256 = "6cdb6f552439c109b841755fadbed2d027e47445bdb505d9de0a67810d0a0229"


def _actions(payload: JsonObject) -> list[JsonObject]:
    game = cast(JsonObject, payload["game"])
    return cast(list[JsonObject], game["actions"])


def test_normalizer_matches_exact_basic_v1_golden_and_safe_issues() -> None:
    implementation = NBAPlayByPlayV3PossessionNormalizer()
    assert implementation.version == "nba-pbp-v2"
    normalizer: PossessionNormalizer = implementation

    normalized = normalizer.normalize(make_snapshot())

    assert normalized_actions_projection(normalized) == load_json_fixture(
        "pbp_v3_normalizer_v1.expected.json"
    )
    assert normalized_actions_checksum(normalized) == NORMALIZED_GOLDEN_SHA256
    assert [(issue.severity, issue.code, issue.sequence_number) for issue in normalized.issues] == [
        (IssueSeverity.WARNING, "duplicate_provider_action", 10),
        (IssueSeverity.WARNING, "unsupported_administrative_action", 11),
    ]
    assert normalized.issues[0].related_sequence_number == 9
    assert "control-secret-must-not-escape" not in repr(normalized)
    assert "admin-secret-must-not-escape" not in repr(normalized)


def test_normalizer_preserves_provider_order_duplicates_nulls_and_one_to_one_cardinality() -> None:
    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot())

    assert [action.sequence_number for action in normalized.actions] == list(range(1, 13))
    assert [action.provider_action_number for action in normalized.actions][8:10] == [9, 9]
    assert [action.provider_action_id for action in normalized.actions][8:10] == [None, None]
    assert len(normalized.actions) == 12


@pytest.mark.parametrize(
    ("raw_field", "other_value"),
    [
        ("actionNumber", 999),
        ("actionId", 999),
        ("period", 2),
        ("clock", "PT11M19.00S"),
        ("teamId", 1_610_612_744),
        ("personId", 999),
        ("actionType", "Missed Shot"),
        ("subType", "Other Shot"),
        ("description", "Different display text"),
        ("scoreHome", "2"),
        ("scoreAway", "1"),
        ("shotResult", "Missed"),
        ("shotDistance", 24),
        ("xLegacy", -119),
        ("yLegacy", 234),
        ("location", "v"),
        ("isFieldGoal", 0),
        ("pointsTotal", 4),
        ("videoAvailable", 0),
    ],
)
def test_normalizer_cross_checks_every_phase_2_canonical_field_positionally(
    raw_field: str,
    other_value: JsonValue,
) -> None:
    original = make_snapshot()
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(payload)[1][raw_field] = other_value
    snapshot = make_snapshot(payload, canonical_events=original.canonical_events)

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(snapshot)

    assert normalized.actions == ()
    assert [(issue.code, issue.sequence_number) for issue in normalized.issues] == [
        ("raw_canonical_mismatch", 2)
    ]


def test_normalizer_rejects_raw_canonical_cardinality_or_position_changes() -> None:
    original = make_snapshot()
    missing_payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(missing_payload).pop()
    reversed_payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(reversed_payload)[0:2] = reversed(_actions(reversed_payload)[0:2])

    cardinality = NBAPlayByPlayV3PossessionNormalizer().normalize(
        make_snapshot(missing_payload, canonical_events=original.canonical_events)
    )
    positional = NBAPlayByPlayV3PossessionNormalizer().normalize(
        make_snapshot(reversed_payload, canonical_events=original.canonical_events)
    )

    assert cardinality.actions == positional.actions == ()
    assert [issue.code for issue in cardinality.issues] == ["raw_canonical_mismatch"]
    assert [(issue.code, issue.sequence_number) for issue in positional.issues] == [
        ("raw_canonical_mismatch", 1)
    ]


@pytest.mark.parametrize("attribute", ["source", "endpoint", "resource_key", "checksum_sha256"])
def test_normalizer_cross_checks_the_exact_applied_v3_document_identity(attribute: str) -> None:
    snapshot = make_snapshot()
    replacements = {
        "source": "other-provider",
        "endpoint": "other-endpoint",
        "resource_key": "play_by_play:0022500999",
        "checksum_sha256": "f" * 64,
    }
    object.__setattr__(snapshot.source_document, attribute, replacements[attribute])

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(snapshot)

    assert normalized.actions == ()
    assert [issue.code for issue in normalized.issues] == ["raw_canonical_mismatch"]


@pytest.mark.parametrize("version", [None, 0, 2, "1", True])
def test_normalizer_requires_exact_play_by_play_v3_meta_version_one(version: JsonValue) -> None:
    original = make_snapshot()
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    meta = cast(JsonObject, payload["meta"])
    meta["version"] = version

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(
        make_snapshot(payload, canonical_events=original.canonical_events)
    )

    assert normalized.actions == ()
    assert [issue.code for issue in normalized.issues] == ["raw_canonical_mismatch"]


def test_normalizer_rejects_mixed_raw_game_identity() -> None:
    original = make_snapshot()
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    cast(JsonObject, payload["game"])["gameId"] = "0022500999"

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(
        make_snapshot(payload, canonical_events=original.canonical_events)
    )

    assert normalized.actions == ()
    assert [issue.code for issue in normalized.issues] == ["mixed_game"]


def test_exact_blank_block_fullmatch_does_not_accept_near_matches() -> None:
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(payload)[8]["description"] = "Defender BLOCK (1 BLK) trailing"
    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))

    block = normalized.actions[8]

    assert block.consumption is ActionConsumption.UNSUPPORTED
    assert block.signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unknown_control_action" and issue.sequence_number == 9
        for issue in normalized.issues
    )


def test_unknown_semantics_fail_closed_unless_the_form_is_explicitly_safe() -> None:
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(payload)[9]["actionType"] = "Control Award"
    _actions(payload)[9]["subType"] = "Opponent Inbound"

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))

    unfamiliar_control = normalized.actions[9]
    safe_notice = normalized.actions[10]
    assert unfamiliar_control.signals == (Unsupported(control_relevant=True),)
    assert safe_notice.signals == (Unsupported(control_relevant=False),)
    assert [(issue.severity, issue.code, issue.sequence_number) for issue in normalized.issues] == [
        (IssueSeverity.WARNING, "duplicate_provider_action", 10),
        (IssueSeverity.ERROR, "unknown_control_action", 10),
        (IssueSeverity.WARNING, "unsupported_administrative_action", 11),
    ]


def test_field_goals_use_zero_baseline_and_survive_blank_intervening_checkpoints() -> None:
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    for index in (0, 2):
        _actions(payload)[index]["scoreHome"] = ""
        _actions(payload)[index]["scoreAway"] = ""

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))

    assert normalized.actions[1].signals == (
        FieldGoalAttempt(
            team=1_610_612_736,
            value=3,
            made=True,
            and_one_candidate=False,
        ),
    )
    assert normalized.actions[2].signals == (
        FieldGoalAttempt(
            team=1_610_612_744,
            value=2,
            made=False,
            and_one_candidate=False,
        ),
    )
    assert normalized.actions[3].signals == (
        FieldGoalAttempt(
            team=1_610_612_744,
            value=2,
            made=True,
            and_one_candidate=False,
        ),
    )
    assert [
        (issue.code, issue.sequence_number)
        for issue in normalized.issues
        if issue.code in {"missing_score_checkpoint", "unknown_control_action"}
    ] == [
        ("missing_score_checkpoint", 1),
        ("missing_score_checkpoint", 3),
    ]


def test_field_goal_checkpoint_conflict_reports_error_without_reclassifying_shot() -> None:
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(payload)[1]["scoreHome"] = "2"

    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))

    assert normalized.actions[1].signals == (
        FieldGoalAttempt(
            team=1_610_612_736,
            value=3,
            made=True,
            and_one_candidate=False,
        ),
    )
    assert any(
        issue.severity is IssueSeverity.ERROR
        and issue.code == "invalid_score_delta"
        and issue.sequence_number == 2
        for issue in normalized.issues
    )


def test_compact_field_goal_alias_requires_matching_raw_shot_value() -> None:
    payload = load_json_fixture("pbp_v3_normalizer_v1.json")
    _actions(payload)[3]["shotValue"] = 3
    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot(payload))

    compact = normalized.actions[3]

    assert compact.consumption is ActionConsumption.UNSUPPORTED
    assert compact.signals == (Unsupported(control_relevant=True),)
    assert any(
        issue.code == "unknown_control_action" and issue.sequence_number == 4
        for issue in normalized.issues
    )


@pytest.mark.parametrize(
    ("index", "expected_kind"),
    [(5, "timeout"), (6, "substitution"), (7, "instant_replay"), (8, "block")],
)
def test_basic_administrative_forms_are_exact_and_description_free(
    index: int,
    expected_kind: str,
) -> None:
    normalized = NBAPlayByPlayV3PossessionNormalizer().normalize(make_snapshot())

    action = normalized.actions[index]

    assert action.consumption is ActionConsumption.ADMINISTRATIVE
    assert action.signals == (Administrative(expected_kind),)
