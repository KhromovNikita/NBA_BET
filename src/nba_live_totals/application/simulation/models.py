"""Typed selectors and bounded metadata for streamed offline simulation artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildMode
from nba_live_totals.application.modeling.models import ModelSourceReference, resolved_path
from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.domain.model_features import FeatureProjectionDescriptor, require_sha256
from nba_live_totals.domain.possessions import PossessionVersions
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationQuery,
    SimulationRuntime,
    validate_unresolved_counts,
)
from nba_live_totals.domain.simulation_hashing import SIMULATION_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class SimulateTotalsRequest:
    seasons: tuple[SeasonId, ...]
    model_path: Path
    output_path: Path
    config: SimulationConfig
    query: SimulationQuery
    game_id: GameId | None = None
    state_number: int | None = None
    config_path: Path | None = None

    def __post_init__(self) -> None:
        selector = BuildDatasetRequest(
            self.seasons, game_id=self.game_id, mode=FeatureBuildMode.VERIFY
        )
        object.__setattr__(self, "seasons", selector.seasons)
        for name in ("model_path", "output_path"):
            object.__setattr__(self, name, resolved_path(getattr(self, name)))
        if self.config_path is not None:
            object.__setattr__(self, "config_path", resolved_path(self.config_path))
        if not isinstance(self.config, SimulationConfig) or not isinstance(
            self.query, SimulationQuery
        ):
            raise ValueError("simulation requires typed configuration and query")
        if self.state_number is not None:
            bounded_integer(self.state_number)
            if self.game_id is None:
                raise ValueError("simulation state selector requires one game")
        ensure_simulation_paths_distinct(self)


def ensure_simulation_paths_distinct(request: SimulateTotalsRequest) -> None:
    output = request.output_path.resolve()
    for source in (request.model_path, request.config_path):
        if source is not None and (
            source.resolve() == output
            or (source.exists() and output.exists() and source.samefile(output))
        ):
            raise ValueError("simulation output must differ from model and config inputs")


@dataclass(frozen=True, slots=True)
class SelectedSimulationStates:
    total_states: int
    state_number: int | None = None

    def __post_init__(self) -> None:
        bounded_integer(self.total_states, minimum=1)
        if self.state_number is not None:
            bounded_integer(self.state_number, maximum=self.total_states - 1)

    @property
    def count(self) -> int:
        return self.total_states if self.state_number is None else 1

    @property
    def numbers(self) -> range | tuple[int, ...]:
        return range(self.total_states) if self.state_number is None else (self.state_number,)


@dataclass(frozen=True, slots=True)
class SimulationSourceSelection:
    source: ModelSourceReference
    selection: SelectedSimulationStates

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, ModelSourceReference)
            or not isinstance(self.selection, SelectedSimulationStates)
            or self.selection.total_states != self.source.reference.state_row_count
        ):
            raise ValueError("simulation selection contradicts published source row count")


@dataclass(frozen=True, slots=True)
class SimulationModelIdentity:
    recipe_sha256: str
    artifact_sha256: str
    descriptor: FeatureProjectionDescriptor
    feature_versions: FeatureVersions
    possession_versions: PossessionVersions
    lineup_versions: LineupVersions

    def __post_init__(self) -> None:
        require_sha256(self.recipe_sha256)
        require_sha256(self.artifact_sha256)
        if (
            not isinstance(self.descriptor, FeatureProjectionDescriptor)
            or not isinstance(self.feature_versions, FeatureVersions)
            or not isinstance(self.possession_versions, PossessionVersions)
            or not isinstance(self.lineup_versions, LineupVersions)
        ):
            raise ValueError("invalid simulation model descriptor or source versions")


@dataclass(frozen=True, slots=True)
class SimulationCounters:
    games: int
    states: int
    complete: int
    incomplete: int
    unavailable: int
    unresolved: int
    unresolved_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        bounded_integer(self.games)
        bounded_integer(self.states, minimum=self.games)
        for count in (self.complete, self.incomplete, self.unavailable):
            bounded_integer(count, maximum=self.states)
        bounded_integer(self.unresolved)
        if (
            bool(self.games) != bool(self.states)
            or self.states != self.complete + self.incomplete + self.unavailable
            or not self.incomplete <= self.unresolved <= self.incomplete * 1_000_000
        ):
            raise ValueError("simulation aggregate counters contradict population mass")
        validate_unresolved_counts(self.unresolved_counts, self.unresolved)


@dataclass(frozen=True, slots=True)
class SimulationManifest:
    seasons: tuple[SeasonId, ...]
    game_id: GameId | None
    state_number: int | None
    config: SimulationConfig
    query: SimulationQuery
    model: SimulationModelIdentity
    runtime: SimulationRuntime
    sources: tuple[SimulationSourceSelection, ...]
    counts: SimulationCounters
    source_sha256: str
    body_sha256: str
    semantic_body_sha256: str
    run_semantic_revision: str
    schema_version: str = SIMULATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        selector = BuildDatasetRequest(
            self.seasons, game_id=self.game_id, mode=FeatureBuildMode.VERIFY
        )
        object.__setattr__(self, "seasons", selector.seasons)
        if self.state_number is not None:
            bounded_integer(self.state_number)
            if self.game_id is None:
                raise ValueError("simulation manifest state selector requires a game")
        if (
            self.schema_version != SIMULATION_SCHEMA_VERSION
            or not isinstance(self.config, SimulationConfig)
            or not isinstance(self.query, SimulationQuery)
            or not isinstance(self.model, SimulationModelIdentity)
            or not isinstance(self.runtime, SimulationRuntime)
            or not isinstance(self.counts, SimulationCounters)
            or type(self.sources) is not tuple
            or not self.sources
            or any(not isinstance(s, SimulationSourceSelection) for s in self.sources)
        ):
            raise ValueError("invalid simulation manifest schema or typed metadata")
        keys = tuple((s.source.game.started_at, s.source.game.game_id.value) for s in self.sources)
        if keys != tuple(sorted(keys)) or len({s.source.game.game_id for s in self.sources}) != len(
            keys
        ):
            raise ValueError("simulation sources must be ordered unique games")
        for item in self.sources:
            source = item.source
            if (
                source.game.season not in self.seasons
                or (self.game_id is not None and source.game.game_id != self.game_id)
                or item.selection.state_number != self.state_number
                or source.reference.attempt.input_token.versions != self.model.feature_versions
                or source.possession_versions != self.model.possession_versions
                or source.lineup_versions != self.model.lineup_versions
            ):
                raise ValueError("simulation source selectors or versions mismatch")
        if (
            self.counts.games != len(self.sources)
            or self.counts.states != sum(s.selection.count for s in self.sources)
            or self.counts.unresolved > self.counts.incomplete * self.config.simulations
        ):
            raise ValueError("simulation manifest counters contradict selected sources")
        for digest in (
            self.source_sha256,
            self.body_sha256,
            self.semantic_body_sha256,
            self.run_semantic_revision,
        ):
            require_sha256(digest)


@dataclass(frozen=True, slots=True)
class ValidatedSimulationExport:
    path: Path
    manifest: SimulationManifest
    artifact_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", resolved_path(self.path))
        if not isinstance(self.manifest, SimulationManifest):
            raise ValueError("invalid validated simulation manifest")
        require_sha256(self.artifact_sha256)


@dataclass(frozen=True, slots=True)
class SimulateTotalsResult:
    counts: SimulationCounters
    run_semantic_revision: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.counts, SimulationCounters) or not self.counts.games:
            raise ValueError("published simulation requires selected games")
        require_sha256(self.run_semantic_revision)
        require_sha256(self.artifact_sha256)

    @property
    def exit_code(self) -> int:
        return int(bool(self.counts.incomplete or self.counts.unavailable))
