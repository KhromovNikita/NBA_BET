"""Keep repeated SIGINTs inside one owned task and restore the caller's handler."""

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable
from types import FrameType


async def _invoke[T](command: Callable[[], Awaitable[T]]) -> T:
    return await command()


def run_owned_async[T](command: Callable[[], Awaitable[T]]) -> T:
    previous_handler = signal.getsignal(signal.SIGINT)
    installed_handler: Callable[[int, FrameType | None], None] | None = None
    try:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            task = loop.create_task(_invoke(command))

            def cancel_owned_task(signum: int, frame: FrameType | None) -> None:
                loop.call_soon_threadsafe(task.cancel)

            if (
                threading.current_thread() is threading.main_thread()
                and previous_handler is signal.default_int_handler
            ):
                signal.signal(signal.SIGINT, cancel_owned_task)
                installed_handler = cancel_owned_task
            return runner.run(task)
    finally:
        if installed_handler is not None and signal.getsignal(signal.SIGINT) is installed_handler:
            signal.signal(signal.SIGINT, previous_handler)
