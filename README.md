# NBA Live Totals Prediction Engine

Production-oriented backend foundation for an eventual NBA final-score and totals
forecasting service. Phase 1 provides the runtime, data-store, migration, logging,
and health-check infrastructure. Phase 2 adds resumable historical NBA ingestion. Phase 3
adds deterministic possession reconstruction from stored play-by-play. Phase 4 reconstructs
lineups, and Phase 5 builds reproducible offline feature datasets. Phase 6 adds deterministic
offline baseline estimates and JSONL exports. Phase 7 adds offline CPU CatBoost training,
checked model bundles and causal state prediction exports. Phase 8 adds reproducible
offline Monte Carlo final-score distributions including future overtimes.

## What is in this phase

The running application is a modular FastAPI service with application-level health
contracts and infrastructure adapters for PostgreSQL, ClickHouse, and Redis. Runtime
resources are created in the FastAPI lifespan and closed during shutdown. Historical
ingestion runs as an operator-invoked CLI and persists raw and canonical data in
PostgreSQL. Possession, lineup, and feature snapshots are materialized offline in ClickHouse,
with authoritative publication state in PostgreSQL. The approved designs are the
[Phase 1 design](docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md),
[Phase 2 design](docs/superpowers/specs/2026-08-31-phase-2-historical-ingestion-design.md),
[Phase 3 design](docs/superpowers/specs/2026-09-02-phase-3-possession-parser-design.md),
[Phase 4 design](docs/superpowers/specs/2026-09-15-phase-4-lineup-reconstruction-design.md),
and [Phase 5 design](docs/superpowers/specs/2026-09-24-phase-5-feature-engineering-design.md).

The offline modeling workflows are operator CLIs. Monte Carlo final-score simulation,
backtests, live polling and prediction API endpoints remain later roadmap milestones.

## Prerequisites

- Python 3.14
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
| `POSSESSION_RESOURCE_LEASE_SECONDS` | Possession claim lease, from 1 to 86400 seconds | `300` |
| `POSSESSION_BUILD_MAX_CONCURRENCY` | Concurrent possession builds, from 1 to 32 | `4` |
| `CLICKHOUSE_OPERATION_TIMEOUT_SECONDS` | Positive finite analytical-operation timeout | `30` |
| `POSSESSION_NORMALIZER_VERSION` | Safe NBA PBP normalizer identity | `nba-pbp-v2` |
| `POSSESSION_PARSER_VERSION` | Safe analytical parser identity | `analytical-v1` |
| `POSSESSION_RULES_VERSION` | Safe basketball rules identity | `nba-2025-v1` |
| `POSSESSION_OUTPUT_SCHEMA_VERSION` | Safe possession output-schema identity | `possessions-v1` |
| `LIVE_POLL_INTERVAL_SECONDS` | Reserved live-poll interval | `3.0` |
| `MONTE_CARLO_SIMULATIONS` | Simulation N when neither config nor explicit `--simulations` is supplied | `10000` |
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

## Lineup reconstruction runbook

Phase 4 builds immutable lineup segments offline from stored Phase 2 box score and
play-by-play inputs: no NBA.com requests are made. Select completed Regular Season or
Playoffs games with both resources successfully applied. There is no automatic backfill.
Run migrations in this order before building (the API container already has connection settings):

```bash
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --season 2025-26
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --season 2025-26 --game-id 0022500123
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --season-from 2021-22 --season-to 2025-26
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --season 2025-26 --rebuild
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --season 2025-26 --verify
```

These direct commands also work in PowerShell. For host execution, set `DATABASE_URL`
to a `postgresql+asyncpg` URL and `CLICKHOUSE_URL` to the published loopback endpoint,
with the same credentials as Compose, before running `uv run python -m scripts.build_lineups`.
The Make equivalents require a POSIX shell:

```text
make build-lineups SEASON=2025-26
make build-lineups SEASON=2025-26 GAME_ID=0022500123
make build-lineups SEASON_FROM=2021-22 SEASON_TO=2025-26
make build-lineups SEASON=2025-26 REBUILD=1
make build-lineups SEASON=2025-26 VERIFY=1
```

Choose one season or a complete inclusive season range. `GAME_ID` requires its matching
single season. `--rebuild` and `--verify` are mutually exclusive; Make accepts only `1`
for an explicitly supplied `REBUILD` or `VERIFY`.

Default mode skips a fresh published snapshot, including a published invalid result.
Freshness is dual-input: both applied source-document identities/checksums, canonical
player box and event checksums, game context, and all four lineup versions must match.
Refreshing either input invalidates the previous effective current result even when
its old analytical rows remain. `--rebuild` forces a new snapshot UUID and generation.
`--verify` records a run and verifies the exact current snapshot without claims, analytical
writes, or pointer changes; an absent, stale, or inconsistent current snapshot fails.

The authoritative PostgreSQL pointer in `lineup_rebuild_resources` identifies the published
`snapshot_id`, `lineup_revision_id`, and attempt `generation`. Resolve current freshness
before reading `nba.lineup_segments_v1` with that exact snapshot identity, then verify
row count and canonical output checksum. Never choose the latest ClickHouse row or maximum
generation as current: orphan rows and superseded attempts are retained and are not published.
Publication follows immutable insert, exact readback, verification, and a fenced pointer update.

Recovery follows the persisted state. `allocated` has no insert intent yet; `writing`
has persisted expected output metadata and may have an ambiguous insert. Only the same
run may resume its live fenced claim and check exact readback before any retry. A separate
service or CLI invocation always creates a new run: it reports busy while the original
lease is live, then supersedes the expired attempt and allocates a fresh attempt after
expiry. It never resends the old snapshot ID. Exact rows can advance to `verified`; only a
verified, fresh attempt can publish. Mismatched readback becomes `poisoned`; changed inputs
also make a candidate `superseded`. A failed forced rebuild retains the previous fresh
published pointer. Ctrl+C stops new work, cancels local workers, and best-effort records
an interrupted run; a later invocation follows the same busy-then-expiry policy.

The summary order is `run`, `status`, `discovered`, `materialized`, `invalid`,
`skipped_current`, `verified`, `failed`, `busy`. `invalid` is an independent quality count:
a game can be materialized, skipped, or verified and also invalid. Published invalid
segments are auditable quality evidence, not a storage failure; they produce partial
status and exit code `1`. A successful exit code `0` requires invalid=failed=busy=0. Operational
failures, busy games, and stale verification also produce exit code `1`; malformed
selectors, settings, or schema produce exit code `2`. Ctrl+C returns status `130`.

The six settings in `.env.example` pass through the existing API container:
`LINEUP_NORMALIZER_VERSION=nba-pbp-v3-lineup-v1`,
`LINEUP_RECONSTRUCTOR_VERSION=causal-lineup-v1`, `LINEUP_RULES_VERSION=nba-5x5-v1`,
`LINEUP_OUTPUT_SCHEMA_VERSION=lineup-segments-v1`, `LINEUP_BUILD_MAX_CONCURRENCY=4`,
and `LINEUP_CLAIM_LEASE_SECONDS=300`. Versions are lowercase safe identifiers of at most
128 characters; concurrency is 1–32 and lease is 1–86400 seconds. The existing finite
positive `CLICKHOUSE_OPERATION_TIMEOUT_SECONDS` applies. Settings and async URL validation,
then explicit lineup schema ledger/catalog validation, precede build adapter composition.

Logs contain bounded event names, run/snapshot/game IDs, generation, counters, state/status,
and safe error codes/types. Do not log raw descriptions, payloads, player names, SQL,
response bodies/headers, DSNs, credentials, or exception messages.
Possession-to-lineup joins, training/model features, live reconstruction, and new public
API endpoints are deferred. Health endpoints, readiness, and Redis behavior are unchanged.

Destructive migration tests require `TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1` and dedicated
disposable `TEST_DATABASE_URL` and `TEST_CLICKHOUSE_URL` stores. They drop and recreate both
`nba.possessions_v1` and `nba.lineup_segments_v1`; never point them at shared or persistent
development data. Keep this opt-in unset during ordinary integration tests.

### Opt-in lineup provider canary

`tests/live/test_nba_lineup_canary.py` is skipped at collection unless all five
variables are supplied: `TEST_NBA_SEASON`, `TEST_NBA_GAME_ID`, `TEST_DATABASE_URL`,
`TEST_CLICKHOUSE_URL`, and `TEST_LIVE_ISOLATED_STORES=1`. Pin one completed game
with substitutions. Use exclusively owned, migrated, empty disposable stores;
the canary checks every PostgreSQL data table and both analytical tables before
making any NBA.com request. Existing data or an unowned identity collision fails
closed. The usual loopback-only test URL guards apply.

The canary permits exactly one schedule, one box-score, and one PBP request,
with retries disabled. It checks five starters per team, a normalized substitution,
valid five-player sides, identical rebuilt segments and semantic hash, successful
verification, and a default rerun with no further network requests. Cleanup records
primary keys before persistence, removes only owned snapshot UUIDs from ClickHouse,
deletes owned PostgreSQL rows in dependency order, and checks both stores are empty
again. Provider failures are reported; the test does not silently retry or switch games.

With those variables explicitly set in a disposable shell, run:

```text
uv run pytest tests/live/test_nba_lineup_canary.py -v -m live -rs
```

For final non-live Docker acceptance, choose a fresh `COMPOSE_PROJECT_NAME` and
unused loopback ports through `API_HOST_PORT`, `POSTGRES_HOST_PORT`,
`CLICKHOUSE_HOST_PORT`, and `REDIS_HOST_PORT`. Set the corresponding `TEST_*_URL`
variables in that same disposable shell (including `TEST_API_URL`, for example
`http://127.0.0.1:18000`). Do not reuse development volumes. After
`docker compose config --quiet` and `docker compose up -d --build --wait`, require
four healthy services and run PostgreSQL and ClickHouse migrations twice each.
Check both reconstruction CLI `--help` commands, `docker compose exec -T api id -u`
(nonzero), `/health`, `/health/ready`, and the unchanged public route set. Then run
`uv run pytest -q`, `uv run pytest tests/e2e -v -m e2e`, Ruff check/format, and strict
mypy. Confirm both analytical tables are empty before the separately opted-in
destructive migration suite; require executed tests, not skips. Keep NBA live
variables unset for this acceptance. Remove only the temporary project's containers
and volumes after recording results.

## Possession reconstruction runbook

Phase 3 is a CLI-only analytical workflow for completed Regular Season and Playoffs games
whose Phase 2 play-by-play is already successful and applied. It adds no service, worker,
or HTTP route. A possession build makes no NBA.com requests: refresh source data separately
with `scripts.backfill_games --refresh`, then run the local build against the newly stored PBP.

Apply the schema systems in this order. PostgreSQL owns lifecycle and publication metadata;
the forward-only ClickHouse migration creates and verifies the immutable analytical table.
Both commands are idempotent, but reversing or skipping this order is unsupported.

```text
uv run alembic upgrade head
uv run python -m scripts.migrate_clickhouse
```

The Compose-image equivalents are:

```text
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
```

Build one game, one season, or an inclusive season range from the host:

```text
uv run python -m scripts.build_possessions --season 2025-26 --game-id 0022500123
uv run python -m scripts.build_possessions --season 2025-26
uv run python -m scripts.build_possessions --season-from 2021-22 --season-to 2025-26
uv run python -m scripts.build_possessions --season 2025-26 --rebuild
uv run python -m scripts.build_possessions --season 2025-26 --verify
```

Exactly one selector form is required: either `--season` or the complete
`--season-from`/`--season-to` pair. `--game-id` is valid only with `--season`.
`--rebuild` and `--verify` are mutually exclusive. There is deliberately no Phase 3
`--refresh` option and no network fallback.

On systems with `make`, the validated equivalents are:

```text
make migrate-clickhouse
make build-possessions SEASON=2025-26
make build-possessions SEASON=2025-26 GAME_ID=0022500123
make build-possessions SEASON_FROM=2021-22 SEASON_TO=2025-26
make build-possessions SEASON=2025-26 REBUILD=1
make build-possessions SEASON=2025-26 VERIFY=1
```

Default mode materializes only missing, stale, failed, or version-mismatched results and
skips an exact current revision. `--rebuild` creates a new immutable attempt and snapshot
identity from the same current PBP; identical input still has the same deterministic content
hash. `--verify` is read-only for snapshot rows, publication pointers, and freshness metadata;
it records only its audit run and counters.

The CLI prints a structured summary with `run`, `status`, `discovered`, `materialized`,
`skipped_current`, `verified`, `failed`, and `busy`. The exit code `0` means no failed or busy
game, exit code `1` means a partial, failed, busy, or stale verification result, and exit code `2`
means invalid selectors, settings, or schema. Ctrl+C stops new claims, cancels active
local tasks, best-effort marks the run interrupted, and returns shell status `130`.

The authoritative PostgreSQL pointer is the only route to current analytical rows. ClickHouse
snapshots are immutable: never select a current game with `max(snapshot_id)` or the newest raw
row. An orphan snapshot remains audit-only. An invalid parse or an incomplete/different
readback marks the attempt poisoned without replacing a prior valid pointer. An unavailable
ClickHouse readback leaves an uncertain attempt writing. Only its owning run can resume exact
readback; a normal rerun has a different run ID and reports busy until the lease expires. After
expiry it supersedes the old attempt and rebuilds under a fresh snapshot ID, so stale workers
cannot publish or resend the uncertain snapshot. If stored PBP or game context changes during a
build, the candidate is superseded and a later run rebuilds it from the new input.

The possession live canary is opt-in and destructive only to its proven-empty isolated stores.
Set both explicit identities and point all test URLs at disposable empty PostgreSQL and
ClickHouse instances before running it:

```text
TEST_DATABASE_URL="postgresql+asyncpg://nba:nba@127.0.0.1:${POSTGRES_HOST_PORT:-5432}/nba" TEST_CLICKHOUSE_URL="http://127.0.0.1:${CLICKHOUSE_HOST_PORT:-8123}" TEST_LIVE_ISOLATED_STORES=1 TEST_NBA_SEASON=2022-23 TEST_NBA_GAME_ID=0022200001 uv run pytest tests/live/test_nba_possession_canary.py -q -m live
```

PowerShell preserves any existing values and removes only the temporary overrides:

```powershell
$hadTestNbaSeason = Test-Path Env:TEST_NBA_SEASON
$previousTestNbaSeason = if ($hadTestNbaSeason) { $env:TEST_NBA_SEASON } else { $null }
$hadTestNbaGameId = Test-Path Env:TEST_NBA_GAME_ID
$previousTestNbaGameId = if ($hadTestNbaGameId) { $env:TEST_NBA_GAME_ID } else { $null }
$hadTestDatabaseUrl = Test-Path Env:TEST_DATABASE_URL
$previousTestDatabaseUrl = if ($hadTestDatabaseUrl) { $env:TEST_DATABASE_URL } else { $null }
$hadTestClickhouseUrl = Test-Path Env:TEST_CLICKHOUSE_URL
$previousTestClickhouseUrl = if ($hadTestClickhouseUrl) { $env:TEST_CLICKHOUSE_URL } else { $null }
$hadTestLiveIsolatedStores = Test-Path Env:TEST_LIVE_ISOLATED_STORES
$previousTestLiveIsolatedStores = if ($hadTestLiveIsolatedStores) { $env:TEST_LIVE_ISOLATED_STORES } else { $null }
$postgresHostPort = if ($env:POSTGRES_HOST_PORT) { $env:POSTGRES_HOST_PORT } else { '5432' }
$clickhouseHostPort = if ($env:CLICKHOUSE_HOST_PORT) { $env:CLICKHOUSE_HOST_PORT } else { '8123' }
try {
    $env:TEST_NBA_SEASON = '2022-23'
    $env:TEST_NBA_GAME_ID = '0022200001'
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://nba:nba@127.0.0.1:$postgresHostPort/nba"
    $env:TEST_CLICKHOUSE_URL = "http://127.0.0.1:$clickhouseHostPort"
    $env:TEST_LIVE_ISOLATED_STORES = '1'
    uv run pytest tests/live/test_nba_possession_canary.py -q -m live
} finally {
    if ($hadTestNbaSeason) { $env:TEST_NBA_SEASON = $previousTestNbaSeason } else { Remove-Item Env:TEST_NBA_SEASON -ErrorAction SilentlyContinue }
    if ($hadTestNbaGameId) { $env:TEST_NBA_GAME_ID = $previousTestNbaGameId } else { Remove-Item Env:TEST_NBA_GAME_ID -ErrorAction SilentlyContinue }
    if ($hadTestDatabaseUrl) { $env:TEST_DATABASE_URL = $previousTestDatabaseUrl } else { Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue }
    if ($hadTestClickhouseUrl) { $env:TEST_CLICKHOUSE_URL = $previousTestClickhouseUrl } else { Remove-Item Env:TEST_CLICKHOUSE_URL -ErrorAction SilentlyContinue }
    if ($hadTestLiveIsolatedStores) { $env:TEST_LIVE_ISOLATED_STORES = $previousTestLiveIsolatedStores } else { Remove-Item Env:TEST_LIVE_ISOLATED_STORES -ErrorAction SilentlyContinue }
}
```

The canary fails closed before an NBA request if isolation cannot be proven, ingests exactly
one completed game, verifies the published snapshot through the PostgreSQL pointer, confirms
the rerun adds no requests or ClickHouse writes, and cleans both stores. Unsupported provider
forms require a reviewed normalizer version and golden fixture; do not relax the parser during
an operator run.

Phase 4 and later work remains excluded: no lineup state, feature views, models, prediction
API, simulations, backtests, incremental live possessions, workers, or orphan garbage
collection are part of this workflow.

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

The ClickHouse migration module is intentionally excluded from an ordinary integration run:
its fixtures drop and recreate both `nba.possessions_v1` and `nba.lineup_segments_v1`.
Run it only against dedicated disposable
PostgreSQL and ClickHouse stores. The guard requires an exact opt-in and refuses to proceed if
the ClickHouse table contains any rows:

```bash
TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1 uv run pytest tests/integration/test_clickhouse_migrations.py -v -m integration
```

```powershell
$env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS = '1'
try {
    uv run pytest tests/integration/test_clickhouse_migrations.py -v -m integration
} finally {
    Remove-Item Env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS -ErrorAction SilentlyContinue
}
```

Do not enable this guard for the persistent development Compose stores after possession data
has been built. Use separate `TEST_DATABASE_URL` and `TEST_CLICKHOUSE_URL` targets instead.

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
changing the public API. Phases 3-5 add possession reconstruction, lineup reconstruction,
and offline feature datasets. Phase 6 adds offline baselines; Phase 7 adds offline CatBoost
models. Phase 8 adds offline Monte Carlo distributions and line probabilities. Later
phases cover replay/backtest, prediction API, and live workers.
The full phase sequence is in
the [original roadmap](docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md#16-roadmap-после-phase-1).

## Phase 5 offline feature dataset

The supported offline order is
`backfill -> build_possessions -> build_lineups -> build_dataset`. The dataset command accepts the default, `--rebuild`, and read-only
`--verify` modes:

```text
uv run python -m scripts.build_dataset --season 2025-26
uv run python -m scripts.build_dataset --season 2025-26 --game-id 0022500123
uv run python -m scripts.build_dataset --season-from 2021-22 --season-to 2025-26
uv run python -m scripts.build_dataset --season 2025-26 --rebuild
uv run python -m scripts.build_dataset --season 2025-26 --verify
```

The equivalent guarded Make invocations are `make dataset SEASON=2025-26`, with optional
`GAME_ID=0022500123`, `REBUILD=1`, or `VERIFY=1`, and `make dataset
SEASON_FROM=2021-22 SEASON_TO=2025-26`.

Feature freshness requires both the published possession and lineup source identities.
Historical inputs are a strict causal manifest of games that started before the target;
target-game box scores never become pregame features, and there is no NBA network fallback.
The `quality-invalid` lineup states are published as auditable invalid datasets and make the CLI
exit non-zero; they are distinct from operational failures.

Recovery resumes `allocated` by recalculating, `writing` by exact readback only, and
`verified` by publication only. PostgreSQL owns the current pointer. Readers use that exact
snapshot UUID with `FINAL` and require matching rows from both
`nba.game_state_features_v1` and `nba.player_pregame_features_v1`; no latest-row query is
allowed. Apply ClickHouse migrations in numeric order after PostgreSQL migrations. The
runtime validates the possessions, lineup-segments, game-state-feature, and
player-pregame-feature schema IDs before composing adapters.

Feature values are signed fixed-point integers scaled by `1_000_000`; `NULL` means the
causal denominator or complete inputs were unavailable, not zero. Structured logs contain
only safe scalar IDs, counts, versions, statuses, issue codes, and elapsed time—never raw
events, player arrays, payloads, SQL, URLs/DSNs, headers, proxies, credentials, or exception
messages. Model training, inference, simulations, backtests, live incremental features, and
orphan garbage collection remain deferred.

Destructive isolated migration tests may drop and recreate all four analytical tables:
`nba.possessions_v1`, `nba.lineup_segments_v1`, `nba.game_state_features_v1`, and
`nba.player_pregame_features_v1`. Never enable their destructive opt-in against a shared or
persistent ClickHouse database.

Final Phase 5 acceptance lives in `tests/e2e/test_feature_engineering.py`. It uses only owned
far-future fixture identities, proves equal-time/target/future leakage exclusions, validates an
invalid lineup interval followed by recovery, and round-trips the exact two-table snapshot with
no NBA call. The subsequent offline baseline workflow is described below.

## Phase 6 offline baselines

The offline order extends to `backfill -> build_possessions -> build_lineups -> build_dataset -> build_baselines`.
Historical, live, and shrinkage methods export pace (team possessions per 48 minutes)
and home/away PPP in fixed-point units scaled by `1_000_000`. No model training,
final-score forecast, betting probabilities, API routes, or NBA network calls are added.

```text
uv run python -m scripts.build_baselines --season 2025-26 --output baselines.jsonl
uv run python -m scripts.build_baselines --season 2025-26 --game-id 0022500123 --output game.jsonl
uv run python -m scripts.build_baselines --season-from 2021-22 --season-to 2025-26 --output baselines.jsonl
uv run python -m scripts.build_baselines --season 2025-26 --history-window 10 --ppp-prior-possessions 20 --pace-prior-combined-possessions 40 --output baselines.jsonl
```

On Windows use the direct commands above, or `.\.venv\Scripts\python.exe` instead of
`uv run python` if uv is absent from PATH. Configure host DATABASE_URL and CLICKHOUSE_URL
for the published loopback stores as in the dataset runbook. Parent output directory must
already exist. Paths resolve relative to the current directory; repeat runs perform
atomic replacement of the explicitly selected output file.

On POSIX systems, `make build-baselines SEASON=2025-26 OUTPUT=baselines.jsonl` accepts
optional GAME_ID, HISTORY_WINDOW, PPP_PRIOR_POSSESSIONS, PACE_PRIOR_COMBINED_POSSESSIONS;
use SEASON_FROM and SEASON_TO for an inclusive range. OUTPUT is required.

The `baseline-jsonl-v1` artifact starts with a canonical manifest identifying exact
verified feature snapshots, versions, parameters, source quality and outcome checksum.
Each state has three outcomes. Rates are integer units; null means unavailable and has
a reason. Invalid states remain unavailable; valid states after recovery are calculated.
Before publication, both analytical tables are verified and every source is rechecked.
Stale/missing sources or I/O failure abort the complete export and preserve the old file.
Exports pin source identities; they do not become a new authoritative database pointer.

Exit `0` means all outcome rows available, `1` means a complete artifact with partial or
unavailable rows, `2` means a validation/source/schema/I/O failure before publication,
and `130` means interrupted before publication. An ordinary full dataset includes an
unavailable pregame live row and therefore normally returns `1`; this is a quality result,
not an export failure. Initial shrinkage strengths (20 for PPP, 40 combined possessions
for pace) are configuration defaults and have not been calibrated for forecast accuracy.

Acceptance: `tests/integration/test_baseline_source.py` and `tests/e2e/test_baselines.py`.
The [Phase 6 checkpoint](docs/superpowers/reports/2026-10-13-phase-6-acceptance.md)
records the isolated full-suite results and the synthetic provider-neutral recovery case.

## Phase 7 offline CPU models

The [Phase 7 design](docs/superpowers/specs/2026-10-13-phase-7-catboost-design.md)
defines three independent regressors: remaining-horizon pace, home PPP and away PPP.
The horizon ends at regulation, or at the end of the current known OT. Future overtime
periods are excluded. Rates and persisted MAE/RMSE use integer scale `1_000_000`;
pace is possessions per team per 48 minutes and PPP is points per possession.

Install the optional, frozen ML extra before training or prediction:

```text
uv sync --frozen --extra ml
uv run python -m scripts.build_training_dataset --season-from 2021-22 --season-to 2025-26 --validation-from 2024-07-01 --test-from 2025-07-01 --output training.jsonl
uv run --extra ml python -m scripts.train_models --dataset training.jsonl --output models/v1.nbamodel
uv run --extra ml python -m scripts.predict_models --model models/v1.nbamodel --season 2025-26 --game-id 0022500123 --output predictions.jsonl
```

On Windows, the same commands work in PowerShell. Once dependencies are installed,
`.\.venv\Scripts\python.exe -m scripts.train_models ...` and the other direct Python
commands run without uv on PATH. Create the output parent directory beforehand.
Dataset preparation and prediction use the published loopback PostgreSQL/ClickHouse
configuration from the Phase 5 runbook. Training uses only its local input file and
does not read database settings. Source CLIs validate the four analytical schemas
without migrating, refreshing or rebuilding snapshots.

Selectors are `--season` or an inclusive `--season-from`/`--season-to` pair. Dataset
preparation requires `--validation-from`, `--test-from` and `--output`, and has no
`--game-id`. Prediction permits `--game-id` only with a single season. Its baseline
configuration comes from the selected model bundle.

Cutoffs mean midnight UTC. Whole games are split chronologically, with one excluded
day before each cutoff by default (`--embargo-days` accepts 1–30). This is an offline
holdout: canonical sources have no actual wall-clock `finished_at`, and provider
corrections are not historical as-of replay. Phase 9 addresses replay semantics.
Each head needs two eligible train games, variation in train labels and at least one
eligible validation and test game. Dataset export can succeed with insufficient
counts; training rejects that dataset before fitting.

CPU defaults are 1000 iterations, depth 6, learning rate 0.03, L2 3, seed 42 and one
thread. `--early-stopping-rounds` defaults to 100 and must not exceed iterations;
for a quick run pass both `--iterations 12 --early-stopping-rounds 3`.
`--max-rows-per-partition` defaults to 1,000,000 (maximum 10,000,000) before native
pool allocation. This limits rows, not RAM in bytes. Per-game weights give every
game equal total influence. Test data is evaluated after model selection, and
baseline comparisons use the same rows with recomputed weights.

The single `.nbamodel` contains checked manifest/metrics and three native CBM files.
Limits are 512 MiB for the bundle, 128 MiB per model and 16 MiB per JSON entry;
JSONL records/manifests also have a 16 MiB ceiling. Model recipe revision and exact
artifact checksum are separate identities. Repeated predictions from the same
loaded artifact are deterministic; fresh fit CBM bytes and cross-platform fit bits
are not promised identical. Dataset/prediction output paths do not affect bytes.

Invalid and terminal states are retained with null rates and reasons. Finite negative
model output clips to zero with a recorded flag. Nonfinite output and overflow fail
the complete export. All workflows prepare, read back and fsync before one atomic
replace. Precommit failure/cancellation preserves the old file; a committed result
survives cleanup interrupts. The owned CPU child receives an allowlisted environment,
is terminated/reaped on cancellation, and leaves no training work running afterward.

Exit `0`: train published all three models, dataset is trainable, or every prediction
row is available. Exit `1`: dataset published with insufficient counts, or prediction
export contains invalid/terminal rows. Exit `2`: precommit operational/configuration
failure. Exit `130`: interruption before publication. Metrics are reported as measured;
publication has no synthetic threshold requiring a model to beat baselines.

Optional Make targets:

```text
make training-dataset SEASON=2025-26 VALIDATION_FROM=2025-11-01 TEST_FROM=2025-12-01 OUTPUT=training.jsonl
make train-models DATASET=training.jsonl OUTPUT=models/v1.nbamodel
make predict-models SEASON=2025-26 MODEL=models/v1.nbamodel OUTPUT=predictions.jsonl
```

The separate ML image leaves the API image dependency scope unchanged:

```text
docker build -f docker/ml.Dockerfile -t nba-bet-ml .
docker run --rm --mount type=bind,source=/absolute/input,target=/input,readonly --mount type=bind,source=/absolute/output,target=/output nba-bet-ml -m scripts.train_models --dataset /input/training.jsonl --output /output/v1.nbamodel
```

It runs as nonroot UID 999 with Python 3.14.8 and frozen optional dependencies.
Use the ML extra for native tests; without it, native-only cases are explicitly skipped,
while dataset preparation, help and missing-extra checks remain available.

The [Phase 7 checkpoint](docs/superpowers/reports/2026-10-13-phase-7-acceptance.md)
records Windows/Linux native acceptance, the owned Phase 2→7 pipeline, full-suite
results and the implementation decisions. Accuracy calibration remains Phase 9 work.

## Phase 8 offline Monte Carlo totals

The [Phase 8 design](docs/superpowers/specs/2026-10-14-phase-8-monte-carlo-design.md)
uses a checked Phase 7 bundle and published Phase 5 features. It reads verified snapshots,
checks the existing four ClickHouse schemas and PostgreSQL provenance, and writes one
canonical JSONL file. It performs no NBA requests, snapshot rebuilds or store writes.
Output parent directories must exist; model/config inputs and their hardlink/symlink
aliases cannot be output destinations. Run schema migrations and materialization using
the existing operator commands before simulation.

Install `uv sync --frozen --extra ml`. Set `DATABASE_URL`, `CLICKHOUSE_URL`, and existing
ClickHouse credentials for your materialized stores, then run directly in PowerShell:

```powershell
uv run --extra ml python -m scripts.simulate_totals --season 2025-26 --model models/v1.nbamodel --output totals.jsonl --simulations 10000 --seed 42 --line 210 --line 210.5 --quantile 0.05 --quantile 0.5 --quantile 0.95
uv run --extra ml python -m scripts.simulate_totals --season 2025-26 --game-id 0022500001 --state-number 3 --model models/v1.nbamodel --output one-state.jsonl --simulations 128 --batch-size 64
```

Use `--season-from 2024-25 --season-to 2025-26` instead of `--season` for a range.
A game requires one season; a state requires one game and must exist in its snapshot.
Repeated `--line` values must be nonnegative multiples of 0.5; `--quantile` accepts
0 through 1 in exact millionths. Query values are sorted and deduplicated. Default
quantiles are 0.05, 0.5, 0.95; there are no default lines. Query changes preserve the
sampled population and seed, while changing the run's semantic revision.

N precedence is explicit `--simulations`, then `MONTE_CARLO_SIMULATIONS`, then 10000.
The omitted batch size is `min(1024, N)`. A supplied `--config` replaces all kernel
settings and ignores that environment variable; kernel flags cannot accompany it.
Queries remain independent. Config JSON must be the exact canonical projection plus
one LF, with its policy version, integer fields and integer score weights. Generate it
through the public projection, rather than a formatter that changes its bytes:

```powershell
@'
from pathlib import Path
from nba_live_totals.domain.canonical import canonical_json_bytes
from nba_live_totals.domain.simulation import SimulationConfig
from nba_live_totals.domain.simulation_hashing import simulation_config_projection
config = SimulationConfig(simulations=10000, batch_size=1024, seed=42)
Path("simulation-config.json").write_bytes(canonical_json_bytes(simulation_config_projection(config)) + b"\n")
'@ | uv run python -
uv run --extra ml python -m scripts.simulate_totals --season 2025-26 --model models/v1.nbamodel --output totals.jsonl --config simulation-config.json --line 210.5
```

Where GNU Make is installed, the equivalent command is:

```text
make simulate-totals SEASON=2025-26 MODEL=models/v1.nbamodel OUTPUT=totals.jsonl SIMULATIONS=10000 SEED=42 LINES="210 210.5" QUANTILES="0.05 0.5 0.95"
```

The target also accepts `SEASON_FROM`, `SEASON_TO`, `GAME_ID`, `STATE_NUMBER`, `CONFIG`,
`BATCH_SIZE`, `MAX_OVERTIMES`, and `MAX_POSSESSIONS_PER_HORIZON`. The existing ML image
supports the same CLI: `nba-bet-ml -m scripts.simulate_totals --help`. Bind model/config
inputs read-only and an output directory writable by its nonroot UID 999.

Exit 0 means every selected state completed all requested paths. Exit 1 means an artifact
was published with unavailable states or unresolved paths. Exit 2 means failure before
publication; exit 130 means interruption before publication. The summary prints state and
unresolved-reason counts, semantic run revision and exact artifact SHA. Publication uses
one atomic replace, so a failed/canceled run keeps previous bytes. An interruption during
cleanup after publication preserves the committed result.

Each row includes frozen causal rates, PMFs, seed/runtime identity, completed home/away/total
histograms, quantiles and line counts. The simulator freezes current rates through future
300-second OTs. Possession counts use the integrated exponential-duration model with
alternating offense and an unbiased initial side; integer score PMFs match the predicted
PPP. Remaining ties at the future-OT cap and draws exceeding the possession cap are
reported as unresolved, never resampled or clamped. Incomplete rows have null full-population
summaries/probabilities and explicit unknown mass. Terminal and invalid states remain
unavailable rows. Marginal histograms do not reconstruct a joint score distribution.

The default score prior and duration model are explicit versioned assumptions awaiting
Phase 9 calibration. These distributions are implementation outputs, not validated betting
probabilities. Reproducibility requires the same pinned Python/NumPy/OS/architecture,
model, causal state, kernel config and seed; cross-runtime byte equality is not promised.


The [Phase 8 checkpoint](docs/superpowers/reports/2026-10-15-phase-8-acceptance.md)
records acceptance on 2026-10-16: 2722 tests passed, 21 expected skips, native
Windows/Linux CLI checks and a fresh independent review with no findings.
Phase 9 historical replay and empirical calibration are the next unimplemented phase.
