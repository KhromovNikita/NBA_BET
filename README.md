# NBA Live Totals Prediction Engine

Production-oriented backend foundation for an eventual NBA final-score and totals
forecasting service. Phase 1 provides the runtime, data-store, migration, logging,
and health-check infrastructure. Phase 2 adds resumable historical NBA ingestion while
intentionally exposing no predictions.

## What is in this phase

The running application is a modular FastAPI service with application-level health
contracts and infrastructure adapters for PostgreSQL, ClickHouse, and Redis. Runtime
resources are created in the FastAPI lifespan and closed during shutdown. Historical
ingestion runs as an operator-invoked CLI and persists raw and canonical data in
PostgreSQL. The approved designs are the
[Phase 1 design](docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md)
and [Phase 2 design](docs/superpowers/specs/2026-08-31-phase-2-historical-ingestion-design.md).

Phase 2 does not implement possession reconstruction, features, model training or
inference, simulations, backtests, live polling, workers, or prediction endpoints. Do
not treat this service as a betting or forecasting API yet.

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
`.env` only for container application-setting overrides; `.env` is ignored by Git.
Compose already supplies the service environment for the containerized API.
Do not set `*_HOST_PORT` only in `.env`: Compose reads that file, but later host-side
commands do not. Export host ports in the current shell with the matching block below
and keep that shell open for the whole run.

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
| `NBA_API_TIMEOUT_SECONDS` | Provider request timeout | `15.0` |
| `NBA_API_MAX_CONCURRENCY` | Maximum concurrent provider requests | `1` |
| `NBA_API_MIN_INTERVAL_SECONDS` | Minimum interval between provider request starts | `0.75` |
| `NBA_API_MAX_ATTEMPTS` | Maximum attempts for retryable provider failures | `4` |
| `NBA_API_BACKOFF_BASE_SECONDS` | Initial retry backoff | `1.0` |
| `NBA_API_BACKOFF_MAX_SECONDS` | Maximum retry backoff | `15.0` |
| `NBA_API_RESOURCE_LEASE_SECONDS` | Ingestion resource-claim lease | `600` |
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

For Linux/macOS, select ports and matching test targets in the current shell:

```bash
# Example alternatives when all default ports are occupied:
# export API_HOST_PORT=18000
# export POSTGRES_HOST_PORT=15432
# export CLICKHOUSE_HOST_PORT=18123
# export REDIS_HOST_PORT=16379
export API_HOST_PORT="${API_HOST_PORT:-8000}"
export POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-5432}"
export CLICKHOUSE_HOST_PORT="${CLICKHOUSE_HOST_PORT:-8123}"
export REDIS_HOST_PORT="${REDIS_HOST_PORT:-6379}"
export TEST_API_URL="http://127.0.0.1:${API_HOST_PORT}"
export TEST_DATABASE_URL="postgresql+asyncpg://nba:nba@127.0.0.1:${POSTGRES_HOST_PORT}/nba"
export TEST_CLICKHOUSE_URL="http://127.0.0.1:${CLICKHOUSE_HOST_PORT}"
export TEST_REDIS_URL="redis://127.0.0.1:${REDIS_HOST_PORT}/0"
```

For PowerShell:

```powershell
# Example alternatives when all default ports are occupied:
# $env:API_HOST_PORT = '18000'
# $env:POSTGRES_HOST_PORT = '15432'
# $env:CLICKHOUSE_HOST_PORT = '18123'
# $env:REDIS_HOST_PORT = '16379'
$apiHostPort = if ($env:API_HOST_PORT) { $env:API_HOST_PORT } else { '8000' }
$postgresHostPort = if ($env:POSTGRES_HOST_PORT) { $env:POSTGRES_HOST_PORT } else { '5432' }
$clickhouseHostPort = if ($env:CLICKHOUSE_HOST_PORT) { $env:CLICKHOUSE_HOST_PORT } else { '8123' }
$redisHostPort = if ($env:REDIS_HOST_PORT) { $env:REDIS_HOST_PORT } else { '6379' }
$env:API_HOST_PORT = $apiHostPort
$env:POSTGRES_HOST_PORT = $postgresHostPort
$env:CLICKHOUSE_HOST_PORT = $clickhouseHostPort
$env:REDIS_HOST_PORT = $redisHostPort
$env:TEST_API_URL = "http://127.0.0.1:$apiHostPort"
$env:TEST_DATABASE_URL = "postgresql+asyncpg://nba:nba@127.0.0.1:$postgresHostPort/nba"
$env:TEST_CLICKHOUSE_URL = "http://127.0.0.1:$clickhouseHostPort"
$env:TEST_REDIS_URL = "redis://127.0.0.1:$redisHostPort/0"
```

Real-service tests reject non-loopback targets by default so an inherited CI variable
cannot migrate or probe a production service. Only an isolated disposable remote test
environment may opt in explicitly with `ALLOW_REMOTE_TEST_TARGETS=1`.

## Install and run

### Linux and macOS

```bash
# Optional: copy only for container app-setting overrides; do not commit .env.
# Host ports must be exported in this shell with the configuration block above.
cp .env.example .env

# Resolve nothing: install exactly the committed lockfile.
uv sync --frozen

# Build and wait for api, postgres, clickhouse, and redis.
docker compose up -d --build --wait

# Inspect service health and call the API.
docker compose ps
curl --fail "http://127.0.0.1:${API_HOST_PORT:-8000}/health"
curl --fail "http://127.0.0.1:${API_HOST_PORT:-8000}/health/ready"
```

### Windows PowerShell

```powershell
# Optional: copy only for container app-setting overrides; do not commit .env.
# Host ports must be exported in this shell with the configuration block above.
Copy-Item .env.example .env

# Resolve nothing: install exactly the committed lockfile.
uv sync --frozen

# Build and wait for api, postgres, clickhouse, and redis.
docker compose up -d --build --wait

# Inspect service health and call the API. curl.exe avoids the PowerShell curl alias.
docker compose ps
$apiHostPort = if ($env:API_HOST_PORT) { $env:API_HOST_PORT } else { '8000' }
curl.exe --fail "http://127.0.0.1:$apiHostPort/health"
curl.exe --fail "http://127.0.0.1:$apiHostPort/health/ready"
```

The API listens on loopback port `API_HOST_PORT` (default `8000`). To stop the stack while
preserving named volumes, use `docker compose stop`; `docker compose down` removes
containers and network but not named volumes unless `--volumes` is also supplied.

## Historical ingestion runbook

Historical ingestion is a CLI-only operator workflow. It adds no public game or
prediction HTTP routes; the existing health API remains unchanged. The importer accepts
Regular Season (`002`) and Playoffs (`004`) games and excludes preseason, All-Star, and
Play-In identifiers.

Apply the PostgreSQL migrations before a backfill. With the Compose stack running, use
the migration bundled into the API image:

```text
docker compose exec -T api /app/.venv/bin/alembic upgrade head
```

From the host, point `DATABASE_URL` at the published loopback PostgreSQL port and run:

```text
uv run alembic upgrade head
```

The detailed host commands for Linux/macOS and PowerShell are in [Migrations](#migrations).
Migrations create schema only and never contact NBA.com or start a backfill.

Run one completed game, one season, or the explicit five-season example from the host:

```text
uv run python -m scripts.backfill_games --season 2025-26 --game-id 0022500123
uv run python -m scripts.backfill_games --season 2025-26
uv run python -m scripts.backfill_games --season-from 2021-22 --season-to 2025-26
```

The `2021-22..2025-26` range was current at specification time. There is no
calendar-dependent default: operators must explicitly choose future ranges. On systems
with `make`, the equivalent selector forms are:

```text
make backfill SEASON=2025-26
make backfill SEASON_FROM=2021-22 SEASON_TO=2025-26
make backfill SEASON=2025-26 REFRESH=1
```

To run the same module inside an already-running API container:

```text
docker compose exec -T api /app/.venv/bin/python -m scripts.backfill_games --season 2025-26
```

A five-season backfill is expected to be a multi-hour operation. Keep the conservative
rate limiting defaults unless the provider contract has been revalidated: concurrency
is `1`, request starts are at least `0.75` seconds apart, and retryable failures use
bounded exponential backoff. The CLI prints a compact structured summary containing the
run ID, status, and discovered/succeeded/skipped/failed counts; structured JSON logs use
safe identifiers and error classifications without raw payloads, headers, proxy values,
or connection strings.

Normal reruns resume the selected work: succeeded resources are skipped, failed
resources with a stored document are reparsed before any network request, and only
missing raw data is fetched. Add `--refresh` to force network fetches. An unchanged
checksum reuses the existing raw document and avoids a canonical rewrite; a changed
payload creates a new immutable raw JSON version and atomically replaces that game's
canonical snapshot.

Pressing Ctrl+C safely marks the run interrupted and releases local resources; rerun the
same command to resume. Exit code `0` means all selected work succeeded or was skipped,
exit code `1` means a partial result (including any failed or busy resource), exit code
`2` means invalid arguments or configuration, and an interrupt uses shell status `130`.

PostgreSQL is the only Phase 2 store. Canonical data lives in `seasons`, `teams`,
`players`, `games`, `team_box_scores`, `player_box_scores`, and
`play_by_play_events`; immutable provider responses live in `source_documents`; run and
resume state lives in `ingestion_runs` and `ingestion_resources`. ClickHouse and Redis
receive no historical-ingestion data in this phase.

Before a larger operator run, use the opt-in live canary against an empty disposable
test database. Supply one known completed game explicitly:

```text
TEST_NBA_SEASON=2022-23 TEST_NBA_GAME_ID=0022200001 uv run pytest tests/live/test_nba_ingestion_canary.py -q -m live
```

In PowerShell, set the two environment variables in the current shell and run the same
`uv run pytest` command. The canary contacts NBA.com, verifies a second run performs no
new requests or row growth, and cleans up only the identities it created.

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
make backfill SEASON=2025-26  # explicit one-season historical backfill
make lint       # ruff check . and ruff format --check .
make typecheck  # mypy src scripts tests (strict configuration in pyproject.toml)
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
uv run mypy src scripts tests
```

In PowerShell, run the same direct commands unchanged. If Docker is unavailable, run
only the unit/API, Ruff, and mypy commands; integration and e2e results are deferred,
not substituted with mocks or a local listener.

### Opt-in NBA.com live canary

Live tests are excluded from ordinary CI and skip unless both `TEST_NBA_SEASON` and
`TEST_NBA_GAME_ID` identify one completed game. They contact NBA.com through the
production transport, respect its concurrency, spacing, and retry limits, and write
only to the guarded disposable test PostgreSQL database. Run them explicitly with
`uv run pytest tests/live/test_nba_ingestion_canary.py -q -m live`.

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
DATABASE_URL="postgresql+asyncpg://nba:nba@127.0.0.1:${POSTGRES_HOST_PORT:-5432}/nba" uv run alembic upgrade head
```

### Windows PowerShell

```powershell
$hadDatabaseUrl = Test-Path Env:DATABASE_URL
$previousDatabaseUrl = if ($hadDatabaseUrl) { $env:DATABASE_URL } else { $null }
$postgresHostPort = if ($env:POSTGRES_HOST_PORT) { $env:POSTGRES_HOST_PORT } else { '5432' }
try {
    $env:DATABASE_URL = "postgresql+asyncpg://nba:nba@127.0.0.1:$postgresHostPort/nba"
    uv run alembic upgrade head
} finally {
    if ($hadDatabaseUrl) {
        $env:DATABASE_URL = $previousDatabaseUrl
    } else {
        Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    }
}
```

The Phase 1 baseline validates the async Alembic runtime. The Phase 2 revision creates
the historical canonical, immutable raw-document, and resumable ingestion-state tables.
Neither migration performs a network backfill.

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
curl --silent --output readiness.json --write-out '%{http_code}' "http://127.0.0.1:${API_HOST_PORT:-8000}/health/ready"
docker compose start redis
docker compose up -d --wait redis
curl --fail "http://127.0.0.1:${API_HOST_PORT:-8000}/health/ready"
docker compose exec -T redis redis-cli DEL phase1_volume_check
rm readiness.json
```

### Windows PowerShell

```powershell
$apiHostPort = if ($env:API_HOST_PORT) { $env:API_HOST_PORT } else { '8000' }
docker compose exec -T redis redis-cli SET phase1_volume_check preserved
docker compose exec -T redis redis-cli SAVE
docker compose stop redis
docker compose rm -f redis
docker compose up -d --wait redis
docker compose exec -T redis redis-cli GET phase1_volume_check
docker compose stop redis
curl.exe --silent --output readiness.json --write-out '%{http_code}' "http://127.0.0.1:$apiHostPort/health/ready"
docker compose start redis
docker compose up -d --wait redis
curl.exe --fail "http://127.0.0.1:$apiHostPort/health/ready"
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
Redis, Alembic, structured logging, Docker Compose, and automated verification. Phase 2
adds historical schedule, box-score, and play-by-play ingestion through the CLI without
changing the public API. Predictions follow only after later feature, model, simulation,
replay/backtest, and live-worker phases define and verify their contracts.
