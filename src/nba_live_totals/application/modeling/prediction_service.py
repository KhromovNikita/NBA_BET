"""Load one pinned model and export causal states one checked game at a time."""

import logging
from datetime import datetime

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    ModelSourceReference,
    PredictModelsRequest,
    PredictModelsResult,
)
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.ports import (
    ModelBundleStore,
    ModelFeatureSource,
    PredictionExporter,
)

logger = logging.getLogger(__name__)


class PredictModelsService:
    def __init__(
        self, source: ModelFeatureSource, bundles: ModelBundleStore, exporter: PredictionExporter
    ) -> None:
        self._source, self._bundles, self._exporter = source, bundles, exporter

    async def predict(self, request: PredictModelsRequest) -> PredictModelsResult:
        result: PredictModelsResult | None = None
        active_error: BaseException | None = None
        try:
            bundle = await run_owned_io(lambda: self._bundles.load(request.model_path))
            games = await self._source.select_games(request.seasons, request.game_id)
            if not games:
                raise ModelingError("no_eligible_games")
            if len(set(games)) != len(games):
                raise ModelingError("model_duplicate_games")
            await self._exporter.begin(request, bundle)
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
                manifest = bundle.manifest
                if (
                    source.reference.attempt.input_token.versions != manifest.feature_versions
                    or source.possession_versions != manifest.dataset.possession_versions
                    or source.lineup_versions != manifest.dataset.lineup_versions
                ):
                    raise ModelingError("model_feature_versions_incompatible")
                features = await self._source.read_features(source)
                if features.game_id != game_id:
                    raise ModelingError("model_source_mismatch")
                await self._exporter.append_game(
                    source, (bundle.predictor.predict_state(state) for state in features.states)
                )
                del features
                await self._source.recheck(source)
                sources.append(source)
                previous = key
            prepared = await self._exporter.prepare(tuple(sources))
            for source in sources:
                await self._source.recheck(source)
            self._exporter.publish()
            result = prepared
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
                    "model_prediction_cleanup_failed",
                    extra={
                        "event": "model_prediction_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
