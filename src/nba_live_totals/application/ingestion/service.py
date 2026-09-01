"""Provider- and storage-neutral orchestration for historical NBA ingestion."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from time import monotonic
from uuid import UUID

from nba_live_totals.application.ingestion.models import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    IngestionResourceError,
    ResourceClaim,
    ResourceKey,
    ResourceType,
)
from nba_live_totals.application.ingestion.ports import (
    HistoricalDataSource,
    HistoricalIngestionStore,
)
from nba_live_totals.domain import Game, GameId, GameStatus, SeasonId, SourceDocument

logger = logging.getLogger(__name__)

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


class _ResourceOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    BUSY = "busy"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _ResourceResult[CanonicalT]:
    outcome: _ResourceOutcome
    canonical: CanonicalT | None = None


class HistoricalIngestionService:
    """Coordinate resumable schedule and game-resource ingestion."""

    def __init__(
        self,
        source: HistoricalDataSource,
        store: HistoricalIngestionStore,
        max_concurrency: int,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency <= 0:
            msg = "max concurrency must be a positive integer"
            raise ValueError(msg)
        self._source = source
        self._store = store
        self._max_concurrency = max_concurrency

    async def backfill(self, request: BackfillRequest) -> BackfillResult:
        """Ingest every selected resource and persist the ordinary terminal result."""
        run_id = await self._store.start_run(request)
        semaphore = asyncio.Semaphore(self._max_concurrency)
        outcomes: list[_ResourceOutcome] = []
        discovered = 0
        seen_game_ids: set[str] = set()

        try:
            for season in request.seasons:
                schedule_result = await self._process_resource(
                    run_id=run_id,
                    season=season,
                    refresh=request.refresh,
                    key=ResourceKey.schedule(season),
                    fetch=partial(self._source.fetch_schedule, season),
                    parse=self._source.parse_schedule,
                    apply=self._store.apply_schedule,
                    outcomes=outcomes,
                )
                if schedule_result.canonical is None:
                    continue

                games = tuple(
                    game
                    for game in self._selected_games(
                        schedule_result.canonical.games,
                        target=request.game_id,
                    )
                    if game.game_id.value not in seen_game_ids
                )
                seen_game_ids.update(game.game_id.value for game in games)
                discovered += len(games)
                async with asyncio.TaskGroup() as task_group:
                    for game in games:
                        task_group.create_task(
                            self._process_game(
                                run_id=run_id,
                                game=game,
                                refresh=request.refresh,
                                semaphore=semaphore,
                                outcomes=outcomes,
                            )
                        )
        except asyncio.CancelledError:
            interrupted = self._build_interrupted_result(run_id, discovered, outcomes)
            try:
                await self._store.finish_run(run_id, interrupted)
            except Exception as error:
                logger.error(
                    "ingestion_run_finish_failed",
                    extra={
                        "event": "ingestion_run_finish_failed",
                        "run_id": str(run_id),
                        "status": BackfillStatus.INTERRUPTED.value,
                        "error_code": "persistence_error",
                        "error_type": type(error).__name__,
                    },
                )
            raise

        result = self._build_result(run_id, discovered, outcomes)
        await self._store.finish_run(run_id, result)
        return result

    async def _process_game(
        self,
        *,
        run_id: UUID,
        game: Game,
        refresh: bool,
        semaphore: asyncio.Semaphore,
        outcomes: list[_ResourceOutcome],
    ) -> None:
        async with semaphore:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(
                    self._process_resource(
                        run_id=run_id,
                        season=game.season,
                        refresh=refresh,
                        key=ResourceKey.box_score(game.game_id),
                        fetch=lambda: self._source.fetch_box_score(game.game_id),
                        parse=self._source.parse_box_score,
                        apply=self._store.replace_box_score,
                        outcomes=outcomes,
                    )
                )
                task_group.create_task(
                    self._process_resource(
                        run_id=run_id,
                        season=game.season,
                        refresh=refresh,
                        key=ResourceKey.play_by_play(game.game_id),
                        fetch=lambda: self._source.fetch_play_by_play(game.game_id),
                        parse=self._source.parse_play_by_play,
                        apply=self._store.replace_play_by_play,
                        outcomes=outcomes,
                    )
                )

    async def _process_resource[CanonicalT](
        self,
        *,
        run_id: UUID,
        season: SeasonId,
        refresh: bool,
        key: ResourceKey,
        fetch: Callable[[], Awaitable[SourceDocument]],
        parse: Callable[[SourceDocument], CanonicalT],
        apply: Callable[[ResourceClaim, UUID, CanonicalT], Awaitable[None]],
        outcomes: list[_ResourceOutcome],
    ) -> _ResourceResult[CanonicalT]:
        started = monotonic()
        claim: ResourceClaim | None = None
        canonical: CanonicalT | None = None
        outcome = _ResourceOutcome.FAILED
        error_code: str | None = None
        error_type: str | None = None
        domain_validation = False
        try:
            decision = await self._store.claim_resource(run_id, key, refresh=refresh)
            if decision.action is ClaimAction.SKIP:
                outcome = _ResourceOutcome.SKIPPED
            elif decision.action is ClaimAction.BUSY:
                outcome = _ResourceOutcome.BUSY
                error_code = "resource_busy"
            else:
                claim = decision.claim
                if claim is None:
                    msg = "fetch and reparse decisions require a resource claim"
                    raise RuntimeError(msg)

                if decision.action is ClaimAction.REPARSE:
                    stored_document = decision.stored_document
                    if stored_document is None:
                        msg = "reparse decisions require a stored source document"
                        raise RuntimeError(msg)
                    document = stored_document.document
                    document_id = stored_document.document_id
                else:
                    document = await fetch()
                    document_id = await self._store.save_document(claim, document)

                domain_validation = True
                canonical = parse(document)
                domain_validation = False
                await apply(claim, document_id, canonical)
                outcome = _ResourceOutcome.SUCCEEDED
        except Exception as error:
            error_code = self._error_code(
                error,
                key.resource_type,
                domain_validation=domain_validation,
            )
            error_type = type(error).__name__
            canonical = None
            if claim is not None:
                try:
                    await self._store.fail_resource(
                        claim,
                        error_code=error_code,
                        error_type=error_type,
                    )
                except Exception as persistence_error:
                    error_code = (
                        persistence_error.code
                        if isinstance(persistence_error, IngestionResourceError)
                        and persistence_error.code in _SAFE_ERROR_CODES
                        else "persistence_error"
                    )
                    error_type = type(persistence_error).__name__

        duration_ms = round(max(0.0, monotonic() - started) * 1000, 3)
        log_extra = {
            "event": "ingestion_resource_finished",
            "run_id": str(run_id),
            "season": season.value,
            "game_id": key.target.value if isinstance(key.target, GameId) else None,
            "resource_type": key.resource_type.value,
            "attempt": claim.version if claim is not None else None,
            "status": outcome.value,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "error_type": error_type,
        }
        log = (
            logger.warning
            if outcome in {_ResourceOutcome.BUSY, _ResourceOutcome.FAILED}
            else logger.info
        )
        log("ingestion_resource_finished", extra=log_extra)
        outcomes.append(outcome)
        return _ResourceResult(outcome=outcome, canonical=canonical)

    @staticmethod
    def _error_code(
        error: Exception,
        resource_type: ResourceType,
        *,
        domain_validation: bool,
    ) -> str:
        if isinstance(error, IngestionResourceError) and error.code in _SAFE_ERROR_CODES:
            return error.code
        if domain_validation and isinstance(error, ValueError):
            return {
                ResourceType.SCHEDULE: "invalid_game",
                ResourceType.BOX_SCORE: "invalid_box_score",
                ResourceType.PLAY_BY_PLAY: "invalid_play_by_play",
            }[resource_type]
        return "persistence_error"

    @staticmethod
    def _selected_games(games: tuple[Game, ...], *, target: GameId | None) -> tuple[Game, ...]:
        accepted = (
            game
            for game in games
            if game.status is GameStatus.FINAL
            and game.game_id.value.startswith(("002", "004"))
            and (target is None or game.game_id == target)
        )
        return tuple(sorted(accepted, key=lambda game: (game.game_date, game.game_id.value)))

    @staticmethod
    def _build_interrupted_result(
        run_id: UUID,
        discovered: int,
        outcomes: list[_ResourceOutcome],
    ) -> BackfillResult:
        return BackfillResult(
            run_id=run_id,
            status=BackfillStatus.INTERRUPTED,
            discovered=discovered,
            succeeded=outcomes.count(_ResourceOutcome.SUCCEEDED),
            skipped=outcomes.count(_ResourceOutcome.SKIPPED),
            failed=(
                outcomes.count(_ResourceOutcome.FAILED) + outcomes.count(_ResourceOutcome.BUSY)
            ),
        )

    @staticmethod
    def _build_result(
        run_id: UUID,
        discovered: int,
        outcomes: list[_ResourceOutcome],
    ) -> BackfillResult:
        succeeded = outcomes.count(_ResourceOutcome.SUCCEEDED)
        skipped = outcomes.count(_ResourceOutcome.SKIPPED)
        busy = outcomes.count(_ResourceOutcome.BUSY)
        failed = outcomes.count(_ResourceOutcome.FAILED) + busy
        if busy > 0:
            status = BackfillStatus.PARTIAL
        elif failed == 0:
            status = BackfillStatus.SUCCEEDED
        elif succeeded > 0 or skipped > 0:
            status = BackfillStatus.PARTIAL
        else:
            status = BackfillStatus.FAILED
        return BackfillResult(
            run_id=run_id,
            status=status,
            discovered=discovered,
            succeeded=succeeded,
            skipped=skipped,
            failed=failed,
        )


__all__ = ["HistoricalIngestionService"]
