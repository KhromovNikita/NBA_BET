"""PostgreSQL persistence for resumable historical-ingestion state."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
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
from nba_live_totals.domain import JsonObject, SourceDocument
from nba_live_totals.infrastructure.postgres.schema import (
    ingestion_resources,
    ingestion_runs,
    source_documents,
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
