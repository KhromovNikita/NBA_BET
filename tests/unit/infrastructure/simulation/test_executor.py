"""Cancellation barriers prove workers finish before resource ownership is released."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import pytest

from nba_live_totals.application.modeling.owned_io import run_owned_io
from nba_live_totals.application.simulation.errors import SimulationError, SimulationStopped
from nba_live_totals.domain.simulation import (
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationRuntime,
)
from nba_live_totals.infrastructure.simulation.executor import OwnedSimulationExecutor
from tests.unit.domain.simulation_helpers import make_simulation_outcome


class ControlledEngine:
    def __init__(
        self, *, block: bool = True, fail: bool = False, block_runtime: bool = False
    ) -> None:
        self.block, self.fail, self.block_runtime = block, fail, block_runtime
        self.started, self.gate, self.finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.runtime_started, self.runtime_gate, self.runtime_finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.stop_observed = threading.Event()
        self.predicate: Callable[[], bool] = lambda: False
        self.calls = 0
        self.thread_id: int | None = None
        self.outcome = make_simulation_outcome()

    def runtime(self) -> SimulationRuntime:
        if self.block_runtime:
            self.runtime_started.set()
            try:
                if not self.runtime_gate.wait(5):
                    raise AssertionError("runtime barrier was not released")
            finally:
                self.runtime_finished.set()
        return SimulationRuntime("3.14.8", "2.5.2", "Windows", "AMD64")

    def simulate(
        self,
        frame: SimulationFrame,
        config: SimulationConfig,
        *,
        stop_requested: Callable[[], bool],
    ) -> SimulationOutcome:
        self.calls += 1
        self.predicate = stop_requested
        self.thread_id = threading.get_ident()
        self.started.set()
        try:
            if self.block and self.calls == 1 and not self.gate.wait(5):
                raise AssertionError("CPU barrier was not released")
            if stop_requested():
                self.stop_observed.set()
            if self.fail:
                raise RuntimeError("controlled native failure")
            if stop_requested():
                raise SimulationStopped()
            return self.outcome
        finally:
            self.finished.set()


async def wait_started(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5), "owned worker did not reach barrier"


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancel_signals_stop_joins_worker_and_next_call_uses_fresh_event(
    cancellations: int,
) -> None:
    engine = ControlledEngine()
    executor = OwnedSimulationExecutor(engine)
    config = SimulationConfig(simulations=4, batch_size=4)
    pending = asyncio.create_task(executor.simulate(engine.outcome.frame, config))
    try:
        await wait_started(engine.started)
        assert engine.thread_id != threading.get_ident()
        for _ in range(cancellations):
            pending.cancel()
            await asyncio.sleep(0)
        assert engine.predicate() and not pending.done() and not engine.finished.is_set()
        with pytest.raises(SimulationError, match="simulation_executor_busy"):
            await executor.simulate(engine.outcome.frame, config)
        with pytest.raises(SimulationError, match="simulation_executor_busy"):
            await executor.runtime()
    finally:
        engine.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert engine.finished.is_set() and engine.stop_observed.is_set()
    assert await executor.simulate(engine.outcome.frame, config) == engine.outcome
    assert not engine.predicate()


async def test_worker_failure_during_cancel_is_joined_and_exception_retrieved() -> None:
    engine = ControlledEngine(fail=True)
    executor = OwnedSimulationExecutor(engine)
    loop = asyncio.get_running_loop()
    prior = loop.get_exception_handler()
    unobserved: list[str] = []
    loop.set_exception_handler(
        lambda _loop, context: unobserved.append(str(context.get("message")))
    )
    pending = asyncio.create_task(
        executor.simulate(engine.outcome.frame, SimulationConfig(simulations=4, batch_size=4))
    )
    try:
        await wait_started(engine.started)
        pending.cancel()
        await asyncio.sleep(0)
        engine.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0)
        assert engine.finished.is_set() and unobserved == []
        engine.fail = False
        assert (
            await executor.simulate(
                engine.outcome.frame, SimulationConfig(simulations=4, batch_size=4)
            )
            == engine.outcome
        )
    finally:
        engine.gate.set()
        loop.set_exception_handler(prior)


async def test_runtime_capture_is_owned_joined_and_exclusive() -> None:
    engine = ControlledEngine(block=False, block_runtime=True)
    executor = OwnedSimulationExecutor(engine)
    pending = asyncio.create_task(executor.runtime())
    try:
        await wait_started(engine.runtime_started)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not engine.runtime_finished.is_set()
        with pytest.raises(SimulationError, match="simulation_executor_busy"):
            await executor.simulate(
                engine.outcome.frame, SimulationConfig(simulations=4, batch_size=4)
            )
    finally:
        engine.runtime_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert engine.runtime_finished.is_set()
    assert (await executor.runtime()).numpy_version == "2.5.2"


async def test_non_cancelled_worker_failure_releases_executor() -> None:
    engine = ControlledEngine(block=False, fail=True)
    executor = OwnedSimulationExecutor(engine)
    with pytest.raises(RuntimeError, match="controlled native failure"):
        await executor.simulate(engine.outcome.frame, SimulationConfig(simulations=4, batch_size=4))
    engine.fail = False
    assert (
        await executor.simulate(engine.outcome.frame, SimulationConfig(simulations=4, batch_size=4))
        == engine.outcome
    )


async def test_owned_stop_callback_is_not_called_on_success() -> None:
    signals: list[str] = []
    assert await run_owned_io(lambda: 7, on_cancel=lambda: signals.append("stop")) == 7
    assert signals == []


async def test_callback_failure_does_not_skip_join_or_replace_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started, gate, finished = threading.Event(), threading.Event(), threading.Event()

    def operation() -> int:
        started.set()
        try:
            if not gate.wait(5):
                raise AssertionError("owned I/O barrier was not released")
            return 7
        finally:
            finished.set()

    def invalid_callback() -> None:
        raise RuntimeError("private callback details")

    pending = asyncio.create_task(run_owned_io(operation, on_cancel=invalid_callback))
    try:
        await wait_started(started)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not finished.is_set()
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set()
    assert "owned_io_cancel_callback_failed" in caplog.text
    assert "private callback details" not in caplog.text
