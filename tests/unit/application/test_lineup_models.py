"""Contract tests for immutable lineup application values and ports."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from nba_live_totals.application.lineups import (
    BuildLineupsRequest,
    BuildLineupsResult,
    CurrentLineupSnapshotReference,
    CurrentLineupSnapshotResolution,
    GameLineupInputSnapshot,
    LineupAttemptState,
    LineupBuildClaim,
    LineupBuildMode,
    LineupBuildStatus,
    LineupClaimAction,
    LineupClaimDecision,
    LineupInputToken,
    LineupNormalizationError,
    LineupNormalizer,
    LineupPublicationState,
    LineupReconstructor,
    LineupSnapshotAttempt,
    LineupSnapshotStore,
    LineupSnapshotVerification,
    LineupVerificationStatus,
    LineupWorkStore,
)
from nba_live_totals.domain import BoundarySide, GameId, SeasonId
from nba_live_totals.domain.lineup_hashing import (
    lineup_input_token_checksum,
    lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    LineupVersions,
    NormalizedGameLineups,
)

from .lineup_fakes import VERSIONS, make_document, make_snapshot, uuid4_value


def make_issue() -> LineupIssue:
    return LineupIssue(10, "invalid_initial_lineup", 1, BoundarySide.BEFORE)


def make_attempt(
    *,
    state: LineupAttemptState = LineupAttemptState.ALLOCATED,
    status: LineupStatus = LineupStatus.VALID,
) -> LineupSnapshotAttempt:
    snapshot = make_snapshot()
    token = LineupInputToken.from_snapshot(snapshot, VERSIONS)
    populated = state in {
        LineupAttemptState.WRITING,
        LineupAttemptState.VERIFIED,
        LineupAttemptState.PUBLISHED,
    }
    return LineupSnapshotAttempt(
        game=snapshot.game,
        snapshot_id=uuid4_value(10),
        generation=1,
        lineup_revision_id=lineup_revision_id(
            game_id=token.game_id,
            box_score_document_sha256=token.applied_box_score_sha256,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_player_box_sha256=token.canonical_player_box_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=token.versions,
        ),
        input_token=token,
        state=state,
        expected_row_count=1 if populated else None,
        expected_output_sha256="b" * 64 if populated else None,
        expected_issue_codes=(make_issue(),)
        if populated and status is LineupStatus.INVALID
        else (() if populated else None),
        result_status=status if populated else None,
    )


def make_claim(*, state: LineupAttemptState = LineupAttemptState.ALLOCATED) -> LineupBuildClaim:
    attempt = make_attempt(state=state)
    return LineupBuildClaim(uuid4_value(20), 1, attempt, make_snapshot())


def make_reference(*, status: LineupStatus = LineupStatus.VALID) -> CurrentLineupSnapshotReference:
    attempt = make_attempt(state=LineupAttemptState.PUBLISHED, status=status)
    return CurrentLineupSnapshotReference(attempt, 1, "b" * 64)


def test_snapshot_validates_intrinsic_dual_input_identity_and_hashes() -> None:
    snapshot = make_snapshot()
    assert snapshot.canonical_player_boxes == tuple(
        sorted(snapshot.canonical_player_boxes, key=lambda row: (row.team_id, row.player_id))
    )
    assert snapshot.box_score_source_document.resource_key == "box_score:0022500123"
    assert snapshot.pbp_source_document.resource_key == "play_by_play:0022500123"

    with pytest.raises(ValueError, match="box-score checksum"):
        replace(snapshot, applied_box_score_sha256="0" * 64)
    wrong_document = make_document(GameId("0022500999"), "box_score")
    with pytest.raises(ValueError, match="resource"):
        replace(
            snapshot,
            box_score_source_document=wrong_document,
            applied_box_score_sha256=wrong_document.checksum_sha256,
        )
    with pytest.raises(ValueError, match="canonical player-box checksum"):
        replace(snapshot, canonical_player_box_sha256="0" * 64)


def test_snapshot_does_not_compare_raw_and_canonical_cardinality() -> None:
    snapshot = make_snapshot()
    # Raw payload cardinality/meaning is deliberately a post-allocation normalizer concern.
    drifted_document = make_document(
        snapshot.game.game_id,
        "box_score",
        payload_extra={"players": []},
    )
    drifted_snapshot = replace(
        snapshot,
        box_score_source_document=drifted_document,
        applied_box_score_sha256=drifted_document.checksum_sha256,
    )
    assert drifted_snapshot.box_score_source_document.payload["players"] == []
    assert drifted_snapshot.canonical_player_boxes


def test_input_token_exactly_uses_task_three_hash_and_exposes_pbp_lineage() -> None:
    snapshot = make_snapshot()
    token = LineupInputToken.from_snapshot(snapshot, VERSIONS)
    assert token.sha256 == lineup_input_token_checksum(
        game_id=snapshot.game.game_id,
        applied_box_score_document_id=snapshot.applied_box_score_document_id,
        applied_box_score_sha256=snapshot.applied_box_score_sha256,
        applied_pbp_document_id=snapshot.applied_pbp_document_id,
        applied_pbp_sha256=snapshot.applied_pbp_sha256,
        canonical_player_box_sha256=snapshot.canonical_player_box_sha256,
        canonical_event_sha256=snapshot.canonical_event_sha256,
        game_context_sha256=snapshot.game_context_sha256,
        versions=VERSIONS,
    )
    assert token.pbp_lineage.game_id == snapshot.game.game_id
    assert token.pbp_lineage.input_pbp_document_id == snapshot.applied_pbp_document_id
    assert token.pbp_lineage.input_pbp_sha256 == snapshot.applied_pbp_sha256


def test_request_enums_and_frozen_range_contract() -> None:
    request = BuildLineupsRequest.for_range(SeasonId("2024-25"), SeasonId("2025-26"))
    assert request.seasons == (SeasonId("2024-25"), SeasonId("2025-26"))
    assert LineupBuildMode.VERIFY.value == "verify"
    assert LineupBuildStatus.PARTIAL.value == "partial"
    assert LineupClaimAction.SKIP_CURRENT.value == "skip_current"
    assert LineupVerificationStatus.DIFFERENT.value == "different"
    assert LineupPublicationState.INVALID.value == "invalid"
    with pytest.raises(FrozenInstanceError):
        request.rebuild = True  # type: ignore[misc]
    with pytest.raises(ValueError, match="mutually exclusive"):
        BuildLineupsRequest((SeasonId("2025-26"),), mode=LineupBuildMode.VERIFY, rebuild=True)


def test_attempt_metadata_tracks_pre_and_post_write_terminal_states() -> None:
    allocated = make_attempt()
    assert allocated.expected_issue_codes is None
    pre_write_poisoned = replace(allocated, state=LineupAttemptState.POISONED)
    assert pre_write_poisoned.expected_row_count is None

    writing = make_attempt(state=LineupAttemptState.WRITING)
    post_write_poisoned = replace(writing, state=LineupAttemptState.POISONED)
    post_write_superseded = replace(writing, state=LineupAttemptState.SUPERSEDED)
    assert post_write_poisoned.expected_issue_codes == ()
    assert post_write_superseded.result_status is LineupStatus.VALID

    for state in (
        LineupAttemptState.WRITING,
        LineupAttemptState.VERIFIED,
        LineupAttemptState.PUBLISHED,
    ):
        with pytest.raises(ValueError, match="requires expected metadata"):
            replace(allocated, state=state)
    with pytest.raises(ValueError, match="all null or all populated"):
        replace(allocated, state=LineupAttemptState.POISONED, expected_row_count=1)


def test_attempt_requires_status_consistent_unique_bounded_issues() -> None:
    writing = make_attempt(state=LineupAttemptState.WRITING)
    issue = make_issue()
    with pytest.raises(ValueError, match=r"valid.*issues"):
        replace(writing, expected_issue_codes=(issue,))
    with pytest.raises(ValueError, match=r"invalid.*issue"):
        replace(writing, result_status=LineupStatus.INVALID, expected_issue_codes=())
    with pytest.raises(ValueError, match="unique"):
        replace(
            writing,
            result_status=LineupStatus.INVALID,
            expected_issue_codes=(issue, issue),
        )
    invalid = replace(
        writing,
        result_status=LineupStatus.INVALID,
        expected_issue_codes=(issue,),
    )
    assert invalid.expected_issue_codes == (issue,)


def test_attempt_rejects_foreign_and_noncanonical_issue_order() -> None:
    writing = make_attempt(state=LineupAttemptState.WRITING, status=LineupStatus.INVALID)
    foreign = LineupIssue(999, "invalid_initial_lineup", 1, BoundarySide.BEFORE)
    with pytest.raises(ValueError, match="home or away"):
        replace(writing, expected_issue_codes=(foreign,))

    home = LineupIssue(10, "home_failure", 1, BoundarySide.BEFORE)
    away = LineupIssue(20, "away_failure", 1, BoundarySide.BEFORE)
    with pytest.raises(ValueError, match="canonical issue order"):
        replace(writing, expected_issue_codes=(away, home))


def test_issue_limit_accepts_exactly_32_and_rejects_33() -> None:
    writing = make_attempt(state=LineupAttemptState.WRITING, status=LineupStatus.INVALID)
    issues = tuple(
        issue
        for sequence in range(1, 17)
        for issue in (
            LineupIssue(10, f"home_failure_{sequence}", sequence, BoundarySide.BEFORE),
            LineupIssue(20, f"away_failure_{sequence}", sequence, BoundarySide.BEFORE),
        )
    )
    accepted = replace(writing, expected_issue_codes=issues)
    assert accepted.expected_issue_codes == issues
    with pytest.raises(ValueError, match="at most 32"):
        replace(
            writing,
            expected_issue_codes=(
                *issues,
                LineupIssue(10, "home_failure_17", 17, BoundarySide.BEFORE),
            ),
        )


@pytest.mark.parametrize(
    "state",
    [LineupAttemptState.WRITING, LineupAttemptState.PUBLISHED],
)
def test_foreign_and_reversed_issues_are_rejected_for_writing_and_published(
    state: LineupAttemptState,
) -> None:
    attempt = make_attempt(state=state, status=LineupStatus.INVALID)
    foreign = LineupIssue(999, "foreign_team", 1, BoundarySide.BEFORE)
    with pytest.raises(ValueError, match="home or away"):
        replace(attempt, expected_issue_codes=(foreign,))

    home = LineupIssue(10, "home_failure", 1, BoundarySide.BEFORE)
    away = LineupIssue(20, "away_failure", 1, BoundarySide.BEFORE)
    with pytest.raises(ValueError, match="canonical issue order"):
        replace(attempt, expected_issue_codes=(away, home))


def test_current_invalid_resolution_preserves_validated_issue_metadata() -> None:
    current = make_reference(status=LineupStatus.INVALID)
    resolution = CurrentLineupSnapshotResolution(LineupPublicationState.INVALID, current)
    assert resolution.reference is current
    assert resolution.reference.issues == (make_issue(),)


def test_snapshot_copies_input_iterables_before_caller_mutation() -> None:
    snapshot = make_snapshot()
    boxes = list(snapshot.canonical_player_boxes)
    events = list(snapshot.canonical_events)
    copied = replace(
        snapshot,
        canonical_player_boxes=boxes,  # type: ignore[arg-type]
        canonical_events=events,  # type: ignore[arg-type]
    )
    boxes.clear()
    events.clear()
    assert copied.canonical_player_boxes
    assert copied.canonical_events

    seasons = [SeasonId("2025-26")]
    request = BuildLineupsRequest(seasons)  # type: ignore[arg-type]
    seasons.clear()
    assert request.seasons == (SeasonId("2025-26"),)


@pytest.mark.parametrize(
    "factory, field_name",
    [
        (
            lambda: BuildLineupsRequest(
                (SeasonId("2025-26"),),
                rebuild=1,  # type: ignore[arg-type]
            ),
            "rebuild",
        ),
        (
            lambda: BuildLineupsResult(uuid4(), LineupBuildStatus.FAILED, 1, 0, 0, 0, 0, True, 0),
            "failed",
        ),
        (lambda: replace(make_attempt(), generation=True), "generation"),
    ],
)
def test_new_model_boundaries_reject_bool_as_integer(factory: object, field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        factory()  # type: ignore[operator]


def test_build_result_accounts_operational_outcomes_and_orthogonal_invalid() -> None:
    result = BuildLineupsResult(uuid4(), LineupBuildStatus.PARTIAL, 2, 1, 1, 1, 0, 0, 0)
    assert result.materialized + result.skipped_current == result.discovered
    assert result.invalid == 1
    with pytest.raises(ValueError, match="account"):
        replace(result, discovered=3)
    with pytest.raises(ValueError, match="successful"):
        replace(result, invalid=3)
    with pytest.raises(ValueError, match="partial"):
        replace(result, status=LineupBuildStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="account"):
        BuildLineupsResult(uuid4(), LineupBuildStatus.INTERRUPTED, 2, 1, 0, 0, 0, 0, 0)


def test_interrupted_result_retains_observed_invalid_success() -> None:
    result = BuildLineupsResult(uuid4(), LineupBuildStatus.INTERRUPTED, 2, 1, 1, 0, 0, 1, 0)
    assert (result.status, result.materialized, result.invalid, result.failed) == (
        LineupBuildStatus.INTERRUPTED,
        1,
        1,
        1,
    )


def test_claim_decision_current_reference_and_resolution_payloads() -> None:
    claim = make_claim()
    current = make_reference()
    invalid = make_reference(status=LineupStatus.INVALID)
    assert LineupClaimDecision(LineupClaimAction.BUILD, claim=claim).claim == claim
    assert LineupClaimDecision(LineupClaimAction.SKIP_CURRENT, current=current).current == current
    assert (
        CurrentLineupSnapshotResolution(LineupPublicationState.CURRENT, current).reference
        == current
    )
    assert (
        CurrentLineupSnapshotResolution(LineupPublicationState.INVALID, invalid).reference
        == invalid
    )
    assert CurrentLineupSnapshotResolution(LineupPublicationState.ABSENT).reference is None
    with pytest.raises(ValueError, match="valid"):
        CurrentLineupSnapshotResolution(LineupPublicationState.CURRENT, invalid)
    with pytest.raises(ValueError, match="cannot include"):
        CurrentLineupSnapshotResolution(LineupPublicationState.STALE, current)


def test_snapshot_verification_semantic_metadata_contract() -> None:
    exact = LineupSnapshotVerification(
        uuid4_value(10),
        LineupVerificationStatus.EXACT,
        1,
        "b" * 64,
        LineupStatus.VALID,
        (),
    )
    assert exact.is_exact
    assert not LineupSnapshotVerification(uuid4_value(10), LineupVerificationStatus.ABSENT).is_exact
    for status in (
        LineupVerificationStatus.ABSENT,
        LineupVerificationStatus.UNAVAILABLE,
    ):
        with pytest.raises(ValueError, match="cannot contain semantic metadata"):
            replace(exact, status=status)
    assert not LineupSnapshotVerification(
        uuid4_value(10), LineupVerificationStatus.DIFFERENT
    ).is_exact


def test_different_verification_can_report_safe_observed_semantic_metadata() -> None:
    issue = make_issue()
    different = LineupSnapshotVerification(
        uuid4_value(10),
        LineupVerificationStatus.DIFFERENT,
        1,
        "c" * 64,
        LineupStatus.INVALID,
        (issue,),
    )
    assert different.status is LineupVerificationStatus.DIFFERENT
    assert different.result_status is LineupStatus.INVALID
    assert different.issues == (issue,)


@pytest.mark.parametrize(
    "code",
    [
        "input_resource_not_applied",
        "input_document_identity_mismatch",
        "input_document_checksum_mismatch",
        "raw_canonical_cardinality_mismatch",
        "raw_canonical_event_mismatch",
        "raw_canonical_roster_mismatch",
        "unsupported_document_schema",
        "empty_completed_game_pbp",
    ],
)
def test_normalization_error_exposes_only_allowlisted_safe_code(code: str) -> None:
    error = LineupNormalizationError(code)
    assert error.code == code
    assert str(error) == code
    with pytest.raises(ValueError, match="allowlisted"):
        LineupNormalizationError("raw player John Doe: secret")


class _Normalizer:
    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        raise NotImplementedError


class _Reconstructor:
    def reconstruct(
        self,
        context: LineupReconstructionContext,
        normalized: NormalizedGameLineups,
    ) -> LineupReconstructionResult:
        raise NotImplementedError


class _WorkStore:
    async def start_run(self, request: BuildLineupsRequest) -> UUID:
        raise NotImplementedError

    async def select_games(self, request: BuildLineupsRequest) -> tuple[GameId, ...]:
        raise NotImplementedError

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: LineupVersions,
        *,
        rebuild: bool,
    ) -> LineupClaimDecision:
        raise NotImplementedError

    async def mark_writing(
        self,
        claim: LineupBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
        expected_issue_codes: tuple[LineupIssue, ...],
        result_status: LineupStatus,
    ) -> LineupSnapshotAttempt:
        raise NotImplementedError

    async def mark_verified(
        self,
        claim: LineupBuildClaim,
        verification: LineupSnapshotVerification,
    ) -> LineupSnapshotAttempt:
        raise NotImplementedError

    async def poison_attempt(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        raise NotImplementedError

    async def publish_snapshot(self, claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
        raise NotImplementedError

    async def fail_game(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        raise NotImplementedError

    async def resolve_current(
        self, game_id: GameId, versions: LineupVersions
    ) -> CurrentLineupSnapshotResolution:
        raise NotImplementedError

    async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
        raise NotImplementedError


class _SnapshotStore:
    async def write_snapshot(
        self,
        claim: LineupBuildClaim,
        result: LineupReconstructionResult,
    ) -> None:
        raise NotImplementedError

    async def verify_snapshot(self, attempt: LineupSnapshotAttempt) -> LineupSnapshotVerification:
        raise NotImplementedError

    async def read_snapshot(
        self, reference: CurrentLineupSnapshotReference
    ) -> tuple[LineupSegment, ...]:
        raise NotImplementedError


def test_ports_are_runtime_checkable_and_expose_exact_signatures() -> None:
    assert isinstance(_Normalizer(), LineupNormalizer)
    assert isinstance(_Reconstructor(), LineupReconstructor)
    assert isinstance(_WorkStore(), LineupWorkStore)
    assert isinstance(_SnapshotStore(), LineupSnapshotStore)

    expected = {
        LineupNormalizer: {
            "normalize": (
                ("self", "snapshot", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2),
                NormalizedGameLineups,
            ),
        },
        LineupReconstructor: {
            "reconstruct": (
                (
                    "self",
                    "context",
                    "normalized",
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 3,
                ),
                LineupReconstructionResult,
            ),
        },
        LineupWorkStore: {
            "start_run": (
                ("self", "request", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2),
                UUID,
            ),
            "select_games": (
                ("self", "request", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2),
                tuple[GameId, ...],
            ),
            "claim_game": (
                (
                    "self",
                    "run_id",
                    "game_id",
                    "versions",
                    "rebuild",
                    (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    ),
                ),
                LineupClaimDecision,
            ),
            "mark_writing": (
                (
                    "self",
                    "claim",
                    "expected_row_count",
                    "expected_output_sha256",
                    "expected_issue_codes",
                    "result_status",
                    (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                        inspect.Parameter.KEYWORD_ONLY,
                        inspect.Parameter.KEYWORD_ONLY,
                        inspect.Parameter.KEYWORD_ONLY,
                    ),
                ),
                LineupSnapshotAttempt,
            ),
            "mark_verified": (
                (
                    "self",
                    "claim",
                    "verification",
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 3,
                ),
                LineupSnapshotAttempt,
            ),
            "poison_attempt": (
                (
                    "self",
                    "claim",
                    "error_code",
                    "error_type",
                    (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                        inspect.Parameter.KEYWORD_ONLY,
                    ),
                ),
                None,
            ),
            "publish_snapshot": (
                (("self", "claim", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2)),
                CurrentLineupSnapshotReference,
            ),
            "fail_game": (
                (
                    "self",
                    "claim",
                    "error_code",
                    "error_type",
                    (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                        inspect.Parameter.KEYWORD_ONLY,
                    ),
                ),
                None,
            ),
            "resolve_current": (
                (
                    "self",
                    "game_id",
                    "versions",
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 3,
                ),
                CurrentLineupSnapshotResolution,
            ),
            "finish_run": (
                (
                    "self",
                    "run_id",
                    "result",
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 3,
                ),
                None,
            ),
        },
        LineupSnapshotStore: {
            "write_snapshot": (
                (
                    "self",
                    "claim",
                    "result",
                    (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 3,
                ),
                None,
            ),
            "verify_snapshot": (
                (("self", "attempt", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2)),
                LineupSnapshotVerification,
            ),
            "read_snapshot": (
                (("self", "reference", (inspect.Parameter.POSITIONAL_OR_KEYWORD,) * 2)),
                tuple[LineupSegment, ...],
            ),
        },
    }
    for protocol, methods in expected.items():
        for method_name, (shape, return_annotation) in methods.items():  # type: ignore[attr-defined]
            signature = inspect.signature(getattr(protocol, method_name))
            names, kinds = shape[0:-1], shape[-1]
            assert tuple(signature.parameters) == names
            assert tuple(parameter.kind for parameter in signature.parameters.values()) == kinds
            assert signature.return_annotation == return_annotation
