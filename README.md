# NBA Live Totals Prediction Engine

Production-oriented backend foundation for an eventual NBA final-score and totals
forecasting service. Phase 1 provides the runtime, data-store, migration, logging,
and health-check infrastructure; it intentionally exposes no predictions.

## What is in this phase

The running application is a modular FastAPI service with application-level health
contracts and infrastructure adapters for PostgreSQL, ClickHouse, and Redis. Runtime
resources are created in the FastAPI lifespan and closed during shutdown. The approved
design is in [the Phase 1 design](docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md).

Phase 1 does not implement NBA ingestion, game/player schemas, features, model
training or inference, simulations, backtests, live polling, workers, or prediction
endpoints. Do not treat this service as a betting or forecasting API yet.

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker Engine/Desktop with Docker Compose v2.17+
- `make` is optional on Linux/macOS; every Make target has a direct `uv`/`docker`
  equivalent below.

Verify the local tools before starting:

```text
uv --version
docker compose version --short  # must report 2.17.0 or newer
```

Compose defaults to the project name `nba-live-totals`. For parallel clones or
worktrees, set a unique `COMPOSE_PROJECT_NAME` in each shell (for example,
`nba-live-totals-review`) before running Compose so containers and named volumes do
not collide.

## Configuration

The tracked [.env.example](.env.example) contains Compose-local defaults. Copy it to
`.env` only when a host-side override is needed; `.env` is ignored by Git. Compose
already supplies the service environment for the containerized API.

| Variable | Purpose | Compose-local value |
|---|---|---|
| `APP_ENV` | Runtime environment name | `local` |
| `LOG_LEVEL` | Python logging threshold | `INFO` |
| `DATABASE_URL` | SQLAlchemy async PostgreSQL DSN | `postgresql+asyncpg://nba:nba@postgres:5432/nba` |
| `CLICKHOUSE_URL` | ClickHouse HTTP base URL | `http://clickhouse:8123` |
| `CLICKHOUSE_USER` | ClickHouse user | `nba` |
| `CLICKHOUSE_PASSWORD` | ClickHouse password | `nba` (local use only) |
| `REDIS_URL` | Async Redis URL | `redis://redis:6379/0` |
| `HEALTHCHECK_TIMEOUT_SECONDS` | Timeout for each readiness dependency | `2.0` |
| `NBA_API_TIMEOUT_SECONDS` | Reserved provider timeout | `15.0` |
| `LIVE_POLL_INTERVAL_SECONDS` | Reserved live-poll interval | `3.0` |
| `MONTE_CARLO_SIMULATIONS` | Reserved simulation count | `10000` |
| `MODEL_PATH` | Reserved model-artifact path | `/app/models` |
| `API_HOST_PORT` | Loopback-only API host port | `8000` |
| `POSTGRES_HOST_PORT` | Loopback-only PostgreSQL host port | `5432` |
| `CLICKHOUSE_HOST_PORT` | Loopback-only ClickHouse host port | `8123` |
| `REDIS_HOST_PORT` | Loopback-only Redis host port | `6379` |

When running Alembic from the host, replace Compose service hostnames with `localhost`
in `DATABASE_URL`; the examples below do that only for the command that needs it.

If a default host port is already occupied, override the corresponding `*_HOST_PORT`
for Compose and point real-service tests at it with `TEST_DATABASE_URL`,
`TEST_CLICKHOUSE_URL`, `TEST_REDIS_URL`, and `TEST_API_URL`. Defaults remain the URLs
shown in the commands below. These overrides affect only loopback publication and test
clients; service-to-service container ports do not change.

## Install and run

### Linux and macOS

```bash
# Optional: create host-side overrides; do not commit .env.
cp .env.example .env

# Resolve nothing: install exactly the committed lockfile.
uv sync --frozen

# Build and wait for api, postgres, clickhouse, and redis.
docker compose up -d --build --wait

# Inspect service health and call the API.
docker compose ps
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/health/ready
```

### Windows PowerShell

```powershell
# Optional: create host-side overrides; do not commit .env.
Copy-Item .env.example .env

# Resolve nothing: install exactly the committed lockfile.
uv sync --frozen

# Build and wait for api, postgres, clickhouse, and redis.
docker compose up -d --build --wait

# Inspect service health and call the API. curl.exe avoids the PowerShell curl alias.
docker compose ps
curl.exe --fail http://localhost:8000/health
curl.exe --fail http://localhost:8000/health/ready
```

The API listens on `http://localhost:8000`. To stop the stack while preserving named
volumes, use `docker compose stop`; `docker compose down` removes containers and network
but not named volumes unless `--volumes` is also supplied.

## Health and failure semantics

`GET /health` is liveness only. It performs no data-store I/O and returns HTTP `200`:

```json
{"status":"alive"}
```

`GET /health/ready` concurrently checks PostgreSQL, ClickHouse, and Redis, applying
`HEALTHCHECK_TIMEOUT_SECONDS` to each dependency. It returns HTTP `200` with
`"status": "ready"` only when every check is up. If a dependency is unavailable or
times out, it returns HTTP `503` with `"status": "not_ready"`; the affected check has
`"status": "down"`. The API remains live so orchestration can distinguish a process
failure from a dependency outage. Readiness responses intentionally omit DSNs,
credentials, query text, exception messages, and stack traces.

An all-up readiness response also includes per-dependency latency:

```json
{
  "status": "ready",
  "checks": {
    "postgres": {"status": "up", "latency_ms": 1.2},
    "clickhouse": {"status": "up", "latency_ms": 0.8},
    "redis": {"status": "up", "latency_ms": 0.4}
  }
}
```

## Tests, linting, and type checks

On Linux/macOS with `make`, the project targets are:

```bash
make test       # frozen sync; unit/API, integration, then Compose e2e tests
make lint       # ruff check . and ruff format --check .
make typecheck  # mypy src tests (strict configuration in pyproject.toml)
```

The direct commands work on every platform. Unit and API tests require no Docker;
integration tests require the three stores; e2e tests require the complete Compose stack.

```text
uv sync --frozen
uv run pytest tests/unit tests/api -v
docker compose up -d --wait postgres clickhouse redis
uv run pytest tests/integration -v -m integration
docker compose up -d --build --wait
uv run pytest tests/e2e -v -m e2e
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

In PowerShell, run the same direct commands unchanged. If Docker is unavailable, run
only the unit/API, Ruff, and mypy commands; integration and e2e results are deferred,
not substituted with mocks or a local listener.

## Migrations

Check the single migration head and render its offline SQL without connecting to a
database:

```text
uv run alembic heads
uv run alembic upgrade head --sql
```

Apply it to the Compose PostgreSQL database from inside the API container:

```text
docker compose exec -T api /app/.venv/bin/alembic upgrade head
```

Or apply it from the host, using its published PostgreSQL port:

### Linux and macOS

```bash
DATABASE_URL=postgresql+asyncpg://nba:nba@localhost:5432/nba uv run alembic upgrade head
```

### Windows PowerShell

```powershell
$hadDatabaseUrl = Test-Path Env:DATABASE_URL
$previousDatabaseUrl = if ($hadDatabaseUrl) { $env:DATABASE_URL } else { $null }
try {
    $env:DATABASE_URL = 'postgresql+asyncpg://nba:nba@localhost:5432/nba'
    uv run alembic upgrade head
} finally {
    if ($hadDatabaseUrl) {
        $env:DATABASE_URL = $previousDatabaseUrl
    } else {
        Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    }
}
```

The Phase 1 baseline validates the async Alembic runtime and creates only Alembic
metadata; it creates no NBA-domain tables.

## Persistence and partial-outage verification

Run this manual procedure only against a disposable local Compose stack. It writes one
exact Redis test key, proves that the named volume retains it across a normal Compose
container recreation, observes the safe readiness failure, and removes only that key
afterwards.

### Linux and macOS

```bash
docker compose exec -T redis redis-cli SET phase1_volume_check preserved
docker compose exec -T redis redis-cli SAVE
docker compose stop redis
docker compose rm -f redis
docker compose up -d --wait redis
docker compose exec -T redis redis-cli GET phase1_volume_check
docker compose stop redis
curl --silent --output readiness.json --write-out '%{http_code}' http://localhost:8000/health/ready
docker compose start redis
docker compose up -d --wait redis
curl --fail http://localhost:8000/health/ready
docker compose exec -T redis redis-cli DEL phase1_volume_check
rm readiness.json
```

### Windows PowerShell

```powershell
docker compose exec -T redis redis-cli SET phase1_volume_check preserved
docker compose exec -T redis redis-cli SAVE
docker compose stop redis
docker compose rm -f redis
docker compose up -d --wait redis
docker compose exec -T redis redis-cli GET phase1_volume_check
docker compose stop redis
curl.exe --silent --output readiness.json --write-out '%{http_code}' http://localhost:8000/health/ready
docker compose start redis
docker compose up -d --wait redis
curl.exe --fail http://localhost:8000/health/ready
docker compose exec -T redis redis-cli DEL phase1_volume_check
Remove-Item -LiteralPath readiness.json
```

Before the cleanup commands, confirm that Redis returned `preserved`, the recorded
HTTP status was `503`, and `readiness.json` has `checks.redis.status` equal to `down`
without a DSN, password, or other credential. After Redis is healthy, readiness must
again return `200`. Do not use `docker compose down --volumes` for this check: it
would destroy the persistence being verified.

## Troubleshooting

- `uv sync --frozen` fails: run `uv lock --check`. If it reports a stale lockfile,
  resolve and review the lock change before updating it; a frozen install never resolves
  dependencies.
- A service cannot bind `5432`, `6379`, `8000`, or `8123`: identify the process using
  the published port, stop that process or change the Compose mapping deliberately, then
  rerun `docker compose up -d --build --wait`.
- `/health` is `200` but `/health/ready` is `503`: the process is running but at least
  one dependency is unavailable. Use `docker compose ps` and service logs to locate the
  failed store; do not infer readiness from liveness.
- Host-side migration cannot connect: use the `localhost` `DATABASE_URL` shown above,
  not the Compose-only hostname `postgres`.
- Docker or `make` is absent from `PATH`: install Docker Compose v2 or use the direct
  `uv` commands. Docker-required integration, e2e, persistence, and partial-outage
  checks cannot be claimed until Docker is available.

## Security and logging caveats

The sample Compose credentials are intentionally public local-development values. Replace
them with secret-managed, non-default credentials outside local development; never commit
`.env`, model artifacts, or real connection strings.

Logs are structured JSON and include operational metadata such as timestamp, level,
logger, environment, and request ID. Health failures log a dependency name, failure type,
and latency while HTTP readiness output remains sanitized. Unhandled exceptions are also
logged with their traceback, so application code and log access controls must prevent
secrets from being placed in exception messages. An `X-Request-ID` supplied by a client is
preserved only when valid; otherwise the API generates a UUID and returns it in the
response header.

## Phase boundary and roadmap

Phase 1 includes FastAPI, configuration, lifecycle management, PostgreSQL, ClickHouse,
Redis, Alembic, structured logging, Docker Compose, and automated verification. Historical
ingestion is Phase 2. Predictions follow only after later domain, feature, model,
simulation, replay/backtest, and live-worker phases define and verify their contracts.
