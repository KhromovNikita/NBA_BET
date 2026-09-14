"""Focused in-memory collaborators for possession service unit tests."""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

from nba_live_totals.application.possessions import (
    BuildPossessionsRequest,
    BuildPossessionsResult,
    ClaimAction,
    ClaimDecision,
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionAttemptState,
    PossessionBuildClaim,
    PossessionInputToken,
    PossessionSnapshotAttempt,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import (
    BoundarySide,
    EventBoundary,
    GameContext,
    GameId,
    GameStatus,
    IssueSeverity,
    JsonObject,
    NormalizedGameActions,
    ParseStatus,
    PlayByPlayEvent,
    Possession,
    PossessionEndReason,
    PossessionIssue,
    PossessionParseContext,
    PossessionParseResult,
    PossessionVersions,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
    parse_revision_id,
    possession_output_checksum,
)

RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
VERSIONS = PossessionVersions(
    normalizer_version="nba-pbp-v2",
    parser_version="analytical-v1",
    rules_version="nba-2025-v1",
    output_schema_version="possessions-v1",
)


def _uuid4(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def make_game(game_id: GameId) -> GameContext:
    return GameContext(
        game_id=game_id,
        season=SeasonId("2025-26"),
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        status=GameStatus.FINAL,
        home_team_id=10,
        away_team_id=20,
        home_score=2,
        away_score=0,
    )


def make_snapshot(game_id: GameId) -> GamePbpSnapshot:
    game = make_game(game_id)
    payload: JsonObject = {"game": {"gameId": game_id.value}, "actions": []}
    document = SourceDocument(
        source="nba_stats",
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{game_id.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": game_id.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
    event = PlayByPlayEvent(
        game_id=game_id,
        sequence_number=1,
        action_number=1,
        action_id=None,
        period=1,
        clock="PT12M00.00S",
        seconds_remaining_period_ms=720_000,
        team_id=None,
        person_id=None,
        action_type="period",
        sub_type="start",
        description="sensitive provider description",
        home_score=0,
        away_score=0,
        shot_result=None,
        shot_distance=None,
        legacy_x=None,
        legacy_y=None,
        location=None,
        is_field_goal=False,
        points_total=0,
        video_available=False,
    )
    events = (event,)
    return GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=_uuid4(int(game_id.value[-4:]) + 100),
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )


def make_claim(
    game_id: GameId,
    *,
    run_id: UUID = RUN_ID,
    generation: int = 1,
    state: PossessionAttemptState = PossessionAttemptState.ALLOCATED,
    snapshot_id: UUID | None = None,
    expected_row_count: int | None = None,
    expected_output_sha256: str | None = None,
) -> PossessionBuildClaim:
    snapshot = make_snapshot(game_id)
    token = PossessionInputToken.from_snapshot(snapshot, VERSIONS)
    if state in {
        PossessionAttemptState.WRITING,
        PossessionAttemptState.VERIFIED,
        PossessionAttemptState.PUBLISHED,
    }:
        expected_row_count = expected_row_count or 1
        expected_output_sha256 = expected_output_sha256 or "b" * 64
    attempt = PossessionSnapshotAttempt(
        game=snapshot.game,
        snapshot_id=snapshot_id or _uuid4(int(game_id.value[-4:]) + 1_000 * generation),
        generation=generation,
        parse_revision_id=parse_revision_id(
            game_id=game_id,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=VERSIONS,
        ),
        input_token=token,
        state=state,
        expected_row_count=expected_row_count,
        expected_output_sha256=expected_output_sha256,
    )
    return PossessionBuildClaim(run_id, generation, attempt, snapshot)


def make_possession(game_id: GameId) -> Possession:
    return Possession(
        game_id=game_id,
        possession_number=1,
        offense_team_id=10,
        defense_team_id=20,
        offense_possession_number=1,
        period=1,
        start_boundary=EventBoundary(1, BoundarySide.BEFORE),
        end_boundary=EventBoundary(1, BoundarySide.AFTER),
        start_seconds_remaining_period_ms=719_000,
        end_seconds_remaining_period_ms=718_500,
        seconds_remaining_game_ms=2_878_500,
        home_score_before=0,
        away_score_before=0,
        home_score=2,
        away_score=0,
        score_diff=2,
        offense_points=2,
        defense_points=0,
        home_possessions=1,
        away_possessions=0,
        end_reason=PossessionEndReason.MADE_FIELD_GOAL,
        source_event_count=1,
    )


def make_result(context: PossessionParseContext) -> PossessionParseResult:
    possessions = (make_possession(context.game.game_id),)
    return PossessionParseResult(
        context=context,
        status=ParseStatus.VALID,
        possessions=possessions,
        issues=(),
        output_sha256=possession_output_checksum(
            parse_revision_id=context.parse_revision_id,
            game=context.game,
            possessions=possessions,
        ),
    )


def make_reference(claim: PossessionBuildClaim) -> CurrentSnapshotReference:
    attempt = replace(claim.attempt, state=PossessionAttemptState.PUBLISHED)
    assert attempt.expected_row_count is not None
    assert attempt.expected_output_sha256 is not None
    return CurrentSnapshotReference(
        attempt=attempt,
        row_count=attempt.expected_row_count,
        output_sha256=attempt.expected_output_sha256,
    )


class FakeNormalizer:
    def __init__(self, events: MutableSequence[str]) -> None:
        self.events = events
        self.calls: list[GamePbpSnapshot] = []

    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions:
        self.events.append(f"normalize:{snapshot.game.game_id.value}")
        self.calls.append(snapshot)
        return NormalizedGameActions(snapshot.game.game_id, ())


class FakeParser:
    def __init__(
        self,
        events: MutableSequence[str],
        *,
        invalid_games: set[GameId] | None = None,
    ) -> None:
        self.events = events
        self.invalid_games = invalid_games or set()
        self.calls: list[PossessionParseContext] = []

    def parse(
        self,
        context: PossessionParseContext,
        normalized: NormalizedGameActions,
    ) -> PossessionParseResult:
        del normalized
        game_id = context.game.game_id
        self.events.append(f"parse:{game_id.value}")
        self.calls.append(context)
        if game_id in self.invalid_games:
            return PossessionParseResult(
                context=context,
                status=ParseStatus.INVALID,
                possessions=(),
                issues=(PossessionIssue(IssueSeverity.ERROR, "unknown_semantics"),),
                output_sha256=None,
            )
        return make_result(context)


class FakeWorkStore:
    def __init__(
        self,
        game_ids: tuple[GameId, ...],
        events: MutableSequence[str],
        *,
        decisions: dict[GameId, ClaimDecision] | None = None,
    ) -> None:
        self.game_ids = game_ids
        self.events = events
        self.decisions = decisions or {}
        self.claim_calls: list[tuple[GameId, bool]] = []
        self.finished: list[BuildPossessionsResult] = []
        self.fail_calls: list[tuple[PossessionBuildClaim, str, str]] = []
        self.poison_calls: list[tuple[PossessionBuildClaim, str, str]] = []
        self.resolve_sequences: dict[GameId, list[CurrentSnapshotReference | None]] = {}

    async def start_run(self, request: BuildPossessionsRequest) -> UUID:
        del request
        self.events.append("start_run")
        return RUN_ID

    async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]:
        del request
        self.events.append("select_games")
        return self.game_ids

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: PossessionVersions,
        *,
        rebuild: bool,
    ) -> ClaimDecision:
        assert run_id == RUN_ID
        assert versions == VERSIONS
        self.events.append(f"claim:{game_id.value}")
        self.claim_calls.append((game_id, rebuild))
        return self.decisions.get(
            game_id,
            ClaimDecision(ClaimAction.BUILD, claim=make_claim(game_id, run_id=run_id)),
        )

    async def mark_writing(
        self,
        claim: PossessionBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
    ) -> PossessionSnapshotAttempt:
        self.events.append(f"mark_writing:{claim.attempt.game_id.value}")
        return replace(
            claim.attempt,
            state=PossessionAttemptState.WRITING,
            expected_row_count=expected_row_count,
            expected_output_sha256=expected_output_sha256,
        )

    async def mark_verified(
        self,
        claim: PossessionBuildClaim,
        verification: SnapshotVerification,
    ) -> PossessionSnapshotAttempt:
        assert verification.is_exact
        self.events.append(f"mark_verified:{claim.attempt.game_id.value}")
        return replace(claim.attempt, state=PossessionAttemptState.VERIFIED)

    async def poison_attempt(
        self,
        claim: PossessionBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        self.events.append(f"poison_attempt:{claim.attempt.game_id.value}")
        self.poison_calls.append((claim, error_code, error_type))

    async def publish_snapshot(self, claim: PossessionBuildClaim) -> CurrentSnapshotReference:
        self.events.append(f"publish_snapshot:{claim.attempt.game_id.value}")
        return make_reference(claim)

    async def fail_game(
        self,
        claim: PossessionBuildClaim,
        *,
        issues: tuple[PossessionIssue, ...],
        error_code: str,
        error_type: str,
    ) -> None:
        del issues
        self.events.append(f"fail_game:{claim.attempt.game_id.value}")
        self.fail_calls.append((claim, error_code, error_type))

    async def resolve_current(
        self,
        game_id: GameId,
        versions: PossessionVersions,
    ) -> CurrentSnapshotReference | None:
        assert versions == VERSIONS
        self.events.append(f"resolve_current:{game_id.value}")
        sequence = self.resolve_sequences[game_id]
        return sequence.pop(0)

    async def finish_run(self, run_id: UUID, result: BuildPossessionsResult) -> None:
        assert run_id == RUN_ID
        self.events.append("finish_run")
        self.finished.append(result)


class FakeSnapshotStore:
    def __init__(self, events: MutableSequence[str]) -> None:
        self.events = events
        self.write_calls: list[PossessionBuildClaim] = []
        self.verify_calls: list[PossessionSnapshotAttempt] = []
        self.write_errors: dict[UUID, Exception] = {}
        self.verifications: dict[UUID, SnapshotVerification] = {}
        self.verify_errors: dict[UUID, Exception] = {}

    async def write_snapshot(
        self,
        claim: PossessionBuildClaim,
        result: PossessionParseResult,
    ) -> None:
        del result
        game_id = claim.attempt.game_id
        self.events.append(f"write_snapshot:{game_id.value}")
        self.write_calls.append(claim)
        error = self.write_errors.get(claim.attempt.snapshot_id)
        if error is not None:
            raise error

    async def verify_snapshot(
        self,
        attempt: PossessionSnapshotAttempt,
    ) -> SnapshotVerification:
        self.events.append(f"verify_snapshot:{attempt.game_id.value}")
        self.verify_calls.append(attempt)
        error = self.verify_errors.get(attempt.snapshot_id)
        if error is not None:
            raise error
        configured = self.verifications.get(attempt.snapshot_id)
        if configured is not None:
            return configured
        assert attempt.expected_row_count is not None
        assert attempt.expected_output_sha256 is not None
        return SnapshotVerification(
            attempt.snapshot_id,
            VerificationStatus.EXACT,
            attempt.expected_row_count,
            attempt.expected_output_sha256,
        )

    async def read_snapshot(self, reference: CurrentSnapshotReference) -> tuple[Possession, ...]:
        self.events.append(f"read_snapshot:{reference.attempt.game_id.value}")
        return ()
