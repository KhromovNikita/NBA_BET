from nba_live_totals.application.baselines.models import (
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId
from tests.unit.application.test_feature_models import make_reference
from tests.unit.domain.baseline_helpers import make_state


class MemoryBaselineSource:
    def __init__(self) -> None:
        ref = make_reference()
        self.reference = BaselineSourceReference(ref.attempt.game, ref)
        self.events: list[str] = []
        self.fail_final = False
        self.rechecks = 0
        self.empty = False

    async def select_games(self, request: BuildBaselinesRequest) -> tuple[GameId, ...]:
        self.events.append("select")
        return () if self.empty else (self.reference.game.game_id,)

    async def resolve(self, game_id: GameId) -> BaselineSourceReference:
        self.events.append("resolve")
        return self.reference

    async def read(self, source: BaselineSourceReference) -> FeatureDataset:
        self.events.append("read")
        return FeatureDataset(
            source.game.game_id,
            tuple(make_state(state_number=n, game_id=source.game.game_id) for n in (0, 1)),
            (),
        )

    async def recheck(self, source: BaselineSourceReference) -> None:
        from nba_live_totals.application.baselines.errors import BaselineExportError

        self.events.append("recheck")
        self.rechecks += 1
        if self.fail_final and self.rechecks == 2:
            raise BaselineExportError("baseline_source_changed")
