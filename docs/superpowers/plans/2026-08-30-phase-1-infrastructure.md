# Phase 1 Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the reproducible FastAPI, configuration, health, storage-lifecycle, migration, observability, Docker Compose, and verification foundation defined for Phase 1.

**Architecture:** Implement one installable `nba_live_totals` package with a FastAPI composition root, application-level health contracts, and async infrastructure adapters for PostgreSQL, ClickHouse, and Redis. Keep runtime resources app-scoped, expose separate liveness/readiness contracts, and prove external integration against the real Compose services.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 async, asyncpg, httpx, redis-py asyncio, Alembic, pytest, pytest-asyncio, Ruff, mypy, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md`

## Global Constraints

- Python requirement is `>=3.12,<3.14`; the container base is `python:3.12.14-slim-bookworm`.
- Use strict typing, async I/O, dependency injection, and no process-global mutable business state.
- Domain/application code must not import FastAPI, SQLAlchemy, Redis, ClickHouse clients, or NBA provider code.
- `/health` performs no external I/O; `/health/ready` checks PostgreSQL, ClickHouse, and Redis concurrently.
- Dependency timeouts use `HEALTHCHECK_TIMEOUT_SECONDS`; failures never expose DSN, credentials, query text, exception messages, or stack traces in HTTP responses.
- PostgreSQL is `postgres:17.11-bookworm`, ClickHouse is `clickhouse:26.3.25.2-jammy`, and Redis is `redis:8.2.9-bookworm`.
- Phase 1 creates no games, players, possessions, predictions, ingestion, ML, or simulation implementation.
- Every behavior change follows red-green-refactor; configuration-only artifacts are validated by their native parsers.
- Run tests, Ruff, and mypy before each task commit; never stage `graphify-out/`.

---

### Task 1: Package toolchain and immutable settings

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `src/nba_live_totals/__init__.py`
- Create: `src/nba_live_totals/settings.py`
- Create: `tests/unit/test_settings.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `uv.lock` with `uv lock`

**Interfaces:**
- Consumes: environment variables listed in the design spec.
- Produces: `LogLevel(StrEnum)` and frozen `Settings(BaseSettings)` with validated URLs, positive timeouts/counts, and `Path` model storage.

- [ ] **Step 1: Create the package and tool configuration**

Create `pyproject.toml` with these project and tool contracts:

```toml
[project]
name = "nba-live-totals"
version = "0.1.0"
description = "Live NBA totals prediction backend"
readme = "README.md"
requires-python = ">=3.12,<3.14"
dependencies = [
    "alembic>=1.16,<2",
    "asyncpg>=0.31,<1",
    "fastapi>=0.141,<1",
    "httpx>=0.28,<1",
    "pydantic>=2.13,<3",
    "pydantic-settings>=2.15,<3",
    "redis>=8,<9",
    "sqlalchemy[asyncio]>=2.0,<3",
    "uvicorn[standard]>=0.35,<1",
]

[dependency-groups]
dev = [
    "mypy>=2.3,<3",
    "pytest>=9.1,<10",
    "pytest-asyncio>=1.4,<2",
    "ruff>=0.16,<1",
]

[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/nba_live_totals"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: requires the local Docker Compose data stores",
    "e2e: requires the built API and data-store containers",
]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["ASYNC", "B", "E", "F", "I", "RUF", "UP"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]
```

Create `.python-version` containing `3.12`, package marker files containing a module docstring, and `.gitignore` containing:

```gitignore
.env
.venv/
.mypy_cache/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
models/
graphify-out/
```

- [ ] **Step 2: Resolve and install the toolchain**

Run:

```bash
uv lock
uv sync --frozen
```

Expected: `uv.lock` is created and the Python 3.12 environment installs without resolution errors.

- [ ] **Step 3: Write the failing settings tests**

Create `tests/unit/test_settings.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from nba_live_totals.settings import LogLevel, Settings


def test_settings_use_documented_local_defaults() -> None:
    settings = Settings(_env_file=None)

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
        Settings(_env_file=None)


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValidationError):
        setattr(settings, "app_env", "production")
```

- [ ] **Step 4: Run the tests and verify the red state**

Run:

```bash
uv run pytest tests/unit/test_settings.py -v
```

Expected: collection fails with `ModuleNotFoundError` for `nba_live_totals.settings`.

- [ ] **Step 5: Implement the minimal settings model**

Create `src/nba_live_totals/settings.py`:

```python
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
```

- [ ] **Step 6: Verify settings, lint, and typing**

Run:

```bash
uv run pytest tests/unit/test_settings.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: all commands pass with no warnings or unused ignores.

- [ ] **Step 7: Commit the package foundation**

```bash
git add pyproject.toml uv.lock .python-version .gitignore src tests
git commit -m "build: initialize typed Python project"
```

---

### Task 2: Concurrent health application service

**Files:**
- Create: `src/nba_live_totals/application/__init__.py`
- Create: `src/nba_live_totals/application/health.py`
- Create: `tests/unit/test_health_service.py`

**Interfaces:**
- Consumes: `DependencyChecker.name: str` and `await DependencyChecker.check() -> None`.
- Produces: `DependencyState`, `ReadinessState`, `DependencyStatus`, `ReadinessStatus`, `DependencyChecker`, and `HealthService.check_readiness() -> ReadinessStatus`.

- [ ] **Step 1: Write the failing all-up and partial-down tests**

Create `tests/unit/test_health_service.py` with literal expected states:

```python
import asyncio

from nba_live_totals.application.health import (
    DependencyState,
    HealthService,
    ReadinessState,
)


class SuccessfulChecker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self) -> None:
        await asyncio.sleep(0)


class FailingChecker:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self._message = message

    async def check(self) -> None:
        raise RuntimeError(self._message)


async def test_readiness_is_ready_when_all_dependencies_are_up() -> None:
    service = HealthService(
        checkers=(SuccessfulChecker("postgres"), SuccessfulChecker("redis")),
        timeout_seconds=0.1,
    )

    result = await service.check_readiness()

    assert result.status is ReadinessState.READY
    assert list(result.checks) == ["postgres", "redis"]
    assert result.checks["postgres"].status is DependencyState.UP
    assert result.checks["redis"].status is DependencyState.UP


async def test_readiness_collects_all_results_without_exposing_errors() -> None:
    service = HealthService(
        checkers=(
            FailingChecker("postgres", "postgresql://user:secret@host/db"),
            SuccessfulChecker("redis"),
        ),
        timeout_seconds=0.1,
    )

    result = await service.check_readiness()
    payload = result.model_dump(mode="json")

    assert result.status is ReadinessState.NOT_READY
    assert result.checks["postgres"].status is DependencyState.DOWN
    assert result.checks["redis"].status is DependencyState.UP
    assert "secret" not in str(payload)
    assert "error" not in payload["checks"]["postgres"]
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run:

```bash
uv run pytest tests/unit/test_health_service.py -v
```

Expected: collection fails because `nba_live_totals.application.health` does not exist.

- [ ] **Step 3: Implement health models, protocol, and aggregation**

Create `src/nba_live_totals/application/health.py`:

```python
import asyncio
import logging
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class DependencyState(StrEnum):
    UP = "up"
    DOWN = "down"


class ReadinessState(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class DependencyStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DependencyState
    latency_ms: float


class ReadinessStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ReadinessState
    checks: dict[str, DependencyStatus]


class DependencyChecker(Protocol):
    name: str

    async def check(self) -> None:
        raise NotImplementedError


class HealthService:
    def __init__(
        self,
        checkers: tuple[DependencyChecker, ...],
        timeout_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._checkers = checkers
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

    async def check_readiness(self) -> ReadinessStatus:
        statuses = await asyncio.gather(*(self._run(checker) for checker in self._checkers))
        checks = {
            checker.name: status for checker, status in zip(self._checkers, statuses, strict=True)
        }
        overall = (
            ReadinessState.READY
            if all(item.status is DependencyState.UP for item in statuses)
            else ReadinessState.NOT_READY
        )
        return ReadinessStatus(status=overall, checks=checks)

    async def _run(self, checker: DependencyChecker) -> DependencyStatus:
        started = perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await checker.check()
        except TimeoutError:
            status = DependencyState.DOWN
            self._log_failure(checker.name, "TimeoutError", started)
        except Exception as error:
            status = DependencyState.DOWN
            self._log_failure(checker.name, type(error).__name__, started)
        else:
            status = DependencyState.UP
        latency_ms = round((perf_counter() - started) * 1000, 1)
        return DependencyStatus(status=status, latency_ms=latency_ms)

    def _log_failure(self, dependency: str, error_type: str, started: float) -> None:
        self._logger.warning(
            "dependency_check_failed",
            extra={
                "dependency": dependency,
                "error_type": error_type,
                "latency_ms": round((perf_counter() - started) * 1000, 1),
            },
        )
```

- [ ] **Step 4: Add timeout and concurrency coverage**

Append tests that catch a missing timeout and sequential execution:

```python
class BlockingChecker:
    def __init__(self, name: str, started: set[str], release: asyncio.Event) -> None:
        self.name = name
        self._started = started
        self._release = release

    async def check(self) -> None:
        self._started.add(self.name)
        if len(self._started) == 3:
            self._release.set()
        await self._release.wait()


class NeverCompletesChecker:
    name = "clickhouse"

    async def check(self) -> None:
        await asyncio.Event().wait()


async def test_checks_start_concurrently() -> None:
    started: set[str] = set()
    release = asyncio.Event()
    service = HealthService(
        checkers=tuple(
            BlockingChecker(name, started, release) for name in ("postgres", "clickhouse", "redis")
        ),
        timeout_seconds=0.2,
    )

    result = await service.check_readiness()

    assert started == {"postgres", "clickhouse", "redis"}
    assert result.status is ReadinessState.READY


async def test_timeout_marks_only_that_dependency_down() -> None:
    service = HealthService(
        checkers=(NeverCompletesChecker(), SuccessfulChecker("redis")),
        timeout_seconds=0.01,
    )

    result = await service.check_readiness()

    assert result.status is ReadinessState.NOT_READY
    assert result.checks["clickhouse"].status is DependencyState.DOWN
    assert result.checks["redis"].status is DependencyState.UP
```

- [ ] **Step 5: Verify the service and full unit suite**

Run:

```bash
uv run pytest tests/unit/test_health_service.py -v
uv run pytest tests/unit -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: all checks pass; cancelling the parent task is not swallowed because `CancelledError` is not caught.

- [ ] **Step 6: Commit the application health service**

```bash
git add src/nba_live_totals/application tests/unit/test_health_service.py
git commit -m "feat: add concurrent readiness service"
```

---

### Task 3: Local PostgreSQL, ClickHouse, and Redis services

**Files:**
- Create: `compose.yaml`
- Create: `.env.example`

**Interfaces:**
- Consumes: Docker Compose and named local ports `5432`, `8123`, and `6379`.
- Produces: healthy `postgres`, `clickhouse`, and `redis` services plus host-side test URLs.

- [ ] **Step 1: Create the Compose storage services**

Create `compose.yaml`:

```yaml
name: nba-live-totals

services:
  postgres:
    image: postgres:17.11-bookworm
    environment:
      POSTGRES_DB: nba
      POSTGRES_USER: nba
      POSTGRES_PASSWORD: nba
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U nba -d nba"]
      interval: 2s
      timeout: 3s
      retries: 20

  clickhouse:
    image: clickhouse:26.3.25.2-jammy
    environment:
      CLICKHOUSE_DB: nba
      CLICKHOUSE_USER: nba
      CLICKHOUSE_PASSWORD: nba
      CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: "1"
    ports:
      - "8123:8123"
    volumes:
      - clickhouse_data:/var/lib/clickhouse
    healthcheck:
      test: ["CMD-SHELL", "clickhouse-client --user nba --password nba --query 'SELECT 1'"]
      interval: 2s
      timeout: 3s
      retries: 30

  redis:
    image: redis:8.2.9-bookworm
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 20

volumes:
  postgres_data:
  clickhouse_data:
  redis_data:
```

Create `.env.example`:

```dotenv
APP_ENV=local
LOG_LEVEL=INFO
DATABASE_URL=postgresql+asyncpg://nba:nba@postgres:5432/nba
CLICKHOUSE_URL=http://clickhouse:8123
CLICKHOUSE_USER=nba
CLICKHOUSE_PASSWORD=nba
REDIS_URL=redis://redis:6379/0
HEALTHCHECK_TIMEOUT_SECONDS=2.0
NBA_API_TIMEOUT_SECONDS=15.0
LIVE_POLL_INTERVAL_SECONDS=3.0
MONTE_CARLO_SIMULATIONS=10000
MODEL_PATH=/app/models
```

- [ ] **Step 2: Validate the Compose model**

Run:

```bash
docker compose config --quiet
```

Expected: exit code `0` and no interpolation or schema errors.

- [ ] **Step 3: Start and verify the storage services**

Run:

```bash
docker compose up -d --wait postgres clickhouse redis
docker compose ps
```

Expected: all three services report `healthy`; no volume is recreated on a second `docker compose up -d`.

- [ ] **Step 4: Commit the local infrastructure**

```bash
git add compose.yaml .env.example
git commit -m "build: add local data stores"
```

---

### Task 4: Real async dependency adapters

**Files:**
- Create: `src/nba_live_totals/infrastructure/__init__.py`
- Create: `src/nba_live_totals/infrastructure/postgres.py`
- Create: `src/nba_live_totals/infrastructure/clickhouse.py`
- Create: `src/nba_live_totals/infrastructure/redis.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_dependency_adapters.py`

**Interfaces:**
- Consumes: SQLAlchemy `AsyncEngine`, `httpx.AsyncClient`, and `redis.asyncio.Redis`.
- Produces: `PostgresChecker`, `ClickHouseChecker`, and `RedisChecker`, each implementing `DependencyChecker.check() -> None`.

- [ ] **Step 1: Write integration tests against the real services**

Create `tests/integration/test_dependency_adapters.py`:

```python
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.infrastructure.clickhouse import ClickHouseChecker
from nba_live_totals.infrastructure.postgres import PostgresChecker
from nba_live_totals.infrastructure.redis import RedisChecker


pytestmark = pytest.mark.integration


async def test_postgres_checker_executes_real_query() -> None:
    engine = create_async_engine("postgresql+asyncpg://nba:nba@localhost:5432/nba")
    try:
        await PostgresChecker(engine).check()
    finally:
        await engine.dispose()


async def test_clickhouse_checker_uses_authenticated_ping() -> None:
    async with httpx.AsyncClient(base_url="http://localhost:8123") as client:
        await ClickHouseChecker(client, user="nba", password="nba").check()


async def test_redis_checker_uses_real_ping() -> None:
    client = Redis.from_url("redis://localhost:6379/0")
    try:
        await RedisChecker(client).check()
    finally:
        await client.aclose()
```

- [ ] **Step 2: Run the tests and verify the red state**

Run:

```bash
docker compose up -d --wait postgres clickhouse redis
uv run pytest tests/integration/test_dependency_adapters.py -v -m integration
```

Expected: collection fails because the three infrastructure modules do not exist.

- [ ] **Step 3: Implement the PostgreSQL adapter**

Create `src/nba_live_totals/infrastructure/postgres.py`:

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresChecker:
    name = "postgres"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
```

- [ ] **Step 4: Implement the ClickHouse adapter**

Create `src/nba_live_totals/infrastructure/clickhouse.py`:

```python
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
```

- [ ] **Step 5: Implement the Redis adapter**

Create `src/nba_live_totals/infrastructure/redis.py`:

```python
from redis.asyncio import Redis


class RedisChecker:
    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> None:
        if not await self._client.ping():
            raise RuntimeError("Redis ping returned a false result")
```

- [ ] **Step 6: Verify adapters against real services**

Run:

```bash
uv run pytest tests/integration/test_dependency_adapters.py -v -m integration
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: the three real checks pass and static verification is clean.

- [ ] **Step 7: Commit the dependency adapters**

```bash
git add src/nba_live_totals/infrastructure tests/integration
git commit -m "feat: add async dependency health adapters"
```

---

### Task 5: App-scoped resource lifecycle

**Files:**
- Create: `src/nba_live_totals/lifecycle.py`
- Create: `tests/unit/test_lifecycle.py`
- Create: `tests/integration/test_lifecycle.py`

**Interfaces:**
- Consumes: `Settings` and the three checker implementations.
- Produces: frozen `AppResources`, `create_resources(settings) -> AppResources`, `close_resources(resources) -> None`, and `create_lifespan(settings) -> Lifespan[FastAPI]`.

- [ ] **Step 1: Write the failing lifecycle integration test**

Create `tests/unit/test_lifecycle.py` to protect reverse-order cleanup when one closer fails:

```python
from collections.abc import Awaitable, Callable

from nba_live_totals.lifecycle import close_in_order


async def test_close_in_order_continues_after_one_failure() -> None:
    calls: list[str] = []

    async def broken_clickhouse_close() -> None:
        calls.append("clickhouse")
        raise RuntimeError("close failed")

    async def redis_close() -> None:
        calls.append("redis")

    async def postgres_close() -> None:
        calls.append("postgres")

    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("clickhouse", broken_clickhouse_close),
        ("redis", redis_close),
        ("postgres", postgres_close),
    )

    await close_in_order(closers)

    assert calls == ["clickhouse", "redis", "postgres"]
```

Create `tests/integration/test_lifecycle.py`:

```python
from nba_live_totals.application.health import ReadinessState
from nba_live_totals.lifecycle import close_resources, create_resources
from nba_live_totals.settings import Settings


async def test_resources_wire_real_dependencies_and_close_cleanly() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nba:nba@localhost:5432/nba",
        clickhouse_url="http://localhost:8123",
        clickhouse_user="nba",
        clickhouse_password="nba",
        redis_url="redis://localhost:6379/0",
    )
    resources = create_resources(settings)

    result = await resources.health_service.check_readiness()
    await close_resources(resources)

    assert result.status is ReadinessState.READY
    assert resources.clickhouse_client.is_closed
```

- [ ] **Step 2: Run the test and verify the red state**

Run:

```bash
uv run pytest tests/unit/test_lifecycle.py tests/integration/test_lifecycle.py -v
```

Expected: collection fails because `nba_live_totals.lifecycle` does not exist.

- [ ] **Step 3: Implement resource creation and resilient reverse-order shutdown**

Create `src/nba_live_totals/lifecycle.py`:

```python
import logging
from collections.abc import AsyncContextManager, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.health import HealthService
from nba_live_totals.infrastructure.clickhouse import ClickHouseChecker
from nba_live_totals.infrastructure.postgres import PostgresChecker
from nba_live_totals.infrastructure.redis import RedisChecker
from nba_live_totals.settings import Settings


@dataclass(frozen=True, slots=True)
class AppResources:
    postgres_engine: AsyncEngine
    clickhouse_client: httpx.AsyncClient
    redis_client: Redis
    health_service: HealthService


def create_resources(settings: Settings) -> AppResources:
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    redis_client = Redis.from_url(str(settings.redis_url))
    clickhouse_client = httpx.AsyncClient(
        base_url=str(settings.clickhouse_url).rstrip("/"),
        timeout=settings.healthcheck_timeout_seconds,
    )
    health_service = HealthService(
        checkers=(
            PostgresChecker(engine),
            ClickHouseChecker(
                clickhouse_client,
                user=settings.clickhouse_user,
                password=settings.clickhouse_password.get_secret_value(),
            ),
            RedisChecker(redis_client),
        ),
        timeout_seconds=settings.healthcheck_timeout_seconds,
    )
    return AppResources(engine, clickhouse_client, redis_client, health_service)


async def close_resources(resources: AppResources) -> None:
    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("clickhouse", resources.clickhouse_client.aclose),
        ("redis", resources.redis_client.aclose),
        ("postgres", resources.postgres_engine.dispose),
    )
    await close_in_order(closers)


async def close_in_order(
    closers: tuple[tuple[str, Callable[[], Awaitable[None]]], ...],
) -> None:
    for resource_name, closer in closers:
        await _close_one(resource_name, closer)


async def _close_one(name: str, close: Callable[[], Awaitable[None]]) -> None:
    try:
        await close()
    except Exception as error:
        logging.getLogger(__name__).error(
            "resource_close_failed",
            extra={"resource": name, "error_type": type(error).__name__},
        )


def create_lifespan(settings: Settings) -> Callable[[FastAPI], AsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resources = create_resources(settings)
        app.state.resources = resources
        try:
            yield
        finally:
            await close_resources(resources)

    return lifespan
```

- [ ] **Step 4: Verify lifecycle behavior and strict typing**

Run:

```bash
uv run pytest tests/integration/test_lifecycle.py -v -m integration
uv run pytest tests/unit/test_lifecycle.py -v
uv run pytest tests/unit -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: resources report ready, `httpx.AsyncClient.is_closed` is true after shutdown, and mypy finds no unawaited or `Any` close path.

- [ ] **Step 5: Commit lifecycle wiring**

```bash
git add src/nba_live_totals/lifecycle.py tests/unit/test_lifecycle.py tests/integration/test_lifecycle.py
git commit -m "feat: manage app-scoped async resources"
```

---

### Task 6: JSON logging and request correlation

**Files:**
- Create: `src/nba_live_totals/observability/__init__.py`
- Create: `src/nba_live_totals/observability/logging.py`
- Create: `src/nba_live_totals/api/__init__.py`
- Create: `src/nba_live_totals/api/request_id.py`
- Create: `tests/unit/test_observability.py`

**Interfaces:**
- Consumes: `Settings.app_env`, `Settings.log_level`, ASGI request headers.
- Produces: `JsonFormatter`, `configure_logging(settings)`, `get_request_id()`, `normalize_request_id(value)`, and `RequestIdMiddleware`.

- [ ] **Step 1: Write failing formatter and request-ID tests**

Create `tests/unit/test_observability.py`:

```python
import json
import logging
from uuid import UUID

from nba_live_totals.api.request_id import normalize_request_id
from nba_live_totals.observability.logging import JsonFormatter


def test_json_formatter_emits_required_fields() -> None:
    formatter = JsonFormatter(app_env="test")
    record = logging.LogRecord(
        name="nba.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="dependency_check_failed",
        args=(),
        exc_info=None,
    )
    setattr(record, "dependency", "postgres")
    setattr(record, "error_type", "TimeoutError")

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "nba.test"
    assert payload["message"] == "dependency_check_failed"
    assert payload["app_env"] == "test"
    assert payload["dependency"] == "postgres"
    assert payload["error_type"] == "TimeoutError"
    assert "timestamp" in payload


def test_valid_request_id_is_preserved() -> None:
    assert normalize_request_id("game-0022500123.trace_1") == "game-0022500123.trace_1"


def test_invalid_request_id_is_replaced_with_uuid() -> None:
    generated = normalize_request_id("contains spaces and 💥")

    assert UUID(generated).version == 4
```

- [ ] **Step 2: Run tests and verify the red state**

Run:

```bash
uv run pytest tests/unit/test_observability.py -v
```

Expected: collection fails because the observability and request-ID modules do not exist.

- [ ] **Step 3: Implement context-local request IDs and ASGI response propagation**

Create `src/nba_live_totals/api/request_id.py`:

```python
import re
from contextvars import ContextVar
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def normalize_request_id(value: str | None) -> str:
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def get_request_id() -> str | None:
    return _request_id.get()


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        raw = headers.get(b"x-request-id")
        request_id = normalize_request_id(raw.decode("ascii", errors="ignore") if raw else None)
        token = _request_id.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("ascii"))
                )
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)
```

- [ ] **Step 4: Implement structured JSON formatting**

Create `src/nba_live_totals/observability/logging.py` with an ISO-8601 UTC timestamp, the required fixed fields, request ID from `get_request_id()`, and a whitelist of structured extras (`dependency`, `error_type`, `latency_ms`, `resource`):

```python
import json
import logging
from datetime import UTC, datetime
from typing import Any

from nba_live_totals.api.request_id import get_request_id
from nba_live_totals.settings import Settings


class JsonFormatter(logging.Formatter):
    def __init__(self, app_env: str) -> None:
        super().__init__()
        self._app_env = app_env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app_env": self._app_env,
        }
        request_id = get_request_id()
        if request_id is not None:
            payload["request_id"] = request_id
        for field in ("dependency", "error_type", "latency_ms", "resource"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(settings.app_env))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.value)
```

- [ ] **Step 5: Verify observability behavior**

Run:

```bash
uv run pytest tests/unit/test_observability.py -v
uv run pytest tests/unit -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: formatter output parses as JSON, request IDs satisfy the exact allowlist, and context-local state is strictly typed.

- [ ] **Step 6: Commit observability primitives**

```bash
git add src/nba_live_totals/api src/nba_live_totals/observability tests/unit/test_observability.py
git commit -m "feat: add structured logging and request correlation"
```

---

### Task 7: FastAPI health contracts and exception boundary

**Files:**
- Create: `src/nba_live_totals/api/dependencies.py`
- Create: `src/nba_live_totals/api/routes/__init__.py`
- Create: `src/nba_live_totals/api/routes/health.py`
- Create: `src/nba_live_totals/api/app.py`
- Create: `src/nba_live_totals/main.py`
- Create: `tests/api/__init__.py`
- Create: `tests/api/test_health_api.py`

**Interfaces:**
- Consumes: `Settings`, `create_lifespan`, `RequestIdMiddleware`, `HealthService`.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`, `/health`, `/health/ready`, and safe HTTP `500` handling.

- [ ] **Step 1: Write failing API contract tests**

Create `tests/api/test_health_api.py`:

```python
import asyncio
from uuid import UUID

import httpx

from nba_live_totals.api.app import create_app
from nba_live_totals.api.dependencies import get_health_service
from nba_live_totals.application.health import HealthService
from nba_live_totals.settings import Settings


class SuccessfulChecker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self) -> None:
        return None


class FailingChecker:
    name = "postgres"

    async def check(self) -> None:
        raise RuntimeError("postgresql://user:secret@host/db")


def build_service(*checkers: SuccessfulChecker | FailingChecker) -> HealthService:
    return HealthService(checkers=checkers, timeout_seconds=0.1)


async def request(
    service: HealthService,
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_health_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


async def test_liveness_has_no_dependency_on_readiness() -> None:
    response = await request(build_service(FailingChecker()), "/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_returns_ordered_all_up_contract() -> None:
    response = await request(
        build_service(
            SuccessfulChecker("postgres"),
            SuccessfulChecker("clickhouse"),
            SuccessfulChecker("redis"),
        ),
        "/health/ready",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert list(response.json()["checks"]) == ["postgres", "clickhouse", "redis"]


async def test_readiness_returns_safe_503_contract() -> None:
    response = await request(
        build_service(
            FailingChecker(),
            SuccessfulChecker("clickhouse"),
            SuccessfulChecker("redis"),
        ),
        "/health/ready",
    )

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["postgres"]["status"] == "down"
    assert "secret" not in response.text


async def test_request_id_is_preserved_or_regenerated() -> None:
    service = build_service(SuccessfulChecker("postgres"))
    preserved = await request(service, "/health", {"X-Request-ID": "trace-123"})
    regenerated = await request(service, "/health", {"X-Request-ID": "bad value"})

    assert preserved.headers["X-Request-ID"] == "trace-123"
    assert UUID(regenerated.headers["X-Request-ID"]).version == 4


async def test_concurrent_request_ids_are_isolated() -> None:
    app = create_app(Settings(_env_file=None))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.get("/health", headers={"X-Request-ID": "trace-first"}),
            client.get("/health", headers={"X-Request-ID": "trace-second"}),
        )

    assert first.headers["X-Request-ID"] == "trace-first"
    assert second.headers["X-Request-ID"] == "trace-second"
```

- [ ] **Step 2: Run API tests and verify the red state**

Run:

```bash
uv run pytest tests/api/test_health_api.py -v
```

Expected: collection fails because `nba_live_totals.api.app` and dependencies do not exist.

- [ ] **Step 3: Implement dependency lookup and health routes**

Create `src/nba_live_totals/api/dependencies.py`:

```python
from fastapi import Request

from nba_live_totals.application.health import HealthService
from nba_live_totals.lifecycle import AppResources


def get_health_service(request: Request) -> HealthService:
    resources: AppResources = request.app.state.resources
    return resources.health_service
```

Create `src/nba_live_totals/api/routes/health.py`:

```python
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from nba_live_totals.api.dependencies import get_health_service
from nba_live_totals.application.health import HealthService, ReadinessState, ReadinessStatus


router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=ReadinessStatus)
async def readiness(
    service: Annotated[HealthService, Depends(get_health_service)],
) -> JSONResponse:
    result = await service.check_readiness()
    status_code = 200 if result.status is ReadinessState.READY else 503
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))
```

- [ ] **Step 4: Implement the app factory and composition root**

Create `src/nba_live_totals/api/app.py`:

```python
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nba_live_totals.api.request_id import RequestIdMiddleware, get_request_id
from nba_live_totals.api.routes.health import router as health_router
from nba_live_totals.lifecycle import create_lifespan
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved)
    app = FastAPI(title="NBA Live Totals", version="0.1.0", lifespan=create_lifespan(resolved))
    app.state.settings = resolved
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, error: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception(
            "unhandled_request_error",
            extra={"error_type": type(error).__name__},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_server_error", "request_id": get_request_id()},
        )

    return app
```

Create `src/nba_live_totals/main.py`:

```python
from nba_live_totals.api.app import create_app

__all__ = ["create_app"]
```

- [ ] **Step 5: Add a safe exception-boundary test**

Append a test that adds a test-only failing route and verifies the public response:

```python
async def test_unhandled_exception_is_sanitized() -> None:
    app = create_app(Settings(_env_file=None))

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("credential=super-secret")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json()["detail"] == "internal_server_error"
    assert "super-secret" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
```

- [ ] **Step 6: Verify API, unit, lint, and typing gates**

Run:

```bash
uv run pytest tests/unit tests/api -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

Expected: API contracts pass, readiness payload ordering is stable, and no secrets enter HTTP responses.

- [ ] **Step 7: Commit the FastAPI boundary**

```bash
git add src/nba_live_totals/api src/nba_live_totals/main.py tests/api
git commit -m "feat: expose liveness and readiness APIs"
```

---

### Task 8: Async Alembic baseline

**Files:**
- Create: `src/nba_live_totals/infrastructure/metadata.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/0001_baseline.py`
- Create: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `Settings.database_url` and SQLAlchemy async engine configuration.
- Produces: deterministic `metadata`, async Alembic runtime, and an empty baseline revision `0001_baseline`.

- [ ] **Step 1: Write the failing migration integration test**

Create `tests/integration/test_migrations.py`:

```python
import os
import subprocess
import sys

import pytest


pytestmark = pytest.mark.integration


def test_alembic_upgrade_head_is_idempotent() -> None:
    environment = os.environ | {
        "DATABASE_URL": "postgresql+asyncpg://nba:nba@localhost:5432/nba",
    }

    first = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    second = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
```

- [ ] **Step 2: Run the migration test and verify the red state**

Run:

```bash
uv run pytest tests/integration/test_migrations.py -v -m integration
```

Expected: both commands fail because `alembic.ini` is absent.

- [ ] **Step 3: Create metadata and async Alembic runtime**

Create `src/nba_live_totals/infrastructure/metadata.py`:

```python
from sqlalchemy import MetaData


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

Create `alembic.ini`:

```ini
[alembic]
script_location = %(here)s/alembic
prepend_sys_path = .
path_separator = os
sqlalchemy.url = postgresql+asyncpg://nba:nba@postgres:5432/nba

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `alembic/env.py`:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.settings import Settings


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(Settings().database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = str(Settings().database_url)
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
```

Create `alembic/script.py.mako` using Alembic's concrete revision template:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Create `alembic/versions/0001_baseline.py` as a real no-op baseline:

```python
from collections.abc import Sequence


revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

- [ ] **Step 4: Verify migrations and static checks**

Run:

```bash
uv run pytest tests/integration/test_migrations.py -v -m integration
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy src tests
```

Expected: both upgrades return `0`; no game-domain table appears; source and tests remain clean.

- [ ] **Step 5: Commit the migration foundation**

```bash
git add alembic alembic.ini src/nba_live_totals/infrastructure/metadata.py tests/integration/test_migrations.py
git commit -m "build: add async Alembic baseline"
```

---

### Task 9: API container, Makefile, and Compose smoke test

**Files:**
- Create: `docker/api.Dockerfile`
- Create: `.dockerignore`
- Modify: `compose.yaml`
- Create: `Makefile`
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/test_compose_smoke.py`

**Interfaces:**
- Consumes: locked Python project, `create_app --factory`, and healthy storage services.
- Produces: non-root API image, four-service Compose stack, portable Make targets, and bounded health polling.

- [ ] **Step 1: Write the failing external smoke test**

Create `tests/e2e/test_compose_smoke.py`:

```python
import asyncio
from time import monotonic

import httpx
import pytest


pytestmark = pytest.mark.e2e


async def wait_for_json(path: str, timeout_seconds: float) -> httpx.Response:
    deadline = monotonic() + timeout_seconds
    last_error: Exception | None = None
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=2.0) as client:
        while monotonic() < deadline:
            try:
                response = await client.get(path)
                if response.status_code < 500:
                    return response
            except httpx.HTTPError as error:
                last_error = error
            await asyncio.sleep(0.25)
    raise AssertionError(f"API did not become ready: {type(last_error).__name__}")


async def test_compose_api_is_alive_and_ready() -> None:
    live = await wait_for_json("/health", timeout_seconds=30.0)
    ready = await wait_for_json("/health/ready", timeout_seconds=30.0)

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
```

- [ ] **Step 2: Verify the smoke test is red before the API container exists**

Run:

```bash
uv run pytest tests/e2e/test_compose_smoke.py -v -m e2e
```

Expected: FAIL after the bounded timeout because nothing listens on port `8000`.

- [ ] **Step 3: Build a locked, non-root API image**

Create `docker/api.Dockerfile`:

```dockerfile
FROM python:3.12.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "nba_live_totals.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

Create `.dockerignore` excluding `.git`, `.venv`, caches, `.env`, `tests`, `docs`, `graphify-out`, and local model artifacts.

- [ ] **Step 4: Add the API service to Compose**

Add this service above the storage services in `compose.yaml`:

```yaml
  api:
    build:
      context: .
      dockerfile: docker/api.Dockerfile
    environment:
      APP_ENV: local
      LOG_LEVEL: INFO
      DATABASE_URL: postgresql+asyncpg://nba:nba@postgres:5432/nba
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_USER: nba
      CLICKHOUSE_PASSWORD: nba
      REDIS_URL: redis://redis:6379/0
      HEALTHCHECK_TIMEOUT_SECONDS: "2.0"
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"
      interval: 2s
      timeout: 3s
      retries: 20
```

- [ ] **Step 5: Add reproducible developer commands**

Create `Makefile`:

```makefile
.PHONY: sync infra dev test test-unit test-integration test-e2e lint typecheck

sync:
	uv sync --frozen

infra:
	docker compose up -d --wait postgres clickhouse redis

dev: sync
	docker compose up -d --build --wait

test-unit: sync
	uv run pytest tests/unit tests/api -v

test-integration: sync infra
	uv run pytest tests/integration -v -m integration

test-e2e: sync
	docker compose up -d --build --wait
	uv run pytest tests/e2e -v -m e2e

test: test-unit test-integration test-e2e

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src tests
```

- [ ] **Step 6: Validate and run the four-service stack**

Run:

```bash
docker compose config --quiet
docker compose up -d --build --wait
uv run pytest tests/e2e/test_compose_smoke.py -v -m e2e
```

Expected: all four services become healthy and the previously red smoke test passes.

- [ ] **Step 7: Run all automated gates**

Run:

```bash
make test
make lint
make typecheck
```

Expected: unit/API, real integration, and external smoke suites pass; Ruff and mypy return `0`.

- [ ] **Step 8: Commit container and command tooling**

```bash
git add docker .dockerignore compose.yaml Makefile tests/e2e
git commit -m "build: containerize Phase 1 API stack"
```

---

### Task 10: Operator documentation and final Phase 1 verification

**Files:**
- Modify: `README.md`
- Modify: `uv.lock` only if `uv lock --check` proves it is stale.

**Interfaces:**
- Consumes: every Phase 1 command and HTTP contract implemented by Tasks 1–9.
- Produces: an operator/developer guide and final evidence for the Phase 1 quality gate.

- [ ] **Step 1: Replace README with the verified operator guide**

Replace `README.md` with this concrete guide:

```markdown
# NBA Live Totals Prediction Engine

Production-oriented backend for forecasting NBA final scores and totals before and during games. Phase 1 provides the runtime and data-store foundation; it intentionally does not expose predictions yet.

## Architecture

The project is a modular monolith with independent API, worker, training, and backtest entry points. Entry points call application services; application services depend on domain contracts; PostgreSQL, ClickHouse, Redis, and provider clients remain infrastructure adapters wired at the composition root.

The approved Phase 1 design is in [`docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md`](docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md).

## Prerequisites

- Python 3.12
- uv
- Docker with Compose v2
- make (optional; direct commands are listed below)

## Configuration

Copy `.env.example` to `.env` only when host-side overrides are needed. `.env` is ignored by Git.

| Variable | Purpose | Local value |
|---|---|---|
| `APP_ENV` | Runtime environment name | `local` |
| `LOG_LEVEL` | Python log threshold | `INFO` |
| `DATABASE_URL` | SQLAlchemy async PostgreSQL DSN | Compose PostgreSQL |
| `CLICKHOUSE_URL` | ClickHouse HTTP base URL | Compose ClickHouse |
| `CLICKHOUSE_USER` | ClickHouse user | `nba` |
| `CLICKHOUSE_PASSWORD` | ClickHouse password | `nba` for local use only |
| `REDIS_URL` | Async Redis URL | Compose Redis |
| `HEALTHCHECK_TIMEOUT_SECONDS` | Per-dependency readiness timeout | `2.0` |
| `NBA_API_TIMEOUT_SECONDS` | Reserved provider timeout | `15.0` |
| `LIVE_POLL_INTERVAL_SECONDS` | Reserved live polling interval | `3.0` |
| `MONTE_CARLO_SIMULATIONS` | Reserved simulation count | `10000` |
| `MODEL_PATH` | Reserved model artifact path | `/app/models` |

## Local startup

```bash
uv sync --frozen
docker compose up -d --build --wait
```

The API is available at `http://localhost:8000`.

## Health endpoints

`GET /health` is process liveness and performs no data-store I/O:

```json
{"status":"alive"}
```

`GET /health/ready` checks PostgreSQL, ClickHouse, and Redis concurrently. It returns HTTP `200` with `status=ready`, or HTTP `503` with `status=not_ready`. Dependency failures never expose credentials or internal exception messages.

## Verification

```bash
make test
make lint
make typecheck
```

Direct equivalents, including Windows PowerShell:

```bash
uv run pytest tests/unit tests/api -v
docker compose up -d --wait postgres clickhouse redis
uv run pytest tests/integration -v -m integration
docker compose up -d --build --wait
uv run pytest tests/e2e -v -m e2e
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## Migrations

```bash
uv run alembic upgrade head
```

The Phase 1 baseline verifies the async migration runtime and creates no game-domain tables.

## Phase boundary and roadmap

Phase 1 includes FastAPI, configuration, lifecycle management, PostgreSQL, ClickHouse, Redis, Alembic, structured logging, Docker Compose, and automated verification. It excludes NBA ingestion, game schemas, features, models, simulation, backtesting, live polling, and prediction endpoints.

Phase 2 is historical ingestion and receives its own design and implementation plan after Phase 1 passes every quality gate.
```

- [ ] **Step 2: Verify the lockfile and clean working inputs**

Run:

```bash
uv lock --check
uv sync --frozen
git status --short
```

Expected: the lockfile is current; only the intended README change and ignored local artifacts appear.

- [ ] **Step 3: Execute the complete Phase 1 quality gate**

Run:

```bash
docker compose config --quiet
make test
make lint
make typecheck
docker compose ps
```

Expected:

```text
All pytest suites pass.
Ruff check and format check pass.
mypy strict mode passes.
api, postgres, clickhouse, and redis report healthy.
```

- [ ] **Step 4: Prove liveness and readiness contracts manually**

Run:

```bash
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
```

Expected JSON statuses are `alive` and `ready`. Then stop Redis without removing its volume, query readiness, and restart it:

```bash
docker compose exec -T redis redis-cli SET phase1_volume_check preserved
docker compose up -d --wait
docker compose exec -T redis redis-cli GET phase1_volume_check
docker compose stop redis
curl --silent --output readiness.json --write-out "%{http_code}" http://localhost:8000/health/ready
docker compose start redis
docker compose up -d --wait redis
curl --fail http://localhost:8000/health/ready
docker compose exec -T redis redis-cli DEL phase1_volume_check
```

Expected: the persisted value is `preserved`, the recorded status is `503`, `readiness.json` contains `redis.status = "down"`, contains no credentials, and readiness returns `200` after Redis is healthy again. Remove only the exact test key and generated `readiness.json` after inspecting them.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md uv.lock
git commit -m "docs: document Phase 1 operations"
```

- [ ] **Step 6: Record final evidence before declaring Phase 1 complete**

Run:

```bash
git status --short --branch
git log --oneline -12
```

Expected: no unintended tracked changes; `graphify-out/` remains ignored; commits show one independently verified deliverable per task.
