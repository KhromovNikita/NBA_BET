"""Valid feature snapshot references and auditable in-memory modeling sources."""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureInputToken,
    FeatureSnapshotAttempt,
)
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    ModelSourceReference,
)
from nba_live_totals.domain.analytical_lineage import PbpLineage
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.feature_hashing import (
    feature_output_checksum,
    player_output_checksum,
    state_output_checksum,
)
from nba_live_totals.domain.features import FeatureDataset, FeatureVersions
from nba_live_totals.domain.games import Game
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupStatus, LineupVersions
from nba_live_totals.domain.model_targets import TrainingGameInput
from nba_live_totals.domain.model_training import TemporalSplitConfig
from nba_live_totals.domain.possession_hashing import game_context_checksum
from nba_live_totals.domain.possessions import GameContext, PossessionVersions
from tests.unit.domain.modeling_helpers import make_training_input

POSSESSION_VERSIONS = PossessionVersions(
    "nba-pbp-v2", "possessions-parser-v1", "possessions-rules-v1", "possessions-v1"
)
LINEUP_VERSIONS = LineupVersions(
    "nba-lineup-events-v1", "lineups-reconstructor-v1", "lineups-rules-v1", "lineups-v1"
)


def make_model_source_reference(game: Game, features: FeatureDataset) -> ModelSourceReference:
    context = game_context_checksum(GameContext.from_game(game))
    token = FeatureInputToken(
        game.game_id,
        game.started_at,
        context,
        UUID("00000000-0000-4000-8000-000000000101"),
        "1" * 64,
        "2" * 64,
        "3" * 64,
        UUID("00000000-0000-4000-8000-000000000102"),
        "4" * 64,
        "5" * 64,
        "6" * 64,
        LineupStatus.VALID,
        "7" * 64,
        "8" * 64,
        FeatureVersions(),
    )
    state_hash = state_output_checksum(features.states)
    player_hash = player_output_checksum(features.players)
    output_hash = feature_output_checksum(features)
    attempt = FeatureSnapshotAttempt(
        game,
        UUID("00000000-0000-4000-8000-000000000103"),
        1,
        token.feature_revision_id,
        token,
        FeatureAttemptState.PUBLISHED,
        UUID("00000000-0000-4000-8000-000000000104"),
        1,
        game.started_at,
        len(features.states),
        state_hash,
        len(features.players),
        player_hash,
        output_hash,
        features.issues,
        features.status,
        game.started_at,
        game.started_at,
        game.started_at,
    )
    reference = CurrentFeatureSnapshotReference(
        attempt, len(features.states), state_hash, len(features.players), player_hash, output_hash
    )
    lineage = PbpLineage(
        game.game_id, UUID("00000000-0000-4000-8000-000000000105"), "9" * 64, "a" * 64, context
    )
    return ModelSourceReference(game, reference, lineage, POSSESSION_VERSIONS, LINEUP_VERSIONS)


def make_dataset_request(path: Path) -> BuildTrainingDatasetRequest:
    return BuildTrainingDatasetRequest(
        (SeasonId("2025-26"),),
        path,
        TemporalSplitConfig(date(2025, 11, 1), date(2025, 12, 1)),
        BaselineConfig(),
    )


def remap_training_input(
    inputs: TrainingGameInput, game_id: GameId, started_at: datetime
) -> TrainingGameInput:
    game = replace(inputs.game, game_id=game_id, started_at=started_at, game_date=started_at.date())
    features = FeatureDataset(
        game_id, tuple(replace(state, game_id=game_id) for state in inputs.features.states), ()
    )
    return TrainingGameInput(
        game,
        features,
        tuple(replace(row, game_id=game_id) for row in inputs.possessions),
        tuple(replace(end, game_id=game_id) for end in inputs.period_ends),
    )


class MemoryModelSource:
    def __init__(self, inputs: tuple[TrainingGameInput, ...] | None = None) -> None:
        examples = inputs if inputs is not None else (make_training_input(),)
        self.inputs = {example.game.game_id: example for example in examples}
        self.references = {
            game_id: make_model_source_reference(example.game, example.features)
            for game_id, example in self.inputs.items()
        }
        self.events: list[str] = []
        self.rechecks = 0
        self.fail_final = False
        self.empty = False

    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]:
        self.events.append("select")
        if self.empty:
            return ()
        return tuple(
            example.game.game_id
            for example in sorted(
                self.inputs.values(), key=lambda e: (e.game.started_at, e.game.game_id.value)
            )
            if example.game.season in seasons
            and (game_id is None or game_id == example.game.game_id)
        )

    async def resolve(self, game_id: GameId) -> ModelSourceReference:
        self.events.append("resolve")
        return self.references[game_id]

    async def read_features(self, source: ModelSourceReference) -> FeatureDataset:
        self.events.append("read_features")
        await self._check(source)
        return self.inputs[source.game.game_id].features

    async def read_training(self, source: ModelSourceReference) -> TrainingGameInput:
        self.events.append("read_training")
        await self._check(source)
        return self.inputs[source.game.game_id]

    async def recheck(self, source: ModelSourceReference) -> None:
        self.events.append("recheck")
        self.rechecks += 1
        await self._check(source)
        if self.fail_final and self.rechecks > len(self.references):
            raise ModelingError("model_source_changed")

    async def _check(self, source: ModelSourceReference) -> None:
        if source != self.references[source.game.game_id]:
            raise ModelingError("model_source_changed")

    def replace_snapshot_uuid(self, game_id: GameId) -> None:
        source = self.references[game_id]
        attempt = replace(
            source.reference.attempt, snapshot_id=UUID("00000000-0000-4000-8000-000000000999")
        )
        self.references[game_id] = replace(
            source, reference=replace(source.reference, attempt=attempt)
        )
