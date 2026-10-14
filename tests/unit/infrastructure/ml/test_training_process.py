import asyncio
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.worker_models import TrainingWorkerJob
from nba_live_totals.infrastructure.ml.process import SubprocessTrainingWorker
from tests.unit.infrastructure.ml.job_helpers import write_tiny_dataset
from tests.unit.infrastructure.ml.test_training_worker import make_job

PROBE = Path(__file__).parents[3] / "fixtures/modeling_worker_probe.py"


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87
        return False
    try:
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


async def wait_started(directory: Path) -> dict[str, object]:
    for _ in range(100):
        path = directory / "started.json"
        if path.exists():
            try:
                return json.loads(path.read_bytes())  # type: ignore[no-any-return]
            except ValueError:
                pass
        await asyncio.sleep(0.02)
    raise AssertionError("controlled worker did not start")


async def test_real_child_does_not_receive_application_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("DATABASE_URL", "CLICKHOUSE_PASSWORD", "REDIS_URL"):
        monkeypatch.setenv(name, "sentinel-must-not-be-in-child")
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    worker = SubprocessTrainingWorker(command=(sys.executable, str(PROBE), "--mode", "invalid"))
    with pytest.raises(ModelingError, match="model_worker_protocol_invalid"):
        await worker.execute(job)
    marker = await wait_started(job.work_dir)
    assert marker["secret_seen"] is False
    assert worker.last_returncode is not None


async def test_cancel_reaps_real_child_before_return(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    worker = SubprocessTrainingWorker(command=(sys.executable, str(PROBE), "--mode", "block"))
    task = asyncio.create_task(worker.execute(job))
    marker = await wait_started(job.work_dir)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.last_returncode is not None
    assert worker.last_pid is not None and not process_alive(worker.last_pid)
    assert type(marker["pid"]) is int and not process_alive(marker["pid"])
    before = (job.work_dir / "heartbeat").read_bytes()
    await asyncio.sleep(0.15)
    assert (job.work_dir / "heartbeat").read_bytes() == before


async def test_kill_fallback_reaps_child_after_grace_timeout(tmp_path: Path) -> None:
    class IgnoreTerminate(SubprocessTrainingWorker):
        def _request_termination(self, process: asyncio.subprocess.Process) -> None:
            pass

    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    worker = IgnoreTerminate(command=(sys.executable, str(PROBE), "--mode", "block"))
    task = asyncio.create_task(worker.execute(job))
    await wait_started(job.work_dir)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 8)
    assert worker.last_returncode is not None


async def test_large_stdout_and_stderr_are_drained_without_deadlock(tmp_path: Path) -> None:
    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    worker = SubprocessTrainingWorker(command=(sys.executable, str(PROBE), "--mode", "noise"))
    with pytest.raises(ModelingError):
        await asyncio.wait_for(worker.execute(job), 10)
    assert worker.last_returncode is not None


async def test_cancel_while_spawn_handle_is_pending_still_reaps_real_child(tmp_path: Path) -> None:
    created, release = asyncio.Event(), asyncio.Event()

    class DelayedSpawn(SubprocessTrainingWorker):
        async def _spawn(self, job: TrainingWorkerJob, path: Path) -> asyncio.subprocess.Process:
            process = await super()._spawn(job, path)
            created.set()
            await release.wait()
            return process

    dataset = await write_tiny_dataset(tmp_path / "training")
    job = make_job(dataset, tmp_path / "job")
    worker = DelayedSpawn(command=(sys.executable, str(PROBE), "--mode", "block"))
    task = asyncio.create_task(worker.execute(job))
    await asyncio.wait_for(created.wait(), 3)
    marker = await wait_started(job.work_dir)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.last_returncode is not None
    assert type(marker["pid"]) is int and not process_alive(marker["pid"])
