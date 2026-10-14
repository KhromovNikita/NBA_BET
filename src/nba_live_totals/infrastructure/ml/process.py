"""Own, drain and reap one hidden CPU child before exposing or removing its files."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.modeling.worker_models import (
    MAX_JSON_BYTES,
    MAX_MODEL_BYTES,
    TrainingWorkerJob,
    TrainingWorkerResult,
)
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes
from nba_live_totals.infrastructure.ml.worker_protocol import (
    parse_worker_result,
    worker_job_projection,
)

_OS_ENV = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "USERPROFILE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "LANG",
        "LC_ALL",
    }
)
_DIAGNOSTIC_LIMIT = 65_536


def child_environment(work_dir: Path) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if key.upper() in _OS_ENV}
    result.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLCONFIGDIR": str(work_dir / "mpl-config"),
        }
    )
    for key in ("TEMP", "TMP", "TMPDIR"):
        result[key] = str(work_dir)
    return result


async def _drain(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
    if stream is None:
        raise ModelingError("model_worker_failed")
    captured = bytearray()
    overflow = False
    while block := await stream.read(4096):
        available = _DIAGNOSTIC_LIMIT - len(captured)
        captured.extend(block[:available])
        if len(block) > available:
            overflow = True
    return bytes(captured), overflow


class SubprocessTrainingWorker:
    def __init__(self, *, command: Sequence[str] | None = None) -> None:
        self._command = (
            tuple(command)
            if command is not None
            else (sys.executable, "-m", "nba_live_totals.infrastructure.ml.training_worker")
        )
        if not self._command or any(type(arg) is not str or not arg for arg in self._command):
            raise ValueError("invalid CPU worker command")
        self.last_pid: int | None = None
        self.last_returncode: int | None = None
        self._running = False

    def _write_job(self, job: TrainingWorkerJob) -> Path:
        if job.dataset.path != job.work_dir / "training.jsonl":
            raise ModelingError("model_worker_protocol_invalid")
        path = job.work_dir / "job.json"
        content = canonical_json_bytes(worker_job_projection(job)) + b"\n"
        if len(content) > MAX_JSON_BYTES:
            raise ModelingError("model_worker_protocol_invalid")
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _read_result(self, job: TrainingWorkerJob) -> TrainingWorkerResult:
        path = job.work_dir / "result.json"
        if path.is_symlink() or path.resolve().parent != job.work_dir:
            raise ModelingError("model_worker_protocol_invalid")
        with path.open("rb") as stream:
            content = stream.read(MAX_JSON_BYTES + 1)
        data = cast(CanonicalObject, json.loads(content))
        if len(content) > MAX_JSON_BYTES or canonical_json_bytes(data) + b"\n" != content:
            raise ModelingError("model_worker_protocol_invalid")
        result = parse_worker_result(data, job)
        for model in result.models:
            if model.path.is_symlink() or model.path.resolve().parent != job.work_dir:
                raise ModelingError("model_worker_protocol_invalid")
            digest, size = hashlib.sha256(), 0
            with model.path.open("rb") as stream:
                while block := stream.read(1_048_576):
                    size += len(block)
                    if size > MAX_MODEL_BYTES:
                        raise ModelingError("model_worker_protocol_invalid")
                    digest.update(block)
            if size != model.size or digest.hexdigest() != model.sha256:
                raise ModelingError("model_worker_protocol_invalid")
        return result

    def _request_termination(self, process: asyncio.subprocess.Process) -> None:
        process.terminate()

    async def _spawn(self, job: TrainingWorkerJob, path: Path) -> asyncio.subprocess.Process:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return await asyncio.create_subprocess_exec(
            *self._command,
            "--job",
            str(path),
            cwd=job.work_dir,
            env=child_environment(job.work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                self._request_termination(process)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        else:
            await process.wait()
        self.last_returncode = process.returncode

    async def execute(self, job: TrainingWorkerJob) -> TrainingWorkerResult:
        if self._running:
            raise ModelingError("model_training_busy")
        self._running = True
        self.last_pid = self.last_returncode = None
        process: asyncio.subprocess.Process | None = None
        spawning: asyncio.Task[asyncio.subprocess.Process] | None = None
        drains: tuple[asyncio.Task[tuple[bytes, bool]], ...] = ()
        try:
            path = await run_owned_io(lambda: self._write_job(job))
            spawning = asyncio.create_task(self._spawn(job, path))
            process = await asyncio.shield(spawning)
            self.last_pid = process.pid
            drains = (
                asyncio.create_task(_drain(process.stdout)),
                asyncio.create_task(_drain(process.stderr)),
            )
            await process.wait()
            output, _ = await asyncio.gather(*drains)
            self.last_returncode = process.returncode
            stdout, overflow = output
            if overflow:
                raise ModelingError("model_worker_protocol_invalid")
            if process.returncode != 0:
                try:
                    record = cast(CanonicalObject, json.loads(stdout))
                    code = record["code"]
                    if (
                        type(code) is not str
                        or canonical_json_bytes({"status": "failed", "code": code}) + b"\n"
                        != stdout
                    ):
                        raise ValueError("invalid child failure protocol")
                    raise ModelingError(code)
                except ValueError, TypeError, KeyError:
                    raise ModelingError("model_worker_failed") from None
            if stdout != canonical_json_bytes({"status": "succeeded"}) + b"\n":
                raise ModelingError("model_worker_protocol_invalid")
            return await run_owned_io(lambda: self._read_result(job))
        except ModelingError:
            raise
        except OSError, ValueError, TypeError, KeyError, IndexError:
            raise ModelingError("model_worker_failed") from None
        finally:
            # Spawn itself is owned: cancellation cannot lose a just-created child handle.
            if spawning is not None and process is None:
                while not spawning.done():
                    try:
                        await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not spawning.cancelled() and spawning.exception() is None:
                    process = spawning.result()
                    self.last_pid = process.pid
                    drains = (
                        asyncio.create_task(_drain(process.stdout)),
                        asyncio.create_task(_drain(process.stderr)),
                    )
            if process is not None:
                stopping = asyncio.create_task(self._stop(process))
                while not stopping.done():
                    try:
                        await asyncio.shield(stopping)
                    except asyncio.CancelledError:
                        continue
                stopping.result()
            if drains:
                joining = asyncio.ensure_future(asyncio.gather(*drains, return_exceptions=True))
                while not joining.done():
                    try:
                        await asyncio.shield(joining)
                    except asyncio.CancelledError:
                        continue
                joining.result()
            self._running = False
