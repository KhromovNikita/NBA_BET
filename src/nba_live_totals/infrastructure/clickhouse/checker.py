import httpx


class ClickHouseChecker:
    name = "clickhouse"

    def __init__(self, client: httpx.AsyncClient, user: str, password: str) -> None:
        self._client = client
        self._headers = {
            "X-ClickHouse-User": user,
            "X-ClickHouse-Key": password,
        }

    async def check(self) -> None:
        response = await self._client.get("/ping", headers=self._headers)
        response.raise_for_status()
        if response.text.strip() != "Ok.":
            raise RuntimeError("unexpected ClickHouse ping response")
