"""Focused fixtures and protocol fakes for lineup application contract tests."""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    BuildLineupsResult,
    CurrentLineupSnapshotReference,
    CurrentLineupSnapshotResolution,
    GameLineupInputSnapshot,
    LineupAttemptState,
    LineupBuildClaim,
    LineupClaimAction,
    LineupClaimDecision,
    LineupInputToken,
    LineupPublicationState,
    LineupSnapshotAttempt,
    LineupSnapshotVerification,
    LineupVerificationStatus,
)
from nba_live_totals.domain import (
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    canonical_json_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    lineup_output_checksum,
    lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupPlayerBox,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    NormalizedGameLineups,
)
from tests.unit.domain.lineup_helpers import (
    make_invalid_state,
    make_lineup_event,
    make_roster,
    make_segment,
)

VERSIONS = LineupVersions(
    normalizer_version="nba-pbp-v3-lineup-v1",
    reconstructor_version="causal-lineup-v1",
    rules_version="nba-5x5-v1",
    output_schema_version="lineup-segments-v1",
)
DEFAULT_GAME_ID = GameId("0022500123")
RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def uuid4_value(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def make_game(game_id: GameId = DEFAULT_GAME_ID) -> GameContext:
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


def make_document(
    game_id: GameId,
    resource: str,
    *,
    payload_extra: JsonObject | None = None,
) -> SourceDocument:
    payload: JsonObject = {"game": {"gameId": game_id.value}, "resource": resource}
    if payload_extra is not None:
        payload.update(payload_extra)
    endpoint = "boxscoretraditionalv3" if resource == "box_score" else "playbyplayv3"
    return SourceDocument(
        source="nba_stats",
        endpoint=endpoint,
        resource_key=f"{resource}:{game_id.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": game_id.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_player_boxes(game_id: GameId) -> tuple[LineupPlayerBox, ...]:
    return (
        LineupPlayerBox(game_id, 10, 101, "G", True, True, None),
        LineupPlayerBox(game_id, 20, 201, "G", True, True, None),
    )


def make_events(game_id: GameId) -> tuple[PlayByPlayEvent, ...]:
    return (
        PlayByPlayEvent(
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
            description="provider text must not escape",
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
        ),
    )


def make_snapshot(game_id: GameId = DEFAULT_GAME_ID) -> GameLineupInputSnapshot:
    game = make_game(game_id)
    boxes = make_player_boxes(game.game_id)
    events = make_events(game.game_id)
    box_document = make_document(game.game_id, "box_score")
    pbp_document = make_document(game.game_id, "play_by_play")
    return GameLineupInputSnapshot(
        game=game,
        applied_box_score_document_id=uuid4_value(1),
        applied_box_score_sha256=box_document.checksum_sha256,
        applied_pbp_document_id=uuid4_value(2),
        applied_pbp_sha256=pbp_document.checksum_sha256,
        canonical_player_boxes=boxes,
        canonical_events=events,
        canonical_player_box_sha256=canonical_player_boxes_checksum(boxes),
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        box_score_source_document=box_document,
        pbp_source_document=pbp_document,
    )


def make_claim(
    game_id: GameId = DEFAULT_GAME_ID,
    *,
    state: LineupAttemptState = LineupAttemptState.ALLOCATED,
    invalid: bool = False,
    generation: int = 1,
) -> LineupBuildClaim:
    snapshot = make_snapshot(game_id)
    token = LineupInputToken.from_snapshot(snapshot, VERSIONS)
    result = make_result(make_context(snapshot, token), invalid=invalid)
    populated = state in {
        LineupAttemptState.WRITING,
        LineupAttemptState.VERIFIED,
        LineupAttemptState.PUBLISHED,
    }
    attempt = LineupSnapshotAttempt(
        game=snapshot.game,
        snapshot_id=uuid4_value(int(game_id.value[-4:]) + 1000 * generation),
        generation=generation,
        lineup_revision_id=lineup_revision_id(
            game_id=game_id,
            box_score_document_sha256=token.applied_box_score_sha256,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_player_box_sha256=token.canonical_player_box_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=VERSIONS,
        ),
        input_token=token,
        state=state,
        expected_row_count=len(result.segments) if populated else None,
        expected_output_sha256=lineup_output_checksum(result) if populated else None,
        expected_issue_codes=result.issues if populated else None,
        result_status=result.status if populated else None,
    )
    return LineupBuildClaim(RUN_ID, generation, attempt, snapshot)


def make_context(
    snapshot: GameLineupInputSnapshot, token: LineupInputToken
) -> LineupReconstructionContext:
    return LineupReconstructionContext(
        game=snapshot.game,
        versions=VERSIONS,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        lineup_revision_id=lineup_revision_id(
            game_id=snapshot.game.game_id,
            box_score_document_sha256=token.applied_box_score_sha256,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_player_box_sha256=token.canonical_player_box_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=VERSIONS,
        ),
    )


def make_result(
    context: LineupReconstructionContext, *, invalid: bool = False
) -> LineupReconstructionResult:
    segment = make_segment(
        home_state=make_invalid_state() if invalid else make_segment().home_state
    )
    issues = (
        (LineupIssue(10, "invalid_initial_lineup", 1, segment.start_boundary.side),)
        if invalid
        else ()
    )
    return LineupReconstructionResult(
        context, LineupStatus.INVALID if invalid else LineupStatus.VALID, (segment,), issues
    )


def make_reference(claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
    attempt = replace(claim.attempt, state=LineupAttemptState.PUBLISHED)
    assert attempt.expected_row_count is not None and attempt.expected_output_sha256 is not None
    return CurrentLineupSnapshotReference(
        attempt, attempt.expected_row_count, attempt.expected_output_sha256
    )


def make_resolution(claim: LineupBuildClaim) -> CurrentLineupSnapshotResolution:
    reference = make_reference(claim)
    status = (
        LineupPublicationState.INVALID
        if reference.result_status is LineupStatus.INVALID
        else LineupPublicationState.CURRENT
    )
    return CurrentLineupSnapshotResolution(status, reference)


class FakeNormalizer:
    def __init__(self, events: MutableSequence[str]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.calls: list[GameLineupInputSnapshot] = []

    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        self.events.append("normalize")
        self.calls.append(snapshot)
        if self.error is not None:
            raise self.error
        return NormalizedGameLineups(
            snapshot.game.game_id, make_roster(10), make_roster(20), (make_lineup_event(1),)
        )


class FakeReconstructor:
    def __init__(self, events: MutableSequence[str], *, invalid: bool = False) -> None:
        self.events = events
        self.invalid = invalid
        self.calls: list[LineupReconstructionContext] = []

    def reconstruct(
        self, context: LineupReconstructionContext, normalized: NormalizedGameLineups
    ) -> LineupReconstructionResult:
        del normalized
        self.events.append("reconstruct")
        self.calls.append(context)
        return make_result(context, invalid=self.invalid)


class FakeWorkStore:
    def __init__(
        self, games: tuple[GameId, ...], events: MutableSequence[str], *, invalid: bool = False
    ) -> None:
        self.games = games
        self.events = events
        self.invalid = invalid
        self.decisions: dict[GameId, LineupClaimDecision] = {}
        self.resolutions: dict[GameId, list[CurrentLineupSnapshotResolution]] = {}
        self.claim_calls: list[GameId] = []
        self.fail_calls: list[tuple[str, str]] = []
        self.poison_calls: list[tuple[str, str]] = []
        self.finished: list[BuildLineupsResult] = []

    async def start_run(self, request: BuildLineupsRequest) -> UUID:
        del request
        self.events.append("start_run")
        return RUN_ID

    async def select_games(self, request: BuildLineupsRequest) -> tuple[GameId, ...]:
        del request
        self.events.append("select_games")
        return self.games

    async def claim_game(
        self, run_id: UUID, game_id: GameId, versions: LineupVersions, *, rebuild: bool
    ) -> LineupClaimDecision:
        assert run_id == RUN_ID and versions == VERSIONS and type(rebuild) is bool
        self.events.append("claim")
        self.claim_calls.append(game_id)
        return self.decisions.get(
            game_id, LineupClaimDecision(LineupClaimAction.BUILD, make_claim(game_id))
        )

    async def mark_writing(
        self,
        claim: LineupBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
        expected_issue_codes: tuple[LineupIssue, ...],
        result_status: LineupStatus,
    ) -> LineupSnapshotAttempt:
        expected = make_result(
            make_context(claim.snapshot, claim.attempt.input_token), invalid=self.invalid
        )
        assert expected_row_count == len(expected.segments)
        assert expected_output_sha256 == lineup_output_checksum(expected)
        assert expected_issue_codes == expected.issues
        assert result_status is expected.status
        self.events.append("mark_writing")
        return replace(
            claim.attempt,
            state=LineupAttemptState.WRITING,
            expected_row_count=expected_row_count,
            expected_output_sha256=expected_output_sha256,
            expected_issue_codes=expected_issue_codes,
            result_status=result_status,
        )

    async def mark_verified(
        self, claim: LineupBuildClaim, verification: LineupSnapshotVerification
    ) -> LineupSnapshotAttempt:
        assert verification.status is LineupVerificationStatus.EXACT
        self.events.append("mark_verified")
        return replace(claim.attempt, state=LineupAttemptState.VERIFIED)

    async def poison_attempt(
        self, claim: LineupBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        del claim
        self.events.append("poison_attempt")
        self.poison_calls.append((error_code, error_type))

    async def publish_snapshot(self, claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
        self.events.append("publish_snapshot")
        return make_reference(claim)

    async def fail_game(self, claim: LineupBuildClaim, *, error_code: str, error_type: str) -> None:
        del claim
        self.events.append("fail_game")
        self.fail_calls.append((error_code, error_type))

    async def resolve_current(
        self, game_id: GameId, versions: LineupVersions
    ) -> CurrentLineupSnapshotResolution:
        assert versions == VERSIONS
        self.events.append("resolve_current")
        sequence = self.resolutions.get(game_id)
        return (
            sequence.pop(0)
            if sequence
            else CurrentLineupSnapshotResolution(LineupPublicationState.ABSENT)
        )

    async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
        assert run_id == RUN_ID
        self.events.append("finish_run")
        self.finished.append(result)


class FakeSnapshotStore:
    def __init__(self, events: MutableSequence[str]) -> None:
        self.events = events
        self.write_calls: list[LineupBuildClaim] = []
        self.verify_calls: list[LineupSnapshotAttempt] = []
        self.write_error: Exception | None = None
        self.verify_error: Exception | None = None
        self.verification: LineupSnapshotVerification | None = None

    async def write_snapshot(
        self, claim: LineupBuildClaim, result: LineupReconstructionResult
    ) -> None:
        del result
        self.events.append("write_snapshot")
        self.write_calls.append(claim)
        if self.write_error is not None:
            raise self.write_error

    async def verify_snapshot(self, attempt: LineupSnapshotAttempt) -> LineupSnapshotVerification:
        self.events.append("verify_snapshot")
        self.verify_calls.append(attempt)
        if self.verify_error is not None:
            raise self.verify_error
        if self.verification is not None:
            return self.verification
        assert attempt.expected_row_count is not None and attempt.expected_output_sha256 is not None
        assert attempt.expected_issue_codes is not None and attempt.result_status is not None
        return LineupSnapshotVerification(
            attempt.snapshot_id,
            LineupVerificationStatus.EXACT,
            attempt.expected_row_count,
            attempt.expected_output_sha256,
            attempt.result_status,
            attempt.expected_issue_codes,
        )

    async def read_snapshot(
        self, reference: CurrentLineupSnapshotReference
    ) -> tuple[LineupSegment, ...]:
        del reference
        return ()
