"""Unit tests for Phase 3 possession application contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from nba_live_totals.application.possessions import (
    BuildMode,
    BuildPossessionsRequest,
    BuildPossessionsResult,
    BuildStatus,
    ClaimAction,
    ClaimDecision,
    CurrentSnapshotReference,
    GamePbpSnapshot,
    PossessionAttemptState,
    PossessionBuildClaim,
    PossessionInputToken,
    PossessionNormalizer,
    PossessionParser,
    PossessionSnapshotAttempt,
    PossessionSnapshotStore,
    PossessionWorkStore,
    SnapshotVerification,
    VerificationStatus,
)
from nba_live_totals.domain import (
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    NormalizedGameActions,
    PlayByPlayEvent,
    Possession,
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
)
from nba_live_totals.domain import (
    parse_revision_id as compute_parse_revision_id,
)
from nba_live_totals.domain.analytical_lineage import PbpLineage

GAME_ID = GameId("0022500123")
SEASON = SeasonId("2025-26")
VERSIONS = PossessionVersions(
    normalizer_version="nba-pbp-v2",
    parser_version="analytical-v1",
    rules_version="nba-2025-v1",
    output_schema_version="possessions-v1",
)


def make_event(*, sequence_number: int = 1) -> PlayByPlayEvent:
    return PlayByPlayEvent(
        game_id=GAME_ID,
        sequence_number=sequence_number,
        action_number=sequence_number,
        action_id=None,
        period=1,
        clock="PT12M00.00S",
        seconds_remaining_period_ms=720_000,
        team_id=None,
        person_id=None,
        action_type="period",
        sub_type="start",
        description="",
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


def make_game() -> GameContext:
    return GameContext(
        game_id=GAME_ID,
        season=SEASON,
        season_type=SeasonType.REGULAR_SEASON,
        game_date=date(2026, 1, 2),
        home_team_id=10,
        away_team_id=20,
        home_score=110,
        away_score=108,
        status=GameStatus.FINAL,
    )


def make_document() -> SourceDocument:
    payload: JsonObject = {"game": {"gameId": GAME_ID.value}, "actions": []}
    return SourceDocument(
        source="nba_stats",
        endpoint="playbyplayv3",
        resource_key=f"play_by_play:{GAME_ID.value}",
        requested_at=datetime(2026, 1, 3, tzinfo=UTC),
        request_parameters={"GameID": GAME_ID.value},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def make_snapshot() -> GamePbpSnapshot:
    game = make_game()
    document = make_document()
    events = (make_event(),)
    return GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=uuid4(),
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=events,
        canonical_event_sha256=canonical_event_checksum(events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )


def make_token(snapshot: GamePbpSnapshot | None = None) -> PossessionInputToken:
    snapshot = snapshot or make_snapshot()
    return PossessionInputToken.from_snapshot(snapshot, VERSIONS)


def test_possession_input_token_exposes_additive_shared_pbp_lineage() -> None:
    token = make_token()

    assert token.pbp_lineage == PbpLineage(
        game_id=token.game_id,
        input_pbp_document_id=token.applied_pbp_document_id,
        input_pbp_sha256=token.applied_pbp_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
    )


def make_attempt(snapshot: GamePbpSnapshot | None = None) -> PossessionSnapshotAttempt:
    snapshot = snapshot or make_snapshot()
    token = make_token(snapshot)
    return PossessionSnapshotAttempt(
        game=snapshot.game,
        snapshot_id=uuid4(),
        generation=1,
        parse_revision_id=compute_parse_revision_id(
            game_id=snapshot.game.game_id,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=token.versions,
        ),
        input_token=token,
        state=PossessionAttemptState.ALLOCATED,
    )


def make_claim() -> PossessionBuildClaim:
    snapshot = make_snapshot()
    return PossessionBuildClaim(
        run_id=uuid4(),
        claim_version=1,
        attempt=make_attempt(snapshot),
        snapshot=snapshot,
    )


def make_reference() -> CurrentSnapshotReference:
    attempt = replace(
        make_attempt(),
        state=PossessionAttemptState.PUBLISHED,
        expected_row_count=1,
        expected_output_sha256="b" * 64,
    )
    return CurrentSnapshotReference(
        attempt=attempt,
        row_count=1,
        output_sha256="b" * 64,
    )


def test_build_request_supports_single_season_and_inclusive_range() -> None:
    single = BuildPossessionsRequest(seasons=(SEASON,))
    season_range = BuildPossessionsRequest.for_range(SeasonId("2023-24"), SeasonId("2025-26"))

    assert single.mode is BuildMode.MATERIALIZE
    assert single.rebuild is False
    assert season_range.seasons == (
        SeasonId("2023-24"),
        SeasonId("2024-25"),
        SeasonId("2025-26"),
    )


def test_build_request_restricts_targeted_games_and_verify_rebuild() -> None:
    assert BuildPossessionsRequest(seasons=(SEASON,), game_id=GAME_ID).game_id == GAME_ID

    with pytest.raises(ValueError, match="exactly one season"):
        BuildPossessionsRequest(
            seasons=(SeasonId("2024-25"), SEASON),
            game_id=GAME_ID,
        )
    with pytest.raises(ValueError, match="selected season"):
        BuildPossessionsRequest(
            seasons=(SeasonId("2024-25"),),
            game_id=GAME_ID,
        )
    with pytest.raises(ValueError, match=r"verify.*rebuild"):
        BuildPossessionsRequest(seasons=(SEASON,), mode=BuildMode.VERIFY, rebuild=True)


def test_game_pbp_snapshot_is_immutable_and_checks_exact_lineage() -> None:
    game = make_game()
    document = make_document()
    mutable_events = [make_event()]
    snapshot = GamePbpSnapshot(
        game=game,
        applied_pbp_document_id=uuid4(),
        applied_pbp_sha256=document.checksum_sha256,
        canonical_events=mutable_events,  # type: ignore[arg-type]  # snapshot boundary
        canonical_event_sha256=canonical_event_checksum(mutable_events),
        game_context_sha256=game_context_checksum(game),
        source_document=document,
    )
    mutable_events.append(make_event(sequence_number=2))

    assert snapshot.canonical_events == (make_event(),)
    with pytest.raises(ValueError, match="document checksum"):
        GamePbpSnapshot(
            game=game,
            applied_pbp_document_id=uuid4(),
            applied_pbp_sha256="c" * 64,
            canonical_events=(make_event(),),
            canonical_event_sha256=canonical_event_checksum((make_event(),)),
            game_context_sha256=game_context_checksum(game),
            source_document=document,
        )
    with pytest.raises(ValueError, match="canonical event checksum"):
        GamePbpSnapshot(
            game=game,
            applied_pbp_document_id=uuid4(),
            applied_pbp_sha256=document.checksum_sha256,
            canonical_events=(make_event(),),
            canonical_event_sha256="d" * 64,
            game_context_sha256=game_context_checksum(game),
            source_document=document,
        )


def test_input_token_is_derived_from_the_immutable_snapshot() -> None:
    snapshot = make_snapshot()
    token = PossessionInputToken.from_snapshot(snapshot, VERSIONS)

    assert token.game_id == snapshot.game.game_id
    assert token.applied_pbp_document_id == snapshot.applied_pbp_document_id
    assert len(token.sha256) == 64
    assert token == PossessionInputToken.from_snapshot(snapshot, VERSIONS)


def test_attempt_and_claim_require_positive_fencing_identity() -> None:
    snapshot = make_snapshot()
    token = make_token(snapshot)

    with pytest.raises(ValueError, match="generation"):
        PossessionSnapshotAttempt(
            snapshot.game,
            uuid4(),
            0,
            "a" * 64,
            token,
            PossessionAttemptState.ALLOCATED,
        )
    with pytest.raises(ValueError, match="claim version"):
        PossessionBuildClaim(uuid4(), 0, make_attempt(snapshot), snapshot)
    with pytest.raises(ValueError, match="same game"):
        PossessionSnapshotAttempt(
            replace(snapshot.game, game_id=GameId("0022500124")),
            uuid4(),
            1,
            "a" * 64,
            token,
            PossessionAttemptState.ALLOCATED,
        )
    with pytest.raises(ValueError, match="parse revision"):
        replace(make_attempt(snapshot), parse_revision_id="a" * 64)

    stale_token = replace(token, game_context_sha256="f" * 64)
    stale_revision = compute_parse_revision_id(
        game_id=stale_token.game_id,
        pbp_document_sha256=stale_token.applied_pbp_sha256,
        canonical_event_sha256=stale_token.canonical_event_sha256,
        game_context_sha256=stale_token.game_context_sha256,
        versions=stale_token.versions,
    )
    with pytest.raises(ValueError, match="game context"):
        replace(
            make_attempt(snapshot),
            input_token=stale_token,
            parse_revision_id=stale_revision,
        )


def test_claim_decision_allows_only_state_valid_payloads() -> None:
    claim = make_claim()
    current = make_reference()

    assert ClaimDecision(ClaimAction.BUILD, claim=claim).claim == claim
    assert ClaimDecision(ClaimAction.RESUME, claim=claim).claim == claim
    writing_claim = replace(
        claim,
        attempt=replace(
            claim.attempt,
            state=PossessionAttemptState.WRITING,
            expected_row_count=1,
            expected_output_sha256="b" * 64,
        ),
    )
    verified_claim = replace(
        writing_claim,
        attempt=replace(writing_claim.attempt, state=PossessionAttemptState.VERIFIED),
    )
    assert ClaimDecision(ClaimAction.RESUME, claim=writing_claim).claim == writing_claim
    assert ClaimDecision(ClaimAction.RESUME, claim=verified_claim).claim == verified_claim
    assert ClaimDecision(ClaimAction.SKIP_CURRENT, current=current).current == current
    assert ClaimDecision(ClaimAction.BUSY) == ClaimDecision(ClaimAction.BUSY)

    with pytest.raises(ValueError, match="require a claim"):
        ClaimDecision(ClaimAction.BUILD)
    with pytest.raises(ValueError, match="require a current"):
        ClaimDecision(ClaimAction.SKIP_CURRENT)
    with pytest.raises(ValueError, match="cannot include"):
        ClaimDecision(ClaimAction.BUSY, claim=claim)


def test_verification_and_build_result_validate_bounded_metadata() -> None:
    exact = SnapshotVerification(
        snapshot_id=uuid4(),
        status=VerificationStatus.EXACT,
        row_count=1,
        output_sha256="b" * 64,
    )
    assert exact.is_exact
    assert not SnapshotVerification(uuid4(), VerificationStatus.UNAVAILABLE).is_exact

    with pytest.raises(ValueError, match="exact verification"):
        SnapshotVerification(uuid4(), VerificationStatus.EXACT)

    result = BuildPossessionsResult(
        run_id=uuid4(),
        status=BuildStatus.SUCCEEDED,
        discovered=2,
        materialized=1,
        skipped_current=1,
        verified=0,
        failed=0,
        busy=0,
    )
    assert result.materialized + result.skipped_current == result.discovered

    with pytest.raises(ValueError, match="nonnegative"):
        BuildPossessionsResult(
            run_id=uuid4(),
            status=BuildStatus.FAILED,
            discovered=0,
            materialized=0,
            skipped_current=0,
            verified=0,
            failed=-1,
            busy=0,
        )


def test_build_result_status_and_accounting_are_consistent() -> None:
    with pytest.raises(ValueError, match="account"):
        BuildPossessionsResult(uuid4(), BuildStatus.SUCCEEDED, 2, 1, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="succeeded"):
        BuildPossessionsResult(uuid4(), BuildStatus.SUCCEEDED, 1, 0, 0, 0, 1, 0)
    with pytest.raises(ValueError, match="partial"):
        BuildPossessionsResult(uuid4(), BuildStatus.PARTIAL, 1, 0, 0, 0, 1, 0)
    with pytest.raises(ValueError, match="failed runs"):
        BuildPossessionsResult(uuid4(), BuildStatus.FAILED, 1, 1, 0, 0, 0, 0)

    interrupted = BuildPossessionsResult(uuid4(), BuildStatus.INTERRUPTED, 3, 1, 0, 0, 0, 0)
    assert interrupted.discovered == 3


class _Normalizer:
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions:
        raise NotImplementedError


class _Parser:
    def parse(
        self,
        context: PossessionParseContext,
        normalized: NormalizedGameActions,
    ) -> PossessionParseResult:
        raise NotImplementedError


class _WorkStore:
    async def start_run(self, request: BuildPossessionsRequest) -> UUID:
        raise NotImplementedError

    async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]:
        raise NotImplementedError

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: PossessionVersions,
        *,
        rebuild: bool,
    ) -> ClaimDecision:
        raise NotImplementedError

    async def mark_writing(
        self,
        claim: PossessionBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
    ) -> PossessionSnapshotAttempt:
        raise NotImplementedError

    async def mark_verified(
        self, claim: PossessionBuildClaim, verification: SnapshotVerification
    ) -> PossessionSnapshotAttempt:
        raise NotImplementedError

    async def poison_attempt(
        self, claim: PossessionBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        raise NotImplementedError

    async def publish_snapshot(self, claim: PossessionBuildClaim) -> CurrentSnapshotReference:
        raise NotImplementedError

    async def fail_game(
        self,
        claim: PossessionBuildClaim,
        *,
        issues: tuple[PossessionIssue, ...],
        error_code: str,
        error_type: str,
    ) -> None:
        raise NotImplementedError

    async def resolve_current(
        self, game_id: GameId, versions: PossessionVersions
    ) -> CurrentSnapshotReference | None:
        raise NotImplementedError

    async def finish_run(self, run_id: UUID, result: BuildPossessionsResult) -> None:
        raise NotImplementedError


class _SnapshotStore:
    async def write_snapshot(
        self, claim: PossessionBuildClaim, result: PossessionParseResult
    ) -> None:
        raise NotImplementedError

    async def verify_snapshot(
        self,
        attempt: PossessionSnapshotAttempt,
    ) -> SnapshotVerification:
        raise NotImplementedError

    async def read_snapshot(self, reference: CurrentSnapshotReference) -> tuple[Possession, ...]:
        raise NotImplementedError


def test_operation_level_ports_are_runtime_checkable() -> None:
    normalizer: PossessionNormalizer = _Normalizer()
    parser: PossessionParser = _Parser()
    work_store: PossessionWorkStore = _WorkStore()
    snapshot_store: PossessionSnapshotStore = _SnapshotStore()

    assert isinstance(normalizer, PossessionNormalizer)
    assert isinstance(parser, PossessionParser)
    assert isinstance(work_store, PossessionWorkStore)
    assert isinstance(snapshot_store, PossessionSnapshotStore)
