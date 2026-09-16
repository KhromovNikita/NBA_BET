"""Immutable application contracts for lineup reconstruction."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import cast
from uuid import UUID

from nba_live_totals.domain import (
    BoundarySide,
    GameContext,
    GameId,
    PlayByPlayEvent,
    SeasonId,
    SourceDocument,
    canonical_event_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.analytical_lineage import PbpLineage
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    lineup_input_token_checksum,
)
from nba_live_totals.domain.lineup_hashing import (
    lineup_revision_id as compute_lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupPlayerBox,
    LineupStatus,
    LineupVersions,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_MAX_ISSUES = 32


def _require_sha256(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_positive_bigint(value: object, *, field_name: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_BIGINT:
        raise ValueError(f"{field_name} must be a positive signed 64-bit integer")


def _require_nonnegative_integer(value: object, *, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


def _snapshot_issues(value: object) -> tuple[LineupIssue, ...]:
    try:
        values = tuple(cast(Iterable[object], value))
    except TypeError as error:
        raise ValueError("expected issue codes must be iterable") from error
    if any(not isinstance(issue, LineupIssue) for issue in values):
        raise ValueError("expected issue codes must contain LineupIssue values")
    issues = cast(tuple[LineupIssue, ...], values)
    if len(issues) > _MAX_ISSUES:
        raise ValueError(f"expected issue codes must contain at most {_MAX_ISSUES} values")
    if len(set(issues)) != len(issues):
        raise ValueError("expected issue codes must be unique")
    return issues


def _validate_semantic_metadata(
    *,
    row_count: int,
    output_sha256: str,
    issues: tuple[LineupIssue, ...],
    result_status: LineupStatus,
) -> None:
    _require_positive_bigint(row_count, field_name="expected row count")
    _require_sha256(output_sha256, field_name="expected output SHA-256")
    if not isinstance(result_status, LineupStatus):
        raise ValueError("result status must be a LineupStatus")
    if result_status is LineupStatus.VALID and issues:
        raise ValueError("valid expected metadata cannot contain issues")
    if result_status is LineupStatus.INVALID and not issues:
        raise ValueError("invalid expected metadata requires at least one issue")


def _validate_canonical_issues(issues: tuple[LineupIssue, ...], *, game: GameContext) -> None:
    """Validate the deterministic issue projection when the game sides are known."""
    home_team_id = game.home_team_id
    away_team_id = game.away_team_id
    valid_team_ids = {home_team_id, away_team_id}
    if any(issue.team_id not in valid_team_ids for issue in issues):
        raise ValueError("lineup issue team must be the game's home or away team")

    def issue_key(issue: LineupIssue) -> tuple[int, int, int]:
        boundary_order = 0 if issue.boundary_side is BoundarySide.BEFORE else 1
        team_order = 0 if issue.team_id == home_team_id else 1
        return (issue.sequence_number, boundary_order, team_order)

    if issues != tuple(sorted(issues, key=issue_key)):
        raise ValueError("lineup issues must use canonical issue order")


@dataclass(frozen=True, slots=True)
class GameLineupInputSnapshot:
    """One immutable, transactionally consistent box-score and PBP input."""

    game: GameContext
    applied_box_score_document_id: UUID
    applied_box_score_sha256: str
    applied_pbp_document_id: UUID
    applied_pbp_sha256: str
    canonical_player_boxes: tuple[LineupPlayerBox, ...]
    canonical_events: tuple[PlayByPlayEvent, ...]
    canonical_player_box_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str
    box_score_source_document: SourceDocument
    pbp_source_document: SourceDocument

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameContext):
            raise ValueError("game must be a GameContext")
        if type(self.applied_box_score_document_id) is not UUID:
            raise ValueError("applied box-score document ID must be a UUID")
        if type(self.applied_pbp_document_id) is not UUID:
            raise ValueError("applied PBP document ID must be a UUID")
        if not isinstance(self.box_score_source_document, SourceDocument):
            raise ValueError("box-score source document must be a SourceDocument")
        if not isinstance(self.pbp_source_document, SourceDocument):
            raise ValueError("PBP source document must be a SourceDocument")
        for field_name, digest in (
            ("applied box-score SHA-256", self.applied_box_score_sha256),
            ("applied PBP SHA-256", self.applied_pbp_sha256),
            ("canonical player-box SHA-256", self.canonical_player_box_sha256),
            ("canonical event SHA-256", self.canonical_event_sha256),
            ("game context SHA-256", self.game_context_sha256),
        ):
            _require_sha256(digest, field_name=field_name)
        try:
            player_boxes = tuple(self.canonical_player_boxes)
            events = tuple(self.canonical_events)
        except TypeError as error:
            raise ValueError("canonical player boxes and events must be iterable") from error
        if not player_boxes:
            raise ValueError("canonical player boxes must not be empty")
        if any(not isinstance(row, LineupPlayerBox) for row in player_boxes):
            raise ValueError("canonical player boxes must contain LineupPlayerBox values")
        if any(row.game_id != self.game.game_id for row in player_boxes):
            raise ValueError("canonical player boxes and game context must belong to one game")
        if player_boxes != tuple(
            sorted(player_boxes, key=lambda row: (row.team_id, row.player_id))
        ):
            raise ValueError("canonical player boxes must be sorted by team and player ID")
        if any(not isinstance(event, PlayByPlayEvent) for event in events):
            raise ValueError("canonical events must contain PlayByPlayEvent values")
        if any(event.game_id != self.game.game_id for event in events):
            raise ValueError("canonical events and game context must belong to one game")
        if events and tuple(event.sequence_number for event in events) != tuple(
            range(1, len(events) + 1)
        ):
            raise ValueError("canonical events must be contiguous and in sequence order")
        game_id = self.game.game_id.value
        if self.box_score_source_document.resource_key != f"box_score:{game_id}":
            raise ValueError("box-score source document resource must match the snapshot game")
        if self.pbp_source_document.resource_key != f"play_by_play:{game_id}":
            raise ValueError("PBP source document resource must match the snapshot game")
        if self.box_score_source_document.checksum_sha256 != self.applied_box_score_sha256:
            raise ValueError("source document checksum must match applied box-score checksum")
        if self.pbp_source_document.checksum_sha256 != self.applied_pbp_sha256:
            raise ValueError("source document checksum must match applied PBP checksum")
        if canonical_player_boxes_checksum(player_boxes) != self.canonical_player_box_sha256:
            raise ValueError("canonical player-box checksum does not match canonical rows")
        if canonical_event_checksum(events) != self.canonical_event_sha256:
            raise ValueError("canonical event checksum does not match canonical events")
        if game_context_checksum(self.game) != self.game_context_sha256:
            raise ValueError("game context checksum does not match game context")
        object.__setattr__(self, "canonical_player_boxes", player_boxes)
        object.__setattr__(self, "canonical_events", events)

    @property
    def box_score_document(self) -> SourceDocument:
        """Return the immutable applied raw box-score document."""
        return self.box_score_source_document

    @property
    def pbp_document(self) -> SourceDocument:
        """Return the immutable applied raw PBP document."""
        return self.pbp_source_document


@dataclass(frozen=True, slots=True)
class LineupInputToken:
    """Freshness identity combining both inputs, canonical rows and versions."""

    game_id: GameId
    applied_box_score_document_id: UUID
    applied_box_score_sha256: str
    applied_pbp_document_id: UUID
    applied_pbp_sha256: str
    canonical_player_box_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str
    versions: LineupVersions
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if type(self.applied_box_score_document_id) is not UUID:
            raise ValueError("applied box-score document ID must be a UUID")
        if type(self.applied_pbp_document_id) is not UUID:
            raise ValueError("applied PBP document ID must be a UUID")
        if not isinstance(self.versions, LineupVersions):
            raise ValueError("versions must be LineupVersions")
        for field_name, digest in (
            ("applied box-score SHA-256", self.applied_box_score_sha256),
            ("applied PBP SHA-256", self.applied_pbp_sha256),
            ("canonical player-box SHA-256", self.canonical_player_box_sha256),
            ("canonical event SHA-256", self.canonical_event_sha256),
            ("game context SHA-256", self.game_context_sha256),
        ):
            _require_sha256(digest, field_name=field_name)
        object.__setattr__(
            self,
            "sha256",
            lineup_input_token_checksum(
                game_id=self.game_id,
                applied_box_score_document_id=self.applied_box_score_document_id,
                applied_box_score_sha256=self.applied_box_score_sha256,
                applied_pbp_document_id=self.applied_pbp_document_id,
                applied_pbp_sha256=self.applied_pbp_sha256,
                canonical_player_box_sha256=self.canonical_player_box_sha256,
                canonical_event_sha256=self.canonical_event_sha256,
                game_context_sha256=self.game_context_sha256,
                versions=self.versions,
            ),
        )

    @classmethod
    def from_snapshot(
        cls, snapshot: GameLineupInputSnapshot, versions: LineupVersions
    ) -> LineupInputToken:
        if not isinstance(snapshot, GameLineupInputSnapshot):
            raise ValueError("snapshot must be a GameLineupInputSnapshot")
        return cls(
            game_id=snapshot.game.game_id,
            applied_box_score_document_id=snapshot.applied_box_score_document_id,
            applied_box_score_sha256=snapshot.applied_box_score_sha256,
            applied_pbp_document_id=snapshot.applied_pbp_document_id,
            applied_pbp_sha256=snapshot.applied_pbp_sha256,
            canonical_player_box_sha256=snapshot.canonical_player_box_sha256,
            canonical_event_sha256=snapshot.canonical_event_sha256,
            game_context_sha256=snapshot.game_context_sha256,
            versions=versions,
        )

    @property
    def pbp_lineage(self) -> PbpLineage:
        return PbpLineage(
            game_id=self.game_id,
            input_pbp_document_id=self.applied_pbp_document_id,
            input_pbp_sha256=self.applied_pbp_sha256,
            canonical_event_sha256=self.canonical_event_sha256,
            game_context_sha256=self.game_context_sha256,
        )


class LineupBuildMode(StrEnum):
    MATERIALIZE = "materialize"
    VERIFY = "verify"


class LineupBuildStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class LineupAttemptState(StrEnum):
    ALLOCATED = "allocated"
    WRITING = "writing"
    VERIFIED = "verified"
    PUBLISHED = "published"
    POISONED = "poisoned"
    SUPERSEDED = "superseded"


class LineupClaimAction(StrEnum):
    BUILD = "build"
    RESUME = "resume"
    SKIP_CURRENT = "skip_current"
    BUSY = "busy"


class LineupVerificationStatus(StrEnum):
    EXACT = "exact"
    ABSENT = "absent"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


class LineupPublicationState(StrEnum):
    ABSENT = "absent"
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BuildLineupsRequest:
    seasons: tuple[SeasonId, ...]
    game_id: GameId | None = None
    mode: LineupBuildMode = LineupBuildMode.MATERIALIZE
    rebuild: bool = False

    def __post_init__(self) -> None:
        try:
            seasons = tuple(self.seasons)
        except TypeError as error:
            raise ValueError("seasons must be iterable") from error
        object.__setattr__(self, "seasons", seasons)
        if not seasons:
            raise ValueError("at least one season is required")
        if any(not isinstance(season, SeasonId) for season in seasons):
            raise ValueError("seasons must contain SeasonId values")
        if any(
            following.start_year != preceding.start_year + 1
            for preceding, following in pairwise(seasons)
        ):
            raise ValueError("seasons must be a continuous ascending range")
        if self.game_id is not None and not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if self.game_id is not None and len(seasons) != 1:
            raise ValueError("a targeted game requires exactly one season")
        if self.game_id is not None and (
            self.game_id.value[3:5] != f"{seasons[0].start_year % 100:02d}"
        ):
            raise ValueError("a targeted game must belong to the selected season")
        if not isinstance(self.mode, LineupBuildMode):
            raise ValueError("mode must be a LineupBuildMode")
        if type(self.rebuild) is not bool:
            raise ValueError("rebuild must be a boolean")
        if self.mode is LineupBuildMode.VERIFY and self.rebuild:
            raise ValueError("verify mode and rebuild are mutually exclusive")

    @classmethod
    def for_range(
        cls,
        season_from: SeasonId,
        season_to: SeasonId,
        *,
        mode: LineupBuildMode = LineupBuildMode.MATERIALIZE,
        rebuild: bool = False,
    ) -> BuildLineupsRequest:
        if not isinstance(season_from, SeasonId) or not isinstance(season_to, SeasonId):
            raise ValueError("season range must contain SeasonId values")
        if season_from.start_year > season_to.start_year:
            raise ValueError("season range must be ascending")
        return cls(
            seasons=tuple(
                SeasonId(f"{year:04d}-{(year + 1) % 100:02d}")
                for year in range(season_from.start_year, season_to.start_year + 1)
            ),
            mode=mode,
            rebuild=rebuild,
        )


@dataclass(frozen=True, slots=True)
class BuildLineupsResult:
    run_id: UUID
    status: LineupBuildStatus
    discovered: int
    materialized: int
    invalid: int
    skipped_current: int
    verified: int
    failed: int
    busy: int

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        if not isinstance(self.status, LineupBuildStatus):
            raise ValueError("status must be a LineupBuildStatus")
        for field_name in (
            "discovered",
            "materialized",
            "invalid",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        ):
            _require_nonnegative_integer(getattr(self, field_name), field_name=field_name)
        successful = self.materialized + self.skipped_current + self.verified
        operational = successful + self.failed + self.busy
        if operational != self.discovered:
            raise ValueError("terminal run counters must account for discovered games")
        if self.invalid > successful:
            raise ValueError("invalid games must be bounded by successful outcomes")
        if self.invalid and self.status not in {
            LineupBuildStatus.PARTIAL,
            LineupBuildStatus.INTERRUPTED,
        }:
            raise ValueError("invalid games force partial completed run status")
        if self.status is LineupBuildStatus.SUCCEEDED and (self.failed or self.busy):
            raise ValueError("succeeded runs cannot contain failed or busy games")
        unsuccessful = self.failed + self.busy
        if self.status is LineupBuildStatus.PARTIAL and (
            not successful or not (unsuccessful or self.invalid)
        ):
            raise ValueError("partial runs require successful and non-successful outcomes")
        if self.status is LineupBuildStatus.FAILED and successful:
            raise ValueError("failed runs cannot contain successful outcomes")


@dataclass(frozen=True, slots=True)
class LineupSnapshotAttempt:
    game: GameContext
    snapshot_id: UUID
    generation: int
    lineup_revision_id: str
    input_token: LineupInputToken
    state: LineupAttemptState
    expected_row_count: int | None = None
    expected_output_sha256: str | None = None
    expected_issue_codes: tuple[LineupIssue, ...] | None = None
    result_status: LineupStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameContext):
            raise ValueError("game must be a GameContext")
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        _require_positive_bigint(self.generation, field_name="generation")
        _require_sha256(self.lineup_revision_id, field_name="lineup revision ID")
        if not isinstance(self.input_token, LineupInputToken):
            raise ValueError("input token must be a LineupInputToken")
        if self.input_token.game_id != self.game.game_id:
            raise ValueError("attempt and input token must belong to the same game")
        if self.input_token.game_context_sha256 != game_context_checksum(self.game):
            raise ValueError("attempt input token must match the game context")
        expected_revision = compute_lineup_revision_id(
            game_id=self.game.game_id,
            box_score_document_sha256=self.input_token.applied_box_score_sha256,
            pbp_document_sha256=self.input_token.applied_pbp_sha256,
            canonical_player_box_sha256=self.input_token.canonical_player_box_sha256,
            canonical_event_sha256=self.input_token.canonical_event_sha256,
            game_context_sha256=self.input_token.game_context_sha256,
            versions=self.input_token.versions,
        )
        if self.lineup_revision_id != expected_revision:
            raise ValueError("lineup revision ID must match the input token")
        if not isinstance(self.state, LineupAttemptState):
            raise ValueError("state must be a LineupAttemptState")
        values = (
            self.expected_row_count,
            self.expected_output_sha256,
            self.expected_issue_codes,
            self.result_status,
        )
        populated = all(value is not None for value in values)
        if any(value is not None for value in values) and not populated:
            raise ValueError("expected metadata must be all null or all populated")
        if self.state is LineupAttemptState.ALLOCATED and populated:
            raise ValueError("allocated attempt requires null expected metadata")
        if (
            self.state
            in {
                LineupAttemptState.WRITING,
                LineupAttemptState.VERIFIED,
                LineupAttemptState.PUBLISHED,
            }
            and not populated
        ):
            raise ValueError(f"{self.state.value} attempt requires expected metadata")
        if populated:
            assert self.expected_row_count is not None
            assert self.expected_output_sha256 is not None
            assert self.expected_issue_codes is not None
            assert self.result_status is not None
            issues = _snapshot_issues(self.expected_issue_codes)
            _validate_canonical_issues(issues, game=self.game)
            _validate_semantic_metadata(
                row_count=self.expected_row_count,
                output_sha256=self.expected_output_sha256,
                issues=issues,
                result_status=self.result_status,
            )
            object.__setattr__(self, "expected_issue_codes", issues)

    @property
    def game_id(self) -> GameId:
        return self.game.game_id


@dataclass(frozen=True, slots=True)
class LineupBuildClaim:
    run_id: UUID
    claim_version: int
    attempt: LineupSnapshotAttempt
    snapshot: GameLineupInputSnapshot

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        _require_positive_bigint(self.claim_version, field_name="claim version")
        if not isinstance(self.attempt, LineupSnapshotAttempt):
            raise ValueError("attempt must be a LineupSnapshotAttempt")
        if not isinstance(self.snapshot, GameLineupInputSnapshot):
            raise ValueError("snapshot must be a GameLineupInputSnapshot")
        expected_token = LineupInputToken.from_snapshot(
            self.snapshot, self.attempt.input_token.versions
        )
        if self.attempt.game != self.snapshot.game:
            raise ValueError("claim attempt and snapshot must belong to the same game")
        if self.attempt.input_token != expected_token:
            raise ValueError("claim attempt token must match the input snapshot")


@dataclass(frozen=True, slots=True)
class CurrentLineupSnapshotReference:
    attempt: LineupSnapshotAttempt
    row_count: int
    output_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, LineupSnapshotAttempt):
            raise ValueError("attempt must be a LineupSnapshotAttempt")
        if self.attempt.state is not LineupAttemptState.PUBLISHED:
            raise ValueError("current reference requires a published attempt")
        _require_positive_bigint(self.row_count, field_name="row count")
        _require_sha256(self.output_sha256, field_name="output SHA-256")
        if (
            self.row_count != self.attempt.expected_row_count
            or self.output_sha256 != self.attempt.expected_output_sha256
        ):
            raise ValueError("current reference output must match the published attempt")

    @property
    def result_status(self) -> LineupStatus:
        assert self.attempt.result_status is not None
        return self.attempt.result_status

    @property
    def issues(self) -> tuple[LineupIssue, ...]:
        assert self.attempt.expected_issue_codes is not None
        return self.attempt.expected_issue_codes


@dataclass(frozen=True, slots=True)
class LineupClaimDecision:
    action: LineupClaimAction
    claim: LineupBuildClaim | None = None
    current: CurrentLineupSnapshotReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, LineupClaimAction):
            raise ValueError("action must be a LineupClaimAction")
        if self.claim is not None and not isinstance(self.claim, LineupBuildClaim):
            raise ValueError("claim must be a LineupBuildClaim")
        if self.current is not None and not isinstance(
            self.current, CurrentLineupSnapshotReference
        ):
            raise ValueError("current must be a CurrentLineupSnapshotReference")
        if self.action in {LineupClaimAction.BUILD, LineupClaimAction.RESUME}:
            if self.claim is None:
                raise ValueError(f"{self.action.value} decisions require a claim")
            if self.current is not None:
                raise ValueError(f"{self.action.value} decisions cannot include current")
            if self.action is LineupClaimAction.BUILD and (
                self.claim.attempt.state is not LineupAttemptState.ALLOCATED
            ):
                raise ValueError("build decisions require an allocated attempt")
            if self.action is LineupClaimAction.RESUME and self.claim.attempt.state not in {
                LineupAttemptState.ALLOCATED,
                LineupAttemptState.WRITING,
                LineupAttemptState.VERIFIED,
            }:
                raise ValueError(
                    "resume decisions require an allocated, writing or verified attempt"
                )
        elif self.action is LineupClaimAction.SKIP_CURRENT:
            if self.current is None:
                raise ValueError("skip_current decisions require a current reference")
            if self.claim is not None:
                raise ValueError("skip_current decisions cannot include a claim")
        elif self.claim is not None or self.current is not None:
            raise ValueError("busy decisions cannot include a claim or current reference")


@dataclass(frozen=True, slots=True)
class CurrentLineupSnapshotResolution:
    status: LineupPublicationState
    reference: CurrentLineupSnapshotReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, LineupPublicationState):
            raise ValueError("status must be a LineupPublicationState")
        if self.reference is not None and not isinstance(
            self.reference, CurrentLineupSnapshotReference
        ):
            raise ValueError("reference must be a CurrentLineupSnapshotReference")
        if self.status in {
            LineupPublicationState.CURRENT,
            LineupPublicationState.INVALID,
        }:
            if self.reference is None:
                raise ValueError(f"{self.status.value} resolution requires a reference")
            expected = (
                LineupStatus.VALID
                if self.status is LineupPublicationState.CURRENT
                else LineupStatus.INVALID
            )
            if self.reference.result_status is not expected:
                raise ValueError(
                    f"{self.status.value} resolution requires a {expected.value} snapshot"
                )
        elif self.reference is not None:
            raise ValueError(f"{self.status.value} resolution cannot include a reference")


@dataclass(frozen=True, slots=True)
class LineupSnapshotVerification:
    snapshot_id: UUID
    status: LineupVerificationStatus
    row_count: int | None = None
    output_sha256: str | None = None
    result_status: LineupStatus | None = None
    issues: tuple[LineupIssue, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        if not isinstance(self.status, LineupVerificationStatus):
            raise ValueError("status must be a LineupVerificationStatus")
        values = (self.row_count, self.output_sha256, self.result_status, self.issues)
        populated = all(value is not None for value in values)
        if any(value is not None for value in values) and not populated:
            raise ValueError("verification semantic metadata must be all null or all populated")
        if self.status is LineupVerificationStatus.EXACT and not populated:
            raise ValueError("exact verification requires semantic metadata")
        if (
            self.status
            in {
                LineupVerificationStatus.ABSENT,
                LineupVerificationStatus.UNAVAILABLE,
            }
            and populated
        ):
            raise ValueError(f"{self.status.value} verification cannot contain semantic metadata")
        if populated:
            assert self.row_count is not None
            assert self.output_sha256 is not None
            assert self.result_status is not None
            assert self.issues is not None
            issues = _snapshot_issues(self.issues)
            _validate_semantic_metadata(
                row_count=self.row_count,
                output_sha256=self.output_sha256,
                issues=issues,
                result_status=self.result_status,
            )
            object.__setattr__(self, "issues", issues)

    @property
    def is_exact(self) -> bool:
        return self.status is LineupVerificationStatus.EXACT


__all__ = [
    "BuildLineupsRequest",
    "BuildLineupsResult",
    "CurrentLineupSnapshotReference",
    "CurrentLineupSnapshotResolution",
    "GameLineupInputSnapshot",
    "LineupAttemptState",
    "LineupBuildClaim",
    "LineupBuildMode",
    "LineupBuildStatus",
    "LineupClaimAction",
    "LineupClaimDecision",
    "LineupInputToken",
    "LineupPublicationState",
    "LineupSnapshotAttempt",
    "LineupSnapshotVerification",
    "LineupVerificationStatus",
]
