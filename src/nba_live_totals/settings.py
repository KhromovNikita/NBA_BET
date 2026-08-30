from enum import StrEnum
from pathlib import Path

from pydantic import AnyUrl, HttpUrl, PositiveFloat, PositiveInt, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


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
    live_poll_interval_seconds: PositiveFloat = 3.0
    monte_carlo_simulations: PositiveInt = 10_000
    model_path: Path = Path("/app/models")
