"""Fenced PostgreSQL selection and claim allocation for possession builds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
)
from nba_live_totals.domain import (
    Game,
    GameContext,
    GameId,
    GameStatus,
    JsonObject,
    PlayByPlayEvent,
    PossessionIssue,
    PossessionVersions,
    SeasonId,
    SeasonType,
    SourceDocument,
    canonical_event_checksum,
    game_context_checksum,
    parse_revision_id,
)
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    play_by_play_events,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
    source_documents,
)

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object) -> UUID:
    """Normalize asyncpg's UUID-compatible values for exact domain contracts."""
    return value if type(value) is UUID else UUID(str(value))


class PostgresPossessionWorkStore:
    """Own the Phase 3 input token, lease and immutable attempt allocation."""

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

    async def start_run(self, request: BuildPossessionsRequest) -> UUID:
        """Persist one running Phase 3 run and return its UUID correlation ID."""
        run_id = uuid4()
        await self._insert_run(run_id, request)
        return run_id

    async def select_games(self, request: BuildPossessionsRequest) -> tuple[GameId, ...]:
        """Select only final regular-season/playoff games with applied successful PBP."""
        resource_key = "play_by_play:" + games.c.game_id
        statement = (
            select(games.c.game_id)
            .select_from(
                games.join(
                    ingestion_resources,
                    and_(
                        ingestion_resources.c.source == self._source,
                        ingestion_resources.c.resource_type == "play_by_play",
                        ingestion_resources.c.resource_key == resource_key,
                        ingestion_resources.c.status == "succeeded",
                        ingestion_resources.c.applied_document_id.is_not(None),
                    ),
                )
            )
            .where(
                games.c.season.in_([season.value for season in request.seasons]),
                games.c.season_type.in_(
                    [SeasonType.REGULAR_SEASON.value, SeasonType.PLAYOFFS.value]
                ),
                games.c.status == GameStatus.FINAL.value,
            )
            .order_by(games.c.game_date, games.c.game_id)
        )
        if request.game_id is not None:
            statement = statement.where(games.c.game_id == request.game_id.value)
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return tuple(GameId(cast(str, game_id)) for game_id in result.scalars())

    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: PossessionVersions,
        *,
        rebuild: bool,
    ) -> ClaimDecision:
        """Atomically load an immutable input and make its fenced claim decision."""
        if type(run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        if not isinstance(game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if not isinstance(versions, PossessionVersions):
            raise ValueError("versions must be PossessionVersions")
        if type(rebuild) is not bool:
            raise ValueError("rebuild must be a boolean")

        async with self._engine.begin() as connection:
            # The order below is a durable cross-plane deadlock contract. Do not
            # move a later lock ahead of the Phase 2 PBP resource lock.
            phase_two = await self._lock_phase_two_pbp(connection, game_id)
            game = await self._lock_game(connection, game_id)
            snapshot = await self._load_snapshot(connection, game, phase_two)
            input_token = PossessionInputToken.from_snapshot(snapshot, versions)
            revision = parse_revision_id(
                game_id=game_id,
                pbp_document_sha256=input_token.applied_pbp_sha256,
                canonical_event_sha256=input_token.canonical_event_sha256,
                game_context_sha256=input_token.game_context_sha256,
                versions=versions,
            )

            await connection.execute(
                insert(possession_rebuild_resources)
                .values(game_id=game_id.value)
                .on_conflict_do_nothing(index_elements=[possession_rebuild_resources.c.game_id])
            )
            resource = await self._lock_possession_resource(connection, game_id)
            now = self._now()

            active_run_id = (
                None if resource["active_run_id"] is None else _uuid(resource["active_run_id"])
            )
            active_snapshot_id = (
                None
                if resource["active_snapshot_id"] is None
                else _uuid(resource["active_snapshot_id"])
            )
            claimed_until = cast(datetime | None, resource["claimed_until"])
            if active_run_id is not None:
                if active_snapshot_id is None or claimed_until is None:
                    raise RuntimeError("active possession claim tuple is incomplete")
                active_attempt = await self._lock_attempt(connection, active_snapshot_id)
                if claimed_until > now:
                    if active_run_id == run_id:
                        return ClaimDecision(
                            ClaimAction.RESUME,
                            claim=self._claim_from_rows(
                                run_id=run_id,
                                claim_version=cast(int, resource["active_claim_version"]),
                                game=game,
                                snapshot=snapshot,
                                attempt=active_attempt,
                            ),
                        )
                    return ClaimDecision(ClaimAction.BUSY)
                await connection.execute(
                    update(possession_snapshot_attempts)
                    .where(
                        possession_snapshot_attempts.c.snapshot_id == active_snapshot_id,
                        possession_snapshot_attempts.c.state.in_(
                            ("allocated", "writing", "verified")
                        ),
                    )
                    .values(state=PossessionAttemptState.SUPERSEDED.value)
                )

            if not rebuild and resource["published_snapshot_id"] is not None:
                published = await self._lock_attempt(
                    connection, _uuid(resource["published_snapshot_id"])
                )
                if (
                    resource["published_input_token_sha256"] == input_token.sha256
                    and published["state"] == PossessionAttemptState.PUBLISHED.value
                ):
                    return ClaimDecision(
                        ClaimAction.SKIP_CURRENT,
                        current=self._current_from_row(game, published),
                    )

            claim_version = cast(int, resource["claim_version"])
            generation = cast(int, resource["next_generation"])
            attempt_count = cast(int, resource["attempt_count"])
            if (
                claim_version >= _MAX_SIGNED_BIGINT
                or generation >= _MAX_SIGNED_BIGINT
                or attempt_count >= _MAX_SIGNED_BIGINT
            ):
                raise RuntimeError("possession claim counter exhausted")
            next_claim_version = claim_version + 1
            snapshot_id = uuid4()

            # Run lock is last and only required for allocation/counter ownership.
            await self._lock_run(connection, run_id)
            await connection.execute(
                possession_snapshot_attempts.insert().values(
                    snapshot_id=snapshot_id,
                    game_id=game_id.value,
                    generation=generation,
                    parse_revision_id=revision,
                    input_token_sha256=input_token.sha256,
                    input_pbp_document_id=snapshot.applied_pbp_document_id,
                    input_pbp_sha256=input_token.applied_pbp_sha256,
                    input_event_sha256=input_token.canonical_event_sha256,
                    game_context_sha256=input_token.game_context_sha256,
                    normalizer_version=versions.normalizer_version,
                    parser_version=versions.parser_version,
                    rules_version=versions.rules_version,
                    output_schema_version=versions.output_schema_version,
                    state=PossessionAttemptState.ALLOCATED.value,
                    created_by_run_id=run_id,
                    created_by_claim_version=next_claim_version,
                )
            )
            await connection.execute(
                update(possession_rebuild_resources)
                .where(possession_rebuild_resources.c.game_id == game_id.value)
                .values(
                    attempt_count=attempt_count + 1,
                    claim_version=next_claim_version,
                    next_generation=generation + 1,
                    last_run_id=run_id,
                    active_run_id=run_id,
                    active_claim_version=next_claim_version,
                    active_snapshot_id=snapshot_id,
                    active_parse_revision_id=revision,
                    active_input_token_sha256=input_token.sha256,
                    claimed_until=now + self._lease,
                    updated_at=now,
                )
            )
            attempt = PossessionSnapshotAttempt(
                game=game,
                snapshot_id=snapshot_id,
                generation=generation,
                parse_revision_id=revision,
                input_token=input_token,
                state=PossessionAttemptState.ALLOCATED,
            )
            return ClaimDecision(
                ClaimAction.BUILD,
                claim=PossessionBuildClaim(
                    run_id=run_id,
                    claim_version=next_claim_version,
                    attempt=attempt,
                    snapshot=snapshot,
                ),
            )

    async def mark_writing(
        self,
        claim: PossessionBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
    ) -> PossessionSnapshotAttempt:
        """Fence and record the expected analytical output before external I/O."""
        async with self._engine.begin() as connection:
            resource = await self._lock_possession_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={PossessionAttemptState.ALLOCATED},
            )
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state=PossessionAttemptState.WRITING.value,
                    expected_row_count=expected_row_count,
                    expected_output_sha256=expected_output_sha256,
                    insert_started_at=now,
                )
            )
            updated = dict(attempt) | {
                "state": PossessionAttemptState.WRITING.value,
                "expected_row_count": expected_row_count,
                "expected_output_sha256": expected_output_sha256,
            }
            return self._attempt_from_row(claim.snapshot.game, updated)

    async def mark_verified(
        self, claim: PossessionBuildClaim, verification: SnapshotVerification
    ) -> PossessionSnapshotAttempt:
        """Advance only an exact expected-output readback to verified."""
        async with self._engine.begin() as connection:
            resource = await self._lock_possession_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={PossessionAttemptState.WRITING},
            )
            if (
                verification.snapshot_id != claim.attempt.snapshot_id
                or not verification.is_exact
                or verification.row_count != attempt["expected_row_count"]
                or verification.output_sha256 != attempt["expected_output_sha256"]
            ):
                raise RuntimeError("verification does not exactly match writing attempt")
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(state=PossessionAttemptState.VERIFIED.value, verified_at=now)
            )
            updated = dict(attempt) | {"state": PossessionAttemptState.VERIFIED.value}
            return self._attempt_from_row(claim.snapshot.game, updated)

    async def poison_attempt(
        self, claim: PossessionBuildClaim, *, error_code: str, error_type: str
    ) -> None:
        """Terminally poison a fenced unpublishable candidate and release its lease."""
        async with self._engine.begin() as connection:
            resource = await self._lock_possession_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={
                    PossessionAttemptState.ALLOCATED,
                    PossessionAttemptState.WRITING,
                    PossessionAttemptState.VERIFIED,
                },
            )
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state=PossessionAttemptState.POISONED.value,
                    safe_error_code=error_code,
                    safe_error_type=error_type,
                )
            )
            await self._release_failed_claim(
                connection,
                claim,
                publication_state="invalid",
                error_code=error_code,
                error_type=error_type,
            )

    async def publish_snapshot(self, claim: PossessionBuildClaim) -> CurrentSnapshotReference:
        """Publish one verified attempt only if its full immutable input is still current."""
        stale = False
        reference: CurrentSnapshotReference | None = None
        async with self._engine.begin() as connection:
            phase_two = await self._lock_phase_two_pbp(connection, claim.attempt.game_id)
            game = await self._lock_game(connection, claim.attempt.game_id)
            snapshot = await self._load_snapshot(connection, game, phase_two)
            current_token = PossessionInputToken.from_snapshot(
                snapshot, claim.attempt.input_token.versions
            )
            resource = await self._lock_possession_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={PossessionAttemptState.VERIFIED},
            )
            if (
                current_token != claim.attempt.input_token
                or attempt["input_token_sha256"] != current_token.sha256
                or attempt["parse_revision_id"] != claim.attempt.parse_revision_id
            ):
                await connection.execute(
                    update(possession_snapshot_attempts)
                    .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(state=PossessionAttemptState.SUPERSEDED.value)
                )
                await self._release_failed_claim(
                    connection,
                    claim,
                    publication_state="invalid",
                    error_code="stale_input",
                    error_type="fence",
                )
                stale = True
            else:
                await connection.execute(
                    update(possession_snapshot_attempts)
                    .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                    .values(state=PossessionAttemptState.PUBLISHED.value, published_at=now)
                )
                await connection.execute(
                    update(possession_rebuild_resources)
                    .where(possession_rebuild_resources.c.game_id == claim.attempt.game_id.value)
                    .values(
                        publication_state="current",
                        published_snapshot_id=claim.attempt.snapshot_id,
                        published_parse_revision_id=claim.attempt.parse_revision_id,
                        published_input_token_sha256=current_token.sha256,
                        published_at=now,
                        active_run_id=None,
                        active_claim_version=None,
                        active_snapshot_id=None,
                        active_parse_revision_id=None,
                        active_input_token_sha256=None,
                        claimed_until=None,
                        last_error_code=None,
                        last_error_type=None,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                published = dict(attempt) | {"state": PossessionAttemptState.PUBLISHED.value}
                reference = self._current_from_row(game, published)
        if stale:
            raise RuntimeError("candidate input token is stale")
        if reference is None:
            raise RuntimeError("publication did not produce a current snapshot reference")
        return reference

    async def fail_game(
        self,
        claim: PossessionBuildClaim,
        *,
        issues: tuple[PossessionIssue, ...],
        error_code: str,
        error_type: str,
    ) -> None:
        """Poison a pre-write parser failure without replacing a fresh publication."""
        async with self._engine.begin() as connection:
            phase_two = await self._lock_phase_two_pbp(connection, claim.attempt.game_id)
            game = await self._lock_game(connection, claim.attempt.game_id)
            snapshot = await self._load_snapshot(connection, game, phase_two)
            current_token = PossessionInputToken.from_snapshot(
                snapshot, claim.attempt.input_token.versions
            )
            resource = await self._lock_possession_resource(connection, claim.attempt.game_id)
            attempt = await self._lock_attempt(connection, claim.attempt.snapshot_id)
            now = self._now()
            self._require_active_claim(
                resource,
                attempt,
                claim,
                now=now,
                allowed_states={PossessionAttemptState.ALLOCATED},
            )
            publication_state = "invalid"
            published_id = resource["published_snapshot_id"]
            if published_id is not None:
                published = await self._lock_attempt(connection, _uuid(published_id))
                if (
                    published["state"] == PossessionAttemptState.PUBLISHED.value
                    and published["input_token_sha256"] == current_token.sha256
                    and resource["published_input_token_sha256"] == current_token.sha256
                ):
                    publication_state = "current"
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(possession_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state=PossessionAttemptState.POISONED.value,
                    safe_error_code=error_code,
                    safe_error_type=error_type,
                )
            )
            await self._release_failed_claim(
                connection,
                claim,
                publication_state=publication_state,
                issues=issues,
                error_code=error_code,
                error_type=error_type,
            )

    async def resolve_current(
        self, game_id: GameId, versions: PossessionVersions
    ) -> CurrentSnapshotReference | None:
        """Resolve a current pointer under the complete cross-plane lock order."""
        async with self._engine.begin() as connection:
            phase_two = await self._lock_phase_two_pbp(connection, game_id)
            game = await self._lock_game(connection, game_id)
            snapshot = await self._load_snapshot(connection, game, phase_two)
            current_token = PossessionInputToken.from_snapshot(snapshot, versions)
            resource_exists = await connection.scalar(
                select(possession_rebuild_resources.c.game_id).where(
                    possession_rebuild_resources.c.game_id == game_id.value
                )
            )
            if resource_exists is None:
                return None
            resource = await self._lock_possession_resource(connection, game_id)
            published_id = resource["published_snapshot_id"]
            if published_id is None:
                return None
            attempt = await self._lock_attempt(connection, _uuid(published_id))
            if (
                resource["publication_state"] != "current"
                or resource["published_parse_revision_id"] != attempt["parse_revision_id"]
                or resource["published_input_token_sha256"] != current_token.sha256
                or attempt["state"] != PossessionAttemptState.PUBLISHED.value
                or attempt["input_token_sha256"] != current_token.sha256
                or attempt["normalizer_version"] != versions.normalizer_version
                or attempt["parser_version"] != versions.parser_version
                or attempt["rules_version"] != versions.rules_version
                or attempt["output_schema_version"] != versions.output_schema_version
            ):
                return None
            return self._current_from_row(game, attempt)

    async def finish_run(self, run_id: UUID, result: BuildPossessionsResult) -> None:
        """Persist one exact terminal result under its run-row lock."""
        if type(run_id) is not UUID:
            raise ValueError("run ID must be a UUID")
        if result.run_id != run_id:
            raise ValueError("result run ID does not match run ID")
        async with self._engine.begin() as connection:
            await self._lock_run(connection, run_id)
            updated = await connection.execute(
                update(possession_rebuild_runs)
                .where(
                    possession_rebuild_runs.c.run_id == run_id,
                    possession_rebuild_runs.c.status == "running",
                )
                .values(
                    status=result.status.value,
                    discovered=result.discovered,
                    materialized=result.materialized,
                    skipped_current=result.skipped_current,
                    verified=result.verified,
                    failed=result.failed,
                    busy=result.busy,
                    finished_at=self._now(),
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("possession rebuild run is already terminal")

    async def _insert_run(self, run_id: UUID, request: BuildPossessionsRequest) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                possession_rebuild_runs.insert().values(
                    run_id=run_id,
                    selectors={
                        "seasons": [season.value for season in request.seasons],
                        "game_id": request.game_id.value if request.game_id is not None else None,
                    },
                    mode=request.mode.value,
                    forced_rebuild=request.rebuild,
                    status="running",
                    started_at=self._now(),
                )
            )

    async def _lock_phase_two_pbp(self, connection: AsyncConnection, game_id: GameId) -> RowMapping:
        row = (
            (
                await connection.execute(
                    select(ingestion_resources)
                    .where(
                        ingestion_resources.c.source == self._source,
                        ingestion_resources.c.resource_type == "play_by_play",
                        ingestion_resources.c.resource_key == f"play_by_play:{game_id.value}",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] != "succeeded" or row["applied_document_id"] is None:
            raise RuntimeError("game does not have successful applied play-by-play")
        return row

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
        if row is None:
            raise RuntimeError("game not found")
        if row["status"] != GameStatus.FINAL.value or row["season_type"] not in {
            SeasonType.REGULAR_SEASON.value,
            SeasonType.PLAYOFFS.value,
        }:
            raise RuntimeError("game is not an eligible completed competition")
        return GameContext.from_game(
            Game(
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
        )

    async def _load_snapshot(
        self, connection: AsyncConnection, game: GameContext, phase_two: RowMapping
    ) -> GamePbpSnapshot:
        document_id = _uuid(phase_two["applied_document_id"])
        document_row = (
            (
                await connection.execute(
                    select(source_documents).where(source_documents.c.document_id == document_id)
                )
            )
            .mappings()
            .one()
        )
        if document_row["resource_key"] != f"play_by_play:{game.game_id.value}":
            raise RuntimeError("applied play-by-play document does not match game")
        events = tuple(
            self._event_from_row(row)
            for row in (
                (
                    await connection.execute(
                        select(play_by_play_events)
                        .where(play_by_play_events.c.game_id == game.game_id.value)
                        .order_by(play_by_play_events.c.sequence_number)
                    )
                )
                .mappings()
                .all()
            )
        )
        source_document = SourceDocument(
            source=cast(str, document_row["source"]),
            endpoint=cast(str, document_row["endpoint"]),
            resource_key=cast(str, document_row["resource_key"]),
            requested_at=cast(datetime, document_row["requested_at"]),
            request_parameters=cast(JsonObject, document_row["request_parameters"]),
            payload=cast(JsonObject, document_row["payload"]),
            checksum_sha256=cast(str, document_row["checksum_sha256"]),
        )
        return GamePbpSnapshot(
            game=game,
            applied_pbp_document_id=document_id,
            applied_pbp_sha256=source_document.checksum_sha256,
            canonical_events=events,
            canonical_event_sha256=canonical_event_checksum(events),
            game_context_sha256=game_context_checksum(game),
            source_document=source_document,
        )

    async def _lock_possession_resource(
        self, connection: AsyncConnection, game_id: GameId
    ) -> RowMapping:
        return (
            (
                await connection.execute(
                    select(possession_rebuild_resources)
                    .where(possession_rebuild_resources.c.game_id == game_id.value)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    async def _lock_attempt(self, connection: AsyncConnection, snapshot_id: UUID) -> RowMapping:
        return (
            (
                await connection.execute(
                    select(possession_snapshot_attempts)
                    .where(possession_snapshot_attempts.c.snapshot_id == snapshot_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )

    async def _lock_run(self, connection: AsyncConnection, run_id: UUID) -> None:
        found = await connection.scalar(
            select(possession_rebuild_runs.c.run_id)
            .where(possession_rebuild_runs.c.run_id == run_id)
            .with_for_update()
        )
        if found is None:
            raise RuntimeError("possession rebuild run not found")

    @staticmethod
    def _require_active_claim(
        resource: RowMapping,
        attempt: RowMapping,
        claim: PossessionBuildClaim,
        *,
        now: datetime,
        allowed_states: set[PossessionAttemptState],
    ) -> None:
        """Require the complete active lease, creator fields and attempt state."""
        if (
            resource["active_run_id"] is None
            or _uuid(resource["active_run_id"]) != claim.run_id
            or resource["active_claim_version"] != claim.claim_version
            or resource["active_snapshot_id"] is None
            or _uuid(resource["active_snapshot_id"]) != claim.attempt.snapshot_id
            or resource["active_parse_revision_id"] != claim.attempt.parse_revision_id
            or resource["active_input_token_sha256"] != claim.attempt.input_token.sha256
            or resource["claimed_until"] is None
            or cast(datetime, resource["claimed_until"]) <= now
            or attempt["created_by_run_id"] is None
            or _uuid(attempt["created_by_run_id"]) != claim.run_id
            or attempt["created_by_claim_version"] != claim.claim_version
            or attempt["snapshot_id"] is None
            or _uuid(attempt["snapshot_id"]) != claim.attempt.snapshot_id
            or attempt["parse_revision_id"] != claim.attempt.parse_revision_id
            or attempt["input_token_sha256"] != claim.attempt.input_token.sha256
        ):
            raise RuntimeError("possession claim fence is stale")
        if PossessionAttemptState(cast(str, attempt["state"])) not in allowed_states:
            raise RuntimeError("possession attempt state is incompatible with mutation")

    async def _release_failed_claim(
        self,
        connection: AsyncConnection,
        claim: PossessionBuildClaim,
        *,
        publication_state: str,
        issues: tuple[PossessionIssue, ...] = (),
        error_code: str,
        error_type: str,
    ) -> None:
        """Clear exactly one fenced lease while retaining any audit publication pointer."""
        await connection.execute(
            update(possession_rebuild_resources)
            .where(
                possession_rebuild_resources.c.game_id == claim.attempt.game_id.value,
                possession_rebuild_resources.c.active_run_id == claim.run_id,
                possession_rebuild_resources.c.active_claim_version == claim.claim_version,
                possession_rebuild_resources.c.active_snapshot_id == claim.attempt.snapshot_id,
            )
            .values(
                publication_state=publication_state,
                active_run_id=None,
                active_claim_version=None,
                active_snapshot_id=None,
                active_parse_revision_id=None,
                active_input_token_sha256=None,
                claimed_until=None,
                last_issue_codes=[
                    {
                        "code": issue.code,
                        **(
                            {"sequence_number": issue.sequence_number}
                            if issue.sequence_number is not None
                            else {}
                        ),
                        **(
                            {"related_sequence_number": issue.related_sequence_number}
                            if issue.related_sequence_number is not None
                            else {}
                        ),
                    }
                    for issue in issues
                ],
                last_error_code=error_code,
                last_error_type=error_type,
                updated_at=self._now(),
                completed_at=self._now(),
            )
        )

    def _claim_from_rows(
        self,
        *,
        run_id: UUID,
        claim_version: int,
        game: GameContext,
        snapshot: GamePbpSnapshot,
        attempt: RowMapping,
    ) -> PossessionBuildClaim:
        return PossessionBuildClaim(
            run_id=run_id,
            claim_version=claim_version,
            attempt=self._attempt_from_row(game, attempt),
            snapshot=snapshot,
        )

    def _current_from_row(
        self, game: GameContext, row: RowMapping | dict[str, Any]
    ) -> CurrentSnapshotReference:
        attempt = self._attempt_from_row(game, row)
        return CurrentSnapshotReference(
            attempt=attempt,
            row_count=cast(int, row["expected_row_count"]),
            output_sha256=cast(str, row["expected_output_sha256"]),
        )

    def _attempt_from_row(
        self, game: GameContext, row: RowMapping | dict[str, Any]
    ) -> PossessionSnapshotAttempt:
        versions = PossessionVersions(
            cast(str, row["normalizer_version"]),
            cast(str, row["parser_version"]),
            cast(str, row["rules_version"]),
            cast(str, row["output_schema_version"]),
        )
        token = PossessionInputToken(
            game_id=game.game_id,
            applied_pbp_document_id=_uuid(row["input_pbp_document_id"]),
            applied_pbp_sha256=cast(str, row["input_pbp_sha256"]),
            canonical_event_sha256=cast(str, row["input_event_sha256"]),
            game_context_sha256=cast(str, row["game_context_sha256"]),
            versions=versions,
        )
        return PossessionSnapshotAttempt(
            game=game,
            snapshot_id=_uuid(row["snapshot_id"]),
            generation=cast(int, row["generation"]),
            parse_revision_id=cast(str, row["parse_revision_id"]),
            input_token=token,
            state=PossessionAttemptState(cast(str, row["state"])),
            expected_row_count=cast(int | None, row["expected_row_count"]),
            expected_output_sha256=cast(str | None, row["expected_output_sha256"]),
        )

    @staticmethod
    def _event_from_row(row: RowMapping) -> PlayByPlayEvent:
        return PlayByPlayEvent(
            game_id=GameId(cast(str, row["game_id"])),
            sequence_number=cast(int, row["sequence_number"]),
            action_number=cast(int, row["action_number"]),
            action_id=cast(int | None, row["action_id"]),
            period=cast(int, row["period"]),
            clock=cast(str, row["clock"]),
            seconds_remaining_period_ms=cast(int, row["seconds_remaining_period_ms"]),
            team_id=cast(int | None, row["team_id"]),
            person_id=cast(int | None, row["person_id"]),
            action_type=cast(str, row["action_type"]),
            sub_type=cast(str, row["sub_type"]),
            description=cast(str, row["description"]),
            home_score=cast(int | None, row["home_score"]),
            away_score=cast(int | None, row["away_score"]),
            shot_result=cast(str | None, row["shot_result"]),
            shot_distance=cast(int | None, row["shot_distance"]),
            legacy_x=cast(int | None, row["legacy_x"]),
            legacy_y=cast(int | None, row["legacy_y"]),
            location=cast(str | None, row["location"]),
            is_field_goal=cast(bool, row["is_field_goal"]),
            points_total=cast(int, row["points_total"]),
            video_available=cast(bool, row["video_available"]),
        )

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            raise ValueError("utc_now must return a timezone-aware datetime")
        return value.astimezone(UTC)


__all__ = ["PostgresPossessionWorkStore"]
