"""Immutable application contracts for Phase 5 feature materialization."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import cast
from uuid import UUID

from nba_live_totals.application.lineups.models import CurrentLineupSnapshotReference
from nba_live_totals.application.possessions.models import CurrentSnapshotReference, GamePbpSnapshot
from nba_live_totals.domain.analytical_lineage import PbpLineage, require_matching_pbp_lineage
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum,
    canonical_team_boxes_checksum,
    dependency_input_checksum,
    dependency_revision_checksum,
    feature_input_token_checksum,
    feature_revision_id,
)
from nba_live_totals.domain.features import (
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureResultStatus,
    FeatureVersions,
    HistoricalPlayerBox,
    HistoricalTeamBox,
)
from nba_live_totals.domain.games import Game, GameStatus
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineup_hashing import lineup_output_checksum
from nba_live_totals.domain.lineups import (
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
)
from nba_live_totals.domain.possession_hashing import (
    game_context_checksum,
    possession_output_checksum,
)
from nba_live_totals.domain.possessions import GameContext, Possession

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_MAX_ISSUES = 32
_MAX_TARGET_PLAYERS = 64
_MAX_DEPENDENCIES = 640


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _positive(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_BIGINT:
        raise ValueError(f"{name} must be a positive signed 64-bit integer")


def _nonnegative(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _tuple_of[T](value: object, expected: type[T], name: str) -> tuple[T, ...]:
    try:
        items = tuple(cast(Iterable[object], value))
    except TypeError as error:
        raise ValueError(f"{name} must be iterable") from error
    if any(not isinstance(item, expected) for item in items):
        raise ValueError(f"{name} contains an invalid value")
    return cast(tuple[T, ...], items)


def _player_ids(value: object) -> tuple[int, ...]:
    try:
        items = tuple(cast(Iterable[object], value))
    except TypeError as error:
        raise ValueError("target player IDs must be iterable") from error
    if any(type(item) is not int or item <= 0 for item in items):
        raise ValueError("target player IDs must be positive integers")
    result = cast(tuple[int, ...], items)
    if result != tuple(sorted(set(result))):
        raise ValueError("target player IDs must be unique and sorted")
    if len(result) > _MAX_TARGET_PLAYERS:
        raise ValueError("target player IDs exceed the bounded player universe")
    return result


def _issue_codes(value: object) -> tuple[str, ...]:
    try:
        items = tuple(cast(Iterable[object], value))
    except TypeError as error:
        raise ValueError("issue codes must be iterable") from error
    if len(items) > _MAX_ISSUES:
        raise ValueError(f"issue codes must contain at most {_MAX_ISSUES} values")
    if any(type(item) is not str or _SAFE_CODE.fullmatch(item) is None for item in items):
        raise ValueError("issue codes must contain bounded safe codes")
    result = cast(tuple[str, ...], items)
    if len(set(result)) != len(result):
        raise ValueError("issue codes must be unique")
    return result


def _pbp_lineage(snapshot: GamePbpSnapshot) -> PbpLineage:
    return PbpLineage(
        snapshot.game.game_id,
        snapshot.applied_pbp_document_id,
        snapshot.applied_pbp_sha256,
        snapshot.canonical_event_sha256,
        snapshot.game_context_sha256,
    )


class FeatureBuildMode(StrEnum):
    MATERIALIZE = "materialize"
    VERIFY = "verify"


class FeatureBuildStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class FeatureAttemptState(StrEnum):
    ALLOCATED = "allocated"
    WRITING = "writing"
    VERIFIED = "verified"
    PUBLISHED = "published"
    POISONED = "poisoned"
    SUPERSEDED = "superseded"


class FeatureClaimAction(StrEnum):
    BUILD = "build"
    RESUME = "resume"
    SKIP_CURRENT = "skip_current"
    BUSY = "busy"


class FeatureVerificationStatus(StrEnum):
    EXACT = "exact"
    ABSENT = "absent"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


class FeaturePublicationState(StrEnum):
    ABSENT = "absent"
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BuildDatasetRequest:
    seasons: tuple[SeasonId, ...]
    game_id: GameId | None = None
    mode: FeatureBuildMode = FeatureBuildMode.MATERIALIZE
    rebuild: bool = False

    def __post_init__(self) -> None:
        seasons = _tuple_of(self.seasons, SeasonId, "seasons")
        object.__setattr__(self, "seasons", seasons)
        if not seasons:
            raise ValueError("at least one season is required")
        if any(b.start_year != a.start_year + 1 for a, b in pairwise(seasons)):
            raise ValueError("seasons must be a continuous ascending range")
        if self.game_id is not None and not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if self.game_id is not None and len(seasons) != 1:
            raise ValueError("a targeted game requires exactly one season")
        if self.game_id is not None and self.game_id.value[3:5] != (
            f"{seasons[0].start_year % 100:02d}"
        ):
            raise ValueError("a targeted game must belong to the selected season")
        if not isinstance(self.mode, FeatureBuildMode):
            raise ValueError("mode must be a FeatureBuildMode")
        if type(self.rebuild) is not bool:
            raise ValueError("rebuild must be a boolean")
        if self.mode is FeatureBuildMode.VERIFY and self.rebuild:
            raise ValueError("verify mode and rebuild are mutually exclusive")

    @classmethod
    def for_range(
        cls,
        season_from: SeasonId,
        season_to: SeasonId,
        *,
        mode: FeatureBuildMode = FeatureBuildMode.MATERIALIZE,
        rebuild: bool = False,
    ) -> BuildDatasetRequest:
        if not isinstance(season_from, SeasonId) or not isinstance(season_to, SeasonId):
            raise ValueError("season range must contain SeasonId values")
        if season_from.start_year > season_to.start_year:
            raise ValueError("season range must be ascending")
        return cls(
            tuple(
                SeasonId(f"{year:04d}-{(year + 1) % 100:02d}")
                for year in range(season_from.start_year, season_to.start_year + 1)
            ),
            mode=mode,
            rebuild=rebuild,
        )


@dataclass(frozen=True, slots=True)
class BuildDatasetResult:
    run_id: UUID
    status: FeatureBuildStatus
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
        if not isinstance(self.status, FeatureBuildStatus):
            raise ValueError("status must be a FeatureBuildStatus")
        for name in (
            "discovered",
            "materialized",
            "invalid",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        ):
            _nonnegative(getattr(self, name), name)
        successful = self.materialized + self.skipped_current + self.verified
        unsuccessful = self.failed + self.busy
        if successful + unsuccessful != self.discovered:
            raise ValueError("terminal run counters must account for discovered games")
        if self.invalid > successful:
            raise ValueError("invalid games must be bounded by successful outcomes")
        if self.invalid and self.status is not FeatureBuildStatus.PARTIAL:
            raise ValueError("invalid games force partial run status")
        if self.status is FeatureBuildStatus.SUCCEEDED and unsuccessful:
            raise ValueError("succeeded runs cannot contain failed or busy games")
        if self.status is FeatureBuildStatus.PARTIAL and (
            not successful or not (unsuccessful or self.invalid)
        ):
            raise ValueError("partial runs require successful and non-successful outcomes")
        if self.status is FeatureBuildStatus.FAILED and successful:
            raise ValueError("failed runs cannot contain successful outcomes")


@dataclass(frozen=True, slots=True)
class FeatureTargetSnapshot:
    game: Game
    pbp_snapshot: GamePbpSnapshot
    possession_reference: CurrentSnapshotReference
    lineup_reference: CurrentLineupSnapshotReference

    def __post_init__(self) -> None:
        if not isinstance(self.game, Game) or self.game.status is not GameStatus.FINAL:
            raise ValueError("feature target requires a final Game")
        if not isinstance(self.pbp_snapshot, GamePbpSnapshot):
            raise ValueError("PBP snapshot must be a GamePbpSnapshot")
        if not isinstance(self.possession_reference, CurrentSnapshotReference):
            raise ValueError("possession reference must be current")
        if not isinstance(self.lineup_reference, CurrentLineupSnapshotReference):
            raise ValueError("lineup reference must be current")
        context = GameContext.from_game(self.game)
        if not (
            self.pbp_snapshot.game
            == self.possession_reference.attempt.game
            == self.lineup_reference.attempt.game
            == context
        ):
            raise ValueError("target game context must match all source attempts")
        target_lineage = _pbp_lineage(self.pbp_snapshot)
        try:
            require_matching_pbp_lineage(
                target_lineage, self.possession_reference.attempt.input_token.pbp_lineage
            )
            require_matching_pbp_lineage(
                target_lineage, self.lineup_reference.attempt.input_token.pbp_lineage
            )
        except ValueError as error:
            raise ValueError("target PBP lineage must match exactly") from error


def _lineup_result(
    target: FeatureTargetSnapshot, segments: tuple[LineupSegment, ...]
) -> LineupReconstructionResult:
    attempt = target.lineup_reference.attempt
    token = attempt.input_token
    context = LineupReconstructionContext(
        game=attempt.game,
        versions=token.versions,
        box_score_document_sha256=token.applied_box_score_sha256,
        pbp_document_sha256=token.applied_pbp_sha256,
        canonical_player_box_sha256=token.canonical_player_box_sha256,
        canonical_event_sha256=token.canonical_event_sha256,
        game_context_sha256=token.game_context_sha256,
        lineup_revision_id=attempt.lineup_revision_id,
    )
    return LineupReconstructionResult(
        context,
        target.lineup_reference.result_status,
        segments,
        target.lineup_reference.issues,
    )


@dataclass(frozen=True, slots=True)
class FeatureTargetRows:
    target: FeatureTargetSnapshot
    possessions: tuple[Possession, ...]
    lineup_segments: tuple[LineupSegment, ...]
    target_player_ids: tuple[int, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, FeatureTargetSnapshot):
            raise ValueError("target must be a FeatureTargetSnapshot")
        possessions = _tuple_of(self.possessions, Possession, "target possessions")
        segments = _tuple_of(self.lineup_segments, LineupSegment, "target lineup segments")
        if len(possessions) != self.target.possession_reference.row_count:
            raise ValueError("target possession output row count does not match")
        if (
            possession_output_checksum(
                parse_revision_id=self.target.possession_reference.attempt.parse_revision_id,
                game=self.target.possession_reference.attempt.game,
                possessions=possessions,
            )
            != self.target.possession_reference.output_sha256
        ):
            raise ValueError("target possession output hash does not match")
        if len(segments) != self.target.lineup_reference.row_count:
            raise ValueError("target lineup output row count does not match")
        try:
            result = _lineup_result(self.target, segments)
        except ValueError as error:
            raise ValueError("target lineup output is invalid") from error
        if lineup_output_checksum(result) != self.target.lineup_reference.output_sha256:
            raise ValueError("target lineup output hash does not match")
        players: set[int] = set()
        for segment in segments:
            for side in (segment.home_state, segment.away_state):
                if side.status is LineupStatus.VALID:
                    players.update(side.player_ids)
        ids = _player_ids(tuple(sorted(players)))
        object.__setattr__(self, "possessions", possessions)
        object.__setattr__(self, "lineup_segments", segments)
        object.__setattr__(self, "target_player_ids", ids)


@dataclass(frozen=True, slots=True)
class HistoricalPossessionSource:
    dependency: FeatureDependency
    possession_reference: CurrentSnapshotReference

    def __post_init__(self) -> None:
        if not isinstance(self.dependency, FeatureDependency):
            raise ValueError("dependency must be a FeatureDependency")
        if not isinstance(self.possession_reference, CurrentSnapshotReference):
            raise ValueError("historical possession reference must be current")
        dependency = self.dependency
        attempt = self.possession_reference.attempt
        if dependency.dependency_game_id != attempt.game.game_id:
            raise ValueError("historical dependency and possession game must match")
        if dependency.game_context_sha256 != game_context_checksum(attempt.game):
            raise ValueError("historical game context hash must match")
        if dependency.possession_snapshot_id != attempt.snapshot_id:
            raise ValueError("historical possession snapshot ID must match")
        if dependency.parse_revision_id != attempt.parse_revision_id:
            raise ValueError("historical parse revision must match")
        if dependency.possession_input_token_sha256 != attempt.input_token.sha256:
            raise ValueError("historical possession input token must match")
        if dependency.possession_output_sha256 != self.possession_reference.output_sha256:
            raise ValueError("historical possession output hash must match")


@dataclass(frozen=True, slots=True)
class FeatureSourceSnapshot:
    target: FeatureTargetSnapshot
    target_player_ids: tuple[int, ...]
    manifest: FeatureDependencyManifest
    historical_team_boxes: tuple[HistoricalTeamBox, ...]
    historical_player_boxes: tuple[HistoricalPlayerBox, ...]
    historical_possession_sources: tuple[HistoricalPossessionSource, ...]
    dependency_input_sha256: str
    dependency_revision_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, FeatureTargetSnapshot):
            raise ValueError("target must be a FeatureTargetSnapshot")
        ids = _player_ids(self.target_player_ids)
        if not isinstance(self.manifest, FeatureDependencyManifest):
            raise ValueError("manifest must be a FeatureDependencyManifest")
        if len(self.manifest.dependencies) > _MAX_DEPENDENCIES:
            raise ValueError("dependency manifest exceeds its bound")
        if any(
            dependency.started_at >= self.target.game.started_at
            for dependency in self.manifest.dependencies
        ):
            raise ValueError("dependencies must precede the target start time")
        teams = _tuple_of(self.historical_team_boxes, HistoricalTeamBox, "historical team boxes")
        players = _tuple_of(
            self.historical_player_boxes, HistoricalPlayerBox, "historical player boxes"
        )
        sources = _tuple_of(
            self.historical_possession_sources,
            HistoricalPossessionSource,
            "historical possession sources",
        )
        dependencies = self.manifest.dependencies
        if tuple(source.dependency for source in sources) != dependencies:
            raise ValueError("historical possession sources must match manifest order one-to-one")
        ordinals = {row.dependency_game_id: row.ordinal for row in dependencies}
        if any(row.game_id not in ordinals for row in teams) or any(
            row.game_id not in ordinals for row in players
        ):
            raise ValueError("historical boxes must be manifest-bound")
        team_order = tuple(sorted(teams, key=lambda row: (ordinals[row.game_id], row.team_id)))
        player_order = tuple(
            sorted(players, key=lambda row: (ordinals[row.game_id], row.team_id, row.player_id))
        )
        if teams != team_order or players != player_order:
            raise ValueError("historical boxes must use canonical manifest order")
        for dependency in dependencies:
            game_teams = tuple(row for row in teams if row.game_id == dependency.dependency_game_id)
            game_players = tuple(
                row for row in players if row.game_id == dependency.dependency_game_id
            )
            if not game_teams or (
                canonical_team_boxes_checksum(game_teams) != dependency.canonical_team_box_sha256
            ):
                raise ValueError("historical team-box hash must match the manifest")
            if not game_players or (
                canonical_player_boxes_checksum(game_players)
                != dependency.canonical_player_box_sha256
            ):
                raise ValueError("historical player-box hash must match the manifest")
        _digest(self.dependency_input_sha256, "dependency input SHA-256")
        _digest(self.dependency_revision_sha256, "dependency revision SHA-256")
        if dependency_input_checksum(self.manifest) != self.dependency_input_sha256:
            raise ValueError("dependency input hash must match the manifest")
        if dependency_revision_checksum(self.manifest) != self.dependency_revision_sha256:
            raise ValueError("dependency revision hash must match the manifest")
        object.__setattr__(self, "target_player_ids", ids)
        object.__setattr__(self, "historical_team_boxes", teams)
        object.__setattr__(self, "historical_player_boxes", players)
        object.__setattr__(self, "historical_possession_sources", sources)


@dataclass(frozen=True, slots=True)
class FeatureInputToken:
    game_id: GameId
    target_started_at: datetime
    game_context_sha256: str
    possession_snapshot_id: UUID
    parse_revision_id: str
    possession_input_token_sha256: str
    possession_output_sha256: str
    lineup_snapshot_id: UUID
    lineup_revision_id: str
    lineup_input_token_sha256: str
    lineup_output_sha256: str
    lineup_result_status: LineupStatus
    dependency_input_sha256: str
    dependency_revision_sha256: str
    versions: FeatureVersions
    sha256: str = field(init=False)
    feature_revision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        object.__setattr__(
            self, "target_started_at", _utc(self.target_started_at, "target start time")
        )
        if (
            type(self.possession_snapshot_id) is not UUID
            or type(self.lineup_snapshot_id) is not UUID
        ):
            raise ValueError("source snapshot IDs must be UUID values")
        if not isinstance(self.lineup_result_status, LineupStatus):
            raise ValueError("lineup result status must be a LineupStatus")
        if not isinstance(self.versions, FeatureVersions):
            raise ValueError("versions must be FeatureVersions")
        for name in (
            "game_context_sha256",
            "parse_revision_id",
            "possession_input_token_sha256",
            "possession_output_sha256",
            "lineup_revision_id",
            "lineup_input_token_sha256",
            "lineup_output_sha256",
            "dependency_input_sha256",
            "dependency_revision_sha256",
        ):
            _digest(getattr(self, name), name.replace("_", " "))
        object.__setattr__(
            self,
            "sha256",
            feature_input_token_checksum(
                game_id=self.game_id,
                target_started_at=self.target_started_at,
                game_context_sha256=self.game_context_sha256,
                possession_snapshot_id=self.possession_snapshot_id,
                parse_revision_id=self.parse_revision_id,
                possession_input_token_sha256=self.possession_input_token_sha256,
                possession_output_sha256=self.possession_output_sha256,
                lineup_snapshot_id=self.lineup_snapshot_id,
                lineup_revision_id=self.lineup_revision_id,
                lineup_input_token_sha256=self.lineup_input_token_sha256,
                lineup_output_sha256=self.lineup_output_sha256,
                lineup_result_status=self.lineup_result_status,
                dependency_input_sha256=self.dependency_input_sha256,
                versions=self.versions,
            ),
        )
        object.__setattr__(
            self,
            "feature_revision_id",
            feature_revision_id(
                game_id=self.game_id,
                target_started_at=self.target_started_at,
                game_context_sha256=self.game_context_sha256,
                parse_revision_id=self.parse_revision_id,
                possession_output_sha256=self.possession_output_sha256,
                lineup_revision_id=self.lineup_revision_id,
                lineup_output_sha256=self.lineup_output_sha256,
                lineup_result_status=self.lineup_result_status,
                dependency_revision_sha256=self.dependency_revision_sha256,
                versions=self.versions,
            ),
        )

    @classmethod
    def from_source(
        cls, source: FeatureSourceSnapshot, versions: FeatureVersions
    ) -> FeatureInputToken:
        if not isinstance(source, FeatureSourceSnapshot):
            raise ValueError("source must be a FeatureSourceSnapshot")
        target = source.target
        possession = target.possession_reference
        lineup = target.lineup_reference
        return cls(
            game_id=target.game.game_id,
            target_started_at=target.game.started_at,
            game_context_sha256=target.pbp_snapshot.game_context_sha256,
            possession_snapshot_id=possession.attempt.snapshot_id,
            parse_revision_id=possession.attempt.parse_revision_id,
            possession_input_token_sha256=possession.attempt.input_token.sha256,
            possession_output_sha256=possession.output_sha256,
            lineup_snapshot_id=lineup.attempt.snapshot_id,
            lineup_revision_id=lineup.attempt.lineup_revision_id,
            lineup_input_token_sha256=lineup.attempt.input_token.sha256,
            lineup_output_sha256=lineup.output_sha256,
            lineup_result_status=lineup.result_status,
            dependency_input_sha256=source.dependency_input_sha256,
            dependency_revision_sha256=source.dependency_revision_sha256,
            versions=versions,
        )


def _validate_output_metadata(
    *,
    state_count: int,
    state_hash: str,
    player_count: int,
    player_hash: str,
    feature_hash: str,
    issues: tuple[str, ...],
    result_status: FeatureResultStatus,
) -> None:
    _positive(state_count, "expected state row count")
    _nonnegative(player_count, "expected player row count")
    _digest(state_hash, "expected state output SHA-256")
    _digest(player_hash, "expected player output SHA-256")
    _digest(feature_hash, "expected feature output SHA-256")
    if not isinstance(result_status, FeatureResultStatus):
        raise ValueError("result status must be a FeatureResultStatus")
    if result_status is FeatureResultStatus.VALID and issues:
        raise ValueError("valid output cannot contain issue codes")
    if result_status is FeatureResultStatus.INVALID and not issues:
        raise ValueError("invalid output requires issue codes")


@dataclass(frozen=True, slots=True)
class FeatureSnapshotAttempt:
    game: Game
    snapshot_id: UUID
    generation: int
    feature_revision_id: str
    input_token: FeatureInputToken
    state: FeatureAttemptState
    created_by_run_id: UUID
    created_by_claim_version: int
    created_at: datetime
    expected_state_row_count: int | None = None
    expected_state_output_sha256: str | None = None
    expected_player_row_count: int | None = None
    expected_player_output_sha256: str | None = None
    expected_feature_output_sha256: str | None = None
    expected_issue_codes: tuple[str, ...] | None = None
    result_status: FeatureResultStatus | None = None
    insert_started_at: datetime | None = None
    verified_at: datetime | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.game, Game) or self.game.status is not GameStatus.FINAL:
            raise ValueError("attempt requires a final Game")
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        _positive(self.generation, "generation")
        _digest(self.feature_revision_id, "feature revision ID")
        if not isinstance(self.input_token, FeatureInputToken):
            raise ValueError("input token must be a FeatureInputToken")
        if self.input_token.game_id != self.game.game_id:
            raise ValueError("attempt and input token must belong to one game")
        if self.input_token.target_started_at != self.game.started_at:
            raise ValueError("attempt and input token start time must match")
        if self.input_token.game_context_sha256 != game_context_checksum(
            GameContext.from_game(self.game)
        ):
            raise ValueError("attempt input token must match the game context")
        if self.feature_revision_id != self.input_token.feature_revision_id:
            raise ValueError("feature revision ID must match the input token")
        if not isinstance(self.state, FeatureAttemptState):
            raise ValueError("state must be a FeatureAttemptState")
        if type(self.created_by_run_id) is not UUID:
            raise ValueError("creator run ID must be a UUID")
        _positive(self.created_by_claim_version, "creator claim version")
        created_at = _utc(self.created_at, "created at")
        values = (
            self.expected_state_row_count,
            self.expected_state_output_sha256,
            self.expected_player_row_count,
            self.expected_player_output_sha256,
            self.expected_feature_output_sha256,
            self.expected_issue_codes,
            self.result_status,
        )
        populated = all(value is not None for value in values)
        if any(value is not None for value in values) and not populated:
            raise ValueError("expected metadata must be all null or all populated")
        if self.state is FeatureAttemptState.ALLOCATED and populated:
            raise ValueError("allocated attempt requires null expected metadata")
        if (
            self.state
            in {
                FeatureAttemptState.WRITING,
                FeatureAttemptState.VERIFIED,
                FeatureAttemptState.PUBLISHED,
            }
            and not populated
        ):
            raise ValueError(f"{self.state.value} attempt requires expected metadata")
        if populated:
            assert self.expected_state_row_count is not None
            assert self.expected_state_output_sha256 is not None
            assert self.expected_player_row_count is not None
            assert self.expected_player_output_sha256 is not None
            assert self.expected_feature_output_sha256 is not None
            assert self.expected_issue_codes is not None
            assert self.result_status is not None
            issues = _issue_codes(self.expected_issue_codes)
            _validate_output_metadata(
                state_count=self.expected_state_row_count,
                state_hash=self.expected_state_output_sha256,
                player_count=self.expected_player_row_count,
                player_hash=self.expected_player_output_sha256,
                feature_hash=self.expected_feature_output_sha256,
                issues=issues,
                result_status=self.result_status,
            )
            object.__setattr__(self, "expected_issue_codes", issues)
        if populated != (self.insert_started_at is not None):
            raise ValueError("insert_started_at must match expected metadata completeness")
        insert_at = (
            None
            if self.insert_started_at is None
            else _utc(self.insert_started_at, "insert started at")
        )
        verified_at = None if self.verified_at is None else _utc(self.verified_at, "verified at")
        published_at = (
            None if self.published_at is None else _utc(self.published_at, "published at")
        )
        if self.state in {FeatureAttemptState.VERIFIED, FeatureAttemptState.PUBLISHED} and (
            verified_at is None
        ):
            raise ValueError(f"{self.state.value} attempt requires verified_at")
        if verified_at is not None and not populated:
            raise ValueError("verified_at requires populated expected metadata")
        if (
            self.state
            not in {
                FeatureAttemptState.VERIFIED,
                FeatureAttemptState.PUBLISHED,
                FeatureAttemptState.POISONED,
                FeatureAttemptState.SUPERSEDED,
            }
            and verified_at is not None
        ):
            raise ValueError(f"{self.state.value} attempt cannot contain verified_at")
        if self.state is FeatureAttemptState.PUBLISHED:
            if published_at is None:
                raise ValueError("published attempt requires published_at")
        elif published_at is not None:
            raise ValueError("only published attempts may contain published_at")
        timestamps = tuple(
            value
            for value in (created_at, insert_at, verified_at, published_at)
            if value is not None
        )
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("attempt timestamps must be monotonic")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "insert_started_at", insert_at)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "published_at", published_at)

    @property
    def game_id(self) -> GameId:
        return self.game.game_id


@dataclass(frozen=True, slots=True)
class FeatureBuildClaim:
    run_id: UUID
    claim_version: int
    attempt: FeatureSnapshotAttempt
    source: FeatureSourceSnapshot

    def __post_init__(self) -> None:
        if type(self.run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        _positive(self.claim_version, "claim version")
        if not isinstance(self.attempt, FeatureSnapshotAttempt):
            raise ValueError("attempt must be a FeatureSnapshotAttempt")
        if not isinstance(self.source, FeatureSourceSnapshot):
            raise ValueError("source must be a FeatureSourceSnapshot")
        if self.run_id != self.attempt.created_by_run_id:
            raise ValueError("claim run must match the attempt creator")
        if self.claim_version != self.attempt.created_by_claim_version:
            raise ValueError("claim version must match the attempt creator")
        if self.attempt.game != self.source.target.game:
            raise ValueError("claim attempt and source must belong to one game")
        if self.attempt.input_token != FeatureInputToken.from_source(
            self.source, self.attempt.input_token.versions
        ):
            raise ValueError("claim attempt input token must match the source")


@dataclass(frozen=True, slots=True)
class CurrentFeatureSnapshotReference:
    attempt: FeatureSnapshotAttempt
    state_row_count: int
    state_output_sha256: str
    player_row_count: int
    player_output_sha256: str
    feature_output_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, FeatureSnapshotAttempt):
            raise ValueError("attempt must be a FeatureSnapshotAttempt")
        if self.attempt.state is not FeatureAttemptState.PUBLISHED:
            raise ValueError("current reference requires a published attempt")
        expected = (
            self.attempt.expected_state_row_count,
            self.attempt.expected_state_output_sha256,
            self.attempt.expected_player_row_count,
            self.attempt.expected_player_output_sha256,
            self.attempt.expected_feature_output_sha256,
        )
        actual = (
            self.state_row_count,
            self.state_output_sha256,
            self.player_row_count,
            self.player_output_sha256,
            self.feature_output_sha256,
        )
        if actual != expected:
            raise ValueError("current reference output must match the published attempt")

    @property
    def result_status(self) -> FeatureResultStatus:
        assert self.attempt.result_status is not None
        return self.attempt.result_status

    @property
    def issues(self) -> tuple[str, ...]:
        assert self.attempt.expected_issue_codes is not None
        return self.attempt.expected_issue_codes


@dataclass(frozen=True, slots=True)
class FeatureClaimDecision:
    action: FeatureClaimAction
    claim: FeatureBuildClaim | None = None
    current: CurrentFeatureSnapshotReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, FeatureClaimAction):
            raise ValueError("action must be a FeatureClaimAction")
        if self.claim is not None and not isinstance(self.claim, FeatureBuildClaim):
            raise ValueError("claim must be a FeatureBuildClaim")
        if self.current is not None and not isinstance(
            self.current, CurrentFeatureSnapshotReference
        ):
            raise ValueError("current must be a CurrentFeatureSnapshotReference")
        if self.action in {FeatureClaimAction.BUILD, FeatureClaimAction.RESUME}:
            if self.claim is None:
                raise ValueError(f"{self.action.value} decisions require a claim")
            if self.current is not None:
                raise ValueError(f"{self.action.value} decisions cannot include current")
            if self.action is FeatureClaimAction.BUILD and (
                self.claim.attempt.state is not FeatureAttemptState.ALLOCATED
            ):
                raise ValueError("build decisions require an allocated attempt")
            if self.action is FeatureClaimAction.RESUME and self.claim.attempt.state not in {
                FeatureAttemptState.ALLOCATED,
                FeatureAttemptState.WRITING,
                FeatureAttemptState.VERIFIED,
            }:
                raise ValueError("resume decisions require a resumable attempt")
        elif self.action is FeatureClaimAction.SKIP_CURRENT:
            if self.current is None:
                raise ValueError("skip_current decisions require a current reference")
            if self.claim is not None:
                raise ValueError("skip_current decisions cannot include a claim")
        elif self.claim is not None or self.current is not None:
            raise ValueError("busy decisions cannot include a claim or current reference")


@dataclass(frozen=True, slots=True)
class CurrentFeatureSnapshotResolution:
    status: FeaturePublicationState
    reference: CurrentFeatureSnapshotReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FeaturePublicationState):
            raise ValueError("status must be a FeaturePublicationState")
        if self.reference is not None and not isinstance(
            self.reference, CurrentFeatureSnapshotReference
        ):
            raise ValueError("reference must be a CurrentFeatureSnapshotReference")
        if self.status in {FeaturePublicationState.CURRENT, FeaturePublicationState.INVALID}:
            if self.reference is None:
                raise ValueError(f"{self.status.value} resolution requires a reference")
            expected = (
                FeatureResultStatus.VALID
                if self.status is FeaturePublicationState.CURRENT
                else FeatureResultStatus.INVALID
            )
            if self.reference.result_status is not expected:
                raise ValueError(
                    f"{self.status.value} resolution requires a {expected.value} snapshot"
                )
        elif self.reference is not None:
            raise ValueError(f"{self.status.value} resolution cannot include a reference")


@dataclass(frozen=True, slots=True)
class FeatureSnapshotVerification:
    snapshot_id: UUID
    status: FeatureVerificationStatus
    state_row_count: int | None = None
    state_output_sha256: str | None = None
    player_row_count: int | None = None
    player_output_sha256: str | None = None
    feature_output_sha256: str | None = None
    result_status: FeatureResultStatus | None = None
    issues: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not UUID or self.snapshot_id.version != 4:
            raise ValueError("snapshot ID must be a UUIDv4")
        if not isinstance(self.status, FeatureVerificationStatus):
            raise ValueError("status must be a FeatureVerificationStatus")
        values = (
            self.state_row_count,
            self.state_output_sha256,
            self.player_row_count,
            self.player_output_sha256,
            self.feature_output_sha256,
            self.result_status,
            self.issues,
        )
        populated = all(value is not None for value in values)
        if any(value is not None for value in values) and not populated:
            raise ValueError("verification metadata must be all null or all populated")
        if self.status is FeatureVerificationStatus.EXACT and not populated:
            raise ValueError("exact verification requires metadata")
        if (
            self.status
            in {
                FeatureVerificationStatus.ABSENT,
                FeatureVerificationStatus.UNAVAILABLE,
            }
            and populated
        ):
            raise ValueError(f"{self.status.value} verification cannot contain metadata")
        if populated:
            assert self.state_row_count is not None
            assert self.state_output_sha256 is not None
            assert self.player_row_count is not None
            assert self.player_output_sha256 is not None
            assert self.feature_output_sha256 is not None
            assert self.result_status is not None
            assert self.issues is not None
            issues = _issue_codes(self.issues)
            _validate_output_metadata(
                state_count=self.state_row_count,
                state_hash=self.state_output_sha256,
                player_count=self.player_row_count,
                player_hash=self.player_output_sha256,
                feature_hash=self.feature_output_sha256,
                issues=issues,
                result_status=self.result_status,
            )
            object.__setattr__(self, "issues", issues)

    @property
    def is_exact(self) -> bool:
        return self.status is FeatureVerificationStatus.EXACT


__all__ = [
    "BuildDatasetRequest",
    "BuildDatasetResult",
    "CurrentFeatureSnapshotReference",
    "CurrentFeatureSnapshotResolution",
    "FeatureAttemptState",
    "FeatureBuildClaim",
    "FeatureBuildMode",
    "FeatureBuildStatus",
    "FeatureClaimAction",
    "FeatureClaimDecision",
    "FeatureInputToken",
    "FeaturePublicationState",
    "FeatureSnapshotAttempt",
    "FeatureSnapshotVerification",
    "FeatureSourceSnapshot",
    "FeatureTargetRows",
    "FeatureTargetSnapshot",
    "FeatureVerificationStatus",
    "HistoricalPossessionSource",
]
