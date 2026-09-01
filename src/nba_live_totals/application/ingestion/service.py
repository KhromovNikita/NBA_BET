"""Provider- and storage-neutral orchestration for historical NBA ingestion."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from uuid import UUID

from nba_live_totals.application.ingestion.models import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    ResourceClaim,
    ResourceKey,
)
from nba_live_totals.application.ingestion.ports import (
    HistoricalDataSource,
    HistoricalIngestionStore,
)
from nba_live_totals.domain import Game, GameId, GameStatus, SourceDocument


class _ResourceOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
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

        for season in request.seasons:
            schedule_result = await self._process_resource(
                run_id=run_id,
                refresh=request.refresh,
                key=ResourceKey.schedule(season),
                fetch=partial(self._source.fetch_schedule, season),
                parse=self._source.parse_schedule,
                apply=self._store.apply_schedule,
            )
            outcomes.append(schedule_result.outcome)
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
            game_results = await asyncio.gather(
                *(
                    self._process_game(
                        run_id=run_id,
                        game=game,
                        refresh=request.refresh,
                        semaphore=semaphore,
                    )
                    for game in games
                )
            )
            for game_outcomes in game_results:
                outcomes.extend(game_outcomes)

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
    ) -> tuple[_ResourceOutcome, _ResourceOutcome]:
        async with semaphore:
            box_score = await self._process_resource(
                run_id=run_id,
                refresh=refresh,
                key=ResourceKey.box_score(game.game_id),
                fetch=lambda: self._source.fetch_box_score(game.game_id),
                parse=self._source.parse_box_score,
                apply=self._store.replace_box_score,
            )
            play_by_play = await self._process_resource(
                run_id=run_id,
                refresh=refresh,
                key=ResourceKey.play_by_play(game.game_id),
                fetch=lambda: self._source.fetch_play_by_play(game.game_id),
                parse=self._source.parse_play_by_play,
                apply=self._store.replace_play_by_play,
            )
        return box_score.outcome, play_by_play.outcome

    async def _process_resource[CanonicalT](
        self,
        *,
        run_id: UUID,
        refresh: bool,
        key: ResourceKey,
        fetch: Callable[[], Awaitable[SourceDocument]],
        parse: Callable[[SourceDocument], CanonicalT],
        apply: Callable[[ResourceClaim, UUID, CanonicalT], Awaitable[None]],
    ) -> _ResourceResult[CanonicalT]:
        decision = await self._store.claim_resource(run_id, key, refresh=refresh)
        if decision.action is ClaimAction.SKIP:
            return _ResourceResult(outcome=_ResourceOutcome.SKIPPED)
        if decision.action is ClaimAction.BUSY:
            return _ResourceResult(outcome=_ResourceOutcome.FAILED)

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

        canonical = parse(document)
        await apply(claim, document_id, canonical)
        return _ResourceResult(outcome=_ResourceOutcome.SUCCEEDED, canonical=canonical)

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
    def _build_result(
        run_id: UUID,
        discovered: int,
        outcomes: list[_ResourceOutcome],
    ) -> BackfillResult:
        succeeded = outcomes.count(_ResourceOutcome.SUCCEEDED)
        skipped = outcomes.count(_ResourceOutcome.SKIPPED)
        failed = outcomes.count(_ResourceOutcome.FAILED)
        if failed == 0:
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
