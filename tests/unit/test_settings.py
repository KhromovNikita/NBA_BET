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
    assert settings.nba_api_max_concurrency == 1
    assert settings.nba_api_min_interval_seconds == 0.75
    assert settings.nba_api_max_attempts == 4
    assert settings.nba_api_backoff_base_seconds == 1.0
    assert settings.nba_api_backoff_max_seconds == 15.0
    assert settings.nba_api_resource_lease_seconds == 600
    assert settings.possession_resource_lease_seconds == 300
    assert settings.possession_build_max_concurrency == 4
    assert settings.clickhouse_operation_timeout_seconds == 30.0
    assert settings.possession_normalizer_version == "nba-pbp-v2"
    assert settings.possession_parser_version == "analytical-v1"
    assert settings.possession_rules_version == "nba-2025-v1"
    assert settings.possession_output_schema_version == "possessions-v1"
    assert settings.lineup_normalizer_version == "nba-pbp-v3-lineup-v1"
    assert settings.lineup_reconstructor_version == "causal-lineup-v1"
    assert settings.lineup_rules_version == "nba-5x5-v1"
    assert settings.lineup_output_schema_version == "lineup-segments-v1"
    assert settings.lineup_build_max_concurrency == 4
    assert settings.lineup_claim_lease_seconds == 300
    assert settings.live_poll_interval_seconds == 3.0
    assert settings.monte_carlo_simulations == 10_000
    assert settings.model_path == Path("/app/models")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("HEALTHCHECK_TIMEOUT_SECONDS", "0"),
        ("NBA_API_TIMEOUT_SECONDS", "-1"),
        ("NBA_API_MAX_CONCURRENCY", "0"),
        ("NBA_API_MAX_ATTEMPTS", "0"),
        ("NBA_API_BACKOFF_BASE_SECONDS", "0"),
        ("NBA_API_BACKOFF_MAX_SECONDS", "0"),
        ("NBA_API_RESOURCE_LEASE_SECONDS", "0"),
        ("POSSESSION_RESOURCE_LEASE_SECONDS", "0"),
        ("LINEUP_CLAIM_LEASE_SECONDS", "0"),
        ("LINEUP_CLAIM_LEASE_SECONDS", "86401"),
        ("LINEUP_BUILD_MAX_CONCURRENCY", "0"),
        ("LINEUP_BUILD_MAX_CONCURRENCY", "33"),
        ("POSSESSION_RESOURCE_LEASE_SECONDS", "86401"),
        ("POSSESSION_BUILD_MAX_CONCURRENCY", "0"),
        ("POSSESSION_BUILD_MAX_CONCURRENCY", "33"),
        ("CLICKHOUSE_OPERATION_TIMEOUT_SECONDS", "0"),
        ("CLICKHOUSE_OPERATION_TIMEOUT_SECONDS", "inf"),
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


def test_nba_api_min_interval_accepts_zero_and_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NBA_API_MIN_INTERVAL_SECONDS", "0")

    assert Settings(_env_file=None).nba_api_min_interval_seconds == 0.0  # type: ignore[call-arg]

    monkeypatch.setenv("NBA_API_MIN_INTERVAL_SECONDS", "-0.1")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_backoff_max_cannot_be_less_than_base() -> None:
    with pytest.raises(ValidationError, match="backoff max"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            nba_api_backoff_base_seconds=2.0,
            nba_api_backoff_max_seconds=1.0,
        )


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        setattr(settings, "app_env", "production")  # noqa: B010


@pytest.mark.parametrize(
    "field",
    [
        "POSSESSION_NORMALIZER_VERSION",
        "LINEUP_NORMALIZER_VERSION",
        "LINEUP_RECONSTRUCTOR_VERSION",
        "LINEUP_RULES_VERSION",
        "LINEUP_OUTPUT_SCHEMA_VERSION",
        "POSSESSION_PARSER_VERSION",
        "POSSESSION_RULES_VERSION",
        "POSSESSION_OUTPUT_SCHEMA_VERSION",
    ],
)
@pytest.mark.parametrize("value", ["", " leading", "trailing ", "UPPER", "valid\n", "a" * 129])
def test_possession_versions_are_safe_untrimmed_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv(field, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
