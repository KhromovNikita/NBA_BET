"""Typed selectors, pinned source references and committed artifact statistics."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from nba_live_totals.application.features import (
    BuildDatasetRequest,
    CurrentFeatureSnapshotReference,
    FeatureBuildMode,
)
from nba_live_totals.domain.baselines import BaselineConfig, bounded_integer
from nba_live_totals.domain.games import Game
from nba_live_totals.domain.identifiers import GameId, SeasonId


@dataclass(frozen=True, slots=True)
class BuildBaselinesRequest:
    seasons: tuple[SeasonId, ...]
    output_path: Path
    game_id: GameId | None = None
    config: BaselineConfig = field(default_factory=BaselineConfig)

    def __post_init__(self) -> None:
        selector = BuildDatasetRequest(
            self.seasons, game_id=self.game_id, mode=FeatureBuildMode.VERIFY
        )
        object.__setattr__(self, "seasons", selector.seasons)
        if (
            not isinstance(self.config, BaselineConfig)
            or not isinstance(self.output_path, Path)
            or not self.output_path.name
        ):
            raise ValueError("invalid baseline configuration or output path")
        object.__setattr__(self, "output_path", self.output_path.resolve())

    @classmethod
    def from_range(
        cls,
        season_from: SeasonId,
        season_to: SeasonId,
        *,
        output_path: Path,
        config: BaselineConfig | None = None,
    ) -> BuildBaselinesRequest:
        selector = BuildDatasetRequest.for_range(
            season_from, season_to, mode=FeatureBuildMode.VERIFY
        )
        return cls(selector.seasons, output_path, config=config or BaselineConfig())


@dataclass(frozen=True, slots=True)
class BaselineSourceReference:
    game: Game
    reference: CurrentFeatureSnapshotReference

    def __post_init__(self) -> None:
        if (
            not isinstance(self.game, Game)
            or not isinstance(self.reference, CurrentFeatureSnapshotReference)
            or self.game != self.reference.attempt.game
        ):
            raise ValueError("invalid baseline source reference")


@dataclass(frozen=True, slots=True)
class BaselineArtifactStats:
    games: int
    states: int
    row_count: int
    available: int
    partial: int
    unavailable: int
    outcome_sha256: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        for value in (self.games, self.states, self.row_count):
            bounded_integer(value, minimum=1)
        for value in (self.available, self.partial, self.unavailable):
            bounded_integer(value)
        if (
            self.row_count != self.states * 3
            or self.row_count != self.available + self.partial + self.unavailable
        ):
            raise ValueError("invalid baseline artifact counts")
        for digest in (self.outcome_sha256, self.artifact_sha256):
            if type(digest) is not str or re.fullmatch("[0-9a-f]{64}", digest) is None:
                raise ValueError("invalid baseline artifact digest")


@dataclass(frozen=True, slots=True)
class BuildBaselinesResult:
    output_path: Path
    stats: BaselineArtifactStats

    @property
    def exit_code(self) -> int:
        return int(bool(self.stats.partial or self.stats.unavailable))
