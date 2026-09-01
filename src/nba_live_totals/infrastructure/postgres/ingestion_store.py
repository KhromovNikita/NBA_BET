"""PostgreSQL persistence for resumable historical-ingestion state."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from nba_live_totals.application.ingestion import (
    BackfillRequest,
    BackfillResult,
    ClaimAction,
    ClaimDecision,
    ClaimLostError,
    ResourceClaim,
    ResourceKey,
    StoredSourceDocument,
)
from nba_live_totals.domain import (
    BoxScore,
    JsonObject,
    PlayByPlayEvent,
    SeasonSchedule,
    SourceDocument,
    TraditionalStats,
    validate_play_by_play,
)
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)

_SAFE_ERROR_CODES = frozenset(
    {
        "network_timeout",
        "network_connection",
        "rate_limited",
        "upstream_server",
        "empty_response",
        "invalid_json",
        "schema_mismatch",
        "invalid_game",
        "invalid_box_score",
        "invalid_play_by_play",
        "unknown_game_type",
        "resource_busy",
        "claim_lost",
        "persistence_error",
    }
)
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")

RowValues = dict[str, object]


def _season_rows(schedule: SeasonSchedule) -> tuple[RowValues, ...]:
    """Convert the schedule's represented season types to typed database rows."""
    season_types = dict.fromkeys(game.season_type for game in schedule.games)
    return tuple(
        {
            "season": schedule.season.value,
            "season_type": season_type.value,
            "season_start_year": schedule.season.start_year,
        }
        for season_type in season_types
    )


def _team_rows(schedule_or_box_score: SeasonSchedule | BoxScore) -> tuple[RowValues, ...]:
    """Convert stable team identities without changing their typed values."""
    domain_teams = (
        schedule_or_box_score.teams
        if isinstance(schedule_or_box_score, SeasonSchedule)
        else tuple(row.team for row in schedule_or_box_score.team_rows)
    )
    return tuple(
        {
            "team_id": team.team_id,
            "city": team.city,
            "name": team.name,
            "tricode": team.tricode,
            "slug": team.slug,
        }
        for team in domain_teams
    )


def _game_rows(schedule: SeasonSchedule) -> tuple[RowValues, ...]:
    """Convert schedule games while explicitly unwrapping identifier values."""
    return tuple(
        {
            "game_id": game.game_id.value,
            "season": game.season.value,
            "season_type": game.season_type.value,
            "game_date": game.game_date,
            "started_at": game.started_at,
            "home_team_id": game.home_team_id,
            "away_team_id": game.away_team_id,
            "home_score": game.home_score,
            "away_score": game.away_score,
            "status": game.status.value,
            "arena_name": game.arena_name,
            "arena_city": game.arena_city,
            "arena_state": game.arena_state,
        }
        for game in schedule.games
    )


def _traditional_stat_values(statistics: TraditionalStats) -> RowValues:
    return {
        "seconds_played": statistics.seconds_played,
        "field_goals_made": statistics.field_goals_made,
        "field_goals_attempted": statistics.field_goals_attempted,
        "field_goals_percentage": statistics.field_goals_percentage,
        "three_pointers_made": statistics.three_pointers_made,
        "three_pointers_attempted": statistics.three_pointers_attempted,
        "three_pointers_percentage": statistics.three_pointers_percentage,
        "free_throws_made": statistics.free_throws_made,
        "free_throws_attempted": statistics.free_throws_attempted,
        "free_throws_percentage": statistics.free_throws_percentage,
        "rebounds_offensive": statistics.rebounds_offensive,
        "rebounds_defensive": statistics.rebounds_defensive,
        "rebounds_total": statistics.rebounds_total,
        "assists": statistics.assists,
        "steals": statistics.steals,
        "blocks": statistics.blocks,
        "turnovers": statistics.turnovers,
        "fouls_personal": statistics.fouls_personal,
        "points": statistics.points,
        "plus_minus": statistics.plus_minus,
    }


def _team_box_rows(box_score: BoxScore) -> tuple[RowValues, ...]:
    """Convert the complete two-team traditional-stat snapshot."""
    return tuple(
        {
            "game_id": row.game_id.value,
            "team_id": row.team.team_id,
            **_traditional_stat_values(row.statistics),
        }
        for row in box_score.team_rows
    )


def _player_rows(box_score: BoxScore) -> tuple[RowValues, ...]:
    """Convert stable player identities and provider-updatable display names."""
    return tuple(
        {
            "player_id": row.player.player_id,
            "first_name": row.player.first_name,
            "family_name": row.player.family_name,
            "full_name": row.player.full_name,
        }
        for row in box_score.player_rows
    )


def _player_box_rows(box_score: BoxScore) -> tuple[RowValues, ...]:
    """Convert all participation and nullable DNP statistics without coercion."""
    return tuple(
        {
            "game_id": row.game_id.value,
            "player_id": row.player.player_id,
            "team_id": row.team_id,
            "position": row.position,
            "jersey_number": row.jersey_number,
            "starter": row.starter,
            "played": row.played,
            "comment": row.comment,
            **_traditional_stat_values(row.statistics),
        }
        for row in box_score.player_rows
    )


def _event_rows(events: tuple[PlayByPlayEvent, ...]) -> tuple[RowValues, ...]:
    """Convert ordered play-by-play events while preserving nullable provider values."""
    return tuple(
        {
            "game_id": event.game_id.value,
            "action_number": event.action_number,
            "action_id": event.action_id,
            "period": event.period,
            "clock": event.clock,
            "seconds_remaining_period_ms": event.seconds_remaining_period_ms,
            "team_id": event.team_id,
            "person_id": event.person_id,
            "action_type": event.action_type,
            "sub_type": event.sub_type,
            "description": event.description,
            "home_score": event.home_score,
            "away_score": event.away_score,
            "shot_result": event.shot_result,
            "shot_distance": event.shot_distance,
            "legacy_x": event.legacy_x,
            "legacy_y": event.legacy_y,
            "location": event.location,
            "is_field_goal": event.is_field_goal,
            "points_total": event.points_total,
            "video_available": event.video_available,
        }
        for event in events
    )


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


class PostgresHistoricalIngestionStore:
    """Persist run, claim, and immutable raw-document state with fencing."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        source: str = "nba.com",
        lease_seconds: int = 600,
        utc_now: Callable[[], datetime] = _system_utc_now,
    ) -> None:
        if not source:
            msg = "source must not be empty"
            raise ValueError(msg)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            msg = "lease seconds must be a positive integer"
            raise ValueError(msg)
        self._engine = engine
        self._source = source
        self._lease = timedelta(seconds=lease_seconds)
        self._utc_now = utc_now
        self._successful_document_ids: dict[ResourceClaim, UUID] = {}

    async def start_run(self, request: BackfillRequest) -> UUID:
        """Create one running ingestion attempt and return its correlation ID."""
        run_id = uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                ingestion_runs.insert().values(
                    run_id=run_id,
                    request_parameters={
                        "seasons": [season.value for season in request.seasons],
                        "game_id": request.game_id.value if request.game_id is not None else None,
                        "refresh": request.refresh,
                    },
                    started_at=self._now(),
                    status="running",
                )
            )
        return run_id

    async def finish_run(self, run_id: UUID, result: BackfillResult) -> None:
        """Persist a terminal run status and its exact summary counters."""
        if result.run_id != run_id:
            msg = "result run ID must match run ID"
            raise ValueError(msg)
        async with self._engine.begin() as connection:
            updated = await connection.execute(
                update(ingestion_runs)
                .where(ingestion_runs.c.run_id == run_id)
                .values(
                    finished_at=self._now(),
                    status=result.status.value,
                    discovered=result.discovered,
                    succeeded=result.succeeded,
                    skipped=result.skipped,
                    failed=result.failed,
                )
            )
            if updated.rowcount != 1:
                msg = "ingestion run not found"
                raise RuntimeError(msg)

    async def claim_resource(
        self,
        run_id: UUID,
        key: ResourceKey,
        *,
        refresh: bool,
    ) -> ClaimDecision:
        """Atomically decide and, when allowed, acquire one resource lease."""
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(ingestion_resources)
                .values(
                    source=self._source,
                    resource_type=key.resource_type.value,
                    resource_key=str(key),
                    status="pending",
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ingestion_resources.c.source,
                        ingestion_resources.c.resource_type,
                        ingestion_resources.c.resource_key,
                    ]
                )
            )
            row = (
                (
                    await connection.execute(
                        select(ingestion_resources)
                        .where(self._resource_identity(key))
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )

            # Keep one grant lock order: resource row, then referenced run row. The
            # later FK update needs KEY SHARE, so acquiring it before reading the
            # clock ensures no grant-related lock wait can consume the lease.
            (
                await connection.execute(
                    select(ingestion_runs.c.run_id)
                    .where(ingestion_runs.c.run_id == run_id)
                    .with_for_update(read=True, key_share=True)
                )
            ).scalar_one()
            now = self._now()
            status = cast(str, row["status"])
            claimed_until = cast(datetime | None, row["claimed_until"])
            if status == "running" and claimed_until is not None and claimed_until > now:
                return ClaimDecision(ClaimAction.BUSY)
            if status == "succeeded" and not refresh:
                return ClaimDecision(ClaimAction.SKIP)

            latest_document_id = cast(UUID | None, row["latest_document_id"])
            action = (
                ClaimAction.REPARSE
                if status == "failed" and latest_document_id is not None and not refresh
                else ClaimAction.FETCH
            )
            current_version = cast(int, row["claim_version"])
            next_version = current_version + 1
            updated = await connection.execute(
                update(ingestion_resources)
                .where(
                    self._resource_identity(key),
                    ingestion_resources.c.claim_version == current_version,
                )
                .values(
                    status="running",
                    attempt_count=ingestion_resources.c.attempt_count + 1,
                    last_run_id=run_id,
                    last_error_code=None,
                    last_error_type=None,
                    claim_version=next_version,
                    claimed_until=now + self._lease,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                raise ClaimLostError

            claim = ResourceClaim(key=key, run_id=run_id, version=next_version)
            if status == "succeeded" and latest_document_id is not None:
                # The schema deliberately has one raw-document link. Retain its
                # pre-refresh value for this process-local claim so apply_* can
                # complete the refreshed state without rewriting unchanged data.
                self._successful_document_ids[claim] = latest_document_id
            if action is ClaimAction.FETCH:
                return ClaimDecision(action=action, claim=claim)

            document_row = (
                (
                    await connection.execute(
                        select(source_documents).where(
                            source_documents.c.document_id == latest_document_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            return ClaimDecision(
                action=action,
                claim=claim,
                stored_document=self._stored_document(document_row),
            )

    async def latest_document(self, key: ResourceKey) -> StoredSourceDocument | None:
        """Return the raw document currently linked to a resource, if any."""
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(source_documents)
                        .select_from(
                            ingestion_resources.join(
                                source_documents,
                                ingestion_resources.c.latest_document_id
                                == source_documents.c.document_id,
                            )
                        )
                        .where(self._resource_identity(key))
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else self._stored_document(row)

    async def save_document(self, claim: ResourceClaim, document: SourceDocument) -> UUID:
        """Deduplicate one immutable raw document and link it under the claim fence."""
        if document.resource_key != str(claim.key):
            msg = "document resource key must match claim resource key"
            raise ValueError(msg)
        if document.source != self._source:
            msg = "document source must match store source"
            raise ValueError(msg)

        document_id = uuid4()
        now = self._now()
        async with self._engine.begin() as connection:
            inserted_id = cast(
                UUID | None,
                await connection.scalar(
                    insert(source_documents)
                    .values(
                        document_id=document_id,
                        source=document.source,
                        endpoint=document.endpoint,
                        resource_key=document.resource_key,
                        request_parameters=document.request_parameters,
                        payload=document.payload,
                        checksum_sha256=document.checksum_sha256,
                        requested_at=document.requested_at,
                        stored_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_source_documents_resource_checksum")
                    .returning(source_documents.c.document_id)
                ),
            )
            if inserted_id is None:
                stored_id = cast(
                    UUID | None,
                    await connection.scalar(
                        select(source_documents.c.document_id).where(
                            source_documents.c.source == document.source,
                            source_documents.c.endpoint == document.endpoint,
                            source_documents.c.resource_key == document.resource_key,
                            source_documents.c.checksum_sha256 == document.checksum_sha256,
                        )
                    ),
                )
                if stored_id is None:
                    msg = "source document deduplication failed"
                    raise RuntimeError(msg)
                document_id = stored_id
            else:
                document_id = inserted_id

            updated = await connection.execute(
                update(ingestion_resources)
                .where(self._claim_fence(claim), ingestion_resources.c.status == "running")
                .values(latest_document_id=document_id, updated_at=now)
            )
            if updated.rowcount != 1:
                raise ClaimLostError
        return document_id

    async def apply_schedule(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        schedule: SeasonSchedule,
    ) -> None:
        """Upsert one schedule and succeed its resource in one fenced transaction."""
        self._validate_schedule_identity(claim, document_id, schedule)
        season_values = _season_rows(schedule)
        team_values = _team_rows(schedule)
        game_values = _game_rows(schedule)

        async with self._engine.begin() as connection:
            await self._lock_claim(connection, claim, document_id)
            now = self._now()
            if not self._document_is_unchanged(claim, document_id):
                if season_values:
                    season_insert = insert(seasons).values(season_values)
                    await connection.execute(
                        season_insert.on_conflict_do_update(
                            index_elements=[seasons.c.season, seasons.c.season_type],
                            set_={
                                "season_start_year": season_insert.excluded.season_start_year,
                                "updated_at": now,
                            },
                        )
                    )
                await self._upsert_teams(connection, team_values, now)
                if game_values:
                    game_insert = insert(games).values(game_values)
                    await connection.execute(
                        game_insert.on_conflict_do_update(
                            index_elements=[games.c.game_id],
                            set_={
                                "season": game_insert.excluded.season,
                                "season_type": game_insert.excluded.season_type,
                                "game_date": game_insert.excluded.game_date,
                                "started_at": game_insert.excluded.started_at,
                                "home_team_id": game_insert.excluded.home_team_id,
                                "away_team_id": game_insert.excluded.away_team_id,
                                "home_score": game_insert.excluded.home_score,
                                "away_score": game_insert.excluded.away_score,
                                "status": game_insert.excluded.status,
                                "arena_name": game_insert.excluded.arena_name,
                                "arena_city": game_insert.excluded.arena_city,
                                "arena_state": game_insert.excluded.arena_state,
                            },
                        )
                    )
            await self._mark_succeeded(connection, claim, document_id, now)
        self._successful_document_ids.pop(claim, None)

    async def replace_box_score(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        box_score: BoxScore,
    ) -> None:
        """Atomically replace one complete game-scoped box-score snapshot."""
        self._validate_box_score_identity(claim, document_id, box_score)
        team_values = _team_rows(box_score)
        team_box_values = _team_box_rows(box_score)
        player_values = _player_rows(box_score)
        player_box_values = _player_box_rows(box_score)
        game_id = box_score.game_id.value

        async with self._engine.begin() as connection:
            await self._lock_claim(connection, claim, document_id)
            now = self._now()
            if not self._document_is_unchanged(claim, document_id):
                await self._upsert_teams(connection, team_values, now)
                game_updated = await connection.execute(
                    update(games)
                    .where(
                        games.c.game_id == game_id,
                        games.c.home_team_id == box_score.home_team_id,
                        games.c.away_team_id == box_score.away_team_id,
                    )
                    .values(
                        home_score=box_score.home_score,
                        away_score=box_score.away_score,
                        status="final",
                    )
                )
                if game_updated.rowcount != 1:
                    msg = "box score teams must match the stored game"
                    raise ValueError(msg)
                await self._upsert_players(connection, player_values, now)
                await connection.execute(
                    delete(player_box_scores).where(player_box_scores.c.game_id == game_id)
                )
                await connection.execute(
                    delete(team_box_scores).where(team_box_scores.c.game_id == game_id)
                )
                await connection.execute(team_box_scores.insert(), team_box_values)
                if player_box_values:
                    await connection.execute(player_box_scores.insert(), player_box_values)
            await self._mark_succeeded(connection, claim, document_id, now)
        self._successful_document_ids.pop(claim, None)

    async def replace_play_by_play(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        events: tuple[PlayByPlayEvent, ...],
    ) -> None:
        """Atomically replace one complete ordered play-by-play snapshot."""
        ordered_events = validate_play_by_play(events)
        self._validate_play_by_play_identity(claim, document_id, ordered_events)
        event_values = _event_rows(ordered_events)
        game_id = ordered_events[0].game_id.value

        async with self._engine.begin() as connection:
            await self._lock_claim(connection, claim, document_id)
            now = self._now()
            if not self._document_is_unchanged(claim, document_id):
                await connection.execute(
                    delete(play_by_play_events).where(play_by_play_events.c.game_id == game_id)
                )
                await connection.execute(play_by_play_events.insert(), event_values)
            await self._mark_succeeded(connection, claim, document_id, now)
        self._successful_document_ids.pop(claim, None)

    async def fail_resource(
        self,
        claim: ResourceClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None:
        """Record only stable safe failure fields under the claim fence."""
        if error_code not in _SAFE_ERROR_CODES:
            msg = "error code must be a stable ingestion error code"
            raise ValueError(msg)
        if _SAFE_ERROR_TYPE.fullmatch(error_type) is None:
            msg = "error type must be a stable exception class name"
            raise ValueError(msg)

        async with self._engine.begin() as connection:
            updated = await connection.execute(
                update(ingestion_resources)
                .where(self._claim_fence(claim), ingestion_resources.c.status == "running")
                .values(
                    status="failed",
                    claimed_until=None,
                    last_error_code=error_code,
                    last_error_type=error_type,
                    updated_at=self._now(),
                )
            )
            if updated.rowcount != 1:
                raise ClaimLostError
        self._successful_document_ids.pop(claim, None)

    async def _lock_claim(
        self,
        connection: AsyncConnection,
        claim: ResourceClaim,
        document_id: UUID,
    ) -> None:
        linked_claim = (
            await connection.execute(
                select(ingestion_resources.c.resource_key)
                .select_from(
                    ingestion_resources.join(
                        source_documents,
                        ingestion_resources.c.latest_document_id == source_documents.c.document_id,
                    )
                )
                .where(
                    self._claim_fence(claim),
                    ingestion_resources.c.status == "running",
                    ingestion_resources.c.latest_document_id == document_id,
                    source_documents.c.source == self._source,
                    source_documents.c.resource_key == str(claim.key),
                )
                .with_for_update(of=ingestion_resources)
            )
        ).one_or_none()
        if linked_claim is None:
            raise ClaimLostError

    async def _mark_succeeded(
        self,
        connection: AsyncConnection,
        claim: ResourceClaim,
        document_id: UUID,
        now: datetime,
    ) -> None:
        updated = await connection.execute(
            update(ingestion_resources)
            .where(
                self._claim_fence(claim),
                ingestion_resources.c.status == "running",
                ingestion_resources.c.latest_document_id == document_id,
            )
            .values(
                status="succeeded",
                claimed_until=None,
                last_error_code=None,
                last_error_type=None,
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            raise ClaimLostError

    @staticmethod
    async def _upsert_teams(
        connection: AsyncConnection,
        values: tuple[RowValues, ...],
        now: datetime,
    ) -> None:
        if not values:
            return
        team_insert = insert(teams).values(values)
        await connection.execute(
            team_insert.on_conflict_do_update(
                index_elements=[teams.c.team_id],
                set_={
                    "city": team_insert.excluded.city,
                    "name": team_insert.excluded.name,
                    "tricode": team_insert.excluded.tricode,
                    "slug": team_insert.excluded.slug,
                    "updated_at": now,
                },
            )
        )

    @staticmethod
    async def _upsert_players(
        connection: AsyncConnection,
        values: tuple[RowValues, ...],
        now: datetime,
    ) -> None:
        if not values:
            return
        player_insert = insert(players).values(values)
        await connection.execute(
            player_insert.on_conflict_do_update(
                index_elements=[players.c.player_id],
                set_={
                    "first_name": player_insert.excluded.first_name,
                    "family_name": player_insert.excluded.family_name,
                    "full_name": player_insert.excluded.full_name,
                    "updated_at": now,
                },
            )
        )

    def _document_is_unchanged(self, claim: ResourceClaim, document_id: UUID) -> bool:
        return self._successful_document_ids.get(claim) == document_id

    @staticmethod
    def _validate_document_id(document_id: UUID) -> None:
        if not isinstance(document_id, UUID):
            msg = "document ID must be a UUID"
            raise ValueError(msg)

    @classmethod
    def _validate_schedule_identity(
        cls,
        claim: ResourceClaim,
        document_id: UUID,
        schedule: SeasonSchedule,
    ) -> None:
        cls._validate_document_id(document_id)
        if claim.key != ResourceKey.schedule(schedule.season):
            msg = "claim resource must match schedule season"
            raise ValueError(msg)
        team_ids = {team.team_id for team in schedule.teams}
        for game in schedule.games:
            if game.season != schedule.season:
                msg = "schedule games must match schedule season"
                raise ValueError(msg)
            if game.home_team_id not in team_ids or game.away_team_id not in team_ids:
                msg = "schedule games must reference schedule teams"
                raise ValueError(msg)

    @classmethod
    def _validate_box_score_identity(
        cls,
        claim: ResourceClaim,
        document_id: UUID,
        box_score: BoxScore,
    ) -> None:
        cls._validate_document_id(document_id)
        if claim.key != ResourceKey.box_score(box_score.game_id):
            msg = "claim resource must match box-score game"
            raise ValueError(msg)
        # Re-run aggregate validation at the persistence boundary rather than
        # assuming every caller retained an untouched frozen instance.
        BoxScore(
            game_id=box_score.game_id,
            home_team_id=box_score.home_team_id,
            away_team_id=box_score.away_team_id,
            home_score=box_score.home_score,
            away_score=box_score.away_score,
            team_rows=box_score.team_rows,
            player_rows=box_score.player_rows,
        )

    @classmethod
    def _validate_play_by_play_identity(
        cls,
        claim: ResourceClaim,
        document_id: UUID,
        events: tuple[PlayByPlayEvent, ...],
    ) -> None:
        cls._validate_document_id(document_id)
        if claim.key != ResourceKey.play_by_play(events[0].game_id):
            msg = "claim resource must match play-by-play game"
            raise ValueError(msg)

    def _resource_identity(self, key: ResourceKey) -> ColumnElement[bool]:
        return and_(
            ingestion_resources.c.source == self._source,
            ingestion_resources.c.resource_type == key.resource_type.value,
            ingestion_resources.c.resource_key == str(key),
        )

    def _claim_fence(self, claim: ResourceClaim) -> ColumnElement[bool]:
        return and_(
            self._resource_identity(claim.key),
            ingestion_resources.c.last_run_id == claim.run_id,
            ingestion_resources.c.claim_version == claim.version,
        )

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            msg = "UTC clock must return a timezone-aware datetime"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @staticmethod
    def _stored_document(row: RowMapping) -> StoredSourceDocument:
        return StoredSourceDocument(
            document_id=cast(UUID, row["document_id"]),
            document=SourceDocument(
                source=cast(str, row["source"]),
                endpoint=cast(str, row["endpoint"]),
                resource_key=cast(str, row["resource_key"]),
                requested_at=cast(datetime, row["requested_at"]),
                request_parameters=cast(JsonObject, row["request_parameters"]),
                payload=cast(JsonObject, row["payload"]),
                checksum_sha256=cast(str, row["checksum_sha256"]),
            ),
        )


__all__ = ["PostgresHistoricalIngestionStore"]
