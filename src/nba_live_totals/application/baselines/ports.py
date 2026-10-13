from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from nba_live_totals.application.baselines.models import (
    BaselineArtifactStats,
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.domain.baselines import StateBaseline
from nba_live_totals.domain.features import FeatureDataset
from nba_live_totals.domain.identifiers import GameId


@runtime_checkable
class BaselineSource(Protocol):
    async def select_games(self, request: BuildBaselinesRequest) -> tuple[GameId, ...]: ...
    async def resolve(self, game_id: GameId) -> BaselineSourceReference: ...
    async def read(self, source: BaselineSourceReference) -> FeatureDataset: ...
    async def recheck(self, source: BaselineSourceReference) -> None: ...


@runtime_checkable
class BaselineExporter(Protocol):
    async def begin(self, request: BuildBaselinesRequest) -> None: ...
    async def append_game(
        self, source: BaselineSourceReference, rows: Iterable[StateBaseline]
    ) -> None: ...
    async def prepare(
        self, sources: tuple[BaselineSourceReference, ...]
    ) -> BaselineArtifactStats: ...
    def publish(self) -> None: ...
    async def abort(self) -> None: ...
