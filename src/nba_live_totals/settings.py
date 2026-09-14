import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import (
    AfterValidator,
    AnyUrl,
    Field,
    HttpUrl,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    RedisDsn,
    SecretStr,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_POSSESSION_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


def _validate_possession_version(value: str) -> str:
    if _POSSESSION_VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("possession version must be a bounded lowercase safe identifier")
    return value


PossessionVersion = Annotated[str, AfterValidator(_validate_possession_version)]
PossessionLeaseSeconds = Annotated[int, Field(ge=1, le=86_400)]
PossessionConcurrency = Annotated[int, Field(ge=1, le=32)]
FinitePositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    app_env: str = "local"
    log_level: LogLevel = LogLevel.INFO
    database_url: AnyUrl = AnyUrl("postgresql+asyncpg://nba:nba@postgres:5432/nba")
    clickhouse_url: HttpUrl = HttpUrl("http://clickhouse:8123")
    clickhouse_user: str = "default"
    clickhouse_password: SecretStr = SecretStr("")
    redis_url: RedisDsn = RedisDsn("redis://redis:6379/0")
    healthcheck_timeout_seconds: PositiveFloat = 2.0
    nba_api_timeout_seconds: PositiveFloat = 15.0
    nba_api_max_concurrency: PositiveInt = 1
    nba_api_min_interval_seconds: NonNegativeFloat = 0.75
    nba_api_max_attempts: PositiveInt = 4
    nba_api_backoff_base_seconds: PositiveFloat = 1.0
    nba_api_backoff_max_seconds: PositiveFloat = 15.0
    nba_api_resource_lease_seconds: PositiveInt = 600
    possession_resource_lease_seconds: PossessionLeaseSeconds = 300
    possession_build_max_concurrency: PossessionConcurrency = 4
    clickhouse_operation_timeout_seconds: FinitePositiveFloat = 30.0
    possession_normalizer_version: PossessionVersion = "nba-pbp-v2"
    possession_parser_version: PossessionVersion = "analytical-v1"
    possession_rules_version: PossessionVersion = "nba-2025-v1"
    possession_output_schema_version: PossessionVersion = "possessions-v1"
    live_poll_interval_seconds: PositiveFloat = 3.0
    monte_carlo_simulations: PositiveInt = 10_000
    model_path: Path = Path("/app/models")

    @model_validator(mode="after")
    def validate_nba_backoff(self) -> "Settings":
        if self.nba_api_backoff_max_seconds < self.nba_api_backoff_base_seconds:
            raise ValueError("NBA API backoff max must be greater than or equal to base")
        return self
