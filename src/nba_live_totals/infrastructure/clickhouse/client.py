from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast

import httpx


class ClickHouseError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClickHouseClient:
    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        password: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        options: dict[str, object] = {
            "base_url": base_url.rstrip("/"),
            "headers": {
                "X-ClickHouse-User": user,
                "X-ClickHouse-Key": password,
            },
            "timeout": timeout_seconds,
            "trust_env": False,
        }
        if transport is not None:
            options["transport"] = transport
        self._client = httpx.AsyncClient(**options)  # type: ignore[arg-type]

    @property
    def timeout(self) -> httpx.Timeout:
        return self._client.timeout

    async def execute(self, statement: str) -> None:
        await self._post(statement.encode("utf-8"))

    async def query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str | int] | None = None,
    ) -> list[dict[str, object]]:
        query = statement.rstrip().rstrip(";")
        if re.search(r"\bFORMAT\s+JSONEachRow\s*$", query, re.IGNORECASE) is None:
            query = f"{query} FORMAT JSONEachRow"
        query_parameters = {
            f"param_{name}": str(value) for name, value in (parameters or {}).items()
        }
        response = await self._post(query.encode("utf-8"), parameters=query_parameters)
        try:
            rows = [json.loads(line) for line in response.text.splitlines() if line]
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClickHouseError("clickhouse_invalid_response") from None
        if any(type(row) is not dict for row in rows):
            raise ClickHouseError("clickhouse_invalid_response")
        return cast(list[dict[str, object]], rows)

    async def insert_json_each_row(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table) is None:
            raise ValueError("table must be a qualified ClickHouse identifier")
        try:
            encoded_rows = "".join(
                f"{json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))}\n"
                for row in rows
            )
        except (TypeError, ValueError):
            raise ClickHouseError("clickhouse_invalid_payload") from None
        payload = f"INSERT INTO {table} FORMAT JSONEachRow\n{encoded_rows}".encode()
        await self._post(payload)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(
        self,
        content: bytes,
        *,
        parameters: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.post("/", content=content, params=parameters)
        except httpx.TimeoutException:
            raise ClickHouseError("clickhouse_timeout") from None
        except httpx.RequestError:
            raise ClickHouseError("clickhouse_unavailable") from None
        if response.is_error:
            raise ClickHouseError("clickhouse_http_error")
        return response


__all__ = ["ClickHouseClient", "ClickHouseError"]
