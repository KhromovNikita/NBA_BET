"""Async, rate-limited raw JSON transport for synchronous ``nba_api`` calls."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

import requests
from nba_api.stats.library.http import NBAStatsHTTP  # type: ignore[import-untyped]

from nba_live_totals.domain import JsonObject
from nba_live_totals.infrastructure.nba.errors import (
    EmptyProviderResponseError,
    InvalidProviderJsonError,
    ProviderConnectionError,
    ProviderSchemaError,
    ProviderTimeoutError,
    UpstreamHttpError,
)
from nba_live_totals.settings import Settings

type SyncSender = Callable[[str, JsonObject, float], JsonObject]
type AsyncSleep = Callable[[float], Awaitable[None]]


class _PinnedNBAResponse(Protocol):
    """The minimal response seam supplied by pinned ``nba_api`` 1.11.x."""

    _status_code: object

    def get_dict(self) -> object: ...


def _private_status_code(response: _PinnedNBAResponse) -> int:
    """Read the only status field exposed by pinned ``nba_api`` safely."""
    try:
        status_code = response._status_code
    except AttributeError:
        raise ProviderSchemaError("schema_mismatch") from None
    if type(status_code) is not int:
        raise ProviderSchemaError("schema_mismatch")
    return status_code


def _send_nba_api_request(
    endpoint: str,
    parameters: JsonObject,
    timeout_seconds: float,
) -> JsonObject:
    """Perform one raw request without invoking provider dataframe parsing."""
    try:
        response = cast(
            _PinnedNBAResponse,
            NBAStatsHTTP().send_api_request(
                endpoint=endpoint,
                parameters=parameters,
                timeout=timeout_seconds,
            ),
        )
    except requests.Timeout:
        raise ProviderTimeoutError from None
    except requests.ConnectionError:
        raise ProviderConnectionError from None

    status_code = _private_status_code(response)
    if status_code == 429:
        raise UpstreamHttpError(status_code)
    if status_code >= 500:
        raise UpstreamHttpError(status_code)

    try:
        payload = response.get_dict()
    except ValueError:
        raise InvalidProviderJsonError from None
    if not isinstance(payload, dict) or not payload:
        raise EmptyProviderResponseError
    return cast(JsonObject, payload)


_RETRYABLE_ERRORS = (
    ProviderTimeoutError,
    ProviderConnectionError,
    UpstreamHttpError,
    EmptyProviderResponseError,
    InvalidProviderJsonError,
)


class NBAApiTransport:
    """Run raw NBA requests with shared concurrency, spacing, and retry policy."""

    def __init__(
        self,
        settings: Settings,
        *,
        sender: SyncSender = _send_nba_api_request,
        sleep: AsyncSleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._sender = sender
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._timeout_seconds = float(settings.nba_api_timeout_seconds)
        self._min_interval_seconds = float(settings.nba_api_min_interval_seconds)
        self._max_attempts = int(settings.nba_api_max_attempts)
        self._backoff_base_seconds = float(settings.nba_api_backoff_base_seconds)
        self._backoff_max_seconds = float(settings.nba_api_backoff_max_seconds)
        self._semaphore = asyncio.Semaphore(int(settings.nba_api_max_concurrency))
        self._start_lock = asyncio.Lock()
        self._next_start = 0.0

    async def get(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        """Return a non-empty raw JSON object from one NBA endpoint."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._send_attempt(endpoint, parameters)
            except _RETRYABLE_ERRORS:
                if attempt == self._max_attempts:
                    raise
                await self._sleep(self._retry_delay(attempt))
        raise AssertionError("unreachable retry state")

    async def _send_attempt(self, endpoint: str, parameters: JsonObject) -> JsonObject:
        await self._semaphore.acquire()
        release_here = True
        worker: asyncio.Task[JsonObject] | None = None
        try:
            async with self._start_lock:
                await self._wait_for_start()
                self._next_start = self._monotonic() + self._min_interval_seconds
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self._sender,
                        endpoint,
                        parameters,
                        self._timeout_seconds,
                    )
                )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                release_here = False
                worker.add_done_callback(self._release_after_worker)
                raise
        except asyncio.CancelledError:
            if worker is not None and release_here:
                release_here = False
                worker.add_done_callback(self._release_after_worker)
            raise
        finally:
            if release_here:
                self._semaphore.release()

    def _release_after_worker(self, worker: asyncio.Task[JsonObject]) -> None:
        self._semaphore.release()
        if not worker.cancelled():
            worker.exception()

    async def _wait_for_start(self) -> None:
        delay = self._next_start - self._monotonic()
        while delay > 0:
            await self._sleep(delay)
            delay = self._next_start - self._monotonic()

    def _retry_delay(self, failed_attempt: int) -> float:
        exponential = self._backoff_base_seconds * 2.0 ** (failed_attempt - 1)
        return min(self._backoff_max_seconds, exponential + max(0.0, self._jitter()))
