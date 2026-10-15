"""Cancellation never leaves an owned I/O thread using files being cleaned up."""

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


async def run_owned_io[T](
    operation: Callable[[], T],
    *,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        await asyncio.wait({worker})
        return worker.result()
    except asyncio.CancelledError:
        if on_cancel is not None:
            try:
                on_cancel()
            except BaseException as error:
                logger.error(
                    "owned_io_cancel_callback_failed",
                    extra={
                        "event": "owned_io_cancel_callback_failed",
                        "error_type": type(error).__name__[:64],
                    },
                )
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
