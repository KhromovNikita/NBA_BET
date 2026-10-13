# Phase 6 Baselines — Acceptance Checkpoint

Дата: 2026-10-13 (Europe/Moscow)

Реализованы pure historical/live/shrinkage baselines, exact fixed-point arithmetic,
canonical config/revision hashes, read-only published-source adapter, sequential
orchestration, atomic streaming JSONL export, CLI, Make target и operator runbook.

## Verification

| Gate | Результат |
|---|---|
| Unit/API | 1727 passed, 42.23 s |
| Focused new integration/E2E | 4 passed, 41.12 s |
| Full `pytest -q -m 'not live' --tb=short -rs` | 2208 passed, 18 skipped, 905.13 s |
| Ruff check / format | Clean; 258 files formatted |
| Strict mypy | No issues in 238 source files |
| PostgreSQL migrations, twice | Successful, head unchanged |
| ClickHouse migrations, twice | Successful, four schemas unchanged |
| Container CLI help / runtime UID | Successful / UID 999 |
| Git whitespace check | Clean |

18 skips: three opt-in live-canary modules and 15 destructive migration cases.
No new migration is introduced; destructive migration execution was previously verified
in the Phase 5 recovery checkpoint. NBA.com was not requested by the Phase 6 workflow.

The full suite includes earlier-phase regressions; focused/unit counts are subsets and
must not be added to 2208 as unique tests.

## Isolated environment

Compose project `nba-phase6-37e0bb62`, fresh named volumes, loopback ports:
API 61908, PostgreSQL 61909, ClickHouse 61910, Redis 61911. Docker Desktop initially
reported an unavailable engine; restarting its stopped engine restored operation.
The API image was built from the existing frozen lockfile on Python 3.14.8.

Tests journal primary keys before INSERT/upsert, refuse unowned collisions, remove
only owned analytical snapshot UUIDs, and reap subprocesses before database cleanup.
Before/after projections of PostgreSQL tables and all four analytical row counts verify
that baseline source reads and the real CLI make no storage changes.

Two local synthetic games (`0029696101`, `0029696102`, season `2096-97`) run the real
Phase 2 → 3 → 4 → 5 → 6 pipeline. The target has a real causal historical prefix;
the earlier game has cold history. Exports at different paths are byte-identical.
A change to the previous game's canonical box after file preparation makes the final
recheck fail and preserves the previous destination file.

## Deliberate boundaries and execution decisions

- Work remained in the approved current main checkout. Commits are local; no separate
  integration branch was created. Recovery relies on Git history.
- New JSONL goldens explicitly use LF through `.gitattributes`, preventing Windows
  checkout from changing canonical bytes. Without this rule, byte goldens can fail.
- Recovery acceptance uses a separate `fixture-lineup-reset-v1` normalizer with an
  explicit provider-neutral reset. NBA normalizer V1 does not emit resets; production
  Phase 4 semantics remain unchanged. Confusing this fixture with native NBA recovery
  would overstate the provider's capabilities.

An ordinary export includes an unavailable pregame live outcome, so exit code 1 is
normally expected for a successfully published artifact. NULL rates have explicit
reasons, invalid states remain unavailable, and later valid states recover independently
of snapshot-wide invalid quality. Parameters 20/40 are uncalibrated defaults.

No training, final-score forecast, betting probabilities, API or live worker was added.
Phase 7 requires its own design before implementation.

## Final review and cleanup

Independent whole-change review: pending. Runtime acceptance above is complete.
Temporary Compose cleanup: pending until any review fixes have been verified.
