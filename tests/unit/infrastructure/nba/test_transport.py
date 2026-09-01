import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import pairwise
from typing import ClassVar, cast

import pytest
import requests

from nba_live_totals.domain import JsonObject
from nba_live_totals.infrastructure.nba.errors import (
    EmptyProviderResponseError,
    InvalidProviderJsonError,
    ProviderConnectionError,
    ProviderError,
    ProviderSchemaError,
    ProviderTimeoutError,
    UpstreamHttpError,
)
from nba_live_totals.infrastructure.nba.transport import NBAApiTransport
from nba_live_totals.settings import Settings


class StubSender:
    def __init__(self, outcomes: list[JsonObject | BaseException]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    def __call__(self, endpoint: str, parameters: JsonObject, timeout_seconds: float) -> JsonObject:
        del endpoint, parameters, timeout_seconds
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_transport(
    *,
    sender: Callable[[str, JsonObject, float], JsonObject],
    sleep: Callable[[float], object] | None = None,
    sync_sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    jitter: Callable[[], float] = lambda: 0.0,
    max_concurrency: int = 1,
    min_interval_seconds: float = 0.0,
    max_attempts: int = 4,
    backoff_base_seconds: float = 1.0,
    backoff_max_seconds: float = 15.0,
) -> NBAApiTransport:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        nba_api_max_concurrency=max_concurrency,
        nba_api_min_interval_seconds=min_interval_seconds,
        nba_api_max_attempts=max_attempts,
        nba_api_backoff_base_seconds=backoff_base_seconds,
        nba_api_backoff_max_seconds=backoff_max_seconds,
    )
    kwargs: dict[str, object] = {"sender": sender, "jitter": jitter}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if sync_sleep is not None:
        kwargs["sync_sleep"] = sync_sleep
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return NBAApiTransport(settings, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transport_retries_transient_error_then_returns_json() -> None:
    sender = StubSender([UpstreamHttpError(429), {"ok": True}])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = make_transport(sender=sender, sleep=record_sleep)

    assert await transport.get("scheduleleaguev2", {"Season": "2025-26"}) == {"ok": True}
    assert sender.calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_transport_does_not_retry_schema_or_parent_cancellation() -> None:
    sender = StubSender([ProviderSchemaError("schema_mismatch")])
    transport = make_transport(sender=sender)

    with pytest.raises(ProviderSchemaError):
        await transport.get("x", {})
    assert sender.calls == 1

    cancelled = StubSender([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await make_transport(sender=cancelled).get("x", {})
    assert cancelled.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        ProviderTimeoutError(),
        ProviderConnectionError(),
        UpstreamHttpError(429),
        UpstreamHttpError(503),
        EmptyProviderResponseError(),
        InvalidProviderJsonError(),
    ],
)
@pytest.mark.asyncio
async def test_transport_retries_only_enumerated_transport_errors(error: ProviderError) -> None:
    sender = StubSender([error, error])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(type(error)):
        await make_transport(sender=sender, sleep=record_sleep, max_attempts=2).get("x", {})

    assert sender.calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_transport_does_not_retry_unclassified_failure() -> None:
    sender = StubSender([ValueError("programming bug")])

    with pytest.raises(ValueError, match="programming bug"):
        await make_transport(sender=sender).get("x", {})

    assert sender.calls == 1


@pytest.mark.asyncio
async def test_transport_caps_exponential_backoff_and_jitter() -> None:
    sender = StubSender(
        [
            ProviderTimeoutError(),
            ProviderTimeoutError(),
            ProviderTimeoutError(),
            ProviderTimeoutError(),
            {"ok": True},
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = make_transport(
        sender=sender,
        sleep=record_sleep,
        jitter=lambda: 0.75,
        max_attempts=5,
        backoff_base_seconds=10.0,
        backoff_max_seconds=15.0,
    )

    assert await transport.get("x", {}) == {"ok": True}
    assert sleeps == [10.75, 15.0, 15.0, 15.0]


@pytest.mark.asyncio
async def test_transport_spaces_actual_sender_entries_with_backlog_reordering_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    clock_lock = threading.Lock()
    sync_sleeps: list[float] = []
    backoff_sleeps: list[float] = []
    starts: list[tuple[str, float]] = []
    attempts: dict[str, int] = {}

    def monotonic() -> float:
        with clock_lock:
            return now

    def advance_sync(delay: float) -> None:
        nonlocal now
        with clock_lock:
            sync_sleeps.append(delay)
            now += delay

    async def record_backoff(delay: float) -> None:
        backoff_sleeps.append(delay)

    def sender(endpoint: str, parameters: JsonObject, timeout_seconds: float) -> JsonObject:
        del parameters, timeout_seconds
        starts.append((endpoint, monotonic()))
        attempts[endpoint] = attempts.get(endpoint, 0) + 1
        if endpoint == "retry" and attempts[endpoint] == 1:
            raise ProviderTimeoutError
        return {"endpoint": endpoint}

    executor = ThreadPoolExecutor(max_workers=1)
    backlog_started = threading.Event()
    release_backlog = threading.Event()

    def occupy_only_worker() -> None:
        backlog_started.set()
        release_backlog.wait(timeout=2.0)

    backlog = executor.submit(occupy_only_worker)
    assert backlog_started.wait(timeout=1.0)

    loop = asyncio.get_running_loop()
    allow_retry_enqueue = asyncio.Event()
    two_nonretry_enqueued = asyncio.Event()
    nonretry_enqueued = 0
    retry_enqueues = 0

    async def reordered_to_thread(function: Callable[..., JsonObject], *args: object) -> JsonObject:
        nonlocal nonretry_enqueued, retry_enqueues
        endpoint = cast(str, args[0])
        if endpoint == "retry" and retry_enqueues == 0:
            retry_enqueues += 1
            await allow_retry_enqueue.wait()
        elif endpoint != "retry":
            nonretry_enqueued += 1
            if nonretry_enqueued == 2:
                two_nonretry_enqueued.set()
        return await loop.run_in_executor(executor, partial(function, *args))

    monkeypatch.setattr(
        "nba_live_totals.infrastructure.nba.transport.asyncio.to_thread",
        reordered_to_thread,
    )

    transport = make_transport(
        sender=sender,
        sleep=record_backoff,
        sync_sleep=advance_sync,
        monotonic=monotonic,
        max_concurrency=3,
        min_interval_seconds=0.75,
        max_attempts=2,
    )
    tasks = [
        asyncio.create_task(transport.get("retry", {})),
        asyncio.create_task(transport.get("first", {})),
        asyncio.create_task(transport.get("second", {})),
    ]
    try:
        await asyncio.wait_for(two_nonretry_enqueued.wait(), timeout=1.0)
        allow_retry_enqueue.set()
        await asyncio.sleep(0)
        release_backlog.set()
        assert await asyncio.gather(*tasks) == [
            {"endpoint": "retry"},
            {"endpoint": "first"},
            {"endpoint": "second"},
        ]
        backlog.result(timeout=1.0)
    finally:
        release_backlog.set()
        executor.shutdown(wait=True)

    assert [endpoint for endpoint, _ in starts] == ["first", "second", "retry", "retry"]
    assert all(next_start - start >= 0.75 for (_, start), (_, next_start) in pairwise(starts))
    assert sync_sleeps == [0.75, 0.75, 0.75]
    assert backoff_sleeps == [1.0]


@pytest.mark.asyncio
async def test_transport_limits_sender_concurrency_and_runs_it_in_threads() -> None:
    lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    sender_threads: set[int] = set()

    def sender(endpoint: str, parameters: JsonObject, timeout_seconds: float) -> JsonObject:
        nonlocal active, maximum_active
        del endpoint, parameters, timeout_seconds
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            sender_threads.add(threading.get_ident())
            if active == 2:
                two_started.set()
        release.wait(timeout=2.0)
        with lock:
            active -= 1
        return {"ok": True}

    transport = make_transport(sender=sender, max_concurrency=2)
    event_loop_thread = threading.get_ident()
    tasks = [asyncio.create_task(transport.get("x", {})) for _ in range(3)]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(two_started.wait, 1.0), timeout=2.0)
        assert maximum_active == 2
    finally:
        release.set()

    assert await asyncio.gather(*tasks) == [{"ok": True}] * 3
    assert event_loop_thread not in sender_threads


@pytest.mark.asyncio
async def test_parent_cancellation_holds_capacity_until_blocking_worker_finishes() -> None:
    first_started = threading.Event()
    replacement_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def sender(endpoint: str, parameters: JsonObject, timeout_seconds: float) -> JsonObject:
        nonlocal calls
        del endpoint, parameters, timeout_seconds
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(timeout=2.0)
            raise ValueError("secret orphan worker failure")
        replacement_started.set()
        return {"ok": True}

    loop = asyncio.get_running_loop()
    orphan_contexts: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()

    def record_orphan(
        event_loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        del event_loop
        orphan_contexts.append(context)

    loop.set_exception_handler(record_orphan)
    transport = make_transport(sender=sender, max_concurrency=1)
    caller = asyncio.create_task(transport.get("x", {}))
    replacement: asyncio.Task[JsonObject] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(first_started.wait, 1.0), timeout=2.0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(caller), timeout=0.25)
        assert not release_first.is_set()

        replacement = asyncio.create_task(transport.get("x", {}))
        assert not await asyncio.wait_for(
            asyncio.to_thread(replacement_started.wait, 0.05), timeout=1.0
        )

        release_first.set()
        assert await asyncio.wait_for(replacement, timeout=2.0) == {"ok": True}
        await asyncio.sleep(0)
    finally:
        release_first.set()
        if replacement is not None and not replacement.done():
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
        loop.set_exception_handler(previous_handler)

    assert calls == 2
    assert orphan_contexts == []


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self._status_code = status_code
        self._payload = payload

    def get_dict(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeHTTP:
    response: object
    requests: ClassVar[list[tuple[str, JsonObject, float]]] = []

    def send_api_request(
        self, *, endpoint: str, parameters: JsonObject, timeout: float
    ) -> FakeResponse:
        self.requests.append((endpoint, parameters, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return cast(FakeResponse, self.response)


@pytest.mark.asyncio
async def test_default_sender_uses_raw_nba_http_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: JsonObject = {"leagueSchedule": {"gameDates": []}}
    FakeHTTP.response = FakeResponse(200, payload)
    FakeHTTP.requests = []
    monkeypatch.setattr("nba_live_totals.infrastructure.nba.transport.NBAStatsHTTP", FakeHTTP)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        nba_api_timeout_seconds=7.5,
        nba_api_min_interval_seconds=0.0,
    )

    result = await NBAApiTransport(settings).get("scheduleleaguev2", {"Season": "2025-26"})

    assert result is payload
    assert FakeHTTP.requests == [("scheduleleaguev2", {"Season": "2025-26"}, 7.5)]


@pytest.mark.parametrize(
    ("response", "error_type", "code"),
    [
        (requests.Timeout("secret response body"), ProviderTimeoutError, "network_timeout"),
        (requests.ConnectionError("secret proxy"), ProviderConnectionError, "network_connection"),
        (FakeResponse(429, AssertionError("must not parse")), UpstreamHttpError, "rate_limited"),
        (FakeResponse(502, AssertionError("must not parse")), UpstreamHttpError, "upstream_server"),
        (FakeResponse(200, {}), EmptyProviderResponseError, "empty_response"),
        (FakeResponse(200, []), EmptyProviderResponseError, "empty_response"),
        (
            FakeResponse(200, ValueError("secret invalid JSON body")),
            InvalidProviderJsonError,
            "invalid_json",
        ),
    ],
)
@pytest.mark.asyncio
async def test_default_sender_classifies_failures_without_leaking_provider_data(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse | BaseException,
    error_type: type[ProviderError],
    code: str,
) -> None:
    FakeHTTP.response = response
    monkeypatch.setattr("nba_live_totals.infrastructure.nba.transport.NBAStatsHTTP", FakeHTTP)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        nba_api_min_interval_seconds=0.0,
        nba_api_max_attempts=1,
    )

    with pytest.raises(error_type) as caught:
        await NBAApiTransport(settings).get("x", {})

    assert caught.value.code == code
    assert str(caught.value) == f"{error_type.__name__}(code={code})"
    assert "secret" not in str(caught.value)
    assert "secret" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(caught.value.args) == 1
    assert cast(str, caught.value.args[0]) == code


class ResponseWithoutUsableStatus:
    def __init__(self, status: object, *, include_attribute: bool) -> None:
        if include_attribute:
            self._status_code = status

    def get_dict(self) -> object:
        return {"ok": True}


@pytest.mark.parametrize(
    "response",
    [
        ResponseWithoutUsableStatus(None, include_attribute=False),
        ResponseWithoutUsableStatus("200", include_attribute=True),
    ],
)
@pytest.mark.asyncio
async def test_default_sender_safely_rejects_missing_or_non_integer_private_status(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    FakeHTTP.response = response
    monkeypatch.setattr("nba_live_totals.infrastructure.nba.transport.NBAStatsHTTP", FakeHTTP)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        nba_api_min_interval_seconds=0.0,
        nba_api_max_attempts=1,
    )

    with pytest.raises(ProviderSchemaError) as caught:
        await NBAApiTransport(settings).get("x", {})

    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"
