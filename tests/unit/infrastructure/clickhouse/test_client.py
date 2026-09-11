from __future__ import annotations

import json
import traceback
from collections.abc import Callable

import httpx
import pytest

from nba_live_totals.infrastructure.clickhouse.client import (
    ClickHouseClient,
    ClickHouseError,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    timeout_seconds: float = 17.5,
) -> ClickHouseClient:
    return ClickHouseClient(
        base_url="http://clickhouse.invalid:8123",
        user="nba-user",
        password="nba-password",
        timeout_seconds=timeout_seconds,
        transport=httpx.MockTransport(handler),
    )


async def test_execute_sends_one_authenticated_parameterized_http_command() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="")

    client = _client(handler)
    try:
        await client.execute("CREATE DATABASE IF NOT EXISTS nba")
    finally:
        await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/"
    assert request.headers["X-ClickHouse-User"] == "nba-user"
    assert request.headers["X-ClickHouse-Key"] == "nba-password"
    assert request.content == b"CREATE DATABASE IF NOT EXISTS nba"
    assert request.extensions["timeout"] == {
        "connect": 17.5,
        "read": 17.5,
        "write": 17.5,
        "pool": 17.5,
    }


async def test_query_uses_clickhouse_parameters_instead_of_sql_interpolation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='{"name":"possessions_v1","rows":7}\n')

    client = _client(handler)
    try:
        rows = await client.query(
            "SELECT name, rows FROM system.tables WHERE database = {database:String}",
            parameters={"database": "nba'; DROP DATABASE nba; --", "limit": 7},
        )
    finally:
        await client.aclose()

    assert rows == [{"name": "possessions_v1", "rows": 7}]
    assert len(requests) == 1
    request = requests[0]
    assert request.content == (
        b"SELECT name, rows FROM system.tables WHERE database = {database:String} "
        b"FORMAT JSONEachRow"
    )
    assert "DROP" not in request.content.decode("utf-8")
    assert dict(request.url.params) == {
        "param_database": "nba'; DROP DATABASE nba; --",
        "param_limit": "7",
    }


async def test_query_does_not_append_a_second_format_clause() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='{"value":1}\n')

    client = _client(handler)
    try:
        assert await client.query("SELECT 1 AS value FORMAT JSONEachRow") == [{"value": 1}]
    finally:
        await client.aclose()

    assert requests[0].content == b"SELECT 1 AS value FORMAT JSONEachRow"


async def test_insert_serializes_all_rows_as_one_json_each_row_block() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    rows = [
        {"snapshot_id": "00000000-0000-0000-0000-000000000001", "possession_number": 1},
        {"snapshot_id": "00000000-0000-0000-0000-000000000001", "possession_number": 2},
    ]
    client = _client(handler)
    try:
        await client.insert_json_each_row("nba.possessions_v1", rows)
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert requests[0].content == (
        b"INSERT INTO nba.possessions_v1 FORMAT JSONEachRow\n"
        b'{"snapshot_id":"00000000-0000-0000-0000-000000000001","possession_number":1}\n'
        b'{"snapshot_id":"00000000-0000-0000-0000-000000000001","possession_number":2}\n'
    )


@pytest.mark.parametrize(
    "table",
    ["nba.possessions_v1 FORMAT Null", "nba.`possessions_v1`", "nba", "nba..possessions_v1"],
)
async def test_insert_rejects_unsafe_or_unqualified_table_names_without_http(
    table: str,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = _client(handler)
    try:
        with pytest.raises(ValueError, match="qualified ClickHouse identifier"):
            await client.insert_json_each_row(table, [{"value": 1}])
    finally:
        await client.aclose()

    assert called is False


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("http", "clickhouse_http_error"),
        ("timeout", "clickhouse_timeout"),
        ("connection", "clickhouse_unavailable"),
        ("response", "clickhouse_invalid_response"),
    ],
)
async def test_errors_expose_only_bounded_safe_codes(
    failure: str,
    expected_code: str,
) -> None:
    secrets = (
        "secret-response-body",
        "SELECT secret_sql",
        "url-secret",
        "nba-password",
        "secret-payload",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(500, text=secrets[0])
        if failure == "timeout":
            raise httpx.ReadTimeout(f"leaked {secrets[2]}", request=request)
        if failure == "connection":
            raise httpx.ConnectError(f"leaked {secrets[2]}", request=request)
        return httpx.Response(200, text="not-json secret-response-body")

    client = _client(handler)
    try:
        with pytest.raises(ClickHouseError) as captured:
            await client.query(secrets[1], parameters={"payload": secrets[4]})
    finally:
        await client.aclose()

    error = captured.value
    assert error.code == expected_code
    assert str(error) == expected_code
    assert repr(error) == f"ClickHouseError('{expected_code}')"
    assert len(error.code) <= 64
    assert error.code.replace("_", "").islower()
    serialized = json.dumps(
        {
            "str": str(error),
            "repr": repr(error),
            "args": error.args,
            "traceback": "".join(traceback.format_exception(error)),
        }
    )
    for secret in secrets:
        assert secret not in serialized


async def test_operation_timeout_is_independent_from_readiness_timeout() -> None:
    operation = ClickHouseClient(
        base_url="http://clickhouse.invalid:8123",
        user="nba",
        password="nba",
        timeout_seconds=31.25,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    readiness = httpx.AsyncClient(
        base_url="http://clickhouse.invalid:8123",
        timeout=1.75,
        trust_env=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    try:
        assert operation.timeout.read == 31.25
        assert readiness.timeout.read == 1.75
    finally:
        await operation.aclose()
        await readiness.aclose()


def test_owned_http_client_ignores_ambient_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_options: dict[str, object] = {}

    class RecordingAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            constructor_options.update(kwargs)

    monkeypatch.setattr(
        "nba_live_totals.infrastructure.clickhouse.client.httpx.AsyncClient",
        RecordingAsyncClient,
    )

    ClickHouseClient(
        base_url="http://clickhouse.invalid:8123",
        user="nba",
        password="secret",
        timeout_seconds=12.0,
    )

    assert constructor_options["base_url"] == "http://clickhouse.invalid:8123"
    assert constructor_options["timeout"] == 12.0
    assert constructor_options["trust_env"] is False
