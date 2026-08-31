# Task 8 — Async Alembic baseline

## Scope

Implemented only the migration foundation from `task-8-brief.md`:

- deterministic SQLAlchemy `MetaData` naming convention;
- async Alembic environment consuming `Settings().database_url` in both offline and
  online modes;
- safe async-to-sync migration bridge via `AsyncConnection.run_sync`;
- empty `0001_baseline` revision;
- idempotent-upgrade integration test.

No game or domain table was added. `compose.yaml` and its published service ports
were not changed. `graphify-out` was not modified.

## TDD record

### RED

Created `tests/integration/test_migrations.py` first, then ran:

```powershell
uv run pytest tests/integration/test_migrations.py -v -m integration
```

The test collected one case and failed as expected before implementation. Both
subprocesses exited non-zero because Alembic reported:

```text
FAILED: No 'script_location' key found in configuration.
```

This demonstrated the absence of `alembic.ini` rather than a test setup error.

### Implementation

Added `alembic.ini`, async `alembic/env.py`, Alembic revision template, the empty
baseline revision, and the shared metadata object. The online path builds its engine
with `async_engine_from_config`, `pool.NullPool`, and the DSN resolved from
`Settings().database_url`; it invokes synchronous Alembic migration work only through
`await connection.run_sync(do_run_migrations)`.

### Runtime gate — deferred

After implementation, the focused integration command reached the async Alembic
connection path but could not connect to the real local PostgreSQL service:

```powershell
uv run pytest tests/integration/test_migrations.py -v -m integration
```

The test failed because `asyncpg.exceptions.ConnectionDoesNotExistError` reported
that the connection was closed during the operation. The direct online diagnostic
also failed for the same external reason:

```powershell
$env:DATABASE_URL = 'postgresql+asyncpg://nba:nba@localhost:5432/nba'
uv run alembic current
```

Docker was unavailable, so no database was started or simulated. Therefore the real
PostgreSQL idempotent-upgrade assertion is intentionally **deferred**, not reported as
GREEN. With PostgreSQL running, execute:

```powershell
uv run pytest tests/integration/test_migrations.py -v -m integration
```

Expected runtime proof: both `alembic upgrade head` subprocesses return zero.

## GREEN evidence available without PostgreSQL

```powershell
uv run ruff check src tests alembic
# All checks passed!

uv run ruff format --check src tests alembic
# 33 files already formatted

uv run mypy src tests
# Success: no issues found in 31 source files

uv run alembic heads
# 0001_baseline (head)

uv run alembic upgrade head --sql
# Generates only alembic_version DDL and inserts 0001_baseline.

uv run pytest tests/unit tests/api -v
# 28 passed
```

The offline SQL output contains only Alembic's `alembic_version` table and the
baseline version insert, which verifies that this task introduced no game/domain DDL.

## Atomic commit

```powershell
git add alembic alembic.ini src/nba_live_totals/infrastructure/metadata.py tests/integration/test_migrations.py .superpowers/sdd/2026-08-30-phase-1-infrastructure/task-8-report.md
git commit -m "build: add async Alembic baseline"
```

Commit subject: `build: add async Alembic baseline`.
