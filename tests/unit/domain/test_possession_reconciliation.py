"""Hand-scored complete streams exercise semantic accounting and checkpoint audits."""

from dataclasses import replace

import pytest

from nba_live_totals.domain.possession_hashing import game_context_checksum, parse_revision_id
from nba_live_totals.domain.possession_parser import AnalyticalPossessionParser
from nba_live_totals.domain.possessions import (
    Administrative,
    ControlAward,
    FieldGoalAttempt,
    FreeThrowAttempt,
    FreeThrowTripEnd,
    Goaltending,
    GoaltendingSide,
    IssueSeverity,
    NormalizedGameActions,
    ParseStatus,
    PossessionParseContext,
    PossessionParseResult,
    PossessionSignal,
    ResumeDisposition,
)
from tests.unit.domain.possession_helpers import game, shot
from tests.unit.domain.test_possession_parser_procedures import TRIP, attempt, start, technical
from tests.unit.domain.test_possessions import make_game_context, make_parse_context


def scored_context(home: int, away: int) -> PossessionParseContext:
    context = make_parse_context(game=make_game_context(home_score=home, away_score=away))
    context = replace(context, game_context_sha256=game_context_checksum(context.game))
    return replace(
        context,
        parse_revision_id=parse_revision_id(
            game_id=context.game.game_id,
            pbp_document_sha256=context.pbp_document_sha256,
            canonical_event_sha256=context.canonical_event_sha256,
            game_context_sha256=context.game_context_sha256,
            versions=context.versions,
        ),
    )


def checkpoint(
    stream: NormalizedGameActions,
    sequence: int,
    home: int | None,
    away: int | None,
    combined: int | None = None,
) -> NormalizedGameActions:
    actions = list(stream.actions)
    actions[sequence - 1] = replace(
        actions[sequence - 1],
        checkpoint_home_score=home,
        checkpoint_away_score=away,
        checkpoint_combined_score=combined,
    )
    return replace(stream, actions=tuple(actions))


def assert_invalid(result: PossessionParseResult, code: str, sequence: int | None) -> None:
    assert result.status is ParseStatus.INVALID
    assert result.possessions == ()
    assert result.output_sha256 is None
    assert any(
        issue.code == code
        and issue.sequence_number == sequence
        and issue.severity is IssueSeverity.ERROR
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("signals", "home", "away", "last"),
    [
        ((shot(10),), 2, 0, 2),
        ((FieldGoalAttempt(20, 3, True, False),), 0, 3, 2),
        ((ControlAward(10, "opening"), Goaltending(20, GoaltendingSide.DEFENSIVE, 3)), 3, 0, 3),
        (
            (
                (start(), attempt(1)),
                (attempt(2), FreeThrowTripEnd(TRIP, ResumeDisposition.OPPONENT_INBOUND)),
            ),
            2,
            0,
            3,
        ),
    ],
)
def test_scoring_signals_explain_cumulative_checkpoints(
    signals: tuple[PossessionSignal | tuple[PossessionSignal, ...], ...],
    home: int,
    away: int,
    last: int,
) -> None:
    stream = checkpoint(game(*signals), last, home, away, home + away)
    result = AnalyticalPossessionParser().parse(scored_context(home, away), stream)
    assert result.status is ParseStatus.VALID
    row = result.possessions[0]
    assert (row.home_score_before, row.away_score_before) == (0, 0)
    assert (row.home_score, row.away_score) == (home, away)
    assert row.offense_points == home + away
    assert row.defense_points == 0


@pytest.mark.parametrize(
    ("sequence", "home", "away", "combined", "code"),
    [
        (1, 2, 0, None, "invalid_score_delta"),
        (2, 3, 0, 3, "invalid_score_delta"),
        (2, 0, 2, 2, "invalid_score_delta"),
        (2, 2, 0, 1, "invalid_score_delta"),
        (2, None, None, 0, "invalid_score_delta"),
        (3, 1, 0, None, "score_regression"),
        (3, 2, 1, None, "invalid_score_delta"),
        (3, None, None, 2, "invalid_score_delta"),
    ],
)
def test_checkpoints_cannot_seed_rewrite_or_invent_points(
    sequence: int, home: int | None, away: int | None, combined: int | None, code: str
) -> None:
    stream = checkpoint(game(shot(10), Administrative("timeout")), sequence, home, away, combined)
    result = AnalyticalPossessionParser().parse(scored_context(2, 0), stream)
    assert_invalid(result, code, sequence)


def test_sparse_checkpoints_are_cumulative_not_event_deltas() -> None:
    stream = game(shot(10), shot(20), FieldGoalAttempt(10, 3, True, False))
    stream = checkpoint(checkpoint(stream, 2, 2, None, 2), 4, 5, 2, 7)
    result = AnalyticalPossessionParser().parse(scored_context(5, 2), stream)
    assert result.status is ParseStatus.VALID
    assert [(r.home_score, r.away_score) for r in result.possessions] == [(2, 0), (2, 2), (5, 2)]
    assert any(
        i.code == "missing_score_checkpoint" and i.sequence_number == 2 for i in result.issues
    )


def test_combined_checkpoint_uses_post_action_total_after_defense_technical() -> None:
    stream = game(ControlAward(10, "opening"), technical(), shot(10))
    stream = checkpoint(checkpoint(stream, 3, 0, 1, 1), 4, 2, 1, 3)
    result = AnalyticalPossessionParser().parse(scored_context(2, 1), stream)
    assert result.status is ParseStatus.VALID
    row = result.possessions[0]
    assert (row.offense_team_id, row.offense_points, row.defense_points, row.score_diff) == (
        10,
        2,
        1,
        1,
    )
    assert row.source_event_count == 3
    wrong = checkpoint(stream, 4, 2, 1, 2)
    assert_invalid(
        AnalyticalPossessionParser().parse(scored_context(2, 1), wrong), "invalid_score_delta", 4
    )


def test_between_possession_technical_becomes_next_baseline() -> None:
    stream = checkpoint(game(shot(10), technical(team=10), shot(20)), 3, 3, 0, 3)
    result = AnalyticalPossessionParser().parse(scored_context(3, 2), stream)
    assert result.status is ParseStatus.VALID
    first, second = result.possessions
    assert (first.home_score, first.away_score) == (2, 0)
    assert (second.home_score_before, second.away_score_before) == (3, 0)
    assert (second.offense_points, second.defense_points, second.score_diff) == (2, 0, 1)
    assert (second.home_possessions, second.away_possessions) == (1, 1)


def test_terminal_technical_baseline_has_no_publishable_row() -> None:
    result = AnalyticalPossessionParser().parse(scored_context(2, 1), game(shot(10), technical()))
    assert_invalid(result, "unattributed_administrative_score", 4)


@pytest.mark.parametrize(("home", "away"), [(0, 0), (3, 0), (2, 1), (0, 2)])
def test_final_game_score_must_match_both_teams_exactly(home: int, away: int) -> None:
    result = AnalyticalPossessionParser().parse(scored_context(home, away), game(shot(10)))
    assert_invalid(result, "final_score_mismatch", None)


def test_missing_checkpoints_warn_but_do_not_prevent_semantic_final_reconciliation() -> None:
    result = AnalyticalPossessionParser().parse(scored_context(2, 0), game(shot(10)))
    assert result.status is ParseStatus.VALID
    assert any(
        i.code == "missing_score_checkpoint" and i.sequence_number == 2 for i in result.issues
    )
    assert result.output_sha256 is not None


def test_ft_award_must_match_effective_make() -> None:
    stream = game((start(), FreeThrowAttempt(TRIP, 1, 2, False, 1)))
    assert_invalid(
        AnalyticalPossessionParser().parse(scored_context(1, 0), stream), "invalid_score_delta", 2
    )


@pytest.mark.parametrize(
    ("team", "side", "points", "final"),
    [
        (10, GoaltendingSide.OFFENSIVE, 1, 0),
        (10, GoaltendingSide.OFFENSIVE, 2, 0),
        (20, GoaltendingSide.DEFENSIVE, 0, 0),
        (20, GoaltendingSide.DEFENSIVE, 1, 1),
    ],
)
def test_goaltending_effect_must_be_zero_offensively_or_field_goal_value_defensively(
    team: int, side: GoaltendingSide, points: int, final: int
) -> None:
    stream = game(ControlAward(10, "opening"), Goaltending(team, side, points))
    assert_invalid(
        AnalyticalPossessionParser().parse(scored_context(final, 0), stream),
        "invalid_score_delta",
        3,
    )
