# Phase 7 Offline CatBoost — Acceptance Checkpoint

Дата исполнения: 2026-10-14 (Europe/Moscow). Имя документа сохраняет дату
одобренных specification/plan: 2026-10-13.

Реализованы три независимые CPU-модели (`pace`, `home_ppp`, `away_ppp`), exact
known-horizon labels, общий causal-преобразователь 96 numeric features, temporal
whole-game split с embargo, per-game weights, matched baseline comparison,
проверенный native model bundle и три offline CLI. API и live worker не расширены.

## Verification

| Gate | Результат |
|---|---|
| Initial unit/API before implementation | 1730 passed, 61.13 s |
| Unit/API after Task 8 | 1821 passed, 34.11 s |
| CLI / Phase 6 actual SIGINT regressions | 22 passed, 16.45 s |
| Real Phase 2→7 integration/E2E | 4 passed, 137.54 s |
| Full frozen ML suite | 2371 passed, 18 expected skips, 817.76 s |
| Ruff check / format | Clean; 315 files formatted |
| Strict configured mypy | No issues in 292 source files |
| ML image import / UID | Python 3.14.8, CatBoost 1.2.10, NumPy 2.5.2 / 999 |
| Actual Linux native training / bundle readback | Successful, 4 games / 32 states / 3 heads |
| Git whitespace check | Clean |

Focused counts are subsets and must not be added to the full-suite total.
18 skips: three opt-in live-canary modules and 15 destructive migration cases.
No native ML cases were skipped. No new migration is introduced by Phase 7;
destructive migration execution was previously verified in Phase 5.
Windows uses the same frozen Python/CatBoost/NumPy versions as the Linux image.
Make is absent on PATH; equivalent direct CLI commands and Make guards were checked.
The operator runbook provides direct PowerShell commands without Make or global uv.

## Owned real pipeline

Compose project `nba-phase7-f7cef0e`, fresh named volumes, loopback ports:
API 62018, PostgreSQL 62019, ClickHouse 62020, Redis 62021. Docker Desktop was
started before testing. PostgreSQL and four ClickHouse schemas were migrated
explicitly before acceptance; modeling CLIs only validate schemas and read sources.

Five local synthetic games (`0029494101`–`0029494105`, season `2094-95`) run
real ingestion, possession reconstruction, lineup reconstruction and feature
materialization. They provide two train games, one embargo game, one validation
game and one test game. Full regulation and two known OT endpoints are checked.
The first OT label uses only the first OT endpoint; a second-OT endpoint is rejected.
There are 49 states: 22 train, 9 validation, 9 test and 9 embargo. Every training
head has two eligible games and distinct targets. Terminal and invalid states remain
in artifacts; embargo is separate from label availability.

Actual CLI dataset export → hidden CPU child → native bundle → prediction export
is exercised without provider HTTP calls. The checked bundle has three native CBMs,
manifest and metrics. Metrics contain all 18 head/partition/scope cohorts; baseline
and model scores use identical comparison rows/games. Save/load yields identical
encoded predictions. Repeated dataset and prediction exports at different paths
are byte-identical. The earliest cold-history pregame state still receives model
predictions from missing numeric inputs.

The first successful focused acceptance produced these exact SHA-256 identities:

| Artifact | SHA-256 |
|---|---|
| Dataset semantic revision | `eb77750ee28b893f82b51de018149c87c9d5bb0b43a023ba25574eb7d6c36250` |
| Dataset bytes | `11dbb98e97bd1b9ab7cc73cd0dcc89283464bc35f49de0f6e1fdbc5f3bb48885` |
| Bundle bytes | `49e766471fe1ec33cdc7c92beb93fd834ac8fd7bc7eac989689bc59d9b43b57f` |
| Prediction bytes | `06d45159fcb584ac5edb3f1999a43b4a4a406dcd69479afbdde4f2ad04337650` |

These identify that particular run, not cross-platform or newly fitted CBM identity.
Operational source UUIDs change exact bytes after rematerialization; semantic recipe
identity is separate. The Linux native CLI bundle digest was
`69f67dda2de0c6138b9aae174a185423389854bcd08e01c203fedc77f34432de`.

Before/after projections of all PostgreSQL metadata tables and all four analytical
row counts verify no writes from modeling sources/CLIs. A dependency mutation after
dataset preparation rejects publication and preserves the previous output. Ownership
journals refuse pre-existing primary-key collisions, remove only owned snapshot UUIDs
and canonical records, and reap CLI processes before storage cleanup.

Recovery uses separately tagged `fixture-model-reset-v1` with a provider-neutral
reset at period 3; production NBA lineup normalizer V1 does not emit this reset.
Late valid states recover independently of the snapshot-wide invalid status.

## Deliberate boundaries

Synthetic acceptance demonstrates contracts, not forecast accuracy or baseline
superiority. Real historical-data quality assessment/calibration remains separate.
No final-score simulation, betting probabilities, live worker or Phase 8 implementation
was added. Canonical sources have no actual `finished_at`: this is chronological
holdout, not historical wall-clock as-of replay. Cross-store freshness is pinned by
final checks rather than a distributed transaction with filesystem publication.

Limits: 16 MiB per JSONL record/manifest and bundle JSON entry, 128 MiB per CBM,
512 MiB per bundle, configured rows before native pool allocation. Row limits are
not a byte-accurate RAM budget. Fresh fits can have different CBM metadata/bytes;
same loaded artifact predictions and canonical artifact paths are deterministic.

## Final review and cleanup

Full gate passed; independent whole-change review pending. Execution decisions and
review outcomes are archived below before owned runtime scratch is removed.
