# Phase 6 Baselines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать воспроизводимые historical/live/shrinkage оценки pace и PPP поверх Phase 5, с offline CLI и атомарным JSONL-экспортом.

**Architecture:** Pure domain рассчитывает один state независимо от последующих states. Read-only adapter разрешает PostgreSQL published pointers и читает exact verified ClickHouse snapshots. Application обрабатывает одну игру за раз, экспортирует outcomes потоково, повторно проверяет все источники и публикует один файл через `os.replace`.

**Tech Stack:** Python 3.14, frozen slotted dataclasses, StrEnum, Fraction/integer arithmetic, asyncio, существующие SQLAlchemy/asyncpg и ClickHouse HTTP adapters, hashlib/json/pathlib/tempfile, pytest, Ruff, strict mypy, Docker Compose. Новые зависимости не нужны.

**Spec:** [Approved Phase 6 design](../specs/2026-10-12-phase-6-baselines-design.md), письменный review одобрен пользователем 2026-10-13.

## Global Constraints

- Работать в текущем checkout. Сохранённый Phase 5 plan фиксирует пользовательский отказ от worktrees; отдельного разрешения на worktree, push или merge нет.
- Перед исполнением проверить актуальные AGENTS.md и `git status`; не сбрасывать и не включать в commit чужие изменения.
- `BASELINE_SCALE = FEATURE_SCALE = 1_000_000`; formula version `baseline-formulas-v1`; output schema `baseline-jsonl-v1`.
- `history_window in {5, 10, 20}`, default `20`; `ppp_prior_possessions=20`; `pace_prior_combined_possessions=40`. Strengths — exact positive integers, bool запрещён.
- Все конечные метрики и веса — nonnegative signed Int64; missing — `None`/JSON null. Точный ноль допустим. Float, NaN, infinity, imputation и числовые строки запрещены.
- Промежуточные выражения точны. Округление round-half-away-from-zero — только для конечной метрики/веса; rounded historical estimate не используется как intermediate shrinkage prior.
- Методы идут строго `historical`, `live`, `shrinkage`: ровно три rows на каждый state, включая pregame и invalid states.
- Никаких target final box-score counters, будущих states, будущих OT, league constants, lineup adjustments, labels, totals, models, backtest, API или live worker.
- Источники только локальные: exact PostgreSQL references и проверка обоих ClickHouse feature tables. Нет network fallback, claims, build runs, migrations или writes в базы.
- Не менять product contracts, схемы, hashes, parsing и publication rules Phase 1–5. В частности, не исправлять существующий join-version default в рамках Phase 6; явно передавать все configured versions.
- Одна игра в памяти; outcomes сезона накапливаются на диске. Source reference list допускается в памяти.
- Export UTF-8/LF, sorted JSON keys, compact separators, `ensure_ascii=False`; никакого времени запуска, random run ID или output path в artifact identity.
- Оба temporary files лежат рядом с destination. До `os.replace` ошибка/отмена сохраняет старый destination; после commit point опубликованный файл считается результатом.
- Read-only source failures отменяют весь выбранный export. Нормальная missing-data row не является operational failure.
- Exit `0`: все rows available; `1`: complete artifact с partial/unavailable; `2`: validation/source/schema/I/O error до публикации; `130`: interrupt до публикации.
- Обычный полный dataset включает unavailable pregame live row, поэтому штатный CLI обычно возвращает `1`, сохранив исправный artifact. Не менять это правило без изменения одобренной спецификации.
- RED/GREEN выполнять для product behavior, а не для самого markdown. Использовать `apply_patch` для edits; каждый task завершать своим commit после relevant checks.

## Review Focus

1. Historical input имеет дробный fixed-point prior: shrinkage округляет конечную дробь, а не предварительно округлённый prior; тест Task 2.
2. Snapshot-wide invalid quality после восстановленного lineup: только invalid states unavailable, последующие valid states рассчитываются; тесты Tasks 2 и 9.
3. Другой процесс меняет published pointer или предыдущую dependency после успешного read: финальная recheck отменяет export, старый файл остаётся; тесты Tasks 5 и 7.
4. Cancel во время worker I/O или Windows `PermissionError` на replace: worker завершается до удаления temp files, destination не исчезает; тест Task 6.
5. Successful atomic replace уже выполнен, но cleanup/второй interrupt происходит позже: не возвращать ошибку «artifact not published» и не удалять готовый файл; тесты Tasks 6–8.

---

## File Structure and Responsibilities

| Files | Responsibility |
|---|---|
| `domain/baselines.py` | Typed config/results, validation и pure calculator |
| `domain/baseline_hashing.py` | Canonical config/revision/outcome projections |
| `application/baselines/{__init__,models,ports,errors,service}.py` | Request/result, dependency boundaries и export workflow |
| `infrastructure/postgres/baseline_source.py` | Existing Phase 5 readers, exact freshness checks |
| `infrastructure/files/{__init__,baseline_export}.py` | Temp body, final manifest, verification и atomic publication |
| `scripts/build_baselines.py` | Arguments, settings composition, resource cleanup, exit codes |
| `tests/unit/domain/baseline_helpers.py` | Small independently typed numeric fixtures |
| `tests/unit/domain/test_baselines.py`, `test_baseline_hashing.py` | Formulas, quality, identities |
| `tests/unit/application/baseline_fakes.py`, `test_baseline_models.py`, `test_baseline_service.py` | Workflow contract и failure injection |
| `tests/unit/infrastructure/postgres/test_baseline_source.py` | Adapter call protocol без внешних stores |
| `tests/unit/infrastructure/files/test_baseline_export.py` | Real tmp-path filesystem и cancellation boundaries |
| `tests/unit/test_build_baselines_cli.py` | CLI selectors, composition, summaries, safe errors |
| `tests/integration/baseline_harness.py`, `test_baseline_source.py` | Owned real-store fixture, freshness и отсутствие writes |
| `tests/e2e/test_baselines.py` | Full fixture pipeline и real CLI |
| `Makefile`, `README.md`, `tests/unit/test_operator_documentation.py` | Operator entry point и runbook |

Все product paths в таблице относительны к `src/nba_live_totals/`, если не начинаются с `scripts/`, `tests/` или корневого filename. Создавать package `__init__.py` вместе с первым потребителем, не добавлять пустые пакеты заранее.

## Frozen Interfaces and Artifact Fields

Task 1 defines domain types:

```text
BaselineConfig(history_window=20, ppp_prior_possessions=20, pace_prior_combined_possessions=40)
BaselineMethod: HISTORICAL='historical', LIVE='live', SHRINKAGE='shrinkage'
BaselineStatus: AVAILABLE='available', PARTIAL='partial', UNAVAILABLE='unavailable'
BaselineUnavailableReason:
  MISSING_HISTORICAL_INPUTS='missing_historical_inputs'
  NO_LIVE_OBSERVATIONS='no_live_observations'
  MISSING_LIVE_INPUTS='missing_live_inputs'
  INVALID_FEATURE_STATE='invalid_feature_state'
BaselineMetric(value: int | None, reason: BaselineUnavailableReason | None, live_weight: int | None = None)
StateBaseline(state: GameStateFeature, method: BaselineMethod,
              history_window: int, home_games_used: int, away_games_used: int,
              pace: BaselineMetric, home_ppp: BaselineMetric, away_ppp: BaselineMetric)
  status: BaselineStatus  # derived from the three metric values
DeterministicBaselineCalculator.calculate_state(state: GameStateFeature, config: BaselineConfig)
  -> tuple[StateBaseline, StateBaseline, StateBaseline]
DeterministicBaselineCalculator.calculate_dataset(dataset: FeatureDataset, config: BaselineConfig)
  -> Iterator[StateBaseline]
```

Task 4 defines application types:

```text
BuildBaselinesRequest(seasons: tuple[SeasonId, ...], output_path: Path,
                     game_id: GameId | None = None, config: BaselineConfig = default)
  from_range(season_from: SeasonId, season_to: SeasonId, *, output_path: Path,
             config: BaselineConfig = default) -> BuildBaselinesRequest
BaselineSourceReference(game: Game, reference: CurrentFeatureSnapshotReference)
BaselineArtifactStats(games: int, states: int, row_count: int, available: int,
                      partial: int, unavailable: int, outcome_sha256: str, artifact_sha256: str)
BuildBaselinesResult(output_path: Path, stats: BaselineArtifactStats)
  exit_code: int  # 0 or 1, derived from partial/unavailable counters
BaselineExportError(code: str)  # bounded safe code, no raw exception text
```

Use the exact projection keys below; later tasks must not introduce aliases:

- Config: `schema='baseline-config-v1'`, `formula_version`, `output_schema_version`, `scale`, `history_window`, `ppp_prior_possessions`, `pace_prior_combined_possessions`.
- Revision: `schema='baseline-revision-v1'`, `game_id`, `feature_revision_id`, `feature_output_sha256`, `state_number`, `boundary_sequence_number`, `boundary_side`, `method`, `config_sha256`, `formula_version`, `output_schema_version`.
- Outcome: `record_type='baseline'`, `game_id`, `state_number`, `state_kind`, `boundary_sequence_number`, `boundary_side`, `method`, `feature_snapshot_id`, `baseline_revision_sha256`, `period`, `seconds_remaining_game_ms`, `seconds_remaining_period_ms`, `home_score`, `away_score`, `status`, `home_games_used`, `away_games_used`, `home_possessions`, `away_possessions`, `combined_possessions`, `pace`, `home_ppp`, `away_ppp`, `pace_unavailable_reason`, `home_ppp_unavailable_reason`, `away_ppp_unavailable_reason`, `pace_live_weight`, `home_ppp_live_weight`, `away_ppp_live_weight`.
- Manifest: `record_type='manifest'`, `formula_version`, `output_schema_version`, `scale`, `selectors`, `config`, `config_sha256`, `sources`, `outcome_row_count`, `outcome_sha256`.
- Selectors: `seasons` as ordered season strings and `game_id` as string/null; no output path.
- Each source: `game_id`, `feature_snapshot_id`, `feature_revision_id`, `feature_input_token_sha256`, `feature_output_sha256`, `state_output_sha256`, `player_output_sha256`, `state_row_count`, `player_row_count`, `feature_versions` (all five existing names), `source_status`, `issue_codes`.

Every outcome line ends with LF. `outcome_sha256` hashes exactly the concatenated outcome bytes including their LF. `artifact_sha256` hashes the entire final file, including manifest LF. Manifest must not contain its own artifact hash. Semantic revision excludes operational snapshot UUID; outcome and manifest retain it, so exact snapshot identity affects artifact bytes.

### Task 1: Define domain contracts and numeric fixtures

**Files:** Create `src/nba_live_totals/domain/baselines.py`, `tests/unit/domain/baseline_helpers.py`, `tests/unit/domain/test_baselines.py`.

**Interfaces:** Consumes existing `GameStateFeature`, `FeatureDataset`, `FEATURE_SCALE`. Produces the config, enums, metrics and result types above. Fixture helpers: `make_state(**changes: object) -> GameStateFeature`, `make_dataset(*states: GameStateFeature) -> FeatureDataset`.

- [x] **Step 1: Add `test_config_defaults_and_metric_invariants` and parametric validation tests.**

```python
config = BaselineConfig()
assert (
    config.history_window,
    config.ppp_prior_possessions,
    config.pace_prior_combined_possessions,
) == (20, 20, 40)
assert BaselineMetric(0, None).value == 0
with pytest.raises(ValueError):
    BaselineMetric(None, None)
with pytest.raises(ValueError):
    BaselineConfig(history_window=7)
```

Also reject bool/float strengths, zero/negative strengths, negative values, over-Int64 values, non-null reason with available value, out-of-range weight. Unavailable metric requires non-null reason and null weight; valid weight range is `0..1_000_000`. Freeze every dataclass. Derive row status from available metric count, and require no weights on historical/live rows.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baselines.py -q`.** Expected RED: missing baseline module/contracts.
- [x] **Step 3: Implement the contracts and independent fixture builder.** Default fixture is after-possession state 40, home/away counts 20/20, home off/def ratings 120/110, away off/def ratings 110/100, home/away pace 100/104, current live pace 110, PPP 1.5/0.8. All rates use scale. Use existing valid lineup IDs and boundary helpers; initialize all nested Phase 5 objects needed by its dataclasses.
- [x] **Step 4: Run the focused tests, Ruff and strict mypy.** Require GREEN; no changes to existing feature types.
- [x] **Step 5: Commit only this task's files:** `feat: define deterministic baseline contracts`.

### Task 2: Implement historical, live and shrinkage calculations

**Files:** Modify `src/nba_live_totals/domain/baselines.py`; extend `tests/unit/domain/test_baselines.py` and its helper.

**Interfaces:** Consumes Task 1. Produces `DeterministicBaselineCalculator.calculate_state` and lazy `calculate_dataset` with signatures above.

- [x] **Step 1: Add `test_baselines_use_independent_team_counts_and_exact_priors`.**

```python
historical, live, mixed = DeterministicBaselineCalculator().calculate_state(
    make_state(), BaselineConfig()
)
assert (historical.pace.value, historical.home_ppp.value, historical.away_ppp.value) == (
    102_000_000,
    1_100_000,
    1_100_000,
)
assert (live.pace.value, live.home_ppp.value, live.away_ppp.value) == (
    110_000_000,
    1_500_000,
    800_000,
)
assert (mixed.pace.value, mixed.home_ppp.value, mixed.away_ppp.value) == (
    106_000_000,
    1_300_000,
    950_000,
)
assert mixed.home_ppp.live_weight == mixed.away_ppp.live_weight == mixed.pace.live_weight == 500_000
```

Add `test_shrinkage_does_not_round_prior_before_blending`: home off rating `0`, away defensive rating `100` encoded units, live home PPP `0`, home n `1`, PPP k `1`; historical home PPP equals `1`, mixed home PPP equals `0`, mixed weight equals `500_000`. This pins a rational prior of `0.5` encoded units.

Add named cases for window 5/10/20; distinct home/away n; no prior; no live n; positive n with missing live; real zero; half ties; Int64 overflow; invalid input type; missing pregame/live objects; pregame with nonzero counters. For zero n with a numeric damaged live input, live stays unavailable, while mixed equals available prior with zero weight.

Add `test_invalid_state_then_recovery_preserves_later_estimates`: a three-state dataset with valid pregame, invalid state 1, recovered valid state 2 produces nine rows; only the middle three have `invalid_feature_state`. Add `test_later_state_mutation_preserves_earlier_numeric_outcomes`; compare values/status/reasons/weights, not source-wide revisions.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baselines.py -q`.** Expected RED: missing calculator or mismatched expected numbers.
- [x] **Step 3: Implement exact formulas.** Historical rational numerators are `(home.off + away.def)/200`, `(away.off + home.def)/200`, `(home.pace + away.pace)/2` in encoded units. Live uses current prefix metrics. Shrinkage uses the unrounded rational prior and exact `n/(n+k)`. Validate required nested inputs, game identity, pregame counters and sequence before yielding results. In dataset mode require identical pregame profiles across states; do not silently sort damaged state inputs or infer missing players.
- [x] **Step 4: Run domain regressions:** `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baselines.py tests/unit/domain/test_features.py tests/unit/domain/test_live_features.py tests/unit/domain/test_team_pregame_features.py -q`. Require GREEN, plus Ruff/mypy.
- [x] **Step 5: Commit:** `feat: calculate causal historical live and shrinkage baselines`.

### Task 3: Freeze canonical config, revision and outcome bytes

**Files:** Create `src/nba_live_totals/domain/baseline_hashing.py`, `tests/unit/domain/test_baseline_hashing.py`.

**Interfaces:** Consumes Task 1–2 and existing `canonical_json_bytes`, `canonical_json_sha256`. Produces:

```text
baseline_config_projection(config: BaselineConfig) -> CanonicalObject
baseline_config_sha256(config: BaselineConfig) -> str
baseline_revision_sha256(*, game_id: GameId, feature_revision_id: str,
                        feature_output_sha256: str, row: StateBaseline,
                        config: BaselineConfig) -> str
baseline_outcome_projection(*, feature_snapshot_id: UUID, feature_revision_id: str,
                            feature_output_sha256: str, row: StateBaseline,
                            config: BaselineConfig) -> CanonicalObject
baseline_outcome_bytes(*, feature_snapshot_id: UUID, feature_revision_id: str,
                       feature_output_sha256: str, row: StateBaseline,
                       config: BaselineConfig) -> bytes  # adds exactly one LF
```

- [x] **Step 1: Add `test_default_config_has_frozen_canonical_identity`.**

```python
assert (
    baseline_config_sha256(BaselineConfig())
    == "a1bc560d77dadccd4e7198fb2181d739581c6281100a2d9d761ab4ae6f0e878e"
)
assert canonical_json_bytes(baseline_config_projection(BaselineConfig())) == (
    b'{"formula_version":"baseline-formulas-v1","history_window":20,'
    b'"output_schema_version":"baseline-jsonl-v1","pace_prior_combined_possessions":40,'
    b'"ppp_prior_possessions":20,"scale":1000000,"schema":"baseline-config-v1"}'
)
```

Add a literal golden outcome line, including null reasons/weights, exact lowercase UUID, state identity and method. Assert no player arrays enter output. Changing config/source semantic hash/state/method changes revision. Changing only snapshot UUID changes outcome bytes but not semantic revision. Reject malformed hashes and mismatched row game IDs; exact LF/UTF-8 remain stable.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baseline_hashing.py -q`.** Expected RED: missing hashing module.
- [x] **Step 3: Implement the frozen projections without serializing the whole dataclass.** Select only the documented keys; preserve nulls and integer units. Validate semantic identity inputs before hashing, use existing canonical encoder and hashlib.
- [x] **Step 4: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baseline_hashing.py tests/unit/domain/test_baselines.py tests/unit/domain/test_feature_hashing.py tests/unit/domain/test_possession_hashing.py -q`, then Ruff/mypy.** Require golden bytes and hashes to pass; do not regenerate goldens merely to fit implementation.
- [x] **Step 5: Commit:** `feat: hash baseline configuration revisions and outcome rows`.

### Task 4: Define application request, source and exporter boundaries

**Files:** Create `src/nba_live_totals/application/baselines/{__init__,models,ports,errors}.py`, `tests/unit/application/baseline_fakes.py`, `tests/unit/application/test_baseline_models.py`.

**Interfaces:** Produces application types above and these protocols:

```text
BaselineSource:
  async select_games(request: BuildBaselinesRequest) -> tuple[GameId, ...]
  async resolve(game_id: GameId) -> BaselineSourceReference
  async read(source: BaselineSourceReference) -> FeatureDataset
  async recheck(source: BaselineSourceReference) -> None
BaselineExporter:
  async begin(request: BuildBaselinesRequest) -> None
  async append_game(source: BaselineSourceReference, rows: Iterable[StateBaseline]) -> None
  async prepare(sources: tuple[BaselineSourceReference, ...]) -> BaselineArtifactStats
  publish() -> None  # synchronous commit point, no await between final recheck and replace
  async abort() -> None  # joins outstanding worker before temp cleanup
```

Artifact stats count method rows: `available + partial + unavailable == row_count == 3 * states`. Source wrapper checks game/reference identity and published status. Request normalizes ordered contiguous season selectors, enforces matching single season for game ID and nonempty output path. Output path is not included in source/config identity. Do not invent a baseline database run ID.

- [x] **Step 1: Add `test_request_range_and_result_exit_contract`.**

```python
request = BuildBaselinesRequest.from_range(
    SeasonId("2024-25"), SeasonId("2025-26"), output_path=tmp_path / "out.jsonl"
)
assert request.seasons == (SeasonId("2024-25"), SeasonId("2025-26"))
assert result_with_counts(available=3, partial=0, unavailable=0).exit_code == 0
assert result_with_counts(available=2, partial=0, unavailable=1).exit_code == 1
```

The test-local `result_with_counts` builds valid stats and source hashes; do not add that helper to product API. Add reversed/gapped ranges, duplicate seasons, mismatching GameId, invalid source reference, illegal counts/hash and bounded error-code cases. Fakes journal lifecycle calls and retain source objects only, not all game datasets.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/application/test_baseline_models.py -q`.** Expected RED: missing package/contracts.
- [x] **Step 3: Implement models/protocols and fakes.** Follow existing frozen dataclasses and `@runtime_checkable Protocol` patterns. Keep imports one-way: domain never imports application.
- [x] **Step 4: Run the focused tests, Ruff and strict mypy.** Require protocols/fakes to typecheck without broad Any in product code.
- [x] **Step 5: Commit:** `feat: define offline baseline application boundaries`.

### Task 5: Read exact published feature snapshots without writes

**Files:** Create `src/nba_live_totals/infrastructure/postgres/baseline_source.py`, `tests/unit/infrastructure/postgres/{__init__,test_baseline_source}.py`. Real-store tests are added in Task 9.

**Interfaces:** Consumes Task 4 and existing `PostgresFeatureWorkStore.select_games`, `.resolve_target`, `.resolve_current`, `ClickHouseFeatureSourceReader.read_target`, `ClickHouseFeatureSnapshotStore.read_snapshot`. Produces `PublishedFeatureBaselineSource(work_store: FeatureWorkStore, source_reader: FeatureSourceReader, snapshot_store: FeatureSnapshotStore, *, versions: FeatureVersions)` implementing `BaselineSource`. The three protocols are the existing Phase 5 public protocols; unit fakes expose and record every forbidden mutation method.

- [x] **Step 1: Add `test_resolve_uses_exact_current_reference_and_never_claims`.**

```python
source = await adapter.resolve(game_id)
assert source.reference == current.reference
assert fake_store.calls == ["resolve_target", "resolve_current"]
assert fake_target_reader.calls == ["read_target"]
assert fake_store.mutation_calls == []
```

Add named cases: CURRENT and INVALID both addressable; ABSENT/STALE fail; checksum/read error fails safely; changed reference/target/dependency on recheck fails; versions passed exactly, including `possession-lineup-inclusive-v1`; default `FeatureVersions()` is not silently substituted. Selection preserves existing final Regular Season/Playoffs filters using a `BuildDatasetRequest` in VERIFY mode, without calling the Phase 5 build service.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/infrastructure/postgres/test_baseline_source.py -q`.** Expected RED: missing adapter; no real database needed.
- [x] **Step 3: Implement the public-reader wrapper.** Resolve target → read target identities → resolve_current; require CURRENT/INVALID and wrap exact game/reference. `read` calls exact two-table `read_snapshot`. `recheck` resolves afresh and compares the entire wrapper. Translate typed provider/storage errors and missing/stale references to `BaselineExportError` safe codes; never expose raw messages or invoke an implicit build.
- [x] **Step 4: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/infrastructure/postgres/test_baseline_source.py tests/unit/infrastructure/clickhouse/test_feature_source_reader.py tests/unit/infrastructure/clickhouse/test_feature_snapshot_store.py -q`, then Ruff/mypy.** Require GREEN; the baseline adapter must expose no mutation API.
- [x] **Step 5: Commit:** `feat: read published feature snapshots for offline baselines`.

### Task 6: Stream and atomically publish JSONL artifacts

**Files:** Create `src/nba_live_totals/infrastructure/files/{__init__,baseline_export}.py`, `tests/unit/infrastructure/files/{__init__,test_baseline_export}.py`.

**Interfaces:** Consumes Tasks 3–4. Produces `AtomicJsonlBaselineExporter()` implementing `BaselineExporter`. Manifest projection is internal to this adapter, constructed from the exact wrapper references and typed request. `publish()` is legal only after `prepare()` completed and performs exactly one `os.replace`.

- [x] **Step 1: Add `test_streamed_export_is_canonical_and_reproducible`.**

```python
first = await export_fixture(tmp_path / "one.jsonl")
second = await export_fixture(tmp_path / "two.jsonl")
assert first.bytes == second.bytes
assert first.manifest["outcome_row_count"] == 3 * first.states
assert first.stats.artifact_sha256 == hashlib.sha256(first.bytes).hexdigest()
assert first.manifest["outcome_sha256"] == hashlib.sha256(first.outcome_bytes).hexdigest()
assert not list(tmp_path.glob(".baseline-*.tmp"))
```

Define test-local `async export_fixture(path: Path) -> ExportFixture`, with test-only `ExportFixture(bytes: bytes, manifest: dict[str, object], states: int, stats: BaselineArtifactStats, outcome_bytes: bytes)`, using previous typed fixture/fake source metadata. Add real filesystem cases for LF-only bytes, Unicode directory names, missing parent directory, destination directory, PermissionError at replace, fsync/readback failure, corrupted body and successful replacement of an existing file. Use a large single-pass generator to prove rows are neither retained nor iterated twice.

Add deterministic synchronization events for cancellation during append/prepare worker I/O. Assert worker exits before cleanup, old destination bytes survive, both temp files are absent and publish never ran. Add `test_late_abort_cannot_remove_published_destination`: after successful synchronous publish, abort is temp-only and idempotent.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/infrastructure/files/test_baseline_export.py -q`.** Expected RED: missing exporter; use only pytest tmp_path, no Docker.
- [x] **Step 3: Implement two sibling temp files.** Body receives canonical rows once and incremental counters/hash. Prepare builds manifest then copies body in chunks of at most 1 MiB into final temp, flushes/fsyncs, and validates LF/JSON/count/hash by streaming readback. Recompute whole-file hash without self-referential manifest data. Require an existing parent directory; close all handles before Windows replace. No memory list of all outcomes and no delete-destination-then-rename fallback.

Run append/prepare blocking work in one owned `asyncio.to_thread` worker at a time. Shield and join that worker on cancellation before releasing files; no unawaited writer can survive `abort()`. Once prepare succeeds, `publish()` does only synchronous replacement and records committed state. Abort after commit never deletes destination. A failure before commit returns the safe I/O category; a cleanup failure after commit is reported as cleanup evidence, not as an uncommitted export.

- [x] **Step 4: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/infrastructure/files/test_baseline_export.py tests/unit/domain/test_baseline_hashing.py -q`, then Ruff/mypy.** Require all cancellation/failure-injection cases and byte goldens GREEN.
- [x] **Step 5: Commit:** `feat: stream verified atomic baseline JSONL artifacts`.

### Task 7: Orchestrate bounded export with final source checks

**Files:** Create `src/nba_live_totals/application/baselines/service.py`, `tests/unit/application/test_baseline_service.py`; modify package `__init__.py` and baseline fakes.

**Interfaces:** Produces `BaselineBuildService(source: BaselineSource, calculator: DeterministicBaselineCalculator, exporter: BaselineExporter)` and `async build(request: BuildBaselinesRequest) -> BuildBaselinesResult`.

- [x] **Step 1: Add `test_service_rechecks_all_sources_before_single_publication`.**

```python
result = await service.build(request)
assert events == [
    "select",
    "begin",
    "resolve:a",
    "read:a",
    "append:a",
    "recheck:a",
    "resolve:b",
    "read:b",
    "append:b",
    "recheck:b",
    "prepare",
    "recheck:a",
    "recheck:b",
    "publish",
    "abort",
]
assert result.stats.row_count == 3 * result.stats.states
```

Add `test_source_changes_during_final_recheck_preserve_old_output` with prepare already successful; assert no publish and old destination unchanged. Other cases: empty selector `no_eligible_games`; duplicate IDs; out-of-order `(started_at, game_id)`; wrong dataset identity; first/second-game failure; quality-unavailable rows successfully exported; recovered states included; cancelled read/append/prepare/recheck; no next-game prefetch and no retained prior dataset.

Add `test_cleanup_failure_after_commit_preserves_success_result`: committed stats/exit 0 or 1 remain authoritative, safe cleanup log emitted; no second replace, rollback or fabricated exit 2. Before commit, abort cleanup must not mask the original error/cancellation.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/application/test_baseline_service.py -q`.** Expected RED: missing service or incorrect call order.
- [x] **Step 3: Implement sequential lifecycle.** Select first and reject empty/duplicate IDs; begin; for each game resolve/read/check identity and chronology/calculate lazily/append/recheck/release dataset; prepare; recheck every retained wrapper; synchronous publish with no intervening await; return typed stats. All exits call abort to join/clean temp resources. A committed result survives subsequent cleanup failures, which are logged using bounded codes/types only.
- [x] **Step 4: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baselines.py tests/unit/domain/test_baseline_hashing.py tests/unit/application/test_baseline_models.py tests/unit/application/test_baseline_service.py tests/unit/application/test_feature_service.py tests/unit/infrastructure/postgres/test_baseline_source.py tests/unit/infrastructure/files/test_baseline_export.py -q`, then Ruff/mypy.** Require GREEN and no database writes introduced.
- [x] **Step 5: Commit:** `feat: orchestrate causal baseline exports and source rechecks`.

### Task 8: Add CLI, Make entry point and operator documentation

**Files:** Create `scripts/build_baselines.py`, `tests/unit/test_build_baselines_cli.py`; modify `Makefile`, `README.md`, `tests/unit/test_operator_documentation.py`.

**Interfaces:** Defines `RunCommand = Callable[[BuildBaselinesRequest, Settings], Awaitable[BuildBaselinesResult]]`; produces `parse_args(argv: Sequence[str] | None = None) -> BuildBaselinesRequest`, `async run(request: BuildBaselinesRequest, settings: Settings) -> BuildBaselinesResult`, `main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int`. No new Settings fields: baseline parameters are request/CLI config; existing settings supply all resource and feature version values.

- [x] **Step 1: Add `test_baseline_cli_selectors_parameters_and_exit_codes`.**

```python
request = parse_args(["--season", "2025-26", "--output", str(tmp_path / "out.jsonl")])
assert request.config == BaselineConfig()
assert request.output_path.is_absolute()
assert main(valid_argv, run_command=successful_partial_export) == 1
assert main(valid_argv, run_command=successful_complete_export) == 0
assert main(valid_argv, run_command=source_failure_before_publish) == 2
assert main(valid_argv, run_command=interrupt_before_publish) == 130
```

Use injected commands, not real stores, for exit tests. Add missing output, selector conflict, unmatched GameId, incomplete/reversed range, invalid W, zero/negative/bool-like/nonnumeric strengths, forbidden `--refresh/--rebuild`, Settings ValidationError, exact five feature versions, schema validation before readers, and closing both resources when one closer fails. Logs/stderr must not contain supplied secret exception text or DSNs.

Add `test_baseline_runbook_documents_partial_pregame_and_atomic_replacement`: README contains exact offline order, fixed-point units, output overwrite behavior and expected pregame unavailable live row/exit 1. Guard Make OUTPUT plus selector rules; make is POSIX-only and PowerShell receives direct equivalents.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/test_build_baselines_cli.py tests/unit/test_operator_documentation.py -q`.** Expected RED: missing CLI or runbook/target contracts.
- [x] **Step 3: Implement argparse and composition.** Required `--output`; exact selectors/options from spec. Resolve Path relative to cwd; normalize config. Validate four existing analytical schemas, then compose existing PG/CH readers, calculator and exporter. Never migrate, backfill, claim or call provider transport. Reuse the resource-close discipline in `scripts/build_dataset.py`, but use Phase 6's exit rules and protect an already committed result from post-publication cleanup interruption.

Summary keys: `status` (`succeeded` or `partial`), `games`, `states`, `available`, `partial`, `unavailable`, `artifact_sha256`. Operational failure uses `baseline_export_failed code=<safe_code>`; no raw exception messages. No output path in logs or artifact identity. Add guarded `make build-baselines SEASON=... OUTPUT=...` and the range/GAME_ID forms; optional `HISTORY_WINDOW`, `PPP_PRIOR_POSSESSIONS`, `PACE_PRIOR_COMBINED_POSSESSIONS` pass exact CLI flags.

The summary is stdout, following the existing operator CLI pattern. Structured logs use only existing JsonFormatter fields (`event`, `game_id`, `row_count`, `status`, `duration_ms`, `error_code`, `error_type`); do not silently rely on new extra keys that the formatter drops, or broaden its exception allowlist.

- [x] **Step 4: Run `.\.venv\Scripts\python.exe -m pytest tests/unit/domain/test_baselines.py tests/unit/domain/test_baseline_hashing.py tests/unit/application/test_baseline_models.py tests/unit/application/test_baseline_service.py tests/unit/infrastructure/postgres/test_baseline_source.py tests/unit/infrastructure/files/test_baseline_export.py tests/unit/test_build_baselines_cli.py tests/unit/test_build_dataset_cli.py tests/unit/test_operator_documentation.py tests/api -q`, then `.\.venv\Scripts\python.exe -m scripts.build_baselines --help` and Ruff/mypy.** Require GREEN, no new dependencies, unchanged API route set.
- [x] **Step 5: Commit:** `feat: add offline baseline CLI and operator workflow`.

### Task 9: Prove real-store safety and full pipeline acceptance

**Files:** Create `tests/integration/baseline_harness.py`, `tests/integration/test_baseline_source.py`, `tests/e2e/test_baselines.py`; update README with acceptance location, approved spec status and `docs/superpowers/reports/2026-10-13-phase-6-acceptance.md` only after actual successful verification. Report execution date must reflect actual completion day if later.

**Interfaces:** `BaselineAcceptance` is a test-only async context manager with `seed_pipeline() -> tuple[BaselineSourceReference, ...]`, `run_cli(*arguments: str) -> tuple[int, str, str]`, `cleanup() -> None`. Reuse existing fixture source/mapper formats and ownership journal patterns, not private product helpers. Separate these test utilities from production code.

Owned fixture IDs: season `2096-97`; prior game `0029696101`, target `0029696102`; game dates `2096-10-01` and `2096-10-03`. Use known valid local lineup/PBP payloads adapted coherently to those IDs and two teams; no provider requests. Preflight every game/team/player/run/snapshot identity before inserting, and journal identities immediately after creation. Own and clean Phase 2–5 rows, including any feature CLI run emitted without stdout. Stop/reap child processes before database teardown; never remove unowned rows.

- [x] **Step 1: Add `test_baseline_source_reads_real_snapshot_without_control_or_analytical_writes`.** Seed and publish Phase 2→3→4→5 for both games in chronological order, recording exact counts/identities of all ingestion/possession/lineup/feature runs, attempts, resources and dependencies plus CH row counts. Baseline select/resolve/read/recheck leaves those projections unchanged. Repeat with explicit configured join version; stale prior mutation and wrong schema/output checksum fail safely.

Add `test_real_baseline_cli_exports_pinned_reproducible_artifact`: single target selection reads a real current dataset and exports exactly `3 * len(dataset.states)` rows. Historical window uses available prefix rather than requiring 20 games. CLI returns `1` due to pregame live unavailable, but artifact is complete, checksum-valid and byte-identical on rerun at another output path; no new Phase 5 run is created.

Add invalid interval/recovery, cold history, out-of-date prior dependency, nonexistent selected game, late source race, and atomic replacement failure cases. Missing/stale source fails exit 2 without replacing an existing output. Numerically compare one state with independently computed expected historical/live/shrinkage values, not calculator output copied into its own oracle.

- [x] **Step 2: Run `.\.venv\Scripts\python.exe -m pytest tests/integration/test_baseline_source.py tests/e2e/test_baselines.py -q --tb=short` against a fresh isolated Compose project.** Expected GREEN if Tasks 1–8 satisfy the full contract; any failing assertion blocks acceptance. Product behavior already has RED/GREEN evidence from its owning tasks. Do not deliberately break working code to fabricate an acceptance-test RED record.
- [x] **Step 3: Complete only gaps exposed by those cases and fixture ownership checks.** Preserve Phase 1–5 semantics. Exporter cancellation and filesystem cases remain unit tests; real-store races target the existing published references and previous causal dependency.
- [x] **Step 4: Run focused new cases, then final isolated full acceptance.** Required commands below; record exact executed/skipped counts and cleanup evidence.
- [x] **Step 5: Commit verified acceptance artifacts:** `test: accept offline baseline pipeline end to end`. Mark Phase 6 spec implemented only after all gates pass; stop before Phase 7 design.

## Verification Commands and Isolation

At each task checkpoint:

```powershell
.\.venv\Scripts\python.exe -m pytest <the-task-test-files> -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe src scripts tests
git diff --check
```

Replace the angle-bracket task file list with the explicit files named in that task; it is a command template, not a requirement left undecided. Use the existing `.venv` or `uv run` if uv is available; install only the committed lockfile if the environment is missing. Production packages and migrations do not change.

For Task 9, create a generated `nba-phase6-<8 hex chars>` Compose project and preflight four free loopback TCP ports using `TcpListener(IPAddress.Loopback, 0)`, collecting unique ports in a HashSet before starting services. Set `COMPOSE_PROJECT_NAME`, four `*_HOST_PORT` values and matching `TEST_API_URL`, `TEST_DATABASE_URL`, `TEST_CLICKHOUSE_URL`, `TEST_REDIS_URL` in one shell. Locate Docker CLI via PATH or the running Docker Desktop installation; do not change machine-wide PATH. Clear only live-canary TEST_NBA opt-ins in that execution shell. Do not reuse normal volumes.

```powershell
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.build_baselines --help
docker compose exec -T api id -u

.\.venv\Scripts\python.exe -m pytest tests/integration/test_baseline_source.py tests/e2e/test_baselines.py -q --tb=short

# Optional regression of unchanged destructive migrations requires proven-empty
# stores; run on a separate newly created project if any owned fixture rows remain.
# The Phase 5 checkpoint already verifies those migrations; no new schema is added.
.\.venv\Scripts\python.exe -m pytest -q -m 'not live' --tb=short -rs
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe src scripts tests
git diff --check
```

Require four healthy services, ready dependency contract, nonzero UID, idempotent migrations, no provider call, correct hashes and zero failures. Ordinary full-suite destructive migration skips are expected with opt-in unset; report them honestly. Phase 5 full acceptance took approximately 14 minutes on this Windows machine; keep user updated during this gate, without narrowing it to save time.

Before cleanup resolve `docker compose config --format json` and compare its exact name to the generated project recorded for this run. Only then run `docker compose down --volumes --remove-orphans` for that project. Verify no containers, volumes or network with its exact project label remain. No shell-composed recursive filesystem removal is needed.

## Spec Coverage and Self-Review

| Spec sections | Owning tasks |
|---|---|
| 1–2: scope, layering, reuse, no new storage | 1, 4, 5, 7, 8 |
| 3–4: scale, fixed versions, parameters, missing/zero | 1, 2, 3, 8 |
| 5–7: historical, live, exact shrinkage | 2 |
| 8: invalid intervals, recovery, structural failure | 1, 2, 5, 7, 9 |
| 9: published references, two-table hashes, races, chronology, bounded memory | 5, 6, 7, 9 |
| 10: complete manifest, semantic identities, LF/golden bytes, temp files, atomicity | 3, 4, 6, 7 |
| 11: selectors, output path, exit status, summary, logs, Make | 4, 8 |
| 12: meaningful unit/integration/E2E and isolated final gate | every task; 9 |
| 13: Python 3.14 and explicit configured feature versions | 5, 8, 9 |

Self-review checkpoints before handing off this plan:

- No requirement is delegated to an undefined helper or unowned task; examples use declared test helpers.
- Domain types and callable signatures remain identical between the producing and consuming task.
- Each of the five Review Focus cases has explicit assertions in its owning task.
- No existing feature default version is silently changed; operational and semantic hashes remain distinct.
- Final source recheck happens after file preparation and immediately before synchronous publication.
- Cancellation joins I/O workers before cleanup; post-commit cleanup never claims publication failed.
- Expected pregame live unavailability and normal CLI exit 1 are explicit, not hidden in an acceptance test.
- Plan contains interfaces, tests and ordering rather than full implementation bodies.

Self-review completed 2026-10-13: spec coverage mapped to all nine tasks; existing reader signatures and regression paths checked; eight Python example blocks parse; the independent default-config golden SHA-256 was calculated from the frozen projection. Product code and runtime tests are not part of this planning change.

## Execution Handoff

Native execution approved and all nine tasks completed on 2026-10-13. Final independent review found one Important post-publication double-SIGINT defect; a subprocess regression reproduced it before the fix. The single fix pass is complete: full acceptance 2211 passed, 18 expected skips, Ruff/format and strict mypy clean. No Critical findings or deferred minors. See the [acceptance checkpoint](../reports/2026-10-13-phase-6-acceptance.md) for review decisions and verification evidence.

Recommended: **Native** — implement the nine tasks sequentially in the current chat, then perform a fresh whole-change review. These tasks share a small set of frozen interfaces and the highest-risk boundaries have explicit focused tests; per-task subagent contexts would add overhead before those interfaces settle.

Alternative: **Subagent-driven** — fresh implementer/reviewer per task, plus final whole-change review. Use only after the user chooses this method. Neither method changes the requirement to run the final isolated acceptance gate.
