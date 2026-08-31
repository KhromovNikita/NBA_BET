import pytest

from tests import runtime_config


@pytest.mark.parametrize(
    ("name", "getter", "default", "override"),
    [
        (
            "TEST_DATABASE_URL",
            runtime_config.postgres_url,
            "postgresql+asyncpg://nba:nba@localhost:5432/nba",
            "postgresql+asyncpg://nba:nba@localhost:15432/nba",
        ),
        (
            "TEST_CLICKHOUSE_URL",
            runtime_config.clickhouse_url,
            "http://localhost:8123",
            "http://localhost:18123",
        ),
        (
            "TEST_REDIS_URL",
            runtime_config.redis_url,
            "redis://localhost:6379/0",
            "redis://localhost:16379/0",
        ),
        (
            "TEST_API_URL",
            runtime_config.api_url,
            "http://localhost:8000",
            "http://localhost:18000",
        ),
    ],
)
def test_runtime_urls_have_local_defaults_and_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    getter: runtime_config.UrlGetter,
    default: str,
    override: str,
) -> None:
    monkeypatch.delenv(name, raising=False)
    assert getter() == default

    monkeypatch.setenv(name, override)
    assert getter() == override
