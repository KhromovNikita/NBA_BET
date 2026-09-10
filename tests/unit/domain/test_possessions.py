from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest

from nba_live_totals.domain.games import Game, GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.possession_hashing import game_context_checksum, parse_revision_id
from nba_live_totals.domain.possession_parser import AnalyticalPossessionParser
from nba_live_totals.domain.possessions import (
    ActionConsumption,
    Administrative,
    BoundarySide,
    ControlAward,
    EventBoundary,
    FieldGoalAttempt,
    Foul,
    FreeThrowAttempt,
    FreeThrowKind,
    FreeThrowTripEnd,
    FreeThrowTripStart,
    GameContext,
    Goaltending,
    GoaltendingSide,
    IssueSeverity,
    JumpBall,
    NormalizedAction,
    NormalizedGameActions,
    ParseStatus,
    PeriodEnd,
    PeriodStart,
    Possession,
    PossessionIssue,
    PossessionParseContext,
    PossessionParseResult,
    PossessionSignal,
    PossessionVersions,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
    ResumeDisposition,
    StealAnnotation,
    Turnover,
    Unsupported,
    Violation,
    ViolationDisposition,
)

GAME_ID = GameId("0022500123")
SHA256 = "a" * 64


def make_game(**overrides: object) -> Game:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "season": SeasonId("2025-26"),
        "season_type": SeasonType.REGULAR_SEASON,
        "game_date": date(2026, 1, 2),
        "started_at": datetime(2026, 1, 2, 1, tzinfo=UTC),
        "home_team_id": 10,
        "away_team_id": 20,
        "home_score": 110,
        "away_score": 108,
        "status": GameStatus.FINAL,
        "arena_name": "Provider Arena",
        "arena_city": "Provider City",
        "arena_state": "PA",
    }
    values.update(overrides)
    return Game(**values)  # type: ignore[arg-type]


def make_game_context(**overrides: object) -> GameContext:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "season": SeasonId("2025-26"),
        "season_type": SeasonType.REGULAR_SEASON,
        "game_date": date(2026, 1, 2),
        "status": GameStatus.FINAL,
        "home_team_id": 10,
        "away_team_id": 20,
        "home_score": 110,
        "away_score": 108,
    }
    values.update(overrides)
    return GameContext(**values)  # type: ignore[arg-type]


def make_versions(**overrides: object) -> PossessionVersions:
    values: dict[str, object] = {
        "normalizer_version": "nba-pbp-v1",
        "parser_version": "analytical-v1",
        "rules_version": "nba-2025-v1",
        "output_schema_version": "possessions-v1",
    }
    values.update(overrides)
    return PossessionVersions(**values)  # type: ignore[arg-type]


def make_parse_context(**overrides: object) -> PossessionParseContext:
    values: dict[str, object] = {
        "game": make_game_context(),
        "versions": make_versions(),
        "pbp_document_sha256": SHA256,
        "canonical_event_sha256": "b" * 64,
        "game_context_sha256": "c" * 64,
        "parse_revision_id": "d" * 64,
    }
    values.update(overrides)
    return PossessionParseContext(**values)  # type: ignore[arg-type]


def parse_synthetic_game(
    parser: AnalyticalPossessionParser, normalized: NormalizedGameActions
) -> PossessionParseResult:
    """Supply final totals for legacy ownership tests independently of parser output.

    This only tallies declared scoring signals; it does not reconstruct possessions,
    infer ownership, reconcile checkpoints, or derive any row assertion.
    Reconciliation tests supply their own hand-scored contexts instead.
    """
    scores = {10: 0, 20: 0}
    shooters: dict[str, int] = {}
    for action in normalized.actions:
        for signal in action.signals:
            if isinstance(signal, FieldGoalAttempt) and signal.made:
                scores[signal.team] += signal.value
            elif isinstance(signal, FreeThrowTripStart):
                shooters[signal.trip_id] = signal.shooting_team
            elif isinstance(signal, FreeThrowAttempt):
                scores[shooters[signal.trip_id]] += signal.awarded_points
            elif isinstance(signal, Goaltending) and signal.side is GoaltendingSide.DEFENSIVE:
                scores[20 if signal.team == 10 else 10] += signal.awarded_points
    context = make_parse_context(
        game=make_game_context(home_score=scores[10], away_score=scores[20])
    )
    context = replace(context, game_context_sha256=game_context_checksum(context.game))
    context = replace(
        context,
        parse_revision_id=parse_revision_id(
            game_id=context.game.game_id,
            pbp_document_sha256=context.pbp_document_sha256,
            canonical_event_sha256=context.canonical_event_sha256,
            game_context_sha256=context.game_context_sha256,
            versions=context.versions,
        ),
    )
    return parser.parse(context, normalized)


def make_action(**overrides: object) -> NormalizedAction:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "sequence_number": 1,
        "provider_action_number": 7,
        "provider_action_id": None,
        "period": 1,
        "clock_ms": 719_000,
        "actor_team_id": 10,
        "actor_person_id": 101,
        "checkpoint_home_score": 0,
        "checkpoint_away_score": 0,
        "checkpoint_combined_score": 0,
        "consumption": ActionConsumption.TRANSITION,
        "signals": (ControlAward(team=10, reason="opening_tip"),),
    }
    values.update(overrides)
    return NormalizedAction(**values)  # type: ignore[arg-type]


def make_possession(**overrides: object) -> Possession:
    values: dict[str, object] = {
        "game_id": GAME_ID,
        "possession_number": 1,
        "offense_team_id": 10,
        "defense_team_id": 20,
        "offense_possession_number": 1,
        "period": 1,
        "start_boundary": EventBoundary(1, BoundarySide.BEFORE),
        "end_boundary": EventBoundary(3, BoundarySide.AFTER),
        "start_seconds_remaining_period_ms": 720_000,
        "end_seconds_remaining_period_ms": 700_000,
        "seconds_remaining_game_ms": 2_860_000,
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
        "source_event_count": 3,
    }
    values.update(overrides)
    return Possession(**values)  # type: ignore[arg-type]


def test_game_context_accepts_only_complete_final_games_and_copies_projection() -> None:
    game = make_game()

    context = GameContext.from_game(game)

    assert context == make_game_context()
    assert "arena" not in repr(context).casefold()
    assert "started_at" not in repr(context)

    with pytest.raises(ValueError, match="final"):
        GameContext.from_game(make_game(status=GameStatus.LIVE, home_score=None, away_score=None))
    with pytest.raises(ValueError, match="distinct"):
        make_game_context(away_team_id=10)
    with pytest.raises(ValueError, match="positive"):
        make_game_context(home_team_id=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"normalizer_version": ""},
        {"parser_version": "contains space"},
        {"rules_version": "NBA-PBP-v1"},
        {"output_schema_version": "x" * 129},
    ],
)
def test_versions_require_bounded_safe_identifiers(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="version"):
        make_versions(**overrides)


@pytest.mark.parametrize(
    "field_name",
    [
        "pbp_document_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
        "parse_revision_id",
    ],
)
def test_parse_context_requires_lowercase_sha256(field_name: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        make_parse_context(**{field_name: "A" * 64})


def test_event_boundaries_are_totally_ordered_by_sequence_then_side() -> None:
    before = EventBoundary(4, BoundarySide.BEFORE)
    after = EventBoundary(4, BoundarySide.AFTER)

    assert before < after < EventBoundary(5, BoundarySide.BEFORE)
    assert tuple(sorted((after, before))) == (before, after)

    with pytest.raises(ValueError, match="sequence"):
        EventBoundary(True, BoundarySide.BEFORE)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: make_game_context(season_type="bogus"),
        lambda: EventBoundary(1, "bogus"),  # type: ignore[arg-type]
        lambda: Rebound(10, "bogus", ProviderReboundSide.UNKNOWN, ReboundDisposition.NORMAL_LIVE),  # type: ignore[arg-type]
        lambda: Rebound(10, ReboundActor.TEAM, "bogus", ReboundDisposition.NORMAL_LIVE),  # type: ignore[arg-type]
        lambda: Rebound(10, ReboundActor.TEAM, ProviderReboundSide.UNKNOWN, "bogus"),  # type: ignore[arg-type]
        lambda: FreeThrowTripStart(
            "ft:0022500123:6:0",
            10,
            "bogus",  # type: ignore[arg-type]
            1,
            ResumeDisposition.OPPONENT_INBOUND,
        ),
        lambda: FreeThrowTripStart(
            "ft:0022500123:6:0",
            10,
            FreeThrowKind.ORDINARY,
            1,
            "bogus",  # type: ignore[arg-type]
        ),
        lambda: FreeThrowTripEnd("ft:0022500123:6:0", "bogus"),  # type: ignore[arg-type]
        lambda: Violation(10, "bogus"),  # type: ignore[arg-type]
        lambda: Goaltending(10, "bogus", 2),  # type: ignore[arg-type]
        lambda: make_action(consumption="bogus"),
        lambda: PossessionIssue("bogus", "clock_tie"),  # type: ignore[arg-type]
        lambda: make_possession(end_reason="bogus"),
        lambda: PossessionParseResult(
            context=make_parse_context(),
            status="bogus",  # type: ignore[arg-type]
            possessions=(),
            issues=(PossessionIssue(IssueSeverity.ERROR, "incomplete_play_by_play"),),
            output_sha256=None,
        ),
    ],
)
def test_all_enum_fields_reject_values_outside_the_closed_vocabulary(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError, match="invalid"):
        factory()


def test_closed_signals_validate_teams_numbers_and_resume_coupling() -> None:
    signals = (
        PeriodStart(),
        PeriodEnd(),
        FieldGoalAttempt(team=10, value=3, made=True, and_one_candidate=False),
        Rebound(
            rebounding_team=10,
            actor=ReboundActor.PLAYER,
            provider_side=ProviderReboundSide.OFFENSIVE,
            disposition=ReboundDisposition.NORMAL_LIVE,
        ),
        Turnover(committing_team=10, gained_by=20, turnover_kind="bad_pass"),
        StealAnnotation(team=20, related_sequence_number=5),
        Foul(
            committing_team=20,
            foul_kind="shooting",
            shooting_team=10,
            related_sequence_number=4,
        ),
        FreeThrowTripStart(
            trip_id="ft:0022500123:6:0",
            shooting_team=10,
            free_throw_kind=FreeThrowKind.ORDINARY,
            attempts=2,
            resume_disposition=ResumeDisposition.OPPONENT_INBOUND,
        ),
        FreeThrowAttempt(
            trip_id="ft:0022500123:6:0", attempt=1, total=2, made=True, awarded_points=1
        ),
        FreeThrowTripEnd(
            trip_id="ft:0022500123:6:0",
            resolution=ResumeDisposition.OPPONENT_INBOUND,
        ),
        JumpBall(winner_team=10),
        Violation(team=20, disposition=ViolationDisposition.RETAIN_OFFENSE),
        Goaltending(team=20, side=GoaltendingSide.DEFENSIVE, awarded_points=2),
        ControlAward(team=10, reason="opening_tip"),
        Administrative(administrative_kind="timeout"),
        Unsupported(control_relevant=False),
    )

    assert len(signals) == 16
    assert "description" not in repr(signals).casefold()

    with pytest.raises(ValueError, match="resume team"):
        FreeThrowTripStart(
            trip_id="ft:0022500123:6:0",
            shooting_team=10,
            free_throw_kind=FreeThrowKind.FLAGRANT,
            attempts=2,
            resume_disposition=ResumeDisposition.RETAIN_TEAM,
        )
    with pytest.raises(ValueError, match="resume team"):
        FreeThrowTripStart(
            trip_id="ft:0022500123:6:0",
            shooting_team=10,
            free_throw_kind=FreeThrowKind.ORDINARY,
            attempts=2,
            resume_disposition=ResumeDisposition.LIVE_REBOUND,
            resume_team=10,
        )
    with pytest.raises(ValueError, match="awarded points"):
        FreeThrowAttempt(
            trip_id="ft:0022500123:6:0", attempt=1, total=2, made=True, awarded_points=True
        )


def test_normalized_actions_snapshot_iterables_and_reject_empty_or_mixed_signals() -> None:
    signal_list: list[PossessionSignal] = [ControlAward(team=10, reason="opening_tip")]
    action = make_action(signals=signal_list)
    signal_list.append(Administrative(administrative_kind="late_mutation"))

    actions_list = [action]
    issue_list = [PossessionIssue(IssueSeverity.WARNING, "offense_inferred", 1)]
    normalized = NormalizedGameActions(
        GAME_ID,
        actions_list,  # type: ignore[arg-type]  # exercises runtime snapshotting
        issue_list,  # type: ignore[arg-type]  # exercises runtime snapshotting
    )
    actions_list.append(make_action(sequence_number=2))
    issue_list.clear()

    assert action.signals == (ControlAward(team=10, reason="opening_tip"),)
    assert normalized.actions == (action,)
    assert len(normalized.issues) == 1

    with pytest.raises(ValueError, match="signals"):
        make_action(signals=[])
    with pytest.raises(ValueError, match="signal"):
        make_action(signals=("provider description",))
    with pytest.raises(ValueError, match="period"):
        make_action(period=65_536)
    with pytest.raises(ValueError, match="clock"):
        make_action(clock_ms=True)
    with pytest.raises(ValueError, match="one game"):
        NormalizedGameActions(
            GAME_ID,
            (make_action(game_id=GameId("0022500124")),),
        )


def test_nested_identity_and_context_types_are_validated_at_the_domain_boundary() -> None:
    with pytest.raises(ValueError, match="game ID"):
        make_game_context(game_id="0022500123")
    with pytest.raises(ValueError, match="season"):
        make_game_context(season="2025-26")
    with pytest.raises(ValueError, match="game context"):
        make_parse_context(game=object())
    with pytest.raises(ValueError, match="versions"):
        make_parse_context(versions=object())
    with pytest.raises(ValueError, match="game ID"):
        make_action(game_id="0022500123")
    with pytest.raises(ValueError, match="game ID"):
        make_possession(game_id="0022500123")


def test_issue_identifiers_are_safe_and_never_carry_provider_text() -> None:
    issue = PossessionIssue(
        severity=IssueSeverity.ERROR,
        code="unknown_control_action",
        sequence_number=8,
        related_sequence_number=7,
    )

    assert repr(issue) == (
        "PossessionIssue(severity=<IssueSeverity.ERROR: 'error'>, "
        "code='unknown_control_action', sequence_number=8, related_sequence_number=7)"
    )
    with pytest.raises(ValueError, match="code"):
        PossessionIssue(IssueSeverity.ERROR, "provider said: bad action")
    with pytest.raises(ValueError, match="sequence"):
        PossessionIssue(IssueSeverity.WARNING, "clock_tie", sequence_number=False)


def test_possession_validates_distinct_teams_period_boundaries_clocks_and_scores() -> None:
    possession = make_possession()

    assert possession.start_boundary < possession.end_boundary
    with pytest.raises(ValueError, match="distinct"):
        make_possession(defense_team_id=10)
    with pytest.raises(ValueError, match="period"):
        make_possession(period=65_536)
    with pytest.raises(ValueError, match="boundary"):
        make_possession(start_boundary=EventBoundary(4, BoundarySide.AFTER))
    with pytest.raises(ValueError, match="clock"):
        make_possession(end_seconds_remaining_period_ms=-1)
    with pytest.raises(ValueError, match="score"):
        make_possession(home_score=True)


def test_parse_results_snapshot_tuples_and_enforce_validity_contract() -> None:
    context = make_parse_context()
    possessions = [make_possession()]
    warnings = [PossessionIssue(IssueSeverity.WARNING, "clock_tie", 2)]

    result = PossessionParseResult(
        context=context,
        status=ParseStatus.VALID,
        possessions=possessions,  # type: ignore[arg-type]  # runtime snapshotting
        issues=warnings,  # type: ignore[arg-type]  # runtime snapshotting
        output_sha256="e" * 64,
    )
    possessions.clear()
    warnings.clear()

    assert len(result.possessions) == 1
    assert len(result.issues) == 1

    error = PossessionIssue(IssueSeverity.ERROR, "incomplete_play_by_play")
    with pytest.raises(ValueError, match="valid result"):
        PossessionParseResult(
            context=context,
            status=ParseStatus.VALID,
            possessions=(),
            issues=(error,),
            output_sha256="e" * 64,
        )
    with pytest.raises(ValueError, match="invalid result"):
        PossessionParseResult(
            context=context,
            status=ParseStatus.INVALID,
            possessions=(),
            issues=(),
            output_sha256=None,
        )
    with pytest.raises(ValueError, match="output"):
        PossessionParseResult(
            context=context,
            status=ParseStatus.INVALID,
            possessions=(),
            issues=(error,),
            output_sha256="e" * 64,
        )


def test_domain_snapshots_are_frozen_and_slotted() -> None:
    context = make_game_context()

    assert not hasattr(context, "__dict__")
    with pytest.raises(FrozenInstanceError):
        context.home_score = 0  # type: ignore[misc]
