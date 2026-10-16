"""Bounded streaming readback and owned temporary files with one publication point."""

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, cast

from nba_live_totals.application.modeling.models import LoadedModelBundle, ModelSourceReference
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsRequest,
    SimulateTotalsResult,
    SimulationCounters,
    SimulationManifest,
    SimulationModelIdentity,
    SimulationSourceSelection,
    ValidatedSimulationExport,
    ensure_simulation_paths_distinct,
)
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.possessions import EventBoundary
from nba_live_totals.domain.simulation import SimulationOutcome, SimulationRuntime, SimulationStatus
from nba_live_totals.domain.simulation_hashing import (
    simulation_revision_sha256,
    simulation_run_revision,
    state_seed_sha256,
)
from nba_live_totals.domain.simulation_summary import validate_simulation_outcome
from nba_live_totals.infrastructure.files.simulation_codec import (
    parse_simulation_manifest,
    parse_simulation_row,
    simulation_manifest_projection,
    simulation_row_projection,
    simulation_row_semantic_projection,
    simulation_selectors_projection,
    simulation_sources_projection,
)

MAX_RECORD_BYTES = 16 * 1024 * 1024


def _line(record: CanonicalObject) -> bytes:
    content = canonical_json_bytes(record) + b"\n"
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("oversized simulation record")
    return content


def _read_line(stream: BinaryIO) -> tuple[CanonicalObject, bytes]:
    line = stream.readline(MAX_RECORD_BYTES + 1)
    if not line or len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n"):
        raise ValueError("missing, truncated or oversized simulation record")
    value = json.loads(line)
    if type(value) is not dict or _line(value) != line:
        raise ValueError("noncanonical simulation record")
    return cast(CanonicalObject, value), line


class _Counts:
    def __init__(self) -> None:
        self.statuses: Counter[SimulationStatus] = Counter()
        self.reasons: Counter[str] = Counter()
        self.unresolved = 0

    def add(self, outcome: SimulationOutcome) -> None:
        self.statuses[outcome.status] += 1
        self.unresolved += outcome.unresolved
        self.reasons.update(dict(outcome.unresolved_counts))

    def finish(self, games: int) -> SimulationCounters:
        return SimulationCounters(
            games,
            sum(self.statuses.values()),
            self.statuses[SimulationStatus.COMPLETE],
            self.statuses[SimulationStatus.INCOMPLETE],
            self.statuses[SimulationStatus.UNAVAILABLE],
            self.unresolved,
            tuple(sorted(self.reasons.items())),
        )


class CanonicalSimulationReader:
    def _scan(
        self,
        path: Path,
        expected: ValidatedSimulationExport | None = None,
    ) -> Iterator[SimulationOutcome | ValidatedSimulationExport]:
        try:
            with path.open("rb") as stream:
                record, line = _read_line(stream)
                manifest = parse_simulation_manifest(record)
                if expected is not None and expected.manifest != manifest:
                    raise ValueError("simulation manifest changed after validation")
                exact, body, semantic = hashlib.sha256(line), hashlib.sha256(), hashlib.sha256()
                counts = _Counts()
                for selected in manifest.sources:
                    previous: EventBoundary | None = None
                    for number in selected.selection.numbers:
                        record, line = _read_line(stream)
                        outcome = parse_simulation_row(record, manifest)
                        if (
                            outcome.frame.game_id != selected.source.game.game_id
                            or outcome.frame.state_number != number
                            or (previous is not None and outcome.frame.boundary <= previous)
                        ):
                            raise ValueError("simulation source/state stream chronology mismatch")
                        previous = outcome.frame.boundary
                        exact.update(line)
                        body.update(line)
                        semantic.update(
                            _line(
                                simulation_row_semantic_projection(
                                    selected.source,
                                    outcome,
                                    manifest.query,
                                )
                            )
                        )
                        counts.add(outcome)
                        yield outcome
                if stream.read(1):
                    raise ValueError("unexpected trailing simulation records")
                if (
                    counts.finish(len(manifest.sources)) != manifest.counts
                    or body.hexdigest() != manifest.body_sha256
                    or semantic.hexdigest() != manifest.semantic_body_sha256
                    or (expected is not None and exact.hexdigest() != expected.artifact_sha256)
                ):
                    raise ValueError("simulation body digest or aggregate counters mismatch")
                yield ValidatedSimulationExport(path, manifest, exact.hexdigest())
        except OSError:
            raise SimulationError("simulation_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError:
            raise SimulationError("simulation_artifact_invalid") from None

    def validate(self, path: Path) -> ValidatedSimulationExport:
        for item in self._scan(path):
            if isinstance(item, ValidatedSimulationExport):
                return item
        raise SimulationError("simulation_artifact_invalid")

    def iter_outcomes(self, artifact: ValidatedSimulationExport) -> Iterator[SimulationOutcome]:
        for item in self._scan(artifact.path, artifact):
            if isinstance(item, SimulationOutcome):
                yield item


class AtomicSimulationExporter:
    def __init__(self) -> None:
        self._request: SimulateTotalsRequest | None = None
        self._model: SimulationModelIdentity | None = None
        self._runtime: SimulationRuntime | None = None
        self._body: Path | None = None
        self._ready: Path | None = None
        self._owned: list[Path] = []
        self._sources: list[SimulationSourceSelection] = []
        self._active: SimulationSourceSelection | None = None
        self._number = 0
        self._previous: EventBoundary | None = None
        self._counts = _Counts()
        self._body_hash, self._semantic_hash = hashlib.sha256(), hashlib.sha256()
        self._result: SimulateTotalsResult | None = None
        self._busy = self._published = self._closed = False

    async def _io(self, operation: Callable[[], None]) -> None:
        if self._busy:
            raise SimulationError("simulation_executor_busy")
        self._busy = True
        try:
            await run_owned_io(operation)
        except OSError:
            raise SimulationError("simulation_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, ArithmeticError, RecursionError:
            raise SimulationError("simulation_artifact_invalid") from None
        finally:
            self._busy = False

    def _writing(self) -> None:
        if self._request is None or self._body is None or self._result is not None or self._closed:
            raise ValueError("simulation exporter is not writing")

    def _temp(self) -> Path:
        assert self._request is not None
        fd, name = tempfile.mkstemp(
            prefix=".simulation-", suffix=".tmp", dir=self._request.output_path.parent
        )
        path = Path(name)
        self._owned.append(path)
        os.close(fd)
        return path

    async def begin(
        self,
        request: SimulateTotalsRequest,
        bundle: LoadedModelBundle,
        runtime: SimulationRuntime,
    ) -> None:
        if self._request is not None or self._closed:
            raise SimulationError("simulation_invalid_input")
        self._request, self._runtime = request, runtime
        m = bundle.manifest
        self._model = SimulationModelIdentity(
            m.model_revision_sha256,
            bundle.artifact_sha256,
            m.descriptor,
            m.feature_versions,
            m.dataset.possession_versions,
            m.dataset.lineup_versions,
        )
        await self._io(self._begin)

    def _begin(self) -> None:
        assert self._request is not None
        ensure_simulation_paths_distinct(self._request)
        self._body = self._temp()
        self._ready = self._temp()

    async def begin_game(
        self,
        source: ModelSourceReference,
        selection: SelectedSimulationStates,
    ) -> None:
        await self._io(lambda: self._begin_game(source, selection))

    def _begin_game(
        self, source: ModelSourceReference, selection: SelectedSimulationStates
    ) -> None:
        self._writing()
        assert self._request is not None and self._model is not None
        request, model = self._request, self._model
        if self._active is not None:
            raise ValueError("simulation game already active")
        item = SimulationSourceSelection(source, selection)
        if (
            source.game.season not in request.seasons
            or (request.game_id is not None and source.game.game_id != request.game_id)
            or selection.state_number != request.state_number
            or source.reference.attempt.input_token.versions != model.feature_versions
            or source.possession_versions != model.possession_versions
            or source.lineup_versions != model.lineup_versions
            or any(s.source.game.game_id == source.game.game_id for s in self._sources)
            or (
                self._sources
                and (source.game.started_at, source.game.game_id.value)
                <= (
                    self._sources[-1].source.game.started_at,
                    self._sources[-1].source.game.game_id.value,
                )
            )
        ):
            raise ValueError("simulation game selectors, versions or chronology mismatch")
        self._active, self._number, self._previous = item, 0, None

    async def append_state(self, outcome: SimulationOutcome) -> None:
        await self._io(lambda: self._append(outcome))

    def _append(self, outcome: SimulationOutcome) -> None:
        self._writing()
        assert self._request is not None and self._model is not None and self._runtime is not None
        active = self._active
        if active is None or self._number >= active.selection.count:
            raise ValueError("unexpected simulation state")
        frame = outcome.frame
        if (
            frame.game_id != active.source.game.game_id
            or frame.state_number != active.selection.numbers[self._number]
            or (self._previous is not None and frame.boundary <= self._previous)
            or frame.model_revision_sha256 != self._model.recipe_sha256
            or frame.model_artifact_sha256 != self._model.artifact_sha256
            or outcome.seed_sha256 != state_seed_sha256(frame, self._request.config, self._runtime)
            or outcome.simulation_revision_sha256
            != simulation_revision_sha256(frame, self._request.config, self._runtime)
        ):
            raise ValueError("simulation frame chronology or model/population identity mismatch")
        validate_simulation_outcome(outcome, self._request.config)
        content = _line(simulation_row_projection(active.source, outcome, self._request.query))
        semantic = _line(
            simulation_row_semantic_projection(active.source, outcome, self._request.query)
        )
        assert self._body is not None
        with self._body.open("ab") as output:
            output.write(content)
        self._body_hash.update(content)
        self._semantic_hash.update(semantic)
        self._counts.add(outcome)
        self._number += 1
        self._previous = frame.boundary

    async def end_game(self) -> None:
        await self._io(self._end_game)

    def _end_game(self) -> None:
        self._writing()
        if self._active is None or self._number != self._active.selection.count:
            raise ValueError("missing simulation states")
        self._sources.append(self._active)
        self._active = None

    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> SimulateTotalsResult:
        def operation() -> None:
            self._writing()
            if (
                self._active is not None
                or not sources
                or sources != tuple(s.source for s in self._sources)
            ):
                raise ValueError("simulation source list or game completion mismatch")
            self._prepare()

        await self._io(operation)
        assert self._result is not None
        return self._result

    def _prepare(self) -> None:
        assert self._request is not None and self._model is not None and self._runtime is not None
        assert self._body is not None and self._ready is not None
        request = self._request
        sources = tuple(self._sources)
        semantic = tuple(
            cast(CanonicalObject, item)
            for item in simulation_sources_projection(sources, semantic=True)
        )
        revision = simulation_run_revision(
            simulation_selectors_projection(request.seasons, request.game_id, request.state_number),
            request.config,
            request.query,
            self._runtime,
            semantic,
            self._semantic_hash.hexdigest(),
        )
        manifest = SimulationManifest(
            request.seasons,
            request.game_id,
            request.state_number,
            request.config,
            request.query,
            self._model,
            self._runtime,
            sources,
            self._counts.finish(len(sources)),
            canonical_json_sha256(simulation_sources_projection(sources)),
            self._body_hash.hexdigest(),
            self._semantic_hash.hexdigest(),
            revision,
        )
        header = _line(simulation_manifest_projection(manifest))
        with self._body.open("ab") as body:
            body.flush()
            os.fsync(body.fileno())
        with self._ready.open("wb") as output, self._body.open("rb") as body:
            output.write(header)
            while block := body.read(1_048_576):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        checked = CanonicalSimulationReader().validate(self._ready)
        if checked.manifest != manifest:
            raise ValueError("prepared simulation metadata readback mismatch")
        self._result = SimulateTotalsResult(manifest.counts, revision, checked.artifact_sha256)

    def publish(self) -> None:
        if (
            self._busy
            or self._closed
            or self._published
            or self._ready is None
            or self._result is None
        ):
            raise SimulationError("simulation_invalid_input")
        assert self._request is not None
        try:
            ensure_simulation_paths_distinct(self._request)
        except ValueError:
            raise SimulationError("simulation_invalid_input") from None
        try:
            os.replace(self._ready, self._request.output_path)
        except OSError:
            raise SimulationError("simulation_io_error") from None
        self._published = True

    async def abort(self) -> None:
        def cleanup() -> None:
            self._closed = True
            first_error: OSError | None = None
            for path in self._owned:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    first_error = first_error or error
            if first_error is not None:
                raise first_error

        await self._io(cleanup)
