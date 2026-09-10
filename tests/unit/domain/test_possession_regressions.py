"""Final-output invariants and content identity independent of operational lineage."""

from dataclasses import fields, replace
from itertools import pairwise
from uuid import UUID

import pytest

from nba_live_totals.domain.canonical import canonical_json_sha256
from nba_live_totals.domain.possession_hashing import (
    parse_revision_id,
    possession_input_token_checksum,
    possession_output_projection,
)
from nba_live_totals.domain.possession_parser import AnalyticalPossessionParser
from nba_live_totals.domain.possessions import (
    Administrative,
    BoundarySide,
    ControlAward,
    EventBoundary,
    IssueSeverity,
    NormalizedAction,
    NormalizedGameActions,
    ParseStatus,
    PeriodEnd,
    PeriodStart,
    PossessionIssue,
    PossessionParseContext,
    PossessionSignal,
    StealAnnotation,
    Turnover,
    Unsupported,
)
from tests.unit.domain.possession_helpers import action, game, rebound, shot
from tests.unit.domain.test_possession_reconciliation import (
    assert_invalid,
    checkpoint,
    scored_context,
)


def test_complete_rows_have_exact_counters_boundaries_and_home_perspective_scores() -> None:
    stream = game(
        shot(10, made=False),
        rebound(10),
        shot(10),
        shot(20),
        Turnover(10, 20, "lost_ball"),
        StealAnnotation(20, 6),
        shot(20),
    )
    result = AnalyticalPossessionParser().parse(scored_context(2, 4), stream)
    assert result.status is ParseStatus.VALID
    rows = result.possessions
    assert [r.possession_number for r in rows] == [1, 2, 3, 4]
    assert [r.offense_possession_number for r in rows] == [1, 1, 2, 2]
    assert [(r.offense_team_id, r.defense_team_id) for r in rows] == [
        (10, 20),
        (20, 10),
        (10, 20),
        (20, 10),
    ]
    assert [(r.home_possessions, r.away_possessions) for r in rows] == [
        (1, 0),
        (1, 1),
        (2, 1),
        (2, 2),
    ]
    assert [r.score_diff for r in rows] == [2, 0, 0, -2]
    assert [r.offense_points for r in rows] == [2, 2, 0, 2]
    assert [r.defense_points for r in rows] == [0, 0, 0, 0]
    assert [r.source_event_count for r in rows] == [3, 1, 1, 2]
    assert [r.end_boundary for r in rows] == [
        EventBoundary(n, BoundarySide.AFTER) for n in (4, 5, 6, 8)
    ]
    assert rows[2].end_boundary == rows[3].start_boundary
    assert all(left.end_boundary <= right.start_boundary for left, right in pairwise(rows))


@pytest.mark.parametrize(
    ("period", "remaining"),
    [(1, 2_283_456), (2, 1_563_456), (3, 843_456), (4, 123_456), (5, 123_456), (6, 123_456)],
)
def test_remaining_time_includes_only_known_regulation_or_current_ot(
    period: int, remaining: int
) -> None:
    actions: list[NormalizedAction] = []
    for current in range(1, max(4, period) + 1):
        actions.append(action(len(actions) + 1, PeriodStart(), period=current))
        if current == period:
            actions.append(
                replace(action(len(actions) + 1, shot(10), period=current), clock_ms=123_456)
            )
        actions.append(action(len(actions) + 1, PeriodEnd(), period=current))
    result = AnalyticalPossessionParser().parse(
        scored_context(2, 0), NormalizedGameActions(actions[0].game_id, tuple(actions))
    )
    assert result.status is ParseStatus.VALID
    assert result.possessions[0].seconds_remaining_game_ms == remaining


def test_increasing_clock_warns_and_never_reorders_boundaries() -> None:
    stream = game(ControlAward(10, "opening"), Administrative("timeout"), shot(10))
    actions = list(stream.actions)
    actions[1] = replace(actions[1], clock_ms=100_000)
    actions[2] = replace(actions[2], clock_ms=200_000)
    actions[3] = replace(actions[3], clock_ms=150_000)
    result = AnalyticalPossessionParser().parse(
        scored_context(2, 0), replace(stream, actions=tuple(actions))
    )
    assert result.status is ParseStatus.VALID
    row = result.possessions[0]
    assert (row.start_seconds_remaining_period_ms, row.end_seconds_remaining_period_ms) == (
        100_000,
        150_000,
    )
    assert row.start_boundary == EventBoundary(2, BoundarySide.BEFORE)
    assert row.end_boundary == EventBoundary(4, BoundarySide.AFTER)
    assert any(i.code == "clock_increase" and i.sequence_number == 3 for i in result.issues)


def test_clock_ties_are_warnings_with_sequence_order_unchanged() -> None:
    result = AnalyticalPossessionParser().parse(scored_context(2, 2), game(shot(10), shot(20)))
    assert result.status is ParseStatus.VALID
    assert [row.end_boundary.sequence_number for row in result.possessions] == [2, 3]
    assert any(
        issue.code == "clock_tie"
        and issue.severity is IssueSeverity.WARNING
        and issue.sequence_number == 3
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "code",
    [
        "duplicate_provider_action",
        "clock_tie",
        "clock_increase",
        "offense_inferred",
        "missing_score_checkpoint",
        "synthesized_period_boundary",
        "unsupported_administrative_action",
        "orphan_steal_annotation",
    ],
)
def test_normalizer_warnings_preserve_safe_diagnostics_without_changing_output(code: str) -> None:
    parser = AnalyticalPossessionParser()
    context = scored_context(2, 0)
    stream = game(shot(10))
    issue = PossessionIssue(IssueSeverity.WARNING, code, 2, 1)
    result = parser.parse(context, replace(stream, issues=(issue,)))
    original = parser.parse(context, stream)
    assert result.status is ParseStatus.VALID
    assert issue in result.issues
    assert result.possessions == original.possessions
    assert result.output_sha256 == original.output_sha256


@pytest.mark.parametrize(
    "code",
    [
        "mixed_game",
        "raw_canonical_mismatch",
        "invalid_game_teams",
        "unknown_control_action",
        "invalid_score_delta",
        "score_regression",
    ],
)
def test_normalizer_errors_remain_bounded_fatal_diagnostics(code: str) -> None:
    stream = replace(game(shot(10)), issues=(PossessionIssue(IssueSeverity.ERROR, code, 2),))
    assert_invalid(AnalyticalPossessionParser().parse(scored_context(2, 0), stream), code, 2)


def test_exhausted_warning_budget_cannot_hide_score_reconciliation_failure() -> None:
    stream = checkpoint(game(shot(10)), 2, 9, 0, 9)
    stream = replace(
        stream,
        issues=tuple(
            PossessionIssue(IssueSeverity.WARNING, "duplicate_provider_action", n)
            for n in range(1, 33)
        ),
    )
    result = AnalyticalPossessionParser().parse(scored_context(2, 0), stream)
    assert_invalid(result, "invalid_score_delta", 2)
    assert len(result.issues) == 32
    assert all(
        {f.name for f in fields(issue)}
        == {"severity", "code", "sequence_number", "related_sequence_number"}
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("signal", "code"), [(Unsupported(True), "unknown_control_action"), (shot(99), "unknown_owner")]
)
def test_invalid_control_cannot_fabricate_valid_scored_output(
    signal: PossessionSignal, code: str
) -> None:
    assert_invalid(
        AnalyticalPossessionParser().parse(scored_context(2, 0), game(signal, shot(10))), code, 2
    )


def test_identical_input_has_exact_typed_output_and_repeatable_hash() -> None:
    context = scored_context(2, 0)
    stream = game(shot(10))
    parser = AnalyticalPossessionParser()
    result = parser.parse(context, stream)
    assert result.status is ParseStatus.VALID
    assert result == parser.parse(context, stream)
    projection = possession_output_projection(
        parse_revision_id=context.parse_revision_id,
        game=context.game,
        possessions=result.possessions,
    )
    expected = {
        "schema": "possession-output-v1",
        "parse_revision_id": context.parse_revision_id,
        "rows": [
            {
                "game_id": "0022500123",
                "season": "2025-26",
                "season_start_year": 2025,
                "season_type": "regular_season",
                "game_date": "2026-01-02",
                "home_team_id": 10,
                "away_team_id": 20,
                "possession_number": 1,
                "offense_possession_number": 1,
                "period": 1,
                "offense_team_id": 10,
                "defense_team_id": 20,
                "start_sequence_number": 2,
                "start_boundary_side": "before",
                "end_sequence_number": 2,
                "end_boundary_side": "after",
                "start_seconds_remaining_period_ms": 600_000,
                "end_seconds_remaining_period_ms": 600_000,
                "seconds_remaining_game_ms": 2_760_000,
                "home_score_before": 0,
                "away_score_before": 0,
                "home_score": 2,
                "away_score": 0,
                "score_diff": 2,
                "offense_points": 2,
                "defense_points": 0,
                "home_possessions": 1,
                "away_possessions": 0,
                "end_reason": "made_field_goal",
                "source_event_count": 1,
            }
        ],
    }
    assert projection == expected
    assert result.output_sha256 == canonical_json_sha256(expected)


@pytest.mark.parametrize(
    "field",
    [
        "normalizer_version",
        "parser_version",
        "rules_version",
        "output_schema_version",
        "pbp_document_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
    ],
)
def test_changed_version_or_input_invalidates_revision_and_output(field: str) -> None:
    original = scored_context(2, 0)
    if field.endswith("version"):
        changed = replace(original, versions=replace(original.versions, **{field: "changed-v2"}))
    else:
        changes: dict[str, object] = {field: "e" * 64}
        changed = replace(original, **changes)  # type: ignore[arg-type]
    changed = replace(
        changed,
        parse_revision_id=parse_revision_id(
            game_id=changed.game.game_id,
            pbp_document_sha256=changed.pbp_document_sha256,
            canonical_event_sha256=changed.canonical_event_sha256,
            game_context_sha256=changed.game_context_sha256,
            versions=changed.versions,
        ),
    )
    first = AnalyticalPossessionParser().parse(original, game(shot(10)))
    second = AnalyticalPossessionParser().parse(changed, game(shot(10)))
    assert first.status is second.status is ParseStatus.VALID
    assert first.context.parse_revision_id != second.context.parse_revision_id
    assert first.possessions == second.possessions
    assert first.output_sha256 != second.output_sha256


def test_operational_lineage_changes_input_token_only() -> None:
    context = scored_context(2, 0)
    tokens = [
        possession_input_token_checksum(
            game_id=context.game.game_id,
            applied_pbp_document_id=UUID(int=n),
            applied_pbp_sha256=context.pbp_document_sha256,
            canonical_event_sha256=context.canonical_event_sha256,
            game_context_sha256=context.game_context_sha256,
            versions=context.versions,
        )
        for n in (1, 2)
    ]
    assert tokens[0] != tokens[1]
    assert {f.name for f in fields(PossessionParseContext)} == {
        "game",
        "versions",
        "pbp_document_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
        "parse_revision_id",
    }
    result = AnalyticalPossessionParser().parse(context, game(shot(10)))
    assert result.output_sha256 is not None
    assert result == AnalyticalPossessionParser().parse(context, game(shot(10)))
