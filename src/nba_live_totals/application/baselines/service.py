"""Sequential pinned-source export; publication is the synchronous commit point."""

import logging
from datetime import datetime

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineSourceReference,
    BuildBaselinesRequest,
    BuildBaselinesResult,
)
from nba_live_totals.application.baselines.ports import BaselineExporter, BaselineSource
from nba_live_totals.domain.baselines import DeterministicBaselineCalculator

logger = logging.getLogger(__name__)


class BaselineBuildService:
    def __init__(
        self,
        source: BaselineSource,
        calculator: DeterministicBaselineCalculator,
        exporter: BaselineExporter,
    ) -> None:
        self._source, self._calculator, self._exporter = source, calculator, exporter

    async def build(self, request: BuildBaselinesRequest) -> BuildBaselinesResult:
        result: BuildBaselinesResult | None = None
        active_error: BaseException | None = None
        try:
            games = await self._source.select_games(request)
            if not games:
                raise BaselineExportError("no_eligible_games")
            if len(set(games)) != len(games):
                raise BaselineExportError("baseline_duplicate_games")
            await self._exporter.begin(request)
            sources: list[BaselineSourceReference] = []
            previous: tuple[datetime, str] | None = None
            for game_id in games:
                source = await self._source.resolve(game_id)
                key = (source.game.started_at, source.game.game_id.value)
                if source.game.game_id != game_id or (previous is not None and key <= previous):
                    raise BaselineExportError("baseline_source_order")
                dataset = await self._source.read(source)
                if dataset.game_id != game_id:
                    raise BaselineExportError("baseline_source_mismatch")
                await self._exporter.append_game(
                    source, self._calculator.calculate_dataset(dataset, request.config)
                )
                del dataset
                await self._source.recheck(source)
                sources.append(source)
                previous = key
            stats = await self._exporter.prepare(tuple(sources))
            for source in sources:
                await self._source.recheck(source)
            self._exporter.publish()
            result = BuildBaselinesResult(request.output_path, stats)
            return result
        except BaseException as error:
            active_error = error
            raise
        finally:
            try:
                await self._exporter.abort()
            except BaseException as error:
                if result is None and active_error is None:
                    raise
                logger.error(
                    "baseline_cleanup_failed",
                    extra={
                        "event": "baseline_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
