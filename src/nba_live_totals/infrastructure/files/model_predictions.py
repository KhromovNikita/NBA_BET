"""Canonical offline predictions, owned native/file I/O and one commit point."""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    LoadedModelBundle,
    ModelSourceReference,
    PredictModelsRequest,
    PredictModelsResult,
)
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.worker_models import MAX_JSON_BYTES
from nba_live_totals.domain.baseline_hashing import (
    baseline_config_projection,
    baseline_config_sha256,
)
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.model_features import model_input_sha256, project_model_features
from nba_live_totals.domain.model_predictions import (
    PREDICTION_SCHEMA_VERSION,
    PredictedRate,
    StateModelPrediction,
)
from nba_live_totals.domain.model_targets import ModelHead, horizon_period
from nba_live_totals.infrastructure.files.training_dataset import (
    model_source_projection,
    versions_projection,
)


def prediction_projection(
    source: ModelSourceReference, row: StateModelPrediction
) -> CanonicalObject:
    state = row.state
    rates: CanonicalObject = {}
    for head in ModelHead:
        rate = cast(PredictedRate, getattr(row, head.value))
        rates[head.value] = {
            "value_scaled": rate.value_scaled,
            "unavailable_reason": rate.unavailable_reason,
            "was_clipped_to_zero": rate.was_clipped_to_zero,
        }
    return {
        "record_type": "prediction",
        "game_id": state.game_id.value,
        "started_at": source.game.started_at.isoformat(),
        "state_number": state.state_number,
        "state_kind": state.state_kind.value,
        "possession_number": state.possession_number,
        "boundary": {
            "sequence_number": state.boundary.sequence_number,
            "side": state.boundary.side.value,
        },
        "period": state.period,
        "horizon_period": horizon_period(state),
        "remaining_time_ms": state.seconds_remaining_game_ms,
        "source_feature_snapshot_id": str(source.reference.attempt.snapshot_id),
        "source_feature_revision_id": source.reference.attempt.feature_revision_id,
        "source_feature_output_sha256": source.reference.feature_output_sha256,
        "model_revision_sha256": row.model_revision_sha256,
        "model_artifact_sha256": row.model_artifact_sha256,
        "input_sha256": row.input_sha256,
        "rates": rates,
        "status": "available" if row.pace.value_scaled is not None else "unavailable",
    }


class AtomicModelPredictionExporter:
    def __init__(self) -> None:
        self._request: PredictModelsRequest | None = None
        self._bundle: LoadedModelBundle | None = None
        self._body: Path | None = None
        self._ready: Path | None = None
        self._sources: list[ModelSourceReference] = []
        self._states = self._available = self._unavailable = 0
        self._clipped = {head: 0 for head in ModelHead}
        self._body_hash = hashlib.sha256()
        self._result: PredictModelsResult | None = None
        self._busy = self._published = False

    async def _io(self, operation: Callable[[], None]) -> None:
        if self._busy:
            raise ModelingError("model_prediction_busy")
        self._busy = True
        try:
            await run_owned_io(operation)
        except ModelingError:
            raise
        except OSError:
            raise ModelingError("model_prediction_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, ArithmeticError:
            raise ModelingError("model_prediction_invalid") from None
        finally:
            self._busy = False

    def _temp(self) -> Path:
        assert self._request is not None
        fd, name = tempfile.mkstemp(
            prefix=".predictions-", suffix=".tmp", dir=self._request.output_path.parent
        )
        os.close(fd)
        return Path(name)

    async def begin(self, request: PredictModelsRequest, bundle: LoadedModelBundle) -> None:
        if self._request is not None:
            raise ModelingError("model_prediction_already_started")
        self._request, self._bundle = request, bundle
        await self._io(self._begin)

    def _begin(self) -> None:
        self._body, self._ready = self._temp(), self._temp()

    async def append_game(
        self, source: ModelSourceReference, rows: Iterable[StateModelPrediction]
    ) -> None:
        if (
            self._request is None
            or self._body is None
            or self._result is not None
            or self._published
        ):
            raise ModelingError("model_prediction_not_writing")
        await self._io(lambda: self._append(source, rows))

    def _append(self, source: ModelSourceReference, rows: Iterable[StateModelPrediction]) -> None:
        assert self._request is not None and self._body is not None and self._bundle is not None
        request, bundle = self._request, self._bundle
        if source.game.season not in request.seasons or (
            request.game_id is not None and source.game.game_id != request.game_id
        ):
            raise ValueError("prediction source selector mismatch")
        if self._sources and (source.game.started_at, source.game.game_id.value) <= (
            self._sources[-1].game.started_at,
            self._sources[-1].game.game_id.value,
        ):
            raise ValueError("nonchronological prediction sources")
        if any(s.game.game_id == source.game.game_id for s in self._sources):
            raise ValueError("duplicate prediction source")
        manifest = bundle.manifest
        if (
            source.reference.attempt.input_token.versions != manifest.feature_versions
            or source.possession_versions != manifest.dataset.possession_versions
            or source.lineup_versions != manifest.dataset.lineup_versions
        ):
            raise ValueError("prediction feature versions mismatch")
        count = 0
        previous = None
        with self._body.open("ab") as output:
            for row in rows:
                if (
                    not isinstance(row, StateModelPrediction)
                    or row.state.game_id != source.game.game_id
                    or row.state.state_number != count
                    or (previous is not None and row.state.boundary <= previous)
                ):
                    raise ValueError("prediction row source or chronology mismatch")
                if (
                    row.model_revision_sha256 != manifest.model_revision_sha256
                    or row.model_artifact_sha256 != bundle.artifact_sha256
                    or row.input_sha256
                    != model_input_sha256(
                        project_model_features(row.state, manifest.baseline_config)
                    )
                ):
                    raise ValueError("prediction row model or input identity mismatch")
                content = canonical_json_bytes(prediction_projection(source, row)) + b"\n"
                output.write(content)
                self._body_hash.update(content)
                self._states += 1
                self._available += row.pace.value_scaled is not None
                self._unavailable += row.pace.value_scaled is None
                for head in ModelHead:
                    self._clipped[head] += cast(
                        PredictedRate, getattr(row, head.value)
                    ).was_clipped_to_zero
                previous = row.state.boundary
                count += 1
        if count != source.reference.state_row_count:
            raise ValueError("prediction source row count mismatch")
        self._sources.append(source)

    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> PredictModelsResult:
        if not sources or tuple(self._sources) != sources or self._result is not None:
            raise ModelingError("model_prediction_source_mismatch")
        await self._io(self._prepare)
        assert self._result is not None
        return self._result

    def _prepare(self) -> None:
        assert (
            self._request is not None
            and self._bundle is not None
            and self._body is not None
            and self._ready is not None
        )
        request, bundle = self._request, self._bundle
        manifest: CanonicalObject = {
            "record_type": "manifest",
            "output_schema_version": PREDICTION_SCHEMA_VERSION,
            "prediction_policy_version": bundle.manifest.prediction_policy_version,
            "label_version": bundle.manifest.label_version,
            "model_revision_sha256": bundle.manifest.model_revision_sha256,
            "model_artifact_sha256": bundle.artifact_sha256,
            "selectors": {
                "seasons": [s.value for s in request.seasons],
                "game_id": None if request.game_id is None else request.game_id.value,
            },
            "descriptor": bundle.manifest.descriptor.projection(),
            "descriptor_sha256": bundle.manifest.descriptor.sha256,
            "baseline_config": baseline_config_projection(bundle.manifest.baseline_config),
            "baseline_config_sha256": baseline_config_sha256(bundle.manifest.baseline_config),
            "feature_versions": versions_projection(bundle.manifest.feature_versions),
            "possession_versions": versions_projection(bundle.manifest.dataset.possession_versions),
            "lineup_versions": versions_projection(bundle.manifest.dataset.lineup_versions),
            "sources": [model_source_projection(s) for s in self._sources],
            "source_sha256": canonical_json_sha256(
                [model_source_projection(s) for s in self._sources]
            ),
            "games": len(self._sources),
            "states": self._states,
            "available": self._available,
            "unavailable": self._unavailable,
            "negative_prediction_counts": {
                head.value: count for head, count in self._clipped.items()
            },
            "body_sha256": self._body_hash.hexdigest(),
        }
        header = canonical_json_bytes(manifest) + b"\n"
        if len(header) > MAX_JSON_BYTES:
            raise ValueError("oversized prediction source manifest")
        with self._ready.open("wb") as output, self._body.open("rb") as body:
            output.write(header)
            while block := body.read(1_048_576):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        checksum, body_checksum, count = hashlib.sha256(), hashlib.sha256(), 0
        with self._ready.open("rb") as stream:
            first = stream.readline(MAX_JSON_BYTES + 1)
            if first != header:
                raise ValueError("prediction header readback mismatch")
            checksum.update(first)
            while line := stream.readline(MAX_JSON_BYTES + 1):
                record = json.loads(line)
                if (
                    len(line) > MAX_JSON_BYTES
                    or canonical_json_bytes(record) + b"\n" != line
                    or record.get("record_type") != "prediction"
                ):
                    raise ValueError("prediction body readback mismatch")
                count += 1
                body_checksum.update(line)
                checksum.update(line)
        if count != self._states or body_checksum.hexdigest() != self._body_hash.hexdigest():
            raise ValueError("prediction body checksum mismatch")
        self._result = PredictModelsResult(
            request.output_path,
            len(self._sources),
            self._states,
            self._available,
            self._unavailable,
            checksum.hexdigest(),
        )

    def publish(self) -> None:
        if self._ready is None or self._request is None or self._result is None or self._published:
            raise ModelingError("model_prediction_not_prepared")
        try:
            os.replace(self._ready, self._request.output_path)
        except OSError:
            raise ModelingError("model_prediction_io_error") from None
        self._published = True

    async def abort(self) -> None:
        await self._io(self._cleanup)

    def _cleanup(self) -> None:
        for path in (self._body, self._ready):
            if path is not None:
                path.unlink(missing_ok=True)
