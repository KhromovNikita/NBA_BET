import os
from collections.abc import Callable
from ipaddress import ip_address
from urllib.parse import urlsplit

UrlGetter = Callable[[], str]


def postgres_url() -> str:
    return _local_test_url(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://nba:nba@localhost:5432/nba",
    )


def clickhouse_url() -> str:
    return _local_test_url("TEST_CLICKHOUSE_URL", "http://localhost:8123")


def redis_url() -> str:
    return _local_test_url("TEST_REDIS_URL", "redis://localhost:6379/0")


def api_url() -> str:
    return _local_test_url("TEST_API_URL", "http://localhost:8000")


def destructive_clickhouse_migrations_enabled() -> bool:
    return os.getenv("TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS") == "1"


def _local_test_url(name: str, default: str) -> str:
    value = os.getenv(name, default)
    if os.getenv("ALLOW_REMOTE_TEST_TARGETS") == "1":
        return value

    hostname = urlsplit(value).hostname
    is_loopback = hostname is not None and (
        hostname.casefold() == "localhost" or _is_loopback_ip(hostname)
    )
    if not is_loopback:
        raise ValueError(
            f"{name} must target loopback; set ALLOW_REMOTE_TEST_TARGETS=1 "
            "only for an isolated disposable test environment"
        )
    return value


def _is_loopback_ip(hostname: str) -> bool:
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
