import os
from collections.abc import Callable

UrlGetter = Callable[[], str]


def postgres_url() -> str:
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://nba:nba@localhost:5432/nba",
    )


def clickhouse_url() -> str:
    return os.getenv("TEST_CLICKHOUSE_URL", "http://localhost:8123")


def redis_url() -> str:
    return os.getenv("TEST_REDIS_URL", "redis://localhost:6379/0")


def api_url() -> str:
    return os.getenv("TEST_API_URL", "http://localhost:8000")
