"""Bounded JSONL spooling with verified readback and one atomic commit point."""

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.baselines.models import (
    BaselineArtifactStats,
    BaselineSourceReference,
    BuildBaselinesRequest,
)
from nba_live_totals.domain.baseline_hashing import (
    baseline_config_projection,
    baseline_config_sha256,
    baseline_outcome_bytes,
)
from nba_live_totals.domain.baselines import (
    BASELINE_FORMULA_VERSION,
    BASELINE_OUTPUT_SCHEMA_VERSION,
    BASELINE_SCALE,
    BaselineMethod,
    StateBaseline,
)
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes


def _source_projection(source: BaselineSourceReference) -> CanonicalObject:
    ref = source.reference
    versions = ref.attempt.input_token.versions
    return {
        "game_id": source.game.game_id.value,
        "feature_snapshot_id": str(ref.attempt.snapshot_id),
        "feature_revision_id": ref.attempt.feature_revision_id,
        "feature_input_token_sha256": ref.attempt.input_token.sha256,
        "feature_output_sha256": ref.feature_output_sha256,
        "state_output_sha256": ref.state_output_sha256,
        "player_output_sha256": ref.player_output_sha256,
        "state_row_count": ref.state_row_count,
        "player_row_count": ref.player_row_count,
        "source_status": ref.result_status.value,
        "issue_codes": list(ref.issues),
        "feature_versions": {
            "join_version": versions.join_version,
            "formula_version": versions.formula_version,
            "event_normalizer_version": versions.event_normalizer_version,
            "output_schema_version": versions.output_schema_version,
            "player_output_schema_version": versions.player_output_schema_version,
        },
    }


class AtomicJsonlBaselineExporter:
    def __init__(self) -> None:
        self._request: BuildBaselinesRequest | None = None
        self._body: Path | None = None
        self._ready: Path | None = None
        self._worker: asyncio.Task[None] | None = None
        self._sources: list[BaselineSourceReference] = []
        self._counts = {"available": 0, "partial": 0, "unavailable": 0}
        self._row_count = 0
        self._hash = hashlib.sha256()
        self._stats: BaselineArtifactStats | None = None
        self._published = False

    async def _io(self, operation: Callable[[], None]) -> None:
        if self._worker is not None:
            raise BaselineExportError("baseline_export_busy")
        worker = asyncio.create_task(asyncio.to_thread(operation))
        self._worker = worker
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        except OSError:
            raise BaselineExportError("baseline_export_io_error") from None
        except ValueError, TypeError:
            raise BaselineExportError("baseline_export_invalid_artifact") from None
        finally:
            self._worker = None

    def _temp(self) -> Path:
        assert self._request is not None
        fd, name = tempfile.mkstemp(
            prefix=".baseline-", suffix=".tmp", dir=self._request.output_path.parent
        )
        os.close(fd)
        return Path(name)

    async def begin(self, request: BuildBaselinesRequest) -> None:
        if self._request is not None:
            raise BaselineExportError("baseline_export_already_started")
        self._request = request
        await self._io(self._begin)

    def _begin(self) -> None:
        self._body = self._temp()
        self._ready = self._temp()

    async def append_game(
        self, source: BaselineSourceReference, rows: Iterable[StateBaseline]
    ) -> None:
        await self._io(lambda: self._append(source, rows))

    def _append(self, source: BaselineSourceReference, rows: Iterable[StateBaseline]) -> None:
        if self._body is None or self._request is None or self._stats is not None:
            raise BaselineExportError("baseline_export_invalid_state")
        if self._sources and (source.game.started_at, source.game.game_id.value) <= (
            self._sources[-1].game.started_at,
            self._sources[-1].game.game_id.value,
        ):
            raise BaselineExportError("baseline_export_source_order")
        count = 0
        methods = tuple(BaselineMethod)
        with self._body.open("ab") as output:
            for index, row in enumerate(rows):
                if (
                    row.state.game_id != source.game.game_id
                    or row.state.state_number != index // 3
                    or row.method is not methods[index % 3]
                ):
                    raise BaselineExportError("baseline_export_row_order")
                data = baseline_outcome_bytes(
                    feature_snapshot_id=source.reference.attempt.snapshot_id,
                    feature_revision_id=source.reference.attempt.feature_revision_id,
                    feature_output_sha256=source.reference.feature_output_sha256,
                    row=row,
                    config=self._request.config,
                )
                output.write(data)
                self._hash.update(data)
                self._counts[row.status.value] += 1
                count += 1
            output.flush()
            os.fsync(output.fileno())
        if count != 3 * source.reference.state_row_count:
            raise BaselineExportError("baseline_export_row_count")
        self._row_count += count
        self._sources.append(source)

    async def prepare(self, sources: tuple[BaselineSourceReference, ...]) -> BaselineArtifactStats:
        if not sources or tuple(self._sources) != sources or self._stats is not None:
            raise BaselineExportError("baseline_export_source_mismatch")
        await self._io(self._prepare)
        assert self._stats is not None
        return self._stats

    def _prepare(self) -> None:
        assert self._request is not None and self._body is not None and self._ready is not None
        manifest: CanonicalObject = {
            "record_type": "manifest",
            "formula_version": BASELINE_FORMULA_VERSION,
            "output_schema_version": BASELINE_OUTPUT_SCHEMA_VERSION,
            "scale": BASELINE_SCALE,
            "selectors": {
                "seasons": [s.value for s in self._request.seasons],
                "game_id": None if self._request.game_id is None else self._request.game_id.value,
            },
            "config": baseline_config_projection(self._request.config),
            "config_sha256": baseline_config_sha256(self._request.config),
            "sources": [_source_projection(s) for s in self._sources],
            "outcome_row_count": self._row_count,
            "outcome_sha256": self._hash.hexdigest(),
        }
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        with self._ready.open("wb") as output, self._body.open("rb") as body:
            output.write(manifest_bytes)
            while block := body.read(1_048_576):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        body_hash, artifact_hash, count = hashlib.sha256(), hashlib.sha256(), 0
        with self._ready.open("rb") as prepared:
            first = prepared.readline()
            if first != manifest_bytes:
                raise BaselineExportError("baseline_export_readback_mismatch")
            artifact_hash.update(first)
            for line in prepared:
                record = json.loads(line)
                if (
                    canonical_json_bytes(record) + b"\n" != line
                    or record.get("record_type") != "baseline"
                ):
                    raise BaselineExportError("baseline_export_readback_mismatch")
                body_hash.update(line)
                artifact_hash.update(line)
                count += 1
        if count != self._row_count or body_hash.hexdigest() != self._hash.hexdigest():
            raise BaselineExportError("baseline_export_readback_mismatch")
        self._stats = BaselineArtifactStats(
            len(self._sources),
            count // 3,
            count,
            self._counts["available"],
            self._counts["partial"],
            self._counts["unavailable"],
            body_hash.hexdigest(),
            artifact_hash.hexdigest(),
        )

    def publish(self) -> None:
        if self._stats is None or self._request is None or self._ready is None or self._published:
            raise BaselineExportError("baseline_export_not_prepared")
        try:
            os.replace(self._ready, self._request.output_path)
        except OSError:
            raise BaselineExportError("baseline_export_io_error") from None
        self._published = True

    async def abort(self) -> None:
        await self._io(self._cleanup)

    def _cleanup(self) -> None:
        for path in (self._body, self._ready):
            if path is not None:
                path.unlink(missing_ok=True)
