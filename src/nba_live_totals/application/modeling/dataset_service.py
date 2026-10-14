"""One game at a time; verified publication is the synchronous commit point."""

import logging
from datetime import datetime

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    BuildTrainingDatasetResult,
    ModelSourceReference,
)
from nba_live_totals.application.modeling.ports import ModelFeatureSource, TrainingDatasetExporter
from nba_live_totals.domain.model_training import build_training_rows

logger = logging.getLogger(__name__)


class BuildTrainingDatasetService:
    def __init__(self, source: ModelFeatureSource, exporter: TrainingDatasetExporter) -> None:
        self._source, self._exporter = source, exporter

    async def build(self, request: BuildTrainingDatasetRequest) -> BuildTrainingDatasetResult:
        result: BuildTrainingDatasetResult | None = None
        active_error: BaseException | None = None
        try:
            games = await self._source.select_games(request.seasons)
            if not games:
                raise ModelingError("no_eligible_games")
            if len(set(games)) != len(games):
                raise ModelingError("model_duplicate_games")
            await self._exporter.begin(request)
            sources: list[ModelSourceReference] = []
            previous: tuple[datetime, str] | None = None
            for game_id in games:
                source = await self._source.resolve(game_id)
                key = (source.game.started_at, source.game.game_id.value)
                if (
                    source.game.game_id != game_id
                    or source.game.season not in request.seasons
                    or (previous is not None and key <= previous)
                ):
                    raise ModelingError("model_source_order")
                inputs = await self._source.read_training(source)
                if inputs.game != source.game:
                    raise ModelingError("model_source_mismatch")
                await self._exporter.append_game(
                    source, build_training_rows(inputs, request.split, request.baseline_config)
                )
                del inputs
                await self._source.recheck(source)
                sources.append(source)
                previous = key
            summary = await self._exporter.prepare(tuple(sources))
            for source in sources:
                await self._source.recheck(source)
            self._exporter.publish()
            result = BuildTrainingDatasetResult(request.output_path, summary)
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
                    "training_dataset_cleanup_failed",
                    extra={
                        "event": "training_dataset_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
