"""Atomic PostgreSQL source resolution and fenced Phase 5 claim allocation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    BuildDatasetResult,
    CurrentFeatureSnapshotReference,
    CurrentFeatureSnapshotResolution,
    FeatureAttemptState,
    FeatureBuildClaim,
    FeatureClaimAction,
    FeatureClaimDecision,
    FeatureInputToken,
    FeaturePublicationState,
    FeatureSnapshotAttempt,
    FeatureSnapshotVerification,
    FeatureSourceSnapshot,
    FeatureTargetSnapshot,
    FeatureVerificationStatus,
    HistoricalPossessionSource,
)
from nba_live_totals.application.lineups import LineupInputToken
from nba_live_totals.application.possessions import GamePbpSnapshot, PossessionInputToken
from nba_live_totals.domain import (
    Game,
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PossessionVersions,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    game_context_checksum,
    parse_revision_id,
)
from nba_live_totals.domain.feature_hashing import (
    canonical_player_boxes_checksum,
    canonical_team_boxes_checksum,
    dependency_input_checksum,
    dependency_revision_checksum,
)
from nba_live_totals.domain.features import (
    FeatureDependency,
    FeatureDependencyManifest,
    FeatureResultStatus,
    FeatureVersions,
    HistoricalPlayerBox,
    HistoricalTeamBox,
)
from nba_live_totals.domain.lineup_hashing import lineup_revision_id
from nba_live_totals.domain.lineups import LineupStatus, LineupVersions
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
    games,
    ingestion_resources,
    lineup_rebuild_resources,
    lineup_snapshot_attempts,
    play_by_play_events,
    player_box_scores,
    possession_rebuild_resources,
    possession_snapshot_attempts,
    source_documents,
    team_box_scores,
)

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_ACTIVE_STATES = {"allocated", "writing", "verified"}
_HISTORICAL_STAT_FIELDS = (
    "seconds_played",
    "field_goals_made",
    "field_goals_attempted",
    "three_pointers_made",
    "three_pointers_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "rebounds_offensive",
    "rebounds_defensive",
    "rebounds_total",
    "assists",
    "turnovers",
    "points",
)
_INCLUSION_REASON_BITS = {
    "home_team_window": 1,
    "away_team_window": 2,
    "player_history": 4,
}
_CLEAR_ACTIVE = dict.fromkeys(
    (
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_feature_revision_id",
        "active_feature_input_token_sha256",
        "claimed_until",
    )
)


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object) -> UUID:
    return value if type(value) is UUID else UUID(str(value))


class PostgresFeatureWorkStore:
    """Resolve complete source identities and allocate immutable feature attempts."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        possession_versions: PossessionVersions,
        lineup_versions: LineupVersions,
        source: str = "nba.com",
        lease_seconds: int = 300,
        utc_now: Callable[[], datetime] = _system_utc_now,
    ) -> None:
        if not isinstance(possession_versions, PossessionVersions):
            raise ValueError("possession versions must be PossessionVersions")
        if not isinstance(lineup_versions, LineupVersions):
            raise ValueError("lineup versions must be LineupVersions")
        if not source:
            raise ValueError("source must not be empty")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease seconds must be an integer between 1 and 86400")
        self._engine = engine
        self._possession_versions = possession_versions
        self._lineup_versions = lineup_versions
        self._source = source
        self._lease = timedelta(seconds=lease_seconds)
        self._utc_now = utc_now
        self._possession = PostgresPossessionWorkStore(engine, source=source)
        self._lineup = PostgresLineupWorkStore(engine, source=source)

    async def start_run(self, request: BuildDatasetRequest) -> UUID:
        if not isinstance(request, BuildDatasetRequest):
            raise ValueError("request must be a BuildDatasetRequest")
        run_id = uuid4()
        versions = FeatureVersions()
        selectors: dict[str, object] = {
            "seasons": [season.value for season in request.seasons],
            "game_id": None if request.game_id is None else request.game_id.value,
        }
        async with self._engine.begin() as connection:
            await connection.execute(
                feature_build_runs.insert().values(
                    run_id=run_id,
                    selectors=selectors,
                    mode=request.mode.value,
                    forced_rebuild=request.rebuild,
                    feature_join_version=versions.join_version,
                    feature_formula_version=versions.formula_version,
                    feature_event_normalizer_version=versions.event_normalizer_version,
                    output_schema_version=versions.output_schema_version,
                    player_output_schema_version=versions.player_output_schema_version,
                    status="running",
                    started_at=self._now(),
                )
            )
        return run_id

    async def resolve_target(self, game_id: GameId) -> FeatureTargetSnapshot:
        if not isinstance(game_id, GameId):
            raise ValueError("game ID must be a GameId")
        async with self._engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                return await self._resolve_target(connection, game_id)

    async def claim_game(
        self,
        run_id: UUID,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
        *,
        rebuild: bool,
    ) -> FeatureClaimDecision:
        if type(run_id) is not UUID or not isinstance(target, FeatureTargetSnapshot):
            raise ValueError("claim requires a run UUID and target snapshot")
        if not isinstance(versions, FeatureVersions) or type(rebuild) is not bool:
            raise ValueError("claim requires feature versions and a rebuild boolean")
        if any(
            type(player_id) is not int or player_id < 1 for player_id in target_player_ids
        ) or len(set(target_player_ids)) != len(target_player_ids):
            raise ValueError("target player IDs must be unique positive integers")
        target_player_ids = tuple(sorted(target_player_ids))
        async with self._engine.begin() as connection:
            # Deterministic cross-plane order: target applied resources/game,
            # target analytical pointers/attempts, bounded historical sources,
            # feature resource/attempt, then the owning run.
            current_target = await self._resolve_target(connection, target.game.game_id)
            if current_target != target:
                raise RuntimeError("target_sources_changed")
            source = await self._build_source(connection, current_target, target_player_ids)
            token = FeatureInputToken.from_source(source, versions)
            await connection.execute(
                insert(feature_build_resources)
                .values(game_id=target.game.game_id.value)
                .on_conflict_do_nothing(index_elements=[feature_build_resources.c.game_id])
            )
            resource = (
                (
                    await connection.execute(
                        select(feature_build_resources)
                        .where(feature_build_resources.c.game_id == target.game.game_id.value)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = self._now()
            if resource["active_snapshot_id"] is not None:
                active = await self._lock_attempt(connection, _uuid(resource["active_snapshot_id"]))
                self._require_active_fence(resource, active)
                fresh = (
                    active["feature_input_token_sha256"] == token.sha256
                    and active["feature_revision_id"] == token.feature_revision_id
                )
                if resource["claimed_until"] > now and fresh:
                    if _uuid(resource["active_run_id"]) != run_id:
                        return FeatureClaimDecision(FeatureClaimAction.BUSY)
                    return FeatureClaimDecision(
                        FeatureClaimAction.RESUME,
                        claim=FeatureBuildClaim(
                            run_id,
                            cast(int, resource["active_claim_version"]),
                            self._attempt_from_row(target.game, active),
                            source,
                        ),
                    )
                await connection.execute(
                    update(feature_snapshot_attempts)
                    .where(
                        feature_snapshot_attempts.c.snapshot_id == active["snapshot_id"],
                        feature_snapshot_attempts.c.state.in_(_ACTIVE_STATES),
                    )
                    .values(state="superseded")
                )
                await connection.execute(
                    update(feature_build_resources)
                    .where(feature_build_resources.c.game_id == target.game.game_id.value)
                    .values(**_CLEAR_ACTIVE, updated_at=now)
                )

            if resource["published_snapshot_id"] is not None:
                published = await self._lock_attempt(
                    connection, _uuid(resource["published_snapshot_id"])
                )
                dependencies_current = await self._manifest_rows_match(
                    connection,
                    _uuid(published["snapshot_id"]),
                    source.manifest.dependencies,
                )
                current = dependencies_current and self._is_effective_current(
                    resource, published, token
                )
                if current and not rebuild:
                    return FeatureClaimDecision(
                        FeatureClaimAction.SKIP_CURRENT,
                        current=self._current_from_row(target.game, published),
                    )
                if not current:
                    await connection.execute(
                        update(feature_build_resources)
                        .where(feature_build_resources.c.game_id == target.game.game_id.value)
                        .values(publication_state="stale", updated_at=now)
                    )

            claim_version = cast(int, resource["claim_version"])
            generation = cast(int, resource["next_generation"])
            attempt_count = cast(int, resource["attempt_count"])
            if any(
                value >= _MAX_SIGNED_BIGINT for value in (claim_version, generation, attempt_count)
            ):
                raise RuntimeError("feature claim counter exhausted")
            run = (
                (
                    await connection.execute(
                        select(feature_build_runs)
                        .where(feature_build_runs.c.run_id == run_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] != "running" or run["mode"] != "materialize":
                raise RuntimeError("feature allocation requires a running materialize run")
            snapshot_id = uuid4()
            claim_version += 1
            await connection.execute(
                feature_snapshot_attempts.insert().values(
                    snapshot_id=snapshot_id,
                    game_id=target.game.game_id.value,
                    generation=generation,
                    feature_revision_id=token.feature_revision_id,
                    feature_input_token_sha256=token.sha256,
                    possession_snapshot_id=token.possession_snapshot_id,
                    parse_revision_id=token.parse_revision_id,
                    possession_input_token_sha256=token.possession_input_token_sha256,
                    possession_output_sha256=token.possession_output_sha256,
                    lineup_snapshot_id=token.lineup_snapshot_id,
                    lineup_revision_id=token.lineup_revision_id,
                    lineup_input_token_sha256=token.lineup_input_token_sha256,
                    lineup_output_sha256=token.lineup_output_sha256,
                    lineup_result_status=token.lineup_result_status.value,
                    dependency_input_sha256=token.dependency_input_sha256,
                    dependency_revision_sha256=token.dependency_revision_sha256,
                    feature_join_version=versions.join_version,
                    feature_formula_version=versions.formula_version,
                    feature_event_normalizer_version=versions.event_normalizer_version,
                    output_schema_version=versions.output_schema_version,
                    player_output_schema_version=versions.player_output_schema_version,
                    state="allocated",
                    created_by_run_id=run_id,
                    created_by_claim_version=claim_version,
                    created_at=now,
                )
            )
            if source.manifest.dependencies:
                await connection.execute(
                    feature_snapshot_dependencies.insert(),
                    [
                        {
                            "snapshot_id": snapshot_id,
                            "dependency_game_id": dependency.dependency_game_id.value,
                            "started_at": dependency.started_at,
                            "game_context_sha256": dependency.game_context_sha256,
                            "applied_box_score_document_id": (
                                dependency.applied_box_score_document_id
                            ),
                            "applied_box_score_sha256": (dependency.applied_box_score_sha256),
                            "canonical_team_box_sha256": (dependency.canonical_team_box_sha256),
                            "canonical_player_box_sha256": (dependency.canonical_player_box_sha256),
                            "possession_snapshot_id": dependency.possession_snapshot_id,
                            "parse_revision_id": dependency.parse_revision_id,
                            "possession_input_token_sha256": (
                                dependency.possession_input_token_sha256
                            ),
                            "possession_output_sha256": dependency.possession_output_sha256,
                            "ordinal": dependency.ordinal,
                            "inclusion_reasons": sum(
                                _INCLUSION_REASON_BITS[reason]
                                for reason in dependency.inclusion_reasons
                            ),
                        }
                        for dependency in source.manifest.dependencies
                    ],
                )
            await connection.execute(
                update(feature_build_resources)
                .where(feature_build_resources.c.game_id == target.game.game_id.value)
                .values(
                    attempt_count=attempt_count + 1,
                    claim_version=claim_version,
                    next_generation=generation + 1,
                    last_run_id=run_id,
                    active_run_id=run_id,
                    active_claim_version=claim_version,
                    active_snapshot_id=snapshot_id,
                    active_feature_revision_id=token.feature_revision_id,
                    active_feature_input_token_sha256=token.sha256,
                    claimed_until=now + self._lease,
                    updated_at=now,
                )
            )
            attempt = FeatureSnapshotAttempt(
                game=target.game,
                snapshot_id=snapshot_id,
                generation=generation,
                feature_revision_id=token.feature_revision_id,
                input_token=token,
                state=FeatureAttemptState.ALLOCATED,
                created_by_run_id=run_id,
                created_by_claim_version=claim_version,
                created_at=now,
            )
            return FeatureClaimDecision(
                FeatureClaimAction.BUILD,
                claim=FeatureBuildClaim(run_id, claim_version, attempt, source),
            )

    async def mark_writing(
        self,
        claim: FeatureBuildClaim,
        *,
        expected_state_row_count: int,
        expected_state_output_sha256: str,
        expected_player_row_count: int,
        expected_player_output_sha256: str,
        expected_feature_output_sha256: str,
        expected_issue_codes: tuple[str, ...],
        result_status: FeatureResultStatus,
    ) -> FeatureSnapshotAttempt:
        async with self._engine.begin() as connection:
            resource, attempt, source_is_current = await self._lock_claim_for_transition(
                connection, claim, allowed_states={FeatureAttemptState.ALLOCATED}
            )
            now = self._now()
            if not source_is_current:
                await self._supersede_claim(connection, resource, attempt, claim, now)
            else:
                await connection.execute(
                    update(feature_snapshot_attempts)
                    .where(feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(
                        state=FeatureAttemptState.WRITING.value,
                        expected_state_row_count=expected_state_row_count,
                        expected_state_output_sha256=expected_state_output_sha256,
                        expected_player_row_count=expected_player_row_count,
                        expected_player_output_sha256=expected_player_output_sha256,
                        expected_feature_output_sha256=expected_feature_output_sha256,
                        expected_issue_codes=list(expected_issue_codes),
                        result_status=result_status.value,
                        insert_started_at=now,
                    )
                )
                updated = dict(attempt) | {
                    "state": FeatureAttemptState.WRITING.value,
                    "expected_state_row_count": expected_state_row_count,
                    "expected_state_output_sha256": expected_state_output_sha256,
                    "expected_player_row_count": expected_player_row_count,
                    "expected_player_output_sha256": expected_player_output_sha256,
                    "expected_feature_output_sha256": expected_feature_output_sha256,
                    "expected_issue_codes": list(expected_issue_codes),
                    "result_status": result_status.value,
                    "insert_started_at": now,
                }
                result = self._attempt_from_row(claim.attempt.game, updated)
        if not source_is_current:
            raise RuntimeError("candidate source changed")
        return result

    async def mark_verified(
        self, claim: FeatureBuildClaim, verification: FeatureSnapshotVerification
    ) -> FeatureSnapshotAttempt:
        async with self._engine.begin() as connection:
            resource, attempt, source_is_current = await self._lock_claim_for_transition(
                connection, claim, allowed_states={FeatureAttemptState.WRITING}
            )
            now = self._now()
            if not source_is_current:
                await self._supersede_claim(connection, resource, attempt, claim, now)
            else:
                expected = (
                    attempt["expected_state_row_count"],
                    attempt["expected_state_output_sha256"],
                    attempt["expected_player_row_count"],
                    attempt["expected_player_output_sha256"],
                    attempt["expected_feature_output_sha256"],
                    FeatureResultStatus(cast(str, attempt["result_status"])),
                    tuple(attempt["expected_issue_codes"]),
                )
                actual = (
                    verification.state_row_count,
                    verification.state_output_sha256,
                    verification.player_row_count,
                    verification.player_output_sha256,
                    verification.feature_output_sha256,
                    verification.result_status,
                    verification.issues,
                )
                if (
                    verification.snapshot_id != claim.attempt.snapshot_id
                    or verification.status is not FeatureVerificationStatus.EXACT
                    or actual != expected
                ):
                    raise RuntimeError("verification does not exactly match writing attempt")
                await connection.execute(
                    update(feature_snapshot_attempts)
                    .where(feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(state=FeatureAttemptState.VERIFIED.value, verified_at=now)
                )
                result = self._attempt_from_row(
                    claim.attempt.game,
                    dict(attempt)
                    | {
                        "state": FeatureAttemptState.VERIFIED.value,
                        "verified_at": now,
                    },
                )
        if not source_is_current:
            raise RuntimeError("candidate source changed")
        return result

    async def publish_snapshot(self, claim: FeatureBuildClaim) -> CurrentFeatureSnapshotReference:
        reference: CurrentFeatureSnapshotReference | None = None
        async with self._engine.begin() as connection:
            resource, attempt, source_is_current = await self._lock_claim_for_transition(
                connection, claim, allowed_states={FeatureAttemptState.VERIFIED}
            )
            now = self._now()
            if not source_is_current:
                await self._supersede_claim(connection, resource, attempt, claim, now)
            else:
                result_status = FeatureResultStatus(cast(str, attempt["result_status"]))
                publication_state = (
                    FeaturePublicationState.CURRENT
                    if result_status is FeatureResultStatus.VALID
                    else FeaturePublicationState.INVALID
                )
                issues = list(attempt["expected_issue_codes"])
                await connection.execute(
                    update(feature_snapshot_attempts)
                    .where(feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(state=FeatureAttemptState.PUBLISHED.value, published_at=now)
                )
                await connection.execute(
                    update(feature_build_resources)
                    .where(feature_build_resources.c.game_id == claim.attempt.game_id.value)
                    .values(
                        publication_state=publication_state.value,
                        published_snapshot_id=claim.attempt.snapshot_id,
                        published_feature_revision_id=claim.attempt.feature_revision_id,
                        published_feature_input_token_sha256=claim.attempt.input_token.sha256,
                        published_at=now,
                        last_issue_codes=issues,
                        **_CLEAR_ACTIVE,
                        last_error_category=None,
                        last_error_type=None,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                reference = self._current_from_row(
                    claim.attempt.game,
                    dict(attempt)
                    | {
                        "state": FeatureAttemptState.PUBLISHED.value,
                        "published_at": now,
                    },
                )
        if not source_is_current:
            raise RuntimeError("candidate source changed")
        if reference is None:
            raise RuntimeError("publication did not produce a current feature reference")
        return reference

    async def poison_attempt(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        await self._terminate_failed_claim(
            claim,
            error_code=error_code,
            error_type=error_type,
            allowed_states={
                FeatureAttemptState.ALLOCATED,
                FeatureAttemptState.WRITING,
                FeatureAttemptState.VERIFIED,
            },
        )

    async def fail_game(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        await self._terminate_failed_claim(
            claim,
            error_code=error_code,
            error_type=error_type,
            allowed_states={FeatureAttemptState.ALLOCATED},
        )

    async def record_unclaimed_failure(
        self,
        run_id: UUID,
        game_id: GameId,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        if type(run_id) is not UUID or not isinstance(game_id, GameId):
            raise ValueError("unclaimed failure requires a run UUID and game ID")
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(feature_build_resources)
                .values(game_id=game_id.value)
                .on_conflict_do_nothing(index_elements=[feature_build_resources.c.game_id])
            )
            resource = (
                (
                    await connection.execute(
                        select(feature_build_resources)
                        .where(feature_build_resources.c.game_id == game_id.value)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = self._now()
            if (
                resource["active_run_id"] is not None
                and resource["claimed_until"] is not None
                and resource["claimed_until"] > now
            ):
                return
            run_exists = await connection.scalar(
                select(feature_build_runs.c.run_id).where(
                    feature_build_runs.c.run_id == run_id,
                    feature_build_runs.c.status == "running",
                )
            )
            if run_exists is None:
                raise RuntimeError("feature build run is missing or already terminal")
            await connection.execute(
                update(feature_build_resources)
                .where(feature_build_resources.c.game_id == game_id.value)
                .values(
                    last_run_id=run_id,
                    last_error_category=error_code,
                    last_error_type=error_type,
                    updated_at=now,
                    completed_at=now,
                )
            )

    async def resolve_current(
        self,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
    ) -> CurrentFeatureSnapshotResolution:
        if not isinstance(target, FeatureTargetSnapshot) or not isinstance(
            versions, FeatureVersions
        ):
            raise ValueError("current resolution requires a target and feature versions")
        if any(
            type(player_id) is not int or player_id < 1 for player_id in target_player_ids
        ) or len(set(target_player_ids)) != len(target_player_ids):
            raise ValueError("target player IDs must be unique positive integers")
        async with self._engine.begin() as connection:
            try:
                current_target = await self._resolve_target(connection, target.game.game_id)
                source = await self._build_source(
                    connection, current_target, tuple(sorted(target_player_ids))
                )
                token = FeatureInputToken.from_source(source, versions)
            except (RuntimeError, ValueError):
                return CurrentFeatureSnapshotResolution(FeaturePublicationState.STALE)
            exists = await connection.scalar(
                select(feature_build_resources.c.game_id).where(
                    feature_build_resources.c.game_id == target.game.game_id.value
                )
            )
            if exists is None:
                return CurrentFeatureSnapshotResolution(FeaturePublicationState.ABSENT)
            resource = (
                (
                    await connection.execute(
                        select(feature_build_resources)
                        .where(feature_build_resources.c.game_id == target.game.game_id.value)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if resource["published_snapshot_id"] is None:
                return CurrentFeatureSnapshotResolution(FeaturePublicationState.ABSENT)
            attempt = await self._lock_attempt(connection, _uuid(resource["published_snapshot_id"]))
            dependencies_current = await self._manifest_rows_match(
                connection,
                _uuid(attempt["snapshot_id"]),
                source.manifest.dependencies,
            )
            if (
                current_target != target
                or not dependencies_current
                or not self._is_effective_current(resource, attempt, token)
            ):
                return CurrentFeatureSnapshotResolution(FeaturePublicationState.STALE)
            try:
                return CurrentFeatureSnapshotResolution(
                    FeaturePublicationState(cast(str, resource["publication_state"])),
                    self._current_from_row(target.game, attempt),
                )
            except (RuntimeError, ValueError):
                return CurrentFeatureSnapshotResolution(FeaturePublicationState.STALE)

    async def finish_run(self, run_id: UUID, result: BuildDatasetResult) -> None:
        if type(run_id) is not UUID or result.run_id != run_id:
            raise ValueError("result run ID does not match run ID")
        async with self._engine.begin() as connection:
            updated = await connection.execute(
                update(feature_build_runs)
                .where(
                    feature_build_runs.c.run_id == run_id,
                    feature_build_runs.c.status == "running",
                )
                .values(
                    status=result.status.value,
                    discovered=result.discovered,
                    materialized=result.materialized,
                    invalid=result.invalid,
                    skipped_current=result.skipped_current,
                    verified=result.verified,
                    failed=result.failed,
                    busy=result.busy,
                    finished_at=self._now(),
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("feature build run is missing or already terminal")

    async def _build_source(
        self,
        connection: AsyncConnection,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
    ) -> FeatureSourceSnapshot:
        target_game = target.game
        team_rank = func.row_number().over(
            partition_by=team_box_scores.c.team_id,
            order_by=(games.c.started_at.desc(), games.c.game_id.desc()),
        )
        ranked_teams = (
            select(
                games.c.game_id,
                team_box_scores.c.team_id,
                team_rank.label("history_rank"),
            )
            .select_from(games.join(team_box_scores, games.c.game_id == team_box_scores.c.game_id))
            .where(
                games.c.status == GameStatus.FINAL.value,
                games.c.started_at < target_game.started_at,
                team_box_scores.c.team_id.in_((target_game.home_team_id, target_game.away_team_id)),
            )
            .subquery()
        )
        team_candidates = (
            (
                await connection.execute(
                    select(ranked_teams.c.game_id, ranked_teams.c.team_id).where(
                        ranked_teams.c.history_rank <= 20
                    )
                )
            )
            .mappings()
            .all()
        )

        player_candidates: list[RowMapping] = []
        if target_player_ids:
            player_rank = func.row_number().over(
                partition_by=player_box_scores.c.player_id,
                order_by=(games.c.started_at.desc(), games.c.game_id.desc()),
            )
            ranked_players = (
                select(
                    games.c.game_id,
                    player_box_scores.c.player_id,
                    player_rank.label("history_rank"),
                )
                .select_from(
                    games.join(
                        player_box_scores,
                        games.c.game_id == player_box_scores.c.game_id,
                    )
                )
                .where(
                    games.c.status == GameStatus.FINAL.value,
                    games.c.started_at < target_game.started_at,
                    player_box_scores.c.player_id.in_(target_player_ids),
                    player_box_scores.c.played.is_(True),
                    player_box_scores.c.seconds_played > 0,
                )
                .subquery()
            )
            player_candidates = list(
                (
                    await connection.execute(
                        select(
                            ranked_players.c.game_id,
                            ranked_players.c.player_id,
                        ).where(ranked_players.c.history_rank <= 20)
                    )
                )
                .mappings()
                .all()
            )

        reasons_by_game: dict[str, set[str]] = {}
        for row in team_candidates:
            reason = (
                "home_team_window"
                if row["team_id"] == target_game.home_team_id
                else "away_team_window"
            )
            reasons_by_game.setdefault(cast(str, row["game_id"]), set()).add(reason)
        for row in player_candidates:
            reasons_by_game.setdefault(cast(str, row["game_id"]), set()).add("player_history")
        if not reasons_by_game:
            return self._empty_source(target, target_player_ids)

        selected_ids = tuple(reasons_by_game)
        game_rows = (
            (
                await connection.execute(
                    select(games)
                    .where(games.c.game_id.in_(selected_ids))
                    .order_by(games.c.started_at, games.c.game_id)
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .all()
        )
        ordered_ids = tuple(cast(str, row["game_id"]) for row in game_rows)
        if len(ordered_ids) != len(selected_ids):
            raise RuntimeError("historical_game_unavailable")

        team_rows = (
            (
                await connection.execute(
                    select(team_box_scores)
                    .where(team_box_scores.c.game_id.in_(ordered_ids))
                    .order_by(team_box_scores.c.game_id, team_box_scores.c.team_id)
                )
            )
            .mappings()
            .all()
        )
        player_rows = (
            (
                await connection.execute(
                    select(player_box_scores)
                    .where(player_box_scores.c.game_id.in_(ordered_ids))
                    .order_by(
                        player_box_scores.c.game_id,
                        player_box_scores.c.team_id,
                        player_box_scores.c.player_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        resource_keys = tuple(
            f"{resource_type}:{game_id}"
            for game_id in ordered_ids
            for resource_type in ("box_score", "play_by_play")
        )
        document_rows = (
            (
                await connection.execute(
                    select(
                        ingestion_resources.c.resource_type,
                        ingestion_resources.c.resource_key,
                        ingestion_resources.c.status.label("resource_status"),
                        ingestion_resources.c.applied_document_id,
                        source_documents.c.source.label("document_source"),
                        source_documents.c.endpoint,
                        source_documents.c.request_parameters,
                        source_documents.c.payload,
                        source_documents.c.checksum_sha256,
                        source_documents.c.requested_at,
                    )
                    .select_from(
                        ingestion_resources.join(
                            source_documents,
                            ingestion_resources.c.applied_document_id
                            == source_documents.c.document_id,
                        )
                    )
                    .where(
                        ingestion_resources.c.source == self._source,
                        ingestion_resources.c.resource_key.in_(resource_keys),
                    )
                    .with_for_update(of=ingestion_resources)
                )
            )
            .mappings()
            .all()
        )
        possession_resources = (
            (
                await connection.execute(
                    select(possession_rebuild_resources)
                    .where(possession_rebuild_resources.c.game_id.in_(ordered_ids))
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        published_ids = tuple(
            _uuid(row["published_snapshot_id"])
            for row in possession_resources
            if row["published_snapshot_id"] is not None
        )
        possession_attempts = (
            (
                await connection.execute(
                    select(possession_snapshot_attempts)
                    .where(possession_snapshot_attempts.c.snapshot_id.in_(published_ids))
                    .with_for_update()
                )
            )
            .mappings()
            .all()
            if published_ids
            else []
        )
        event_rows = (
            (
                await connection.execute(
                    select(play_by_play_events)
                    .where(play_by_play_events.c.game_id.in_(ordered_ids))
                    .order_by(
                        play_by_play_events.c.game_id,
                        play_by_play_events.c.sequence_number,
                    )
                )
            )
            .mappings()
            .all()
        )

        games_by_id = {cast(str, row["game_id"]): self._game_from_row(row) for row in game_rows}
        documents = {
            (cast(str, row["resource_type"]), cast(str, row["resource_key"])): row
            for row in document_rows
        }
        resources_by_game = {cast(str, row["game_id"]): row for row in possession_resources}
        attempts_by_id = {_uuid(row["snapshot_id"]): row for row in possession_attempts}
        events_by_game: dict[str, list[RowMapping]] = {game_id: [] for game_id in ordered_ids}
        for row in event_rows:
            events_by_game[cast(str, row["game_id"])].append(row)

        historical_teams: list[HistoricalTeamBox] = []
        historical_players: list[HistoricalPlayerBox] = []
        for row in team_rows:
            game_id = cast(str, row["game_id"])
            game = games_by_id[game_id]
            historical_teams.append(
                HistoricalTeamBox(
                    game_id=game.game_id,
                    team_id=cast(int, row["team_id"]),
                    started_at=game.started_at,
                    game_date=game.game_date,
                    is_home=row["team_id"] == game.home_team_id,
                    **self._historical_stats(row),
                )
            )
        for row in player_rows:
            historical_players.append(
                HistoricalPlayerBox(
                    game_id=GameId(cast(str, row["game_id"])),
                    team_id=cast(int, row["team_id"]),
                    player_id=cast(int, row["player_id"]),
                    played=cast(bool, row["played"]),
                    **self._historical_stats(row),
                )
            )

        teams_by_game = {
            game_id: tuple(row for row in historical_teams if row.game_id.value == game_id)
            for game_id in ordered_ids
        }
        players_by_game = {
            game_id: tuple(row for row in historical_players if row.game_id.value == game_id)
            for game_id in ordered_ids
        }
        dependencies: list[FeatureDependency] = []
        references = []
        for ordinal, game_id in enumerate(ordered_ids):
            game = games_by_id[game_id]
            context = GameContext.from_game(game)
            box_document = self._required_document(documents, "box_score", game_id)
            pbp_document = self._required_document(documents, "play_by_play", game_id)
            source_document = self._source_document(pbp_document)
            events = tuple(self._possession._event_from_row(row) for row in events_by_game[game_id])
            snapshot = GamePbpSnapshot(
                game=context,
                applied_pbp_document_id=_uuid(pbp_document["applied_document_id"]),
                applied_pbp_sha256=cast(str, pbp_document["checksum_sha256"]),
                canonical_events=events,
                canonical_event_sha256=canonical_event_checksum(events),
                game_context_sha256=game_context_checksum(context),
                source_document=source_document,
            )
            token = PossessionInputToken.from_snapshot(snapshot, self._possession_versions)
            revision = parse_revision_id(
                game_id=game.game_id,
                pbp_document_sha256=token.applied_pbp_sha256,
                canonical_event_sha256=token.canonical_event_sha256,
                game_context_sha256=token.game_context_sha256,
                versions=self._possession_versions,
            )
            resource = resources_by_game.get(game_id)
            if resource is None or resource["published_snapshot_id"] is None:
                raise RuntimeError("historical_possession_absent")
            attempt = attempts_by_id.get(_uuid(resource["published_snapshot_id"]))
            if attempt is None:
                raise RuntimeError("historical_possession_corrupt")
            try:
                current = self._possession._is_effective_current(resource, attempt, token, revision)
                reference = self._possession._current_from_row(context, attempt)
            except (RuntimeError, ValueError):
                raise RuntimeError("historical_possession_stale") from None
            if not current:
                raise RuntimeError("historical_possession_stale")
            game_teams = teams_by_game[game_id]
            game_players = players_by_game[game_id]
            if len(game_teams) != 2 or not game_players:
                raise RuntimeError("historical_box_scores_incomplete")
            dependency = FeatureDependency(
                dependency_game_id=game.game_id,
                started_at=game.started_at,
                game_context_sha256=game_context_checksum(context),
                applied_box_score_document_id=_uuid(box_document["applied_document_id"]),
                applied_box_score_sha256=cast(str, box_document["checksum_sha256"]),
                canonical_team_box_sha256=canonical_team_boxes_checksum(game_teams),
                canonical_player_box_sha256=canonical_player_boxes_checksum(game_players),
                possession_snapshot_id=reference.attempt.snapshot_id,
                parse_revision_id=reference.attempt.parse_revision_id,
                possession_input_token_sha256=reference.attempt.input_token.sha256,
                possession_output_sha256=reference.output_sha256,
                ordinal=ordinal,
                inclusion_reasons=tuple(sorted(reasons_by_game[game_id])),
            )
            dependencies.append(dependency)
            references.append(HistoricalPossessionSource(dependency, reference))

        manifest = FeatureDependencyManifest(tuple(dependencies))
        ordinals = {
            dependency.dependency_game_id: dependency.ordinal
            for dependency in manifest.dependencies
        }
        historical_teams.sort(key=lambda row: (ordinals[row.game_id], row.team_id))
        historical_players.sort(key=lambda row: (ordinals[row.game_id], row.team_id, row.player_id))
        return FeatureSourceSnapshot(
            target=target,
            target_player_ids=target_player_ids,
            manifest=manifest,
            historical_team_boxes=tuple(historical_teams),
            historical_player_boxes=tuple(historical_players),
            historical_possession_sources=tuple(references),
            dependency_input_sha256=dependency_input_checksum(manifest),
            dependency_revision_sha256=dependency_revision_checksum(manifest),
        )

    @staticmethod
    def _empty_source(
        target: FeatureTargetSnapshot, target_player_ids: tuple[int, ...]
    ) -> FeatureSourceSnapshot:
        manifest = FeatureDependencyManifest(())
        return FeatureSourceSnapshot(
            target=target,
            target_player_ids=target_player_ids,
            manifest=manifest,
            historical_team_boxes=(),
            historical_player_boxes=(),
            historical_possession_sources=(),
            dependency_input_sha256=dependency_input_checksum(manifest),
            dependency_revision_sha256=dependency_revision_checksum(manifest),
        )

    @staticmethod
    def _historical_stats(row: RowMapping) -> dict[str, int | None]:
        return {name: cast(int | None, row[name]) for name in _HISTORICAL_STAT_FIELDS}

    @staticmethod
    def _required_document(
        documents: dict[tuple[str, str], RowMapping],
        resource_type: str,
        game_id: str,
    ) -> RowMapping:
        row = documents.get((resource_type, f"{resource_type}:{game_id}"))
        if row is None or row["resource_status"] != "succeeded":
            raise RuntimeError("historical_source_unavailable")
        return row

    @staticmethod
    def _source_document(row: RowMapping) -> SourceDocument:
        return SourceDocument(
            source=cast(str, row["document_source"]),
            endpoint=cast(str, row["endpoint"]),
            resource_key=cast(str, row["resource_key"]),
            requested_at=cast(datetime, row["requested_at"]),
            request_parameters=cast(JsonObject, row["request_parameters"]),
            payload=cast(JsonObject, row["payload"]),
            checksum_sha256=cast(str, row["checksum_sha256"]),
        )

    async def _resolve_target(
        self, connection: AsyncConnection, game_id: GameId
    ) -> FeatureTargetSnapshot:
        inputs: dict[str, RowMapping] = {}
        for kind in ("box_score", "play_by_play"):
            row = (
                (
                    await connection.execute(
                        select(ingestion_resources)
                        .where(
                            ingestion_resources.c.source == self._source,
                            ingestion_resources.c.resource_type == kind,
                            ingestion_resources.c.resource_key == f"{kind}:{game_id.value}",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != "succeeded" or row["applied_document_id"] is None:
                raise RuntimeError(f"target_{kind}_unavailable")
            inputs[kind] = row
        game_row = (
            (
                await connection.execute(
                    select(games).where(games.c.game_id == game_id.value).with_for_update(read=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if game_row is None or game_row["status"] != "final":
            raise RuntimeError("target_game_unavailable")
        game = self._game_from_row(game_row)
        context = GameContext.from_game(game)
        try:
            pbp_snapshot = await self._possession._load_snapshot(
                connection, context, inputs["play_by_play"]
            )
            lineup_snapshot = await self._lineup._load_snapshot(connection, context, inputs)
        except (NoResultFound, RuntimeError, ValueError):
            raise RuntimeError("source_lineage_mismatch") from None

        possession_resource = (
            (
                await connection.execute(
                    select(possession_rebuild_resources)
                    .where(possession_rebuild_resources.c.game_id == game_id.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if possession_resource is None or possession_resource["published_snapshot_id"] is None:
            raise RuntimeError("target_possession_absent")
        possession_row = (
            (
                await connection.execute(
                    select(possession_snapshot_attempts)
                    .where(
                        possession_snapshot_attempts.c.snapshot_id
                        == possession_resource["published_snapshot_id"]
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if possession_row is None:
            raise RuntimeError("target_possession_corrupt")
        possession_token = PossessionInputToken.from_snapshot(
            pbp_snapshot, self._possession_versions
        )
        possession_revision = parse_revision_id(
            game_id=game_id,
            pbp_document_sha256=possession_token.applied_pbp_sha256,
            canonical_event_sha256=possession_token.canonical_event_sha256,
            game_context_sha256=possession_token.game_context_sha256,
            versions=self._possession_versions,
        )
        try:
            possession_current = self._possession._is_effective_current(
                possession_resource, possession_row, possession_token, possession_revision
            )
            possession_reference = self._possession._current_from_row(context, possession_row)
        except (RuntimeError, ValueError):
            raise RuntimeError("target_possession_stale") from None
        if not possession_current:
            raise RuntimeError("target_possession_stale")

        lineup_resource = (
            (
                await connection.execute(
                    select(lineup_rebuild_resources)
                    .where(lineup_rebuild_resources.c.game_id == game_id.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if lineup_resource is None or lineup_resource["published_snapshot_id"] is None:
            raise RuntimeError("target_lineup_absent")
        lineup_row = (
            (
                await connection.execute(
                    select(lineup_snapshot_attempts)
                    .where(
                        lineup_snapshot_attempts.c.snapshot_id
                        == lineup_resource["published_snapshot_id"]
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if lineup_row is None:
            raise RuntimeError("target_lineup_corrupt")
        lineup_token = LineupInputToken.from_snapshot(lineup_snapshot, self._lineup_versions)
        lineup_revision = lineup_revision_id(
            game_id=game_id,
            box_score_document_sha256=lineup_token.applied_box_score_sha256,
            pbp_document_sha256=lineup_token.applied_pbp_sha256,
            canonical_player_box_sha256=lineup_token.canonical_player_box_sha256,
            canonical_event_sha256=lineup_token.canonical_event_sha256,
            game_context_sha256=lineup_token.game_context_sha256,
            versions=self._lineup_versions,
        )
        try:
            lineup_current = self._lineup._is_effective_current(
                lineup_resource, lineup_row, lineup_token, lineup_revision
            )
            lineup_reference = self._lineup._current_from_row(context, lineup_row)
        except (RuntimeError, ValueError):
            raise RuntimeError("target_lineup_stale") from None
        if not lineup_current:
            raise RuntimeError("target_lineup_stale")
        try:
            return FeatureTargetSnapshot(game, pbp_snapshot, possession_reference, lineup_reference)
        except ValueError:
            raise RuntimeError("source_lineage_mismatch") from None

    async def _lock_claim_for_transition(
        self,
        connection: AsyncConnection,
        claim: FeatureBuildClaim,
        *,
        allowed_states: set[FeatureAttemptState],
    ) -> tuple[RowMapping, RowMapping, bool]:
        source_is_current = False
        try:
            current_target = await self._resolve_target(connection, claim.attempt.game_id)
            current_source = await self._build_source(
                connection, current_target, claim.source.target_player_ids
            )
            current_token = FeatureInputToken.from_source(
                current_source, claim.attempt.input_token.versions
            )
            source_is_current = (
                current_source == claim.source and current_token == claim.attempt.input_token
            )
        except (RuntimeError, ValueError):
            source_is_current = False
        resource = (
            (
                await connection.execute(
                    select(feature_build_resources)
                    .where(feature_build_resources.c.game_id == claim.attempt.game_id.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
        dependencies_match = await self._dependencies_match(connection, claim)
        self._require_active_claim(
            resource,
            attempt,
            claim,
            now=self._now(),
            allowed_states=allowed_states,
            dependencies_match=dependencies_match,
        )
        return resource, attempt, source_is_current

    async def _dependencies_match(
        self, connection: AsyncConnection, claim: FeatureBuildClaim
    ) -> bool:
        return await self._manifest_rows_match(
            connection,
            claim.attempt.snapshot_id,
            claim.source.manifest.dependencies,
        )

    async def _manifest_rows_match(
        self,
        connection: AsyncConnection,
        snapshot_id: UUID,
        dependencies: tuple[FeatureDependency, ...],
    ) -> bool:
        rows = (
            (
                await connection.execute(
                    select(feature_snapshot_dependencies)
                    .where(feature_snapshot_dependencies.c.snapshot_id == snapshot_id)
                    .order_by(feature_snapshot_dependencies.c.ordinal)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != len(dependencies):
            return False
        for row, dependency in zip(rows, dependencies, strict=True):
            expected = {
                "dependency_game_id": dependency.dependency_game_id.value,
                "started_at": dependency.started_at,
                "game_context_sha256": dependency.game_context_sha256,
                "applied_box_score_document_id": (dependency.applied_box_score_document_id),
                "applied_box_score_sha256": dependency.applied_box_score_sha256,
                "canonical_team_box_sha256": dependency.canonical_team_box_sha256,
                "canonical_player_box_sha256": dependency.canonical_player_box_sha256,
                "possession_snapshot_id": dependency.possession_snapshot_id,
                "parse_revision_id": dependency.parse_revision_id,
                "possession_input_token_sha256": (dependency.possession_input_token_sha256),
                "possession_output_sha256": dependency.possession_output_sha256,
                "ordinal": dependency.ordinal,
                "inclusion_reasons": sum(
                    _INCLUSION_REASON_BITS[reason] for reason in dependency.inclusion_reasons
                ),
            }
            if any(row[name] != value for name, value in expected.items()):
                return False
        return True

    async def _terminate_failed_claim(
        self,
        claim: FeatureBuildClaim,
        *,
        error_code: str,
        error_type: str,
        allowed_states: set[FeatureAttemptState],
    ) -> None:
        async with self._engine.begin() as connection:
            resource, attempt, source_is_current = await self._lock_claim_for_transition(
                connection, claim, allowed_states=allowed_states
            )
            now = self._now()
            if not source_is_current:
                await self._supersede_claim(connection, resource, attempt, claim, now)
            else:
                await connection.execute(
                    update(feature_snapshot_attempts)
                    .where(feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(
                        state=FeatureAttemptState.POISONED.value,
                        safe_error_code=error_code,
                        safe_error_type=error_type,
                    )
                )
                await connection.execute(
                    update(feature_build_resources)
                    .where(
                        feature_build_resources.c.game_id == claim.attempt.game_id.value,
                        feature_build_resources.c.active_run_id == claim.run_id,
                        feature_build_resources.c.active_claim_version == claim.claim_version,
                        feature_build_resources.c.active_snapshot_id == claim.attempt.snapshot_id,
                    )
                    .values(
                        **_CLEAR_ACTIVE,
                        last_error_category=error_code,
                        last_error_type=error_type,
                        updated_at=now,
                        completed_at=now,
                    )
                )
        if not source_is_current:
            raise RuntimeError("candidate source changed")

    @staticmethod
    async def _supersede_claim(
        connection: AsyncConnection,
        resource: RowMapping,
        attempt: RowMapping,
        claim: FeatureBuildClaim,
        now: datetime,
    ) -> None:
        await connection.execute(
            update(feature_snapshot_attempts)
            .where(
                feature_snapshot_attempts.c.snapshot_id == attempt["snapshot_id"],
                feature_snapshot_attempts.c.state.in_(_ACTIVE_STATES),
            )
            .values(state=FeatureAttemptState.SUPERSEDED.value)
        )
        await connection.execute(
            update(feature_build_resources)
            .where(
                feature_build_resources.c.game_id == claim.attempt.game_id.value,
                feature_build_resources.c.active_run_id == claim.run_id,
                feature_build_resources.c.active_claim_version == claim.claim_version,
                feature_build_resources.c.active_snapshot_id == claim.attempt.snapshot_id,
            )
            .values(
                **_CLEAR_ACTIVE,
                last_error_category="stale_input",
                last_error_type="fence",
                updated_at=now,
                completed_at=now,
            )
        )

    @staticmethod
    def _require_active_claim(
        resource: RowMapping,
        attempt: RowMapping,
        claim: FeatureBuildClaim,
        *,
        now: datetime,
        allowed_states: set[FeatureAttemptState],
        dependencies_match: bool,
    ) -> None:
        token = claim.attempt.input_token
        if (
            resource["game_id"] != claim.attempt.game_id.value
            or resource["active_run_id"] != claim.run_id
            or resource["active_claim_version"] != claim.claim_version
            or resource["claim_version"] != claim.claim_version
            or resource["active_snapshot_id"] != claim.attempt.snapshot_id
            or resource["active_feature_revision_id"] != claim.attempt.feature_revision_id
            or resource["active_feature_input_token_sha256"] != token.sha256
            or resource["claimed_until"] is None
            or resource["claimed_until"] <= now
            or attempt["created_by_run_id"] != claim.run_id
            or attempt["created_by_claim_version"] != claim.claim_version
            or attempt["snapshot_id"] != claim.attempt.snapshot_id
            or attempt["generation"] != claim.attempt.generation
            or attempt["feature_revision_id"] != claim.attempt.feature_revision_id
            or attempt["feature_input_token_sha256"] != token.sha256
            or attempt["possession_snapshot_id"] != token.possession_snapshot_id
            or attempt["parse_revision_id"] != token.parse_revision_id
            or attempt["possession_input_token_sha256"] != token.possession_input_token_sha256
            or attempt["possession_output_sha256"] != token.possession_output_sha256
            or attempt["lineup_snapshot_id"] != token.lineup_snapshot_id
            or attempt["lineup_revision_id"] != token.lineup_revision_id
            or attempt["lineup_input_token_sha256"] != token.lineup_input_token_sha256
            or attempt["lineup_output_sha256"] != token.lineup_output_sha256
            or attempt["lineup_result_status"] != token.lineup_result_status.value
            or attempt["dependency_input_sha256"] != token.dependency_input_sha256
            or attempt["dependency_revision_sha256"] != token.dependency_revision_sha256
            or attempt["feature_join_version"] != token.versions.join_version
            or attempt["feature_formula_version"] != token.versions.formula_version
            or attempt["feature_event_normalizer_version"]
            != token.versions.event_normalizer_version
            or attempt["output_schema_version"] != token.versions.output_schema_version
            or attempt["player_output_schema_version"]
            != token.versions.player_output_schema_version
            or FeatureAttemptState(cast(str, attempt["state"])) not in allowed_states
            or not dependencies_match
        ):
            raise RuntimeError("feature claim fence is stale")

    async def _lock_attempt(self, connection: AsyncConnection, snapshot_id: UUID) -> RowMapping:
        return (
            (
                await connection.execute(
                    select(feature_snapshot_attempts)
                    .where(feature_snapshot_attempts.c.snapshot_id == snapshot_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    @staticmethod
    def _require_active_fence(resource: RowMapping, attempt: RowMapping) -> None:
        if (
            resource["active_run_id"] != attempt["created_by_run_id"]
            or resource["active_claim_version"] != attempt["created_by_claim_version"]
            or resource["claim_version"] != resource["active_claim_version"]
            or resource["active_snapshot_id"] != attempt["snapshot_id"]
            or resource["active_feature_revision_id"] != attempt["feature_revision_id"]
            or resource["active_feature_input_token_sha256"]
            != attempt["feature_input_token_sha256"]
            or attempt["state"] not in _ACTIVE_STATES
        ):
            raise RuntimeError("feature claim fence is stale")

    @staticmethod
    def _is_effective_current(
        resource: RowMapping, attempt: RowMapping, token: FeatureInputToken
    ) -> bool:
        result_status = attempt["result_status"]
        if result_status not in {"valid", "invalid"}:
            return False
        expected_state = "invalid" if result_status == "invalid" else "current"
        versions = token.versions
        return (
            resource["publication_state"] == expected_state
            and resource["published_snapshot_id"] == attempt["snapshot_id"]
            and resource["published_feature_revision_id"] == token.feature_revision_id
            and attempt["feature_revision_id"] == token.feature_revision_id
            and resource["published_feature_input_token_sha256"] == token.sha256
            and attempt["feature_input_token_sha256"] == token.sha256
            and attempt["possession_snapshot_id"] == token.possession_snapshot_id
            and attempt["parse_revision_id"] == token.parse_revision_id
            and attempt["possession_input_token_sha256"] == token.possession_input_token_sha256
            and attempt["possession_output_sha256"] == token.possession_output_sha256
            and attempt["lineup_snapshot_id"] == token.lineup_snapshot_id
            and attempt["lineup_revision_id"] == token.lineup_revision_id
            and attempt["lineup_input_token_sha256"] == token.lineup_input_token_sha256
            and attempt["lineup_output_sha256"] == token.lineup_output_sha256
            and attempt["lineup_result_status"] == token.lineup_result_status.value
            and attempt["dependency_input_sha256"] == token.dependency_input_sha256
            and attempt["dependency_revision_sha256"] == token.dependency_revision_sha256
            and attempt["state"] == FeatureAttemptState.PUBLISHED.value
            and attempt["feature_join_version"] == versions.join_version
            and attempt["feature_formula_version"] == versions.formula_version
            and attempt["feature_event_normalizer_version"] == versions.event_normalizer_version
            and attempt["output_schema_version"] == versions.output_schema_version
            and attempt["player_output_schema_version"] == versions.player_output_schema_version
            and attempt["expected_state_row_count"] is not None
            and attempt["expected_state_output_sha256"] is not None
            and attempt["expected_player_row_count"] is not None
            and attempt["expected_player_output_sha256"] is not None
            and attempt["expected_feature_output_sha256"] is not None
            and attempt["expected_issue_codes"] is not None
            and tuple(resource["last_issue_codes"]) == tuple(attempt["expected_issue_codes"])
        )

    @classmethod
    def _current_from_row(
        cls, game: Game, row: RowMapping | dict[str, Any]
    ) -> CurrentFeatureSnapshotReference:
        return CurrentFeatureSnapshotReference(
            attempt=cls._attempt_from_row(game, row),
            state_row_count=cast(int, row["expected_state_row_count"]),
            state_output_sha256=cast(str, row["expected_state_output_sha256"]),
            player_row_count=cast(int, row["expected_player_row_count"]),
            player_output_sha256=cast(str, row["expected_player_output_sha256"]),
            feature_output_sha256=cast(str, row["expected_feature_output_sha256"]),
        )

    @staticmethod
    def _attempt_from_row(game: Game, row: RowMapping | dict[str, Any]) -> FeatureSnapshotAttempt:
        versions = FeatureVersions(
            row["feature_join_version"],
            row["feature_formula_version"],
            row["feature_event_normalizer_version"],
            row["output_schema_version"],
            row["player_output_schema_version"],
        )
        token = FeatureInputToken(
            game_id=game.game_id,
            target_started_at=game.started_at,
            game_context_sha256=game_context_checksum(GameContext.from_game(game)),
            possession_snapshot_id=_uuid(row["possession_snapshot_id"]),
            parse_revision_id=cast(str, row["parse_revision_id"]),
            possession_input_token_sha256=cast(str, row["possession_input_token_sha256"]),
            possession_output_sha256=cast(str, row["possession_output_sha256"]),
            lineup_snapshot_id=_uuid(row["lineup_snapshot_id"]),
            lineup_revision_id=cast(str, row["lineup_revision_id"]),
            lineup_input_token_sha256=cast(str, row["lineup_input_token_sha256"]),
            lineup_output_sha256=cast(str, row["lineup_output_sha256"]),
            lineup_result_status=LineupStatus(cast(str, row["lineup_result_status"])),
            dependency_input_sha256=cast(str, row["dependency_input_sha256"]),
            dependency_revision_sha256=cast(str, row["dependency_revision_sha256"]),
            versions=versions,
        )
        return FeatureSnapshotAttempt(
            game=game,
            snapshot_id=_uuid(row["snapshot_id"]),
            generation=cast(int, row["generation"]),
            feature_revision_id=cast(str, row["feature_revision_id"]),
            input_token=token,
            state=FeatureAttemptState(cast(str, row["state"])),
            created_by_run_id=_uuid(row["created_by_run_id"]),
            created_by_claim_version=cast(int, row["created_by_claim_version"]),
            created_at=cast(datetime, row["created_at"]),
            expected_state_row_count=cast(int | None, row["expected_state_row_count"]),
            expected_state_output_sha256=cast(str | None, row["expected_state_output_sha256"]),
            expected_player_row_count=cast(int | None, row["expected_player_row_count"]),
            expected_player_output_sha256=cast(str | None, row["expected_player_output_sha256"]),
            expected_feature_output_sha256=cast(str | None, row["expected_feature_output_sha256"]),
            expected_issue_codes=None
            if row["expected_issue_codes"] is None
            else tuple(row["expected_issue_codes"]),
            result_status=None
            if row["result_status"] is None
            else FeatureResultStatus(cast(str, row["result_status"])),
            insert_started_at=cast(datetime | None, row["insert_started_at"]),
            verified_at=cast(datetime | None, row["verified_at"]),
            published_at=cast(datetime | None, row["published_at"]),
        )

    @staticmethod
    def _game_from_row(row: RowMapping) -> Game:
        return Game(
            game_id=GameId(cast(str, row["game_id"])),
            season=SeasonId(cast(str, row["season"])),
            season_type=SeasonType(cast(str, row["season_type"])),
            game_date=cast(date, row["game_date"]),
            started_at=cast(datetime, row["started_at"]),
            home_team_id=cast(int, row["home_team_id"]),
            away_team_id=cast(int, row["away_team_id"]),
            home_score=cast(int, row["home_score"]),
            away_score=cast(int, row["away_score"]),
            status=GameStatus(cast(str, row["status"])),
            arena_name=cast(str | None, row["arena_name"]),
            arena_city=cast(str | None, row["arena_city"]),
            arena_state=cast(str | None, row["arena_state"]),
        )

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            raise ValueError("utc_now must return a timezone-aware datetime")
        return value.astimezone(UTC)


__all__ = ["PostgresFeatureWorkStore"]
