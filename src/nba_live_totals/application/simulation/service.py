"""Read-only, one-game/one-state orchestration with a synchronous commit point."""

import logging
from datetime import datetime
from functools import partial

from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.ports import ModelBundleStore
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsRequest,
    SimulateTotalsResult,
    ensure_simulation_paths_distinct,
)
from nba_live_totals.application.simulation.ports import (
    SimulationExecutor,
    SimulationExporter,
    SimulationFeatureSource,
)
from nba_live_totals.domain.model_features import (
    model_feature_descriptor,
    model_input_sha256,
    project_model_features,
)
from nba_live_totals.domain.simulation import make_simulation_frame
from nba_live_totals.domain.simulation_hashing import simulation_revision_sha256, state_seed_sha256
from nba_live_totals.domain.simulation_summary import (
    make_unavailable_outcome,
    validate_simulation_outcome,
)

logger = logging.getLogger(__name__)


class SimulateTotalsService:
    def __init__(
        self,
        source: SimulationFeatureSource,
        bundles: ModelBundleStore,
        executor: SimulationExecutor,
        exporter: SimulationExporter,
    ) -> None:
        self._source, self._bundles = source, bundles
        self._executor, self._exporter = executor, exporter

    async def simulate(self, request: SimulateTotalsRequest) -> SimulateTotalsResult:
        result: SimulateTotalsResult | None = None
        active_error: BaseException | None = None
        try:
            ensure_simulation_paths_distinct(request)
            bundle = await run_owned_io(lambda: self._bundles.load(request.model_path))
            manifest = bundle.manifest
            if manifest.descriptor != model_feature_descriptor(manifest.baseline_config):
                raise SimulationError("simulation_feature_versions_incompatible")
            games = await self._source.select_games(request.seasons, request.game_id)
            if not games:
                raise SimulationError("simulation_empty_selection")
            if len(set(games)) != len(games):
                raise SimulationError("simulation_source_order")
            runtime = await self._executor.runtime()
            await self._exporter.begin(request, bundle, runtime)
            sources: list[ModelSourceReference] = []
            previous: tuple[datetime, str] | None = None
            for game_id in games:
                source = await self._source.resolve(game_id)
                key = (source.game.started_at, source.game.game_id.value)
                if (
                    source.game.game_id != game_id
                    or source.game.season not in request.seasons
                    or (request.game_id is not None and game_id != request.game_id)
                    or (previous is not None and key <= previous)
                ):
                    raise SimulationError("simulation_source_order")
                if (
                    source.reference.attempt.input_token.versions != manifest.feature_versions
                    or source.possession_versions != manifest.dataset.possession_versions
                    or source.lineup_versions != manifest.dataset.lineup_versions
                ):
                    raise SimulationError("simulation_feature_versions_incompatible")
                features = await self._source.read_features(source)
                if (
                    features.game_id != game_id
                    or len(features.states) != source.reference.state_row_count
                ):
                    raise SimulationError("simulation_source_mismatch")
                prior_boundary = None
                for number, state in enumerate(features.states):
                    if (
                        state.game_id != game_id
                        or state.state_number != number
                        or (prior_boundary is not None and state.boundary <= prior_boundary)
                    ):
                        raise SimulationError("simulation_duplicate_state")
                    prior_boundary = state.boundary
                if request.state_number is not None and request.state_number >= len(
                    features.states
                ):
                    raise SimulationError("simulation_missing_state")
                selection = SelectedSimulationStates(len(features.states), request.state_number)
                await self._exporter.begin_game(source, selection)
                for number in selection.numbers:
                    state = features.states[number]
                    prediction = await run_owned_io(partial(bundle.predictor.predict_state, state))
                    if (
                        prediction.state != state
                        or prediction.model_revision_sha256 != manifest.model_revision_sha256
                        or prediction.model_artifact_sha256 != bundle.artifact_sha256
                        or prediction.input_sha256
                        != model_input_sha256(
                            project_model_features(state, manifest.baseline_config)
                        )
                    ):
                        raise SimulationError("simulation_model_mismatch")
                    frame = make_simulation_frame(prediction, request.config)
                    outcome = (
                        await self._executor.simulate(frame, request.config)
                        if frame.is_available
                        else make_unavailable_outcome(frame, request.config, runtime)
                    )
                    validate_simulation_outcome(outcome, request.config)
                    if (
                        outcome.frame != frame
                        or outcome.seed_sha256 != state_seed_sha256(frame, request.config, runtime)
                        or outcome.simulation_revision_sha256
                        != simulation_revision_sha256(frame, request.config, runtime)
                    ):
                        raise SimulationError("simulation_model_mismatch")
                    await self._exporter.append_state(outcome)
                    del outcome, frame, prediction
                await self._exporter.end_game()
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
        except ValueError, TypeError, KeyError, IndexError:
            active_error = SimulationError("simulation_invalid_input")
            raise active_error from None
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
                    "simulation_cleanup_failed",
                    extra={
                        "event": "simulation_cleanup_failed",
                        "error_code": "cleanup_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
