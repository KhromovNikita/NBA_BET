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


@pytest.mark.parametrize(
    ("name", "getter", "remote_url"),
    [
        (
            "TEST_DATABASE_URL",
            runtime_config.postgres_url,
            "postgresql+asyncpg://nba:nba@prod.example:5432/nba",
        ),
        ("TEST_CLICKHOUSE_URL", runtime_config.clickhouse_url, "https://prod.example"),
        ("TEST_REDIS_URL", runtime_config.redis_url, "redis://prod.example:6379/0"),
        ("TEST_API_URL", runtime_config.api_url, "https://prod.example"),
    ],
)
def test_runtime_urls_reject_non_loopback_targets_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    getter: runtime_config.UrlGetter,
    remote_url: str,
) -> None:
    monkeypatch.setenv(name, remote_url)
    monkeypatch.delenv("ALLOW_REMOTE_TEST_TARGETS", raising=False)

    with pytest.raises(ValueError, match="loopback"):
        getter()


def test_remote_runtime_target_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_url = "postgresql+asyncpg://nba:nba@ci-db.example:5432/nba"
    monkeypatch.setenv("TEST_DATABASE_URL", remote_url)
    monkeypatch.setenv("ALLOW_REMOTE_TEST_TARGETS", "1")

    assert runtime_config.postgres_url() == remote_url
