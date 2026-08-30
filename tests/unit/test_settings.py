from pathlib import Path

import pytest
from pydantic import ValidationError

from nba_live_totals.settings import LogLevel, Settings


def test_settings_use_documented_local_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_env == "local"
    assert settings.log_level is LogLevel.INFO
    assert str(settings.database_url) == "postgresql+asyncpg://nba:nba@postgres:5432/nba"
    assert str(settings.clickhouse_url) == "http://clickhouse:8123/"
    assert settings.clickhouse_user == "default"
    assert settings.clickhouse_password.get_secret_value() == ""
    assert str(settings.redis_url) == "redis://redis:6379/0"
    assert settings.healthcheck_timeout_seconds == 2.0
    assert settings.nba_api_timeout_seconds == 15.0
    assert settings.live_poll_interval_seconds == 3.0
    assert settings.monte_carlo_simulations == 10_000
    assert settings.model_path == Path("/app/models")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("HEALTHCHECK_TIMEOUT_SECONDS", "0"),
        ("NBA_API_TIMEOUT_SECONDS", "-1"),
        ("LIVE_POLL_INTERVAL_SECONDS", "0"),
        ("MONTE_CARLO_SIMULATIONS", "0"),
    ],
)
def test_settings_reject_non_positive_operational_values(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv(field, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        setattr(settings, "app_env", "production")  # noqa: B010
