from redis.asyncio import Redis


class RedisChecker:
    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> None:
        if not await self._client.ping():
            raise RuntimeError("Redis ping returned a false result")
