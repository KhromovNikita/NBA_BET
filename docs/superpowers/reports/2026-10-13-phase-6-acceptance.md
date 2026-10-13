# Phase 6 Baselines — Acceptance Checkpoint

Дата: 2026-10-13 (Europe/Moscow)

Реализованы pure historical/live/shrinkage baselines, exact fixed-point arithmetic,
canonical config/revision hashes, read-only published-source adapter, sequential
orchestration, atomic streaming JSONL export, CLI, Make target и operator runbook.

## Verification

| Gate | Результат |
|---|---|
| Initial unit/API | 1727 passed, 42.23 s |
| Focused new integration/E2E | 4 passed, 41.12 s |
| Initial full `pytest -q -m 'not live' --tb=short -rs` | 2208 passed, 18 skipped, 905.13 s |
| Post-review CLI/service/exporter | 22 passed, 10.31 s |
| Final full `pytest -q -m 'not live' --tb=short -rs` | 2211 passed, 18 skipped, 911.64 s |
| Ruff check / format | Clean; 260 files formatted |
| Strict mypy | No issues in 239 source files |
| PostgreSQL migrations, twice | Successful, head unchanged |
| ClickHouse migrations, twice | Successful, four schemas unchanged |
| Container CLI help / runtime UID | Successful / UID 999 |
| Git whitespace check | Clean |

18 skips: three opt-in live-canary modules and 15 destructive migration cases.
No new migration is introduced; destructive migration execution was previously verified
in the Phase 5 recovery checkpoint. NBA.com was not requested by the Phase 6 workflow.

The full suite includes earlier-phase regressions; focused/unit counts are subsets and
must not be added to 2211 as unique tests.

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
- Forecast accuracy and calibration remain deferred. The defaults are reproducible
  references within the approved scope; treating them as calibrated forecasts risks
  unreliable predictive estimates.
- Native NBA recovery remains unsupported by normalizer V1. The explicit fixture
  demonstrates the domain's reset contract, not a provider capability. If this boundary
  is misunderstood, operators can expect recovery the provider cannot supply.
- Freshness after the final recheck is pinned rather than transactional across the
  databases and filesystem. The artifact records exact checked source identities;
  it can lag a later database pointer update. A distributed transaction is outside scope.

An ordinary export includes an unavailable pregame live outcome, so exit code 1 is
normally expected for a successfully published artifact. NULL rates have explicit
reasons, invalid states remain unavailable, and later valid states recover independently
of snapshot-wide invalid quality. Parameters 20/40 are uncalibrated defaults.

No training, final-score forecast, betting probabilities, API or live worker was added.
Phase 7 requires its own design before implementation.

## Final review and cleanup

Independent whole-change review of `bcdb5e2..f8b446a`: completed by a fresh reviewer
with `gpt-6-astra`. No Critical or Minor findings; one Important finding confirmed.

Two actual SIGINTs during cleanup after publication caused Python's default
`asyncio.Runner` to escalate `KeyboardInterrupt` outside the main task. The file was
committed, but the CLI returned 130 without its publication summary. A subprocess
regression using the real service, exporter, `os.replace` and threaded I/O reproduced
the failure (RED: expected 1, received 130).
The product fix and regression were committed locally as `848c57b`.

The fix gives the CLI ownership of SIGINT cancellation for its main task, keeping
worker joining and committed-result handling within that task. The previous handler
is restored after the runner closes. GREEN checks cover two SIGINTs after publication,
two before publication, and one after publication; all 22 focused CLI/service/exporter
checks passed. Before publication, cancellation still returns 130 and preserves the
old file; after publication, the summary and complete-artifact exit code survive.
Strict mypy passes for 239 source files; Ruff check and format pass for 260 files.

The three reviewer items declined for judgement were resolved above: uncalibrated
forecast accuracy, native NBA recovery, and cross-store freshness after final recheck.
Deferred minors: none. The fix is verified through RED→GREEN and a full suite rerun;
no second review was dispatched.

Post-fix full-suite rerun: 2211 passed, 18 expected skips, 911.64 s. The rebuilt
production image also passed CLI help and UID 999 checks after the fix.
Temporary Compose project cleanup: completed after verification. Resolved Compose
configuration and volume ownership were checked before `down --volumes`; no container,
volume or network carrying the exact `nba-phase6-37e0bb62` project label remains.
Other Compose projects and their volumes were not selected for cleanup.
Essential execution decisions and verification evidence are archived here. Removal of
the exact owned `.superpowers/sdd/2026-10-13-phase-6-baselines` scratch directory was
rejected before execution by the automatic command approval policy (`blocked by policy`),
despite absolute-path and plan-marker checks. It remains ignored with temporary logs;
no alternate deletion mechanism was used. This does not affect the implemented product.
