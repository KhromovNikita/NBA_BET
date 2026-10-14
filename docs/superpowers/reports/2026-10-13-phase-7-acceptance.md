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
| Initial full frozen ML suite | 2371 passed, 18 expected skips, 817.76 s |
| Final full suite after review fix | 2371 passed, 18 expected skips, 715.22 s |
| Ruff check / format | Clean; 317 files formatted |
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

Independent whole-change review of `8d8af41..672db72` completed with one fresh
`gpt-6-astra/high` reviewer. All five Review Focus items were checked deliberately.
No Critical findings; one Important finding and one Minor finding.

The Important finding concerned native Pool construction: installed CatBoost1.2.10
defaults `Pool(thread_count=-1)` to the number of CPU cores. Although fit/predict
already specified one thread, train/validation and inference Pool construction
could use all cores. Two policy regressions observed RED with the missing keyword,
then GREEN after both Pool call sites and the typed protocol explicitly required
`thread_count=1`. All 13 native adapter checks passed, 2.33 s. The fix is committed
as `0b0ab44`; the final full suite passed (2371/2371), and the rebuilt Linux image
completed actual native training/bundle readback under UID999. The post-fix Linux
bundle checksum is `52595448f1ac559725fd14c24bd678a3915931f6ee703418754d53190d7a8b91`.
No second reviewer
was dispatched; the single fix pass uses RED→GREEN and a full suite rerun.

Deferred minor: `AtomicModelPredictionExporter._begin` assigns both temporary paths
in one tuple assignment. If the first allocation succeeds and the second fails,
the first empty file is not tracked and therefore survives abort. It contains no
prediction data and the old destination remains intact. Separate assignments would
resolve this cleanup edge; it is deferred under the approved execution workflow.

Docker Desktop stopped between user turns after the successful initial full suite.
The checkpoint helper also first omitted the explicit test ports; these follow-up
runs failed at database connection rather than a product assertion. The engine and
only the owned Compose project were resumed with explicit ports; all four real
pipeline checks then passed again, 62.94 s.

The API container separately confirmed UID999 and no installed CatBoost. Essential
execution decisions and final cleanup outcomes are archived below before owned
runtime scratch is removed.

## Execution decisions and review rulings

Final owned Compose cleanup completed after the post-review gate. Resolved project
name and all three volume labels matched `nba-phase7-f7cef0e` before removal.
After `down --volumes`, zero containers, volumes or networks with that exact project
label remain. No project Python child remained on the host. The tested ML image
`nba-bet-phase7-ml:20261014-0b0ab44` remains available locally.

Essential evidence, all rulings and the deferred minor are archived in this report
before the Phase 7 owned runtime scratch cleanup. The exact absolute scratch path,
all ancestor reparse-point flags and its plan identity were validated. Recursive
removal of `.superpowers/sdd/2026-10-13-phase-7-catboost` was rejected by automatic
policy review with `blocked by policy`. The ignored directory remains; no alternate
deletion mechanism was used. Sibling Phase 6 scratch remains unchanged.

The following exhaustive ledger decisions retain their reason and cost if wrong.

- Ruling: Work in approved current main checkout — preserved user preference and explicit approved plan — cost if wrong: recovery requires Git history rather than disposable worktree removal.
- Ruling: baseline gate uses existing Make unit/API directory selection — root marker filter collects a DB-dependent shared fixture even for selected unit tests, reproducing alembic/asyncpg connection failure while stores are intentionally absent — cost if wrong: DB regressions remain deferred to the required isolated full acceptance gate, not claimed passing now.
- Task 5: Ruling: extend ModelSourceReference with mandatory lightweight PbpLineage, PossessionVersions and LineupVersions — the approved spec requires exact PBP provenance and all source versions in dataset/bundle; Phase5 feature reference alone does not expose them, and retaining raw target snapshots violates the bounded-source requirement — cost if wrong: adjust the source DTO, source adapter and manifest serialization together.
- Task 6: Ruling: endpoint completeness follows known PBP period-end markers, including OT with no new possession state — bounding endpoints by last possession period incorrectly rejects an empty known OT; contiguous endpoint validation now allows later known endpoints and avoids constructing an unbounded range — cost if wrong: revise endpoint validation and source normalization together. Regression test_known_empty_overtime_can_end_without_a_new_possession_state observed RED then GREEN.
- Task 7: Ruling: add TrainingDatasetReader.iter_rows(dataset, partition=None) alongside eligible HeadRows — held-out coverage and regulation/OT exclusions require retained invalid/terminal records, which an eligible-only head view cannot expose — cost if wrong: adapt the reader port and worker evaluator together.
- Task 7: Ruling: JSONL records, including source manifest, use a 16 MiB read ceiling aligned with bundle JSON limits — keeps malformed-file parsing bounded while native bundle provenance has the same limit — cost if wrong: larger otherwise valid source ranges require a distinct dataset manifest ceiling or more compact source metadata.
- Task 9: Ruling: internal CPU jobs use work_dir/training.jsonl as the fixed checked private input, alongside fixed head filenames — prevents arbitrary worker input paths and makes parent workspace ownership explicit; public training request still accepts any validated dataset path — cost if wrong: adapt private-copy naming in the supervisor and job protocol together.
- Task 11: Ruling: add a pure application owned_io helper shared by input copying and process protocol I/O — both require cancellation to join a thread before deleting its files, without making application orchestration import infrastructure — cost if wrong: adjust the shared ownership helper and its lifecycle tests.
- Task 11: Ruling: child env additionally sets PYTHONDONTWRITEBYTECODE and owned MPLCONFIGDIR/TEMP/TMP/TMPDIR — optional native imports can create caches/temporaries, so redirect them into the owned workspace while still excluding application credentials — cost if wrong: accommodate another runtime cache path or adjust cold-import behavior.
- Task 11: Ruling: exact Decimal normalization may use scientific notation for extreme exponents — fixed-format normalization materialized a million zeros for a valid tiny decimal, so normalize coefficient/exponent without Decimal context rounding; ordinary defaults stay identical — cost if wrong: canonical hashes for unusual nondefault decimal overrides require a format-version migration.
- Task 11: Ruling: explicitly pass thread_count=1 to native prediction as well as fit — installed CatBoost predict signature defaults to -1, otherwise held-out/prediction CPU work escapes the frozen single-thread policy — cost if wrong: inference throughput is lower, with the same declared resource contract.
- Task 12: Ruling: prediction LF layout golden uses a typed fixed predictor/identity; real checked native bundle is covered separately by service, probe, roundtrip and cross-path byte tests — fresh native fit artifacts contain volatile metadata and the spec promises no cross-runtime fit byte identity — cost if wrong: layout golden does not freeze learned native values; those are guarded by native roundtrip/causality checks.
- Task 13: Ruling: exclude .superpowers from the Docker build context — owned runtime tools, logs and private artifacts must not enter the image context; .dockerignore was not listed in the task files — cost if wrong: explicitly include any future build dependency currently stored only in scratch.
- Final: Ruling: real-world predictive advantage/calibration/betting suitability remains unclaimed — synthetic tests establish pipeline contracts, and Phase7 expressly excludes an accuracy threshold — cost if wrong: operator could treat uncalibrated rate estimates as reliable decisions.
- Final: Ruling: wall-clock historical replay remains deferred to Phase9 — canonical sources lack finished_at and this implementation reports a chronological holdout limitation — cost if wrong: stricter backtesting must add actual availability-time evidence.
- Final: Ruling: fresh-fit bytes across platforms/runtimes are not promised identical — same loaded artifact encoded predictions are checked; semantic recipe and exact artifact are separate identities — cost if wrong: fresh-fit comparison can expose metadata or numerical differences.
- Final: Ruling: arbitrary hostile remote native-model execution is outside locally owned bundle acceptance — structural/hash/compatibility checks prove integrity and corruption rejection, not origin authentication or native sandboxing — cost if wrong: accepting an untrusted external CBM requires a separate trust boundary.
- Final: Ruling: owned Docker/scratch cleanup stays with the parent after all review verification — reviewer is read-only; validate exact project labels and resolved workspace before removing only Phase7 resources — cost if wrong: an ownership-check error could select unrelated resources.
