"""Streaming, canonical readback and single-commit ownership guarantees."""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsResult,
)
from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.simulation import make_simulation_frame
from nba_live_totals.infrastructure.files.simulation_export import (
    AtomicSimulationExporter,
    CanonicalSimulationReader,
)
from nba_live_totals.infrastructure.simulation.numpy_engine import NumpySimulationEngine
from tests.unit.application.modeling_fakes import MemoryModelSource
from tests.unit.application.simulation_fakes import make_simulation_request
from tests.unit.infrastructure.files.test_model_predictions import fixed_bundle


async def prepared(
    path: Path, writer: AtomicSimulationExporter | None = None, *, state: int | None = None
) -> tuple[AtomicSimulationExporter, SimulateTotalsResult]:
    writer = writer or AtomicSimulationExporter()
    bundle = await fixed_bundle(path.parent)
    source = MemoryModelSource()
    reference = next(iter(source.references.values()))
    request = make_simulation_request(path)
    if state is not None:
        request = replace(request, game_id=reference.game.game_id, state_number=state)
    engine = NumpySimulationEngine()
    runtime = engine.runtime()
    await writer.begin(request, bundle, runtime)
    await writer.begin_game(
        reference, SelectedSimulationStates(reference.reference.state_row_count, state)
    )
    features = await source.read_features(reference)
    for index in SelectedSimulationStates(len(features.states), state).numbers:
        frame = make_simulation_frame(
            bundle.predictor.predict_state(features.states[index]), request.config
        )
        await writer.append_state(
            engine.simulate(frame, request.config, stop_requested=lambda: False)
        )
    await writer.end_game()
    result = await writer.prepare((reference,))
    return writer, result


async def test_streamed_readback_and_two_paths_have_identical_bytes(tmp_path: Path) -> None:
    first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    writer, result = await prepared(first)
    assert not first.exists()
    assert not any(
        isinstance(v, list) and v and hasattr(v[0], "frame") for v in vars(writer).values()
    )
    writer.publish()
    await writer.abort()
    other, repeated = await prepared(second)
    other.publish()
    await other.abort()
    assert first.read_bytes() == second.read_bytes()
    assert result == repeated
    artifact = CanonicalSimulationReader().validate(first)
    assert result.artifact_sha256 == hashlib.sha256(first.read_bytes()).hexdigest()
    rows = list(CanonicalSimulationReader().iter_outcomes(artifact))
    assert [row.frame.state_number for row in rows] == list(range(8))
    assert result.counts.states == 8 and result.exit_code == 1
    assert no_temps(tmp_path)


async def test_exact_selected_state_is_a_single_streamed_row(tmp_path: Path) -> None:
    path = tmp_path / "one"
    writer, result = await prepared(path, state=3)
    writer.publish()
    await writer.abort()
    artifact = CanonicalSimulationReader().validate(path)
    assert [
        row.frame.state_number for row in CanonicalSimulationReader().iter_outcomes(artifact)
    ] == [3]
    assert result.counts.states == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_key",
        "crlf",
        "spaces",
        "truncated",
        "hash",
        "counts",
        "row",
        "extra",
        "missing",
        "oversize",
    ],
)
async def test_reader_rejects_noncanonical_corrupted_or_miscounted_streams(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "one"
    writer, _ = await prepared(path)
    writer.publish()
    await writer.abort()
    content = path.read_bytes()
    lines = content.splitlines(keepends=True)
    if mutation == "duplicate_key":
        lines[0] = lines[0].replace(b"{", b'{"record_type":"manifest",', 1)
    elif mutation == "crlf":
        lines[0] = lines[0].replace(b"\n", b"\r\n")
    elif mutation == "spaces":
        lines[0] = json.dumps(json.loads(lines[0])).encode() + b"\n"
    elif mutation == "truncated":
        lines[-1] = lines[-1].rstrip(b"\n")
    elif mutation in {"hash", "counts"}:
        header = json.loads(lines[0])
        if mutation == "hash":
            header["body_sha256"] = "0" * 64
        else:
            header["counts"]["complete"] -= 1
            header["counts"]["unavailable"] += 1
        lines[0] = canonical_json_bytes(header) + b"\n"
    elif mutation == "row":
        lines[1], lines[2] = lines[2], lines[1]
    elif mutation == "extra":
        lines.append(lines[-1])
    elif mutation == "missing":
        lines.pop()
    else:
        lines[1] = b" " * (16 * 1024 * 1024 + 1) + b"\n"
    damaged = b"".join(lines)
    path.write_bytes(damaged)
    with pytest.raises(SimulationError, match="simulation_artifact_invalid"):
        CanonicalSimulationReader().validate(path)
    assert path.read_bytes() == damaged


async def test_failed_second_temp_allocation_cleans_first_and_preserves_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    real = tempfile.mkstemp
    calls = 0

    def allocate(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | Path | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled allocation failure")
        return real(suffix=suffix, prefix=prefix, dir=dir, text=text)

    bundle = await fixed_bundle(tmp_path)
    path = tmp_path / "old"
    path.write_bytes(b"old")
    writer = AtomicSimulationExporter()
    engine = NumpySimulationEngine()
    monkeypatch.setattr(
        "nba_live_totals.infrastructure.files.simulation_export.tempfile.mkstemp", allocate
    )
    with pytest.raises(SimulationError, match="simulation_io_error"):
        await writer.begin(make_simulation_request(path), bundle, engine.runtime())
    await writer.abort()
    await writer.abort()
    assert path.read_bytes() == b"old" and no_temps(tmp_path)


async def test_replace_failure_preserves_old_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old"
    path.write_bytes(b"old")
    writer, _ = await prepared(path)

    def deny(*args: object) -> None:
        raise PermissionError("controlled sharing lock")

    monkeypatch.setattr("nba_live_totals.infrastructure.files.simulation_export.os.replace", deny)
    with pytest.raises(SimulationError, match="simulation_io_error"):
        writer.publish()
    await writer.abort()
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("kind", ["model", "config", "symlink"])
async def test_publish_rechecks_late_hardlink_alias(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "old"
    writer, _ = await prepared(path)
    model = tmp_path / "m.nbamodel"
    if kind == "config":
        assert writer._request is not None
        model = tmp_path / "config.json"
        writer._request = replace(writer._request, config_path=model)
    model.write_bytes(b"model input")
    if kind == "symlink":
        try:
            path.symlink_to(model)
        except OSError:
            await writer.abort()
            pytest.skip("Windows requires symlink creation privileges")
    else:
        os.link(model, path)
    with pytest.raises(SimulationError, match="simulation_invalid_input"):
        writer.publish()
    await writer.abort()
    assert model.read_bytes() == path.read_bytes() == b"model input"


async def test_incomplete_game_and_unexpected_state_are_rejected(tmp_path: Path) -> None:
    bundle = await fixed_bundle(tmp_path)
    source = MemoryModelSource()
    reference = next(iter(source.references.values()))
    writer = AtomicSimulationExporter()
    engine = NumpySimulationEngine()
    request = make_simulation_request(tmp_path / "out")
    await writer.begin(request, bundle, engine.runtime())
    await writer.begin_game(reference, SelectedSimulationStates(8))
    with pytest.raises(SimulationError):
        await writer.end_game()
    features = await source.read_features(reference)
    frame = make_simulation_frame(
        bundle.predictor.predict_state(features.states[1]), request.config
    )
    with pytest.raises(SimulationError):
        await writer.append_state(
            engine.simulate(frame, request.config, stop_requested=lambda: False)
        )
    await writer.abort()
    assert not request.output_path.exists()


def no_temps(path: Path) -> bool:
    return not list(path.glob(".simulation-*.tmp"))


async def test_prepare_checks_actual_body_and_preserves_previous_bytes(tmp_path: Path) -> None:
    class Corrupted(AtomicSimulationExporter):
        def _prepare(self) -> None:
            assert self._body is not None
            content = self._body.read_bytes().splitlines(keepends=True)
            self._body.write_bytes(b"".join(reversed(content)))
            super()._prepare()

    path = tmp_path / "old"
    path.write_bytes(b"old")
    writer = Corrupted()
    with pytest.raises(SimulationError, match="simulation_artifact_invalid"):
        await prepared(path, writer)
    await writer.abort()
    assert path.read_bytes() == b"old" and no_temps(tmp_path)


async def test_postcommit_cleanup_error_preserves_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "out"
    writer, result = await prepared(path)
    writer.publish()
    real = Path.unlink

    def deny_body(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(".simulation-") and self.exists():
            raise PermissionError("controlled cleanup failure")
        real(self, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", deny_body)
        with pytest.raises(SimulationError, match="simulation_io_error"):
            await writer.abort()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result.artifact_sha256
    await writer.abort()
    assert no_temps(tmp_path)


async def test_cancellation_joins_prepare_before_cleanup(tmp_path: Path) -> None:
    import asyncio
    from threading import Event

    started, release = Event(), Event()

    class Paused(AtomicSimulationExporter):
        def _prepare(self) -> None:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release prepare")
            super()._prepare()

    path = tmp_path / "out"
    writer = Paused()
    task = asyncio.create_task(prepared(path, writer))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await writer.abort()
    assert not path.exists() and no_temps(tmp_path)


async def test_oversized_body_record_is_rejected_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nba_live_totals.infrastructure.files.simulation_export as export

    path = tmp_path / "out"
    writer = AtomicSimulationExporter()
    monkeypatch.setattr(export, "MAX_RECORD_BYTES", 100)
    with pytest.raises(SimulationError, match="simulation_artifact_invalid"):
        await prepared(path, writer)
    assert writer._body is not None and writer._body.read_bytes() == b""
    await writer.abort()
    assert not path.exists() and no_temps(tmp_path)
