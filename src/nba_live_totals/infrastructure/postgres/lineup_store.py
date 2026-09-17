"""Atomic dual-input PostgreSQL snapshots and fenced lineup claim allocation."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
)
from nba_live_totals.domain import (
    Game,
    GameContext,
    GameId,
    GameStatus,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    game_context_checksum,
)
from nba_live_totals.domain.lineup_hashing import (
    canonical_player_boxes_checksum,
    lineup_revision_id,
)
from nba_live_totals.domain.lineups import (
    LineupIssue,
    LineupPlayerBox,
    LineupStatus,
    LineupVersions,
)
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    lineup_rebuild_resources,
    lineup_rebuild_runs,
    lineup_snapshot_attempts,
    play_by_play_events,
    player_box_scores,
    source_documents,
)

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_ACTIVE_STATES = {"allocated", "writing", "verified"}
_CLEAR_ACTIVE = dict.fromkeys(
    (
        "active_run_id",
        "active_claim_version",
        "active_snapshot_id",
        "active_lineup_revision_id",
        "active_input_token_sha256",
        "claimed_until",
    )
)


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object) -> UUID:
    return value if type(value) is UUID else UUID(str(value))


class PostgresLineupWorkStore:
    """Own Phase 4 input consistency, freshness identity and leased claims."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        source: str = "nba.com",
        lease_seconds: int = 300,
        utc_now: Callable[[], datetime] = _system_utc_now,
    ) -> None:
        if not source:
            raise ValueError("source must not be empty")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease seconds must be an integer between 1 and 86400")
        self._engine = engine
        self._source = source
        self._lease = timedelta(seconds=lease_seconds)
        self._utc_now = utc_now

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            raise ValueError("utc_now must return a timezone-aware datetime")
        return value.astimezone(UTC)

    async def start_run(self, request: BuildLineupsRequest) -> UUID:
        run_id = uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                lineup_rebuild_runs.insert().values(
                    run_id=run_id,
                    selectors={
                        "seasons": [season.value for season in request.seasons],
                        "game_id": request.game_id.value if request.game_id else None,
                    },
                    mode=request.mode.value,
                    forced_rebuild=request.rebuild,
                    status="running",
                    started_at=self._now(),
                )
            )
        return run_id

    async def finish_run(self, run_id: UUID, result: BuildLineupsResult) -> None:
        """Persist one terminal result, including its independent quality counter."""
        if type(run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        if result.run_id != run_id:
            raise ValueError("result run ID does not match run ID")
        async with self._engine.begin() as connection:
            updated = await connection.execute(
                update(lineup_rebuild_runs)
                .where(
                    lineup_rebuild_runs.c.run_id == run_id,
                    lineup_rebuild_runs.c.status == "running",
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
                raise RuntimeError("lineup rebuild run is missing or already terminal")

    async def select_games(self, request: BuildLineupsRequest) -> tuple[GameId, ...]:
        statement = select(games.c.game_id).where(
            games.c.season.in_([season.value for season in request.seasons]),
            games.c.season_type.in_(("regular_season", "playoffs")),
            games.c.status == "final",
        )
        for kind in ("box_score", "play_by_play"):
            statement = statement.where(
                select(ingestion_resources.c.resource_key)
                .where(
                    ingestion_resources.c.source == self._source,
                    ingestion_resources.c.resource_type == kind,
                    ingestion_resources.c.resource_key == kind + ":" + games.c.game_id,
                    ingestion_resources.c.status == "succeeded",
                    ingestion_resources.c.applied_document_id.is_not(None),
                )
                .exists()
            )
        if request.game_id is not None:
            statement = statement.where(games.c.game_id == request.game_id.value)
        async with self._engine.connect() as connection:
            return tuple(
                GameId(value)
                for value in (
                    await connection.execute(statement.order_by(games.c.game_date, games.c.game_id))
                ).scalars()
            )

    async def claim_game(
        self, run_id: UUID, game_id: GameId, versions: LineupVersions, *, rebuild: bool
    ) -> LineupClaimDecision:
        if type(run_id) is not UUID or not isinstance(game_id, GameId):
            raise ValueError("claim requires a UUID run ID and GameId")
        if not isinstance(versions, LineupVersions) or type(rebuild) is not bool:
            raise ValueError("claim requires LineupVersions and a boolean rebuild flag")
        async with self._engine.begin() as connection:
            # Cross-plane order: both Phase 2 resources, game, Phase 4 resource,
            # attempt rows, run. Retain these locks through canonical reads and commit.
            inputs = await self._lock_inputs(connection, game_id)
            game = await self._lock_game(connection, game_id)
            snapshot = await self._load_snapshot(connection, game, inputs)
            token = LineupInputToken.from_snapshot(snapshot, versions)
            revision = lineup_revision_id(
                game_id=game_id,
                box_score_document_sha256=token.applied_box_score_sha256,
                pbp_document_sha256=token.applied_pbp_sha256,
                canonical_player_box_sha256=token.canonical_player_box_sha256,
                canonical_event_sha256=token.canonical_event_sha256,
                game_context_sha256=token.game_context_sha256,
                versions=versions,
            )
            await connection.execute(
                insert(lineup_rebuild_resources)
                .values(game_id=game_id.value)
                .on_conflict_do_nothing(index_elements=[lineup_rebuild_resources.c.game_id])
            )
            resource = (
                (
                    await connection.execute(
                        select(lineup_rebuild_resources)
                        .where(lineup_rebuild_resources.c.game_id == game_id.value)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            now = self._now()
            if resource["active_run_id"] is not None:
                active = await self._lock_attempt(connection, _uuid(resource["active_snapshot_id"]))
                # Expiry or changed inputs permit supersession, never repairing
                # an inconsistent ownership tuple by mutating someone else's attempt.
                self._require_active_fence(resource, active)
                fresh = (
                    active["input_token_sha256"] == token.sha256
                    and active["lineup_revision_id"] == revision
                )
                if resource["claimed_until"] > now and fresh:
                    if _uuid(resource["active_run_id"]) != run_id:
                        return LineupClaimDecision(LineupClaimAction.BUSY)
                    return LineupClaimDecision(
                        LineupClaimAction.RESUME,
                        claim=LineupBuildClaim(
                            run_id=run_id,
                            claim_version=resource["active_claim_version"],
                            attempt=self._attempt_from_row(game, active),
                            snapshot=snapshot,
                        ),
                    )
                await connection.execute(
                    update(lineup_snapshot_attempts)
                    .where(
                        lineup_snapshot_attempts.c.snapshot_id == active["snapshot_id"],
                        lineup_snapshot_attempts.c.state.in_(_ACTIVE_STATES),
                    )
                    .values(state="superseded")
                )
                await connection.execute(
                    update(lineup_rebuild_resources)
                    .where(lineup_rebuild_resources.c.game_id == game_id.value)
                    .values(**_CLEAR_ACTIVE, updated_at=now)
                )

            if resource["published_snapshot_id"] is not None:
                published = await self._lock_attempt(
                    connection, _uuid(resource["published_snapshot_id"])
                )
                current = self._is_effective_current(resource, published, token, revision)
                if current and not rebuild:
                    attempt = self._attempt_from_row(game, published)
                    return LineupClaimDecision(
                        LineupClaimAction.SKIP_CURRENT,
                        current=CurrentLineupSnapshotReference(
                            attempt=attempt,
                            row_count=published["expected_row_count"],
                            output_sha256=published["expected_output_sha256"],
                        ),
                    )
                if not current:
                    await connection.execute(
                        update(lineup_rebuild_resources)
                        .where(lineup_rebuild_resources.c.game_id == game_id.value)
                        .values(publication_state="stale", updated_at=now)
                    )

            claim_version = resource["claim_version"]
            generation = resource["next_generation"]
            count = resource["attempt_count"]
            if any(value >= _MAX_SIGNED_BIGINT for value in (claim_version, generation, count)):
                raise RuntimeError("lineup claim counter exhausted")
            run = (
                (
                    await connection.execute(
                        select(lineup_rebuild_runs)
                        .where(lineup_rebuild_runs.c.run_id == run_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] != "running" or run["mode"] != "materialize":
                raise RuntimeError("lineup allocation requires a running materialize run")
            snapshot_id = uuid4()
            claim_version += 1
            await connection.execute(
                lineup_snapshot_attempts.insert().values(
                    snapshot_id=snapshot_id,
                    game_id=game_id.value,
                    generation=generation,
                    lineup_revision_id=revision,
                    input_token_sha256=token.sha256,
                    input_box_score_document_id=token.applied_box_score_document_id,
                    input_box_score_sha256=token.applied_box_score_sha256,
                    input_pbp_document_id=token.applied_pbp_document_id,
                    input_pbp_sha256=token.applied_pbp_sha256,
                    canonical_player_box_sha256=token.canonical_player_box_sha256,
                    canonical_event_sha256=token.canonical_event_sha256,
                    game_context_sha256=token.game_context_sha256,
                    normalizer_version=versions.normalizer_version,
                    reconstructor_version=versions.reconstructor_version,
                    rules_version=versions.rules_version,
                    output_schema_version=versions.output_schema_version,
                    state="allocated",
                    created_by_run_id=run_id,
                    created_by_claim_version=claim_version,
                    created_at=now,
                )
            )
            await connection.execute(
                update(lineup_rebuild_resources)
                .where(lineup_rebuild_resources.c.game_id == game_id.value)
                .values(
                    attempt_count=count + 1,
                    claim_version=claim_version,
                    next_generation=generation + 1,
                    last_run_id=run_id,
                    active_run_id=run_id,
                    active_claim_version=claim_version,
                    active_snapshot_id=snapshot_id,
                    active_lineup_revision_id=revision,
                    active_input_token_sha256=token.sha256,
                    claimed_until=now + self._lease,
                    updated_at=now,
                )
            )
            attempt = LineupSnapshotAttempt(
                game=game,
                snapshot_id=snapshot_id,
                generation=generation,
                lineup_revision_id=revision,
                input_token=token,
                state=LineupAttemptState.ALLOCATED,
            )
            return LineupClaimDecision(
                LineupClaimAction.BUILD,
                claim=LineupBuildClaim(
                    run_id=run_id, claim_version=claim_version, attempt=attempt, snapshot=snapshot
                ),
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
        """Persist one immutable expected output while the complete claim fence is live."""
        async with self._engine.begin() as connection:
            resource = await self._lock_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={LineupAttemptState.ALLOCATED},
            )
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state=LineupAttemptState.WRITING.value,
                    expected_row_count=expected_row_count,
                    expected_output_sha256=expected_output_sha256,
                    expected_issue_codes=self._issue_projections(expected_issue_codes),
                    result_status=result_status.value,
                    insert_started_at=now,
                )
            )
            updated = dict(attempt) | {
                "state": LineupAttemptState.WRITING.value,
                "expected_row_count": expected_row_count,
                "expected_output_sha256": expected_output_sha256,
                "expected_issue_codes": self._issue_projections(expected_issue_codes),
                "result_status": result_status.value,
            }
            return self._attempt_from_row(claim.snapshot.game, updated)

    async def mark_verified(
        self, claim: LineupBuildClaim, verification: LineupSnapshotVerification
    ) -> LineupSnapshotAttempt:
        """Advance a writing attempt only after exact semantic readback."""
        async with self._engine.begin() as connection:
            resource = await self._lock_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={LineupAttemptState.WRITING},
            )
            expected_issues = tuple(
                LineupIssue(**issue) for issue in attempt["expected_issue_codes"]
            )
            assert verification.result_status is not None
            if (
                verification.snapshot_id != claim.attempt.snapshot_id
                or not verification.is_exact
                or verification.row_count != attempt["expected_row_count"]
                or verification.output_sha256 != attempt["expected_output_sha256"]
                or verification.result_status.value != attempt["result_status"]
                or verification.issues != expected_issues
            ):
                raise RuntimeError("verification does not exactly match writing attempt")
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(state=LineupAttemptState.VERIFIED.value, verified_at=now)
            )
            return self._attempt_from_row(
                claim.snapshot.game, dict(attempt) | {"state": LineupAttemptState.VERIFIED.value}
            )

    async def poison_attempt(
        self, claim: LineupBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        """Terminally record an unpublishable candidate without replacing the old pointer."""
        await self._terminate_failed_claim(
            claim,
            error_code=error_code,
            error_type=error_type,
            allowed_states={
                LineupAttemptState.ALLOCATED,
                LineupAttemptState.WRITING,
                LineupAttemptState.VERIFIED,
            },
        )

    async def publish_snapshot(self, claim: LineupBuildClaim) -> CurrentLineupSnapshotReference:
        """Atomically publish a verified candidate only while its full input remains current."""
        stale = False
        reference: CurrentLineupSnapshotReference | None = None
        async with self._engine.begin() as connection:
            current_token, current_revision = await self._current_identity(connection, claim)
            resource = await self._lock_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={LineupAttemptState.VERIFIED},
            )
            if (
                current_token != claim.attempt.input_token
                or attempt["input_token_sha256"] != current_token.sha256
                or attempt["lineup_revision_id"] != current_revision
            ):
                await self._supersede_stale_claim(
                    connection, resource, attempt, claim, current_token, current_revision, now
                )
                stale = True
            else:
                result_status = LineupStatus(attempt["result_status"])
                publication_state = (
                    LineupPublicationState.CURRENT
                    if result_status is LineupStatus.VALID
                    else LineupPublicationState.INVALID
                )
                issues = attempt["expected_issue_codes"]
                await connection.execute(
                    update(lineup_snapshot_attempts)
                    .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(state=LineupAttemptState.PUBLISHED.value, published_at=now)
                )
                await connection.execute(
                    update(lineup_rebuild_resources)
                    .where(lineup_rebuild_resources.c.game_id == claim.attempt.game_id.value)
                    .values(
                        publication_state=publication_state.value,
                        published_snapshot_id=claim.attempt.snapshot_id,
                        published_lineup_revision_id=claim.attempt.lineup_revision_id,
                        published_input_token_sha256=current_token.sha256,
                        published_at=now,
                        last_issue_codes=issues,
                        **_CLEAR_ACTIVE,
                        last_error_code=None,
                        last_error_type=None,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                reference = self._current_from_row(
                    claim.snapshot.game,
                    dict(attempt) | {"state": LineupAttemptState.PUBLISHED.value},
                )
        if stale:
            raise RuntimeError("candidate input token is stale")
        if reference is None:
            raise RuntimeError("publication did not produce a current lineup reference")
        return reference

    async def fail_game(self, claim: LineupBuildClaim, *, error_code: str, error_type: str) -> None:
        """Record a pre-write structural failure and release only its still-live claim."""
        await self._terminate_failed_claim(
            claim,
            error_code=error_code,
            error_type=error_type,
            allowed_states={LineupAttemptState.ALLOCATED},
        )

    async def resolve_current(
        self, game_id: GameId, versions: LineupVersions
    ) -> CurrentLineupSnapshotResolution:
        """Resolve the persisted pointer without allocating, repairing, or updating it."""
        if not isinstance(game_id, GameId) or not isinstance(versions, LineupVersions):
            raise ValueError("current resolution requires a GameId and LineupVersions")
        async with self._engine.begin() as connection:
            inputs = await self._lock_inputs(connection, game_id)
            game = await self._lock_game(connection, game_id)
            snapshot = await self._load_snapshot(connection, game, inputs)
            token = LineupInputToken.from_snapshot(snapshot, versions)
            revision = lineup_revision_id(
                game_id=game_id,
                box_score_document_sha256=token.applied_box_score_sha256,
                pbp_document_sha256=token.applied_pbp_sha256,
                canonical_player_box_sha256=token.canonical_player_box_sha256,
                canonical_event_sha256=token.canonical_event_sha256,
                game_context_sha256=token.game_context_sha256,
                versions=versions,
            )
            exists = await connection.scalar(
                select(lineup_rebuild_resources.c.game_id).where(
                    lineup_rebuild_resources.c.game_id == game_id.value
                )
            )
            if exists is None:
                return CurrentLineupSnapshotResolution(LineupPublicationState.ABSENT)
            resource = await self._lock_resource(connection, game_id)
            published_id = resource["published_snapshot_id"]
            if published_id is None:
                return CurrentLineupSnapshotResolution(LineupPublicationState.ABSENT)
            attempt = await self._lock_attempt(connection, _uuid(published_id))
            if not self._is_effective_current(resource, attempt, token, revision):
                return CurrentLineupSnapshotResolution(LineupPublicationState.STALE)
            try:
                return CurrentLineupSnapshotResolution(
                    LineupPublicationState(resource["publication_state"]),
                    self._current_from_row(game, attempt),
                )
            except (RuntimeError, ValueError):
                return CurrentLineupSnapshotResolution(LineupPublicationState.STALE)

    async def _lock_inputs(
        self, connection: AsyncConnection, game_id: GameId
    ) -> dict[str, RowMapping]:
        rows = {}
        for kind in sorted(("box_score", "play_by_play")):
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
                raise RuntimeError(f"game does not have successful applied {kind}")
            rows[kind] = row
        return rows

    async def _lock_game(self, connection: AsyncConnection, game_id: GameId) -> GameContext:
        row = (
            (
                await connection.execute(
                    select(games).where(games.c.game_id == game_id.value).with_for_update(read=True)
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["status"] != "final"
            or row["season_type"] not in {"regular_season", "playoffs"}
        ):
            raise RuntimeError("game is not an eligible completed competition")
        return GameContext.from_game(
            Game(
                game_id=GameId(row["game_id"]),
                season=SeasonId(row["season"]),
                season_type=SeasonType(row["season_type"]),
                game_date=row["game_date"],
                started_at=row["started_at"],
                home_team_id=row["home_team_id"],
                away_team_id=row["away_team_id"],
                home_score=row["home_score"],
                away_score=row["away_score"],
                status=GameStatus(row["status"]),
                arena_name=row["arena_name"],
                arena_city=row["arena_city"],
                arena_state=row["arena_state"],
            )
        )

    async def _load_snapshot(
        self, connection: AsyncConnection, game: GameContext, inputs: dict[str, RowMapping]
    ) -> GameLineupInputSnapshot:
        documents = {}
        for kind, resource in inputs.items():
            row = (
                (
                    await connection.execute(
                        select(source_documents).where(
                            source_documents.c.document_id == resource["applied_document_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            if (
                row["source"] != self._source
                or row["resource_key"] != f"{kind}:{game.game_id.value}"
            ):
                raise RuntimeError("applied source document does not match input resource")
            documents[kind] = SourceDocument(
                **{
                    name: row[name]
                    for name in (
                        "source",
                        "endpoint",
                        "resource_key",
                        "requested_at",
                        "request_parameters",
                        "payload",
                        "checksum_sha256",
                    )
                }
            )
        player_fields = (
            "game_id",
            "team_id",
            "player_id",
            "position",
            "starter",
            "played",
            "comment",
        )
        player_rows = (
            await connection.execute(
                select(*(player_box_scores.c[name] for name in player_fields))
                .where(player_box_scores.c.game_id == game.game_id.value)
                .order_by(player_box_scores.c.team_id, player_box_scores.c.player_id)
            )
        ).mappings()
        boxes = tuple(
            LineupPlayerBox(**(dict(row) | {"game_id": game.game_id})) for row in player_rows
        )
        event_rows = (
            await connection.execute(
                select(play_by_play_events)
                .where(play_by_play_events.c.game_id == game.game_id.value)
                .order_by(play_by_play_events.c.sequence_number)
            )
        ).mappings()
        event_fields = (
            "sequence_number",
            "action_number",
            "action_id",
            "period",
            "clock",
            "seconds_remaining_period_ms",
            "team_id",
            "person_id",
            "action_type",
            "sub_type",
            "description",
            "home_score",
            "away_score",
            "shot_result",
            "shot_distance",
            "legacy_x",
            "legacy_y",
            "location",
            "is_field_goal",
            "points_total",
            "video_available",
        )
        events = tuple(
            PlayByPlayEvent(game_id=game.game_id, **{name: row[name] for name in event_fields})
            for row in event_rows
        )
        return GameLineupInputSnapshot(
            game=game,
            applied_box_score_document_id=_uuid(inputs["box_score"]["applied_document_id"]),
            applied_box_score_sha256=documents["box_score"].checksum_sha256,
            applied_pbp_document_id=_uuid(inputs["play_by_play"]["applied_document_id"]),
            applied_pbp_sha256=documents["play_by_play"].checksum_sha256,
            canonical_player_boxes=boxes,
            canonical_events=events,
            canonical_player_box_sha256=canonical_player_boxes_checksum(boxes),
            canonical_event_sha256=canonical_event_checksum(events),
            game_context_sha256=game_context_checksum(game),
            box_score_source_document=documents["box_score"],
            pbp_source_document=documents["play_by_play"],
        )

    async def _lock_attempt(self, connection: AsyncConnection, snapshot_id: UUID) -> RowMapping:
        return (
            (
                await connection.execute(
                    select(lineup_snapshot_attempts)
                    .where(lineup_snapshot_attempts.c.snapshot_id == snapshot_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    async def _lock_resource(self, connection: AsyncConnection, game_id: GameId) -> RowMapping:
        return (
            (
                await connection.execute(
                    select(lineup_rebuild_resources)
                    .where(lineup_rebuild_resources.c.game_id == game_id.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    async def _current_identity(
        self, connection: AsyncConnection, claim: LineupBuildClaim
    ) -> tuple[LineupInputToken, str]:
        """Re-read both applied inputs under the production lock order."""
        inputs = await self._lock_inputs(connection, claim.attempt.game_id)
        game = await self._lock_game(connection, claim.attempt.game_id)
        snapshot = await self._load_snapshot(connection, game, inputs)
        token = LineupInputToken.from_snapshot(snapshot, claim.attempt.input_token.versions)
        revision = lineup_revision_id(
            game_id=claim.attempt.game_id,
            box_score_document_sha256=token.applied_box_score_sha256,
            pbp_document_sha256=token.applied_pbp_sha256,
            canonical_player_box_sha256=token.canonical_player_box_sha256,
            canonical_event_sha256=token.canonical_event_sha256,
            game_context_sha256=token.game_context_sha256,
            versions=token.versions,
        )
        return token, revision

    async def _terminate_failed_claim(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
        allowed_states: set[LineupAttemptState],
    ) -> None:
        """Poison only a live candidate; stale inputs are superseded instead."""
        async with self._engine.begin() as connection:
            current_token, current_revision = await self._current_identity(connection, claim)
            resource = await self._lock_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource, attempt, claim, now=now, allowed_states=allowed_states
            )
            if (
                current_token != claim.attempt.input_token
                or attempt["input_token_sha256"] != current_token.sha256
                or attempt["lineup_revision_id"] != current_revision
            ):
                await self._supersede_stale_claim(
                    connection, resource, attempt, claim, current_token, current_revision, now
                )
                stale = True
            else:
                publication_state = await self._publication_state(
                    connection, resource, current_token, current_revision
                )
                await connection.execute(
                    update(lineup_snapshot_attempts)
                    .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(
                        state=LineupAttemptState.POISONED.value,
                        safe_error_code=error_code,
                        safe_error_type=error_type,
                    )
                )
                await connection.execute(
                    update(lineup_rebuild_resources)
                    .where(
                        lineup_rebuild_resources.c.game_id == claim.attempt.game_id.value,
                        lineup_rebuild_resources.c.active_run_id == claim.run_id,
                        lineup_rebuild_resources.c.active_claim_version == claim.claim_version,
                        lineup_rebuild_resources.c.active_snapshot_id == claim.attempt.snapshot_id,
                    )
                    .values(
                        publication_state=publication_state.value,
                        **_CLEAR_ACTIVE,
                        last_error_code=error_code,
                        last_error_type=error_type,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                stale = False
        if stale:
            raise RuntimeError("candidate input token is stale")

    async def _supersede_stale_claim(
        self,
        connection: AsyncConnection,
        resource: RowMapping,
        attempt: RowMapping,
        claim: LineupBuildClaim,
        current_token: LineupInputToken,
        current_revision: str,
        now: datetime,
    ) -> None:
        publication_state = await self._publication_state(
            connection, resource, current_token, current_revision
        )
        await connection.execute(
            update(lineup_snapshot_attempts)
            .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
            .values(state=LineupAttemptState.SUPERSEDED.value)
        )
        await connection.execute(
            update(lineup_rebuild_resources)
            .where(
                lineup_rebuild_resources.c.game_id == claim.attempt.game_id.value,
                lineup_rebuild_resources.c.active_run_id == claim.run_id,
                lineup_rebuild_resources.c.active_claim_version == claim.claim_version,
                lineup_rebuild_resources.c.active_snapshot_id == claim.attempt.snapshot_id,
            )
            .values(
                publication_state=publication_state.value,
                **_CLEAR_ACTIVE,
                last_error_code="stale_input",
                last_error_type="fence",
                updated_at=now,
                completed_at=now,
            )
        )

    async def _publication_state(
        self,
        connection: AsyncConnection,
        resource: RowMapping,
        current_token: LineupInputToken,
        current_revision: str,
    ) -> LineupPublicationState:
        published_id = resource["published_snapshot_id"]
        if published_id is None:
            return LineupPublicationState.ABSENT
        published = await self._lock_attempt(connection, _uuid(published_id))
        if self._is_effective_current(resource, published, current_token, current_revision):
            return LineupPublicationState(resource["publication_state"])
        return LineupPublicationState.STALE

    @staticmethod
    def _require_active_claim(
        resource: RowMapping,
        attempt: RowMapping,
        claim: LineupBuildClaim,
        *,
        now: datetime,
        allowed_states: set[LineupAttemptState],
    ) -> None:
        if (
            resource["active_run_id"] is None
            or _uuid(resource["active_run_id"]) != claim.run_id
            or resource["active_claim_version"] != claim.claim_version
            or resource["claim_version"] != claim.claim_version
            or resource["active_snapshot_id"] is None
            or _uuid(resource["active_snapshot_id"]) != claim.attempt.snapshot_id
            or resource["active_lineup_revision_id"] != claim.attempt.lineup_revision_id
            or resource["active_input_token_sha256"] != claim.attempt.input_token.sha256
            or resource["claimed_until"] is None
            or resource["claimed_until"] <= now
            or attempt["created_by_run_id"] is None
            or _uuid(attempt["created_by_run_id"]) != claim.run_id
            or attempt["created_by_claim_version"] != claim.claim_version
            or _uuid(attempt["snapshot_id"]) != claim.attempt.snapshot_id
            or attempt["lineup_revision_id"] != claim.attempt.lineup_revision_id
            or attempt["input_token_sha256"] != claim.attempt.input_token.sha256
            or LineupAttemptState(attempt["state"]) not in allowed_states
        ):
            raise RuntimeError("lineup claim fence is stale")

    @staticmethod
    def _issue_projections(issues: tuple[LineupIssue, ...]) -> list[dict[str, object]]:
        return [
            {
                "team_id": issue.team_id,
                "reason": issue.reason,
                "sequence_number": issue.sequence_number,
                "boundary_side": issue.boundary_side.value,
            }
            for issue in issues
        ]

    def _current_from_row(
        self, game: GameContext, row: RowMapping | dict[str, Any]
    ) -> CurrentLineupSnapshotReference:
        attempt = self._attempt_from_row(game, row)
        return CurrentLineupSnapshotReference(
            attempt=attempt,
            row_count=row["expected_row_count"],
            output_sha256=row["expected_output_sha256"],
        )

    @staticmethod
    def _require_active_fence(resource: RowMapping, attempt: RowMapping) -> None:
        if (
            resource["game_id"] != attempt["game_id"]
            or resource["active_run_id"] != attempt["created_by_run_id"]
            or resource["active_claim_version"] != attempt["created_by_claim_version"]
            or resource["claim_version"] != resource["active_claim_version"]
            or resource["active_snapshot_id"] != attempt["snapshot_id"]
            or resource["active_lineup_revision_id"] != attempt["lineup_revision_id"]
            or resource["active_input_token_sha256"] != attempt["input_token_sha256"]
            or attempt["state"] not in _ACTIVE_STATES
        ):
            raise RuntimeError("lineup claim fence is stale")

    @staticmethod
    def _is_effective_current(
        resource: RowMapping, attempt: RowMapping, token: LineupInputToken, revision: str
    ) -> bool:
        return (
            resource["publication_state"] in {"current", "invalid"}
            and resource["published_snapshot_id"] == attempt["snapshot_id"]
            and resource["published_lineup_revision_id"]
            == attempt["lineup_revision_id"]
            == revision
            and resource["published_input_token_sha256"]
            == attempt["input_token_sha256"]
            == token.sha256
            and attempt["state"] == "published"
            and attempt["result_status"]
            == ("valid" if resource["publication_state"] == "current" else "invalid")
            and all(
                attempt[name] == getattr(token.versions, name)
                for name in (
                    "normalizer_version",
                    "reconstructor_version",
                    "rules_version",
                    "output_schema_version",
                )
            )
        )

    @staticmethod
    def _attempt_from_row(
        game: GameContext, row: RowMapping | dict[str, Any]
    ) -> LineupSnapshotAttempt:
        versions = LineupVersions(
            row["normalizer_version"],
            row["reconstructor_version"],
            row["rules_version"],
            row["output_schema_version"],
        )
        token = LineupInputToken(
            game_id=game.game_id,
            applied_box_score_document_id=_uuid(row["input_box_score_document_id"]),
            applied_box_score_sha256=row["input_box_score_sha256"],
            applied_pbp_document_id=_uuid(row["input_pbp_document_id"]),
            applied_pbp_sha256=row["input_pbp_sha256"],
            canonical_player_box_sha256=row["canonical_player_box_sha256"],
            canonical_event_sha256=row["canonical_event_sha256"],
            game_context_sha256=row["game_context_sha256"],
            versions=versions,
        )
        if token.sha256 != row["input_token_sha256"]:
            raise RuntimeError("stored lineup token does not match its input lineage")
        issues = row["expected_issue_codes"]
        return LineupSnapshotAttempt(
            game=game,
            snapshot_id=_uuid(row["snapshot_id"]),
            generation=row["generation"],
            lineup_revision_id=row["lineup_revision_id"],
            input_token=token,
            state=LineupAttemptState(row["state"]),
            expected_row_count=row["expected_row_count"],
            expected_output_sha256=row["expected_output_sha256"],
            expected_issue_codes=None
            if issues is None
            else tuple(LineupIssue(**issue) for issue in issues),
            result_status=None
            if row["result_status"] is None
            else LineupStatus(row["result_status"]),
        )


__all__ = ["PostgresLineupWorkStore"]
