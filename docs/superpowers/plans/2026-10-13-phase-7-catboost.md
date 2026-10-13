# Phase 7 Offline CatBoost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать полный offline-цикл трёх CPU-моделей темпа и PPP оставшегося основного времени/текущего OT, с temporal holdout, проверенными datasets/models и prediction CLI.

**Architecture:** Pure domain определяет targets, одну versioned numeric projection, split/weights/metrics и prediction policy. Read-only adapter использует public Phase 5 pointers/readers; application строит dataset, supervises отдельный CPU worker и публикует проверенный ZIP model bundle. Prediction использует тот же mapping одного current state и атомарный JSONL-export.

**Tech Stack:** Python 3.14, frozen slotted dataclasses, StrEnum, Fraction/Decimal/isqrt, существующий canonical JSON, asyncio/subprocess, ZIP_STORED/hashlib, CatBoost 1.2.10 optional `ml` extra, NumPy, pytest/Ruff/strict mypy, uv frozen lockfile, Windows и isolated Linux ML image.

**Spec:** [Approved Phase 7 specification](../specs/2026-10-13-phase-7-catboost-design.md), письменное approval пользователя 2026-10-13.

## Global Constraints

- Текущий checkout/main сохраняется по пользовательскому предпочтению предыдущей фазы. Проверить AGENTS.md и git status перед исполнением; не создавать worktree, не reset/push/merge и не включать чужие изменения.
- Никакого product code/dependency installation до review этого плана. Сейчас изменяются только spec/plan docs.
- Три heads строго `pace`, `home_ppp`, `away_ppp`; три отдельных CatBoostRegressor, CPU/thread_count=1.
- Horizon — конец period 4 для pregame/regulation; конец current period p для p>=5. Future OT count/duration не является input или endpoint current horizon.
- Scale=1_000_000; persisted values — exact Int64/null, canonical UTF-8/LF, без JSON floats/NaN/infinity. Half-away-from-zero только на final target/prediction/metric.
- Versions: `remaining-horizon-labels-v1`, `model-feature-projection-v1`, `game-temporal-split-v1`, `training-dataset-jsonl-v1`, `catboost-training-v1`, `model-bundle-v1`, `model-predictions-jsonl-v1`, `nonnegative-rmse-rates-v1`.
- Literal 96 columns/divisors/config, никакого auto-all-dataclass-fields/categorical IDs/target encoding. Projection принимает один state, не dataset/endpoints/final box.
- Runtime NaN допустим только как numeric missing на ML boundary; не подменять missing zero. Negative finite prediction → 0 с clipping flag; nonfinite/overflow → whole-export failure.
- Split по started_at UTC, required ISO validation/test dates, embargo_days default1 integer1–30. Whole-game groups, оба whole-day exclusion windows; не обещать as-of replay при отсутствующем finished_at.
- Train weights=N/(G*n_g) отдельно по head/partition. Test никогда не fit/eval_set/preprocessing/early stopping. Validation выбирает best iteration; нет refit train+validation.
- Minimum каждый head: два eligible train games и два разных train targets; по одному eligible validation/test game. Недостаточность не создаёт фиктивные/частичные models.
- Public Phase 5 CURRENT/INVALID snapshots, exact two-table checksums и explicit configured P/L/F versions. Никаких DB writes/claims/build runs/migrations/NBA calls или private reconstructors в новом adapter.
- Streaming dataset/prediction export держит одну игру; training материализует только текущий head pools. max_rows_per_partition=1_000_000, range1..10_000_000; evaluation batch4096.
- Native fit/evaluate/save — owned subprocess с allowlisted OS runtime env без DB credentials; terminate→wait5s→kill→reap до удаления staging. Windows child без отдельного видимого окна.
- Model bundle — ZIP_STORED с ровно `manifest.json`, `metrics.json`, `pace.cbm`, `home_ppp.cbm`, `away_ppp.cbm`; timestamps1980-01-01; без extractall. Limits512MiB total/128MiB per model/16MiB per JSON.
- Owned same-parent temps, fsync/readback, synchronous os.replace — единственный commit point. Precommit failure/PermissionError сохраняет старый destination; postcommit cleanup/second SIGINT сохраняет result/summary.
- Semantic recipe revision и exact artifact hash различаются. Native .cbm bytes повторного fit/cross-platform не обязаны совпадать; same-runtime round-trip encoded predictions совпадают точно.
- Optional `ml` extra: CatBoost==1.2.10 и NumPy>=2.4,<3; resolved versions frozen в uv.lock. API image остаётся без ML; отдельный offline ML image nonroot. CLI help/dataset preparation работают без native import.
- Exact defaults/overrides берутся из spec §11; decimal config/native diagnostics сериализуются finite normalized strings. Нет HPO/GPU/MLflow/API/Monte Carlo/backtest/calibration/synergy.
- Dataset exit0/1 по trainability; train exit0 при полном bundle, insufficient data→2; predict exit0/1 по row quality. Все precommit failures→2, interruption→130. Empty selectors→2 без artifact.
- TDD RED→GREEN для нового product behavior, relevant regressions и static checks перед каждым commit; markdown не требует искусственных product tests.

## Review Focus

1. Period-4 state в матче с двумя будущими OT и первый-OT state: labels используют только выбранный endpoint; mutation suffix/final box не меняет current projection. Task1/2/6 tests.
2. Только test targets/eligible counts изменились, либо baseline доступен на подмножестве games: fit/selection не меняется; comparison использует matched cohorts и пересчитанные game weights. Task4/9 tests.
3. PBP/possession/feature pointer меняется после read либо после file preparation: source recheck отменяет весь dataset/prediction export, старый destination остаётся. Task6/7/12 tests.
4. Отмена при живом CPU worker и два настоящих SIGINT после successful replace: child reaped до temp cleanup, до commit старый файл сохранён, после commit summary/exit остаётся published. Task11/13 tests.
5. ZIP CRC корректен, но manifest columns/engine/native names либо model checksum несовместимы: loader отказывает до predict, не применяет 96 values к другим признакам. Task10 tests.

---

## File Structure and Dependencies

| Tasks | Files / responsibility |
|---|---|
| 1 | domain/model_targets.py — endpoints/inputs/exact targets; test helpers |
| 2 | domain/model_features.py — descriptor/projection/input hashes |
| 3 | domain/model_training.py — split/config/rows/eligibility/weights |
| 4 | domain/model_predictions.py + model_evaluation.py — clipping/metrics |
| 5 | application/modeling/{errors,models,ports}.py — typed boundaries |
| 6 | infrastructure/postgres/model_source.py — published source adapter |
| 7 | infrastructure/files/training_dataset.py + modeling/dataset_service.py |
| 8 | infrastructure/ml/catboost_adapter.py + optional ML dependencies |
| 9 | infrastructure/ml/training_worker.py — checked job, fit/evaluate/save |
| 10 | infrastructure/files/model_bundle.py — loader/prepared bundle writer |
| 11 | application/modeling/training_service.py + infrastructure/ml/process.py |
| 12 | modeling/prediction_service.py + files/model_predictions.py |
| 13 | scripts CLIs/shared runner, Makefile/README, docker/ml.Dockerfile |
| 14 | owned integration/E2E fixtures and final checkpoint |

Dependencies: 1→2→3→4→5; 5→6/7/8; 6→7; 7+8→9; 8+9→10;
9+10→11; 6+8+10→12; 7+11+12→13; all→14. Выполнять последовательно
при native execution. Setup/docs включены в deliverables, отдельной scaffold-only задачи нет.

### Task 1: Exact known-horizon targets

**Files:** Create `src/nba_live_totals/domain/model_targets.py`;
`tests/unit/domain/modeling_helpers.py`, `tests/unit/domain/test_model_targets.py`.

**Interfaces:** Consumes `GameStateFeature`, `FeatureDataset`, `Game`, `Possession`,
`EventBoundary`. Produces `ModelHead` StrEnum pace/home_ppp/away_ppp;
`HeadTarget(value_scaled: int|None, unavailable_reason: str|None)`;
`RemainingTargets(pace, home_ppp, away_ppp: HeadTarget)`;
`HorizonEnd(game_id, period, boundary, home_score, away_score, home_possessions, away_possessions)`;
`TrainingGameInput(game: Game, features: FeatureDataset, possessions: tuple[Possession,...], period_ends: tuple[HorizonEnd,...])`;
`calculate_remaining_targets(state: GameStateFeature, endpoint: HorizonEnd) -> RemainingTargets`;
`horizon_period(state: GameStateFeature) -> int`.
Helper `make_target_case() -> tuple[GameStateFeature,HorizonEnd]` uses existing
baseline `make_state`: period4/R=60_000, score2:1/counts1:1/state_number2;
endpoint period4/AFTER100/score7:4/counts4:3/same game_id.

- [ ] **Step 1: Write RED numerical/availability tests**

```python
def test_remaining_rates_use_exact_suffix_and_known_clock():
    state, endpoint = make_target_case()
    rates = calculate_remaining_targets(state, endpoint)
    assert rates.pace.value_scaled == 120_000_000
    assert rates.home_ppp.value_scaled == 1_666_667
    assert rates.away_ppp.value_scaled == 1_500_000
```

Add `test_current_ot_rejects_next_ot_endpoint`, `test_terminal_invalid_zero_and_missing`
and `test_negative_suffix_and_boundary_mismatch_raise`: exact spec §6 reasons,
zero points with positive count stays0; no future possessions only masks that PPP;
invalid current state masks all; future invalid lineup does not mask valid prefix.
- [ ] **Step 2: Run RED:** `uv run pytest tests/unit/domain/test_model_targets.py -q`;
expected missing module/function or explicit wrong target assertion.
- [ ] **Step 3: Implement contracts/calculator:** validate exact enums/Int64/identity,
clock/endpoint structure before target masks; derive selected horizon, exact suffix
formula, round once using existing integer ratio rules. TrainingGameInput verifies
state↔possession counts/score/clock/boundary mapping, not arbitrary sorted endpoints.
- [ ] **Step 4: GREEN/regressions:** same test command plus domain features/baselines;
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` all pass.
- [ ] **Step 5: Commit:** `feat: define exact remaining-horizon training targets`.

### Task 2: Frozen common 96-column projection

**Files:** Create `domain/model_features.py`,
`tests/unit/domain/test_model_features.py`,
`tests/fixtures/nba/modeling/default_projection.expected.json`;
modify `.gitattributes` only for new golden LF.

**Interfaces:** Consumes Task1 types plus existing `BaselineConfig` and
`DeterministicBaselineCalculator.calculate_state`. Produces
`FeatureColumn(name: str, divisor: int)`;
`FeatureProjectionDescriptor(version: str, columns: tuple[FeatureColumn,...], baseline_config_sha256: str)` with `sha256`;
`ModelFeatureVector(descriptor_sha256: str, values: tuple[int|None,...])`;
`model_feature_descriptor(config: BaselineConfig) -> FeatureProjectionDescriptor`;
`project_model_features(state: GameStateFeature, baseline_config: BaselineConfig) -> ModelFeatureVector`;
`model_input_sha256(vector: ModelFeatureVector) -> str`.

- [ ] **Step 1: Write RED schema/causality tests**

```python
def test_projection_is_named_fixed_and_has_no_identity_columns():
    descriptor = model_feature_descriptor(BaselineConfig())
    assert len(descriptor.columns) == 96
    assert descriptor.columns[0].name == "period"
    assert descriptor.columns[-1].name == "baseline.shrinkage.away_ppp_live_weight"
    assert all("player_id" not in column.name for column in descriptor.columns)
```

`test_projection_matches_independent_literal_golden` compares full names/divisors/config/hash
to independently constructed literal fixture, not self-generated output. Tests cover
all nested nulls, bool→0/1, signed differences, cold history, selected baseline config,
current-state input hash unchanged by future dataset/final-score mutations.
- [ ] **Step 2: RED:** `uv run pytest tests/unit/domain/test_model_features.py -q`.
- [ ] **Step 3: Implement literal schema/pure mapping:** exact spec §7 field order;
descriptor canonical projection `{version, columns:[{name,divisor}], baseline_config_sha256}`.
Input hash uses descriptor hash+values; no Game/FeatureDataset argument. Explicit
mapping excludes IDs/state_number/targets and never grows through `fields(state)`.
- [ ] **Step 4: GREEN:** target/projection/baseline hashing tests and all static checks;
golden contains LF and independently frozen bytes.
- [ ] **Step 5: Commit:** `feat: project versioned causal model features`.

### Task 3: Temporal rows, config and per-game weights

**Files:** Create `domain/model_training.py`,
`tests/unit/domain/test_model_training.py`; extend modeling_helpers.

**Interfaces:** Consumes Task1/2. Produces
`TemporalPartition` train/validation/test/embargo;
`TemporalSplitConfig(validation_from: date, test_from: date, embargo_days: int=1)`;
`classify_game(started_at: datetime, config: TemporalSplitConfig) -> TemporalPartition`;
`TrainingConfig(iterations=1000, depth=6, learning_rate=Decimal('0.03'), l2_leaf_reg=Decimal('3'), random_seed=42, early_stopping_rounds=100, max_rows_per_partition=1_000_000)`;
`HeadEligibility(eligible: bool, reason: str|None)`;
`TrainingBaseline(method: BaselineMethod, pace: BaselineMetric, home_ppp: BaselineMetric, away_ppp: BaselineMetric)`;
`TrainingRow(game_id, started_at, state_number, boundary, period, horizon_period, is_valid: bool, partition, remaining_time_ms, vector: ModelFeatureVector, targets: RemainingTargets, eligibility: tuple[HeadEligibility,HeadEligibility,HeadEligibility], future_home_possessions, future_away_possessions, future_home_points, future_away_points, baselines: tuple[TrainingBaseline,...])`;
`build_training_rows(inputs: TrainingGameInput, split: TemporalSplitConfig, baseline_config: BaselineConfig) -> Iterator[TrainingRow]`;
`balanced_weights(game_ids: Sequence[GameId]) -> tuple[Fraction,...]`;
`training_config_projection(config: TrainingConfig) -> CanonicalObject` and its hash.

- [ ] **Step 1: RED split/weights/config tests**

```python
def test_embargo_and_whole_game_split():
    config = TemporalSplitConfig(date(2025, 7, 1), date(2026, 7, 1))
    assert classify_game(datetime(2025, 6, 30, tzinfo=UTC), config) is TemporalPartition.EMBARGO
    assert classify_game(datetime(2025, 7, 1, tzinfo=UTC), config) is TemporalPartition.VALIDATION
```

Test equal timestamps/UTC offsets/both gap windows/invalid dates/default1/limits1–30;
each game's rows have one partition. For game IDs A,A,B weights=(3/4,3/4,3/2).
Target reason remains null on available embargo target while eligibility reason=temporal_embargo.
Test Decimal config normalization, bool/nonfinite/out-of-range/early_stop>iterations rejection.
- [ ] **Step 2: RED:** `uv run pytest tests/unit/domain/test_model_training.py -q`.
- [ ] **Step 3: Implement:** one-pass ordered row iterator; exact selected endpoint
per state; all invalid/terminal/embargo rows retained. Weights use only supplied head/partition
eligible game IDs. Freeze spec §11 native defaults and override ranges; no output path in config hash.
- [ ] **Step 4: GREEN:** Tasks1–3 tests plus static checks; no domain native dependencies.
- [ ] **Step 5: Commit:** `feat: define chronological training rows and weights`.

### Task 4: Rate policy and exact matched-cohort metrics

**Files:** Create `domain/model_predictions.py`, `domain/model_evaluation.py`,
`tests/unit/domain/test_model_predictions.py`, `tests/unit/domain/test_model_evaluation.py`.

**Interfaces:** Consumes ModelHead/HeadTarget, ModelFeatureVector and weights rule.
Produces `PredictedRate(value_scaled: int|None, unavailable_reason: str|None, was_clipped_to_zero: bool)`;
`encode_model_rate(value: float) -> PredictedRate`;
`StateModelPrediction(state: GameStateFeature, model_revision_sha256: str, model_artifact_sha256: str, input_sha256: str, pace: PredictedRate, home_ppp: PredictedRate, away_ppp: PredictedRate)`;
`MetricRecord(game_id: GameId, target_scaled: int, prediction_scaled: int, was_clipped_to_zero: bool=False)`;
`MetricScores(row_count, game_count, mae_scaled, rmse_scaled, game_balanced_mae_scaled, game_balanced_rmse_scaled, negative_prediction_count, unavailable_reason)`;
`score_metrics(records: Iterable[MetricRecord]) -> MetricScores`;
`round_sqrt_fraction(value: Fraction) -> int`;
`EvaluationReport` typed per-head/partition/cohort scores and matched comparisons;
`evaluation_projection(report: EvaluationReport) -> CanonicalObject`.

- [ ] **Step 1: RED rounding/cohort tests**

```python
def test_negative_rate_clips_but_nonfinite_never_becomes_missing():
    rate = encode_model_rate(-0.125)
    assert rate.value_scaled == 0 and rate.was_clipped_to_zero
    with pytest.raises(ValueError):
        encode_model_rate(float("nan"))
```

Metric cases: errors0,2_000_000 give MAE1_000_000/RMSE1_414_214;
unequal rows per game give different ordinary/game-balanced scores;
RMSE Q=1/4 rounds1, Q slightly below1/4 rounds0 with exact rational check.
`test_partial_baseline_comparison_reweights_matched_games` recomputes both model/baseline
on same eligible subset; empty cohort null/no_comparable_rows; regulation/OT counts explicit.
- [ ] **Step 2: RED:** both prediction/evaluation test files.
- [ ] **Step 3: Implement:** Decimal(str(value)) final rate rounding/clipping,
nonfinite/overflow rejection; streaming per-game exact error accumulators,
Fraction weighting and integer-isqrt threshold for RMSE. Reports never emit floats.
- [ ] **Step 4: GREEN:** Tasks1–4 tests, existing baseline numerical regressions, static checks.
- [ ] **Step 5: Commit:** `feat: validate model rates and evaluate matched cohorts exactly`.

### Task 5: Typed modeling application boundaries

**Files:** Create `application/modeling/{__init__,errors,models,ports,worker_models}.py`,
`tests/unit/application/test_modeling_models.py`,
`tests/unit/application/modeling_fakes.py`.

**Interfaces:** Consumes Tasks1–4 and existing Phase 5 references/selectors.
Produces frozen requests:
`BuildTrainingDatasetRequest(seasons: tuple[SeasonId,...], output_path: Path, split: TemporalSplitConfig, baseline_config: BaselineConfig)`;
`TrainModelsRequest(dataset_path: Path, output_path: Path, config: TrainingConfig)`;
`PredictModelsRequest(seasons: tuple[SeasonId,...], model_path: Path, output_path: Path, game_id: GameId|None=None)`.
`ModelSourceReference(game: Game, reference: CurrentFeatureSnapshotReference)`;
`TrainingDatasetSummary` with descriptor/config/split/versions/sources, row/game/head
counts and source/body/semantic/artifact hashes, `trainable` property;
`ValidatedTrainingDataset(path: Path, summary: TrainingDatasetSummary)`;
`HeadRows(partition: TemporalPartition, head: ModelHead, row_count: int, game_counts: Mapping[GameId,int], iter_rows: Callable[[],Iterator[TrainingRow]])`.
`ModelBundleManifest` with exact spec §14 fields, typed configs/descriptors/runtime/head metadata;
explicit fields `label_version`, `projection_version`, `split_version`, `training_version`,
`bundle_version`, `prediction_policy_version`, `model_revision_sha256` and `feature_versions`;
`LoadedModelBundle(manifest: ModelBundleManifest, artifact_sha256: str, predictor: ModelPredictor)`.
Worker DTOs: `TrainingWorkerJob(dataset: ValidatedTrainingDataset, work_dir: Path, config: TrainingConfig)`;
`RuntimeVersions(python: str, catboost: str, numpy: str, platform: str, architecture: str)`;
`HeadModelFile(head: ModelHead, path: Path, sha256: str, size: int, best_iteration: int, tree_count: int)`;
`ValidationProbe(head: ModelHead, vectors: tuple[ModelFeatureVector,...], values_scaled: tuple[int,...])` (первые до8 eligible validation rows per head);
`TrainingWorkerResult(models: tuple[HeadModelFile,...], evaluation: EvaluationReport, runtime: RuntimeVersions, resolved_parameters: Mapping[ModelHead,CanonicalObject], probes: tuple[ValidationProbe,...])`.
Results `BuildTrainingDatasetResult(output_path,summary)`,
`TrainModelsResult(output_path,manifest,artifact_sha256)`,
`PredictModelsResult(output_path,games,states,available,unavailable,artifact_sha256)` expose spec exits.
`ModelingError(code: str)` uses bounded safe lowercase code and no raw exception text.

Ports (signatures are the contract; later tasks implement exactly these):

```python
class ModelFeatureSource(Protocol):
    async def select_games(
        self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
    ) -> tuple[GameId, ...]: ...
    async def resolve(self, game_id: GameId) -> ModelSourceReference: ...
    async def read_features(self, source: ModelSourceReference) -> FeatureDataset: ...
    async def read_training(self, source: ModelSourceReference) -> TrainingGameInput: ...
    async def recheck(self, source: ModelSourceReference) -> None: ...


class HorizonNormalizer(Protocol):
    @property
    def version(self) -> str: ...
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions: ...


class TrainingDatasetExporter(Protocol):
    async def begin(self, request: BuildTrainingDatasetRequest) -> None: ...
    async def append_game(
        self, source: ModelSourceReference, rows: Iterable[TrainingRow]
    ) -> None: ...
    async def prepare(
        self, sources: tuple[ModelSourceReference, ...]
    ) -> TrainingDatasetSummary: ...
    def publish(self) -> None: ...
    async def abort(self) -> None: ...


class TrainingDatasetReader(Protocol):
    def validate(self, path: Path) -> ValidatedTrainingDataset: ...
    def head_rows(
        self, dataset: ValidatedTrainingDataset, partition: TemporalPartition, head: ModelHead
    ) -> HeadRows: ...


class RateModel(Protocol):
    @property
    def feature_names(self) -> tuple[str, ...]: ...
    @property
    def best_iteration(self) -> int: ...
    @property
    def tree_count(self) -> int: ...
    @property
    def resolved_parameters(self) -> CanonicalObject: ...
    def predict(
        self, vectors: Sequence[ModelFeatureVector], descriptor: FeatureProjectionDescriptor
    ) -> tuple[float, ...]: ...
    def save(self, path: Path) -> None: ...


class RateModelTrainer(Protocol):
    def fit_head(
        self,
        train: HeadRows,
        validation: HeadRows,
        descriptor: FeatureProjectionDescriptor,
        config: TrainingConfig,
    ) -> RateModel: ...


class ModelPredictor(Protocol):
    def predict_state(self, state: GameStateFeature) -> StateModelPrediction: ...


class TrainingWorker(Protocol):
    async def execute(self, job: TrainingWorkerJob) -> TrainingWorkerResult: ...
```

`ModelBundleStore.load(path)->LoadedModelBundle`, async `prepare(request,dataset,worker_result)->ModelBundleManifest`,
sync `publish()->str` returns exact artifact SHA, async `abort()->None`.
`PredictionExporter` lifecycle: async begin(request,bundle)/append_game(source,rows)/
prepare(sources)->PredictModelsResult, sync publish(), async abort().
All paths typed/resolved; requests reuse `BuildDatasetRequest` VERIFY selection validation.

- [ ] **Step 1: RED contract tests**

```python
def test_trainer_never_overwrites_its_input(tmp_path):
    with pytest.raises(ValueError):
        TrainModelsRequest(tmp_path / "same", tmp_path / "same", TrainingConfig())
```

Test unique ascending seasons, game_id only one season, path aliases/input-model-output
collisions, frozen DTOs, wrong game reference, checksum/enum/count invariants and exact exits.
- [ ] **Step 2: RED:** `uv run pytest tests/unit/application/test_modeling_models.py -q`.
- [ ] **Step 3: Implement DTOs/ports:** no I/O or native imports. `HeadRows` captures
reiterable checked head view and verified per-game counts; it never materializes states.
Fakes expose same methods and audit mutable-source/no-writes behavior.
`modeling_fakes` produces `MemoryModelSource`,
`make_model_source_reference(game: Game, features: FeatureDataset)->ModelSourceReference`
using valid Phase5 token/checksum constructors, and
`make_dataset_request(path: Path)->BuildTrainingDatasetRequest` with explicit UTC cutoffs.
- [ ] **Step 4: GREEN:** model contracts + prior phase selectors, all static checks.
- [ ] **Step 5: Commit:** `feat: define typed offline modeling boundaries`.

### Task 6: Read-only published modeling sources

**Files:** Create `infrastructure/postgres/model_source.py`,
`tests/unit/infrastructure/postgres/test_model_source.py`.

**Interfaces:** Consumes Task5 ModelFeatureSource and public Phase5 `FeatureWorkStore`,
`FeatureSourceReader`, `FeatureSnapshotStore`, configured P/L/F versions and
`NBAPlayByPlayV3PossessionNormalizer.normalize(GamePbpSnapshot)->NormalizedGameActions`.
Produces `PublishedModelFeatureSource(work: FeatureWorkStore, reader: FeatureSourceReader, snapshots: FeatureSnapshotStore, *, versions: FeatureVersions, normalizer: HorizonNormalizer)` implementing every ModelFeatureSource method.

- [ ] **Step 1: RED exact reference/endpoint tests**

```python
async def test_training_read_rechecks_reference_and_never_claims():
    source, work, reader, snapshots = make_model_source()
    ref = await source.resolve(MODEL_GAME_ID)
    inputs = await source.read_training(ref)
    assert inputs.period_ends[0].period == 4
    work.claim_game.assert_not_called()
    work.start_run.assert_not_called()
```

`MODEL_GAME_ID` is the game_id returned by the helper target. Helper `make_model_source()`
lives in this test and adapts existing `feature_fakes.make_target`
and `test_feature_models.make_reference` with valid full-period fixture/contracts, not
SimpleNamespace that bypasses count/lineage checks. Cover selected CURRENT/INVALID,
stale/absent, unsupported configured normalizer, changed PBP/possession ref between
resolve/read, score/counter/clock mismatch, empty period, final-score mismatch;
`test_read_features_never_constructs_training_endpoints` for prediction path.
- [ ] **Step 2: RED:** `uv run pytest tests/unit/infrastructure/postgres/test_model_source.py -q`.
- [ ] **Step 3: Implement:** resolve target/read_target/resolve_current; store only
lightweight source reference. read_training re-resolves exact matching target/pointer,
reads verified features and target rows, normalizes same applied PBP, selects
PeriodEnd4 and every present OT endpoint, validates final/source/state alignment.
Do not cache every game's raw PBP in source manifest. recheck compares full reference.
- [ ] **Step 4: GREEN:** new adapter + Phase5/6 adapter regressions/static checks.
- [ ] **Step 5: Commit:** `feat: read verified sources and horizon endpoints for modeling`.

### Task 7: Canonical training dataset and orchestration

**Files:** Create `infrastructure/files/training_dataset.py`,
`application/modeling/dataset_service.py`,
`tests/unit/infrastructure/files/test_training_dataset.py`,
`tests/unit/application/test_training_dataset_service.py`,
`tests/fixtures/nba/modeling/training.expected.jsonl` (LF golden).

**Interfaces:** Consumes Task3 rows, Task5 ports/source/DTOs. Produces
`AtomicTrainingDatasetExporter`, `CanonicalTrainingDatasetReader`;
`BuildTrainingDatasetService(source: ModelFeatureSource, exporter: TrainingDatasetExporter).build(request)->BuildTrainingDatasetResult` (async).
Reader validate/head_rows conforms to Task5; reconstructed row stores reduced
TrainingBaseline metrics, never needs unavailable original GameStateFeature objects.

- [ ] **Step 1: RED streaming/hash/race tests**

```python
async def test_final_source_race_preserves_previous_dataset(tmp_path):
    path = tmp_path / "training.jsonl"
    path.write_bytes(b"old")
    service = make_dataset_service_with_final_race(path)
    with pytest.raises(ModelingError, match="model_source_changed"):
        await service.build(make_dataset_request(path))
    assert path.read_bytes() == b"old"
```

`make_dataset_service_with_final_race` is local to this task's service test and uses
MemoryModelSource configured to fail the final recheck plus real exporter.
Other helpers are Task5 modeling_fakes; tests cover one-shot row iterable/one-game release,
byte equality at different paths, all state masks retained, selectors/order/duplicate
games/empty selections, source semantic vs UUID identities, canonical corruption/count
tampering/CRLF, reader wrong head eligibility or descriptor, below-min data exit1,
Windows replace failure and cancellation worker join before temp cleanup.
- [ ] **Step 2: RED:** both new file/service test files.
- [ ] **Step 3: Implement:** fixed manifest/body projection, incremental exact counts/
eligibility/per-game counts/distinct train targets and semantic identities. Reader
validates actual streamed rows against all declared counts, then exposes HeadRows;
trainable checks minimums, not label availability of every individual row.
Service selects/resolves/read_training/builds rows/appends/releases/rechecks per-game,
prepares readback then rechecks every source and synchronous publish; committed cleanup
failure does not downgrade result. Temp I/O uses owned-task join policy of Phase6.
- [ ] **Step 4: GREEN:** source/dataset/hash regressions/static checks; literal golden
matches expected masked/terminal/embargo records and source hashes.
- [ ] **Step 5: Commit:** `feat: export and verify pinned chronological training datasets`.

### Task 8: Optional frozen ML extra and real CPU adapter

**Files:** Modify `pyproject.toml`, `uv.lock`; create
`infrastructure/ml/{__init__,catboost_adapter}.py`,
`tests/unit/infrastructure/ml/test_catboost_adapter.py`,
`tests/unit/infrastructure/ml/ml_helpers.py`.

**Interfaces:** Consumes Task5 RateModelTrainer/RateModel/HeadRows and descriptor/config.
Produces `CatBoostRateModelTrainer.fit_head(...) -> RateModel`;
`load_rate_model(path: Path, descriptor: FeatureProjectionDescriptor) -> RateModel`;
`ensure_ml_dependencies() -> RuntimeVersions` (safe lazy missing-extra error).
Helper `tiny_head_rows(partition: TemporalPartition, head: ModelHead)->HeadRows`
creates >=2 train game IDs/distinct targets/96-column nonconstant numeric features,
independent validation/test rows and controlled null inputs, using Tasks1–3 contracts.

- [ ] **Step 1: RED real fit/default/boundary tests**

```python
def test_native_cpu_fit_respects_frozen_feature_names():
    config = TrainingConfig(iterations=12, early_stopping_rounds=3)
    descriptor = model_feature_descriptor(BaselineConfig())
    model = CatBoostRateModelTrainer().fit_head(
        tiny_head_rows(TemporalPartition.TRAIN, ModelHead.PACE),
        tiny_head_rows(TemporalPartition.VALIDATION, ModelHead.PACE),
        descriptor,
        config,
    )
    assert model.feature_names == tuple(column.name for column in descriptor.columns)
    assert 1 <= model.tree_count <= 12
```

Add wrong partition/head rejection before native call, missing→NaN not zero,
no catboost_info side files, exact resolved fixed defaults, finite output/shape/
native names validation, missing-extra help-safe behavior, same-runtime repeat fit values.
- [ ] **Step 2: RED:** new tests fail on missing adapter before dependency installation.
- [ ] **Step 3: Implement/install:** add `[project.optional-dependencies] ml` with
`catboost==1.2.10` and `numpy>=2.4,<3`, resolve/freeze uv.lock,
`uv sync --extra ml --frozen`. Lazy narrow native import/cast/validated numeric boundary;
build current-head pools in chronological order, decode divisors, per-game weights.
All fixed native defaults come from spec §11; labels/rates ordinary units. No test pool in fit.
- [ ] **Step 4: GREEN:** real native tests on Windows, previous domain/source/dataset
tests, Ruff/format/strict mypy. Missing-extra test mocks discovery, does not uninstall packages.
- [ ] **Step 5: Commit:** `feat: add frozen optional CatBoost CPU training adapter`.

### Task 9: Owned CPU job executor and held-out evaluation

**Files:** Create `infrastructure/ml/{training_worker,worker_protocol}.py`,
`tests/unit/infrastructure/ml/test_training_worker.py`,
`tests/unit/infrastructure/ml/job_helpers.py`.

**Interfaces:** Consumes Task5 worker DTOs, Task7 checked reader and Task8 trainer.
Produces `run_training_job(job: TrainingWorkerJob, reader: TrainingDatasetReader, trainer: RateModelTrainer) -> TrainingWorkerResult`;
`worker_job_projection(job)->CanonicalObject`, `parse_worker_job(data: CanonicalObject)->TrainingWorkerJob`,
`worker_result_projection(result)->CanonicalObject`,
`parse_worker_result(data: CanonicalObject, job: TrainingWorkerJob)->TrainingWorkerResult`.
`python -m nba_live_totals.infrastructure.ml.training_worker --job PATH` is internal child entry.
`write_tiny_dataset(path, *, test_points_delta=0)` async test helper creates a real
canonical dataset through Task7 writer/reader from Task5 fake sources; no DB/native shortcut.

- [ ] **Step 1: RED independence/real job tests**

```python
async def test_changing_only_test_targets_never_changes_fitted_values(tmp_path):
    first = await write_tiny_dataset(tmp_path / "a.jsonl")
    second = await write_tiny_dataset(tmp_path / "b.jsonl", test_points_delta=3)
    left = run_tiny_job(first, tmp_path / "job-a")
    right = run_tiny_job(second, tmp_path / "job-b")
    assert left.probes == right.probes
    assert left.evaluation != right.evaluation
```

`run_tiny_job(dataset,work_dir)` local helper uses real native trainer,
TrainingConfig(iterations=12,early_stopping_rounds=3). Also spy fit inputs contain
only train+validation, each head independent masks/weights, test labels change only
report/recipe identity; matrices released between heads; failure third head yields
no complete result; max rows checked before allocation. Model/nonfinite exceptions
map to safe error protocol, no arbitrary stdout/traceback.
- [ ] **Step 2: RED:** `uv run --extra ml pytest tests/unit/infrastructure/ml/test_training_worker.py -q`.
- [ ] **Step 3: Implement:** revalidate private dataset and expected artifact hash,
gate trainability, iterate heads pace/home/away. Build only train+validation HeadRows,
fit/save/fsync/compute SHA and probes; evaluate validation/test in4096 batches after
selection, aggregate exact Task4 reports/full/matched/regulation/OT cohorts.
Job/result JSON is strict canonical with exact allowed fields; child file paths
restricted to owned work_dir and fixed head filenames, result never supplies a
trusted arbitrary absolute model path. Resolved float diagnostics→decimal strings.
- [ ] **Step 4: GREEN:** all real CPU worker/adapter/domain/dataset tests and static checks.
- [ ] **Step 5: Commit:** `feat: execute isolated CPU training jobs and held-out evaluation`.

### Task 10: Checked single-file model bundle

**Files:** Create `infrastructure/files/model_bundle.py`,
`tests/unit/infrastructure/files/test_model_bundle.py`.

**Interfaces:** Consumes Task5 ModelBundleStore/manifest/result/probes and
Task8 load_rate_model. Produces `VerifiedModelBundleStore` implementing Task5 lifecycle;
`CatBoostStatePredictor` implementing ModelPredictor with descriptor/config/models,
semantic recipe revision and exact bundle identity from manifest/load.

- [ ] **Step 1: RED real save/load and tampering tests**

```python
async def test_native_bundle_round_trip_preserves_encoded_predictions(tmp_path):
    dataset = await write_tiny_dataset(tmp_path / "training.jsonl")
    result = run_tiny_job(dataset, tmp_path / "worker")
    store = VerifiedModelBundleStore()
    manifest = await store.prepare(make_train_request(tmp_path / "m.nbamodel"), dataset, result)
    digest = store.publish()
    loaded = store.load(tmp_path / "m.nbamodel")
    assert loaded.artifact_sha256 == digest
    assert loaded.manifest.model_revision_sha256 == manifest.model_revision_sha256
```

Local `make_train_request` uses the same tiny config as worker. Verify all ValidationProbe
encoded values before publish and loaded predictor values on independent states.
`test_valid_zip_crc_with_wrong_native_columns_is_rejected` tampers descriptor/native
names/checksums with valid ZIP CRC; include duplicates/traversal/extra entries/
non-STORED/symlink metadata/engine mismatch/native load failure/size ceilings.
PermissionError preserves old bundle; failed third model or mismatched probe never publishes.
- [ ] **Step 2: RED:** `uv run --extra ml pytest tests/unit/infrastructure/files/test_model_bundle.py -q`.
- [ ] **Step 3: Implement:** typed manifest, semantic recipe hash, exact cbm/metrics
checksums; five ZIP entries fixed order/timestamps, readback/native load/probes,
fsync then one replace. Loader validates fixed supported descriptor against names/
divisors/config, not just self-consistent declared hash; compare native feature_names
and tree_count, not undocumented native persistence of best_iteration. Best iteration
comes from checked training metadata. Extract only owned flat files manually, join I/O
before cleanup. Predictor validates state, projects one state, encodes/clips rates.
- [ ] **Step 4: GREEN:** real bundle/CPU/dataset/domain tests, static checks.
- [ ] **Step 5: Commit:** `feat: publish and load verified atomic model bundles`.

### Task 11: Parent trainer supervision and pinned input

**Files:** Create `infrastructure/ml/process.py`,
`application/modeling/training_service.py`,
`tests/unit/infrastructure/ml/test_training_process.py`,
`tests/unit/application/test_training_service.py`,
`tests/fixtures/modeling_worker_probe.py` (controlled child for lifecycle faults).

**Interfaces:** Consumes Task5 TrainingWorker/DTOs, Task7 reader, Task9 protocol,
Task10 bundle store. Produces `SubprocessTrainingWorker.execute(job)->TrainingWorkerResult`;
`TrainModelsService(reader: TrainingDatasetReader, worker: TrainingWorker, bundles: ModelBundleStore).train(request: TrainModelsRequest)->TrainModelsResult` (async).

- [ ] **Step 1: RED cancellation/input-pinning tests**

```python
async def test_cancel_reaps_worker_before_staging_cleanup(tmp_path):
    probe = start_blocking_training_service(tmp_path)
    await probe.worker_started()
    probe.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe.task
    assert probe.child_returncode is not None
    assert probe.previous_output.read_bytes() == b"old"
    assert not probe.owned_workspace.exists()
```

Probe helpers are local wrappers around real supervisor/controlled child process:
startup/blocking/save markers and PID, no mocked assertion of terminate only.
Also overwrite original dataset after verified private copy: worker/probes use pinned
copy; invalid worker result paths/checksums/exit/protocol/third-head failure preserve old
output. Child cannot observe sentinel DATABASE_URL/password environment. Kill fallback
after5s, cancellation during copy/save, postcommit cleanup failure preserves result.
- [ ] **Step 2: RED:** new process/service test files.
- [ ] **Step 3: Implement:** own same-parent workspace, copy+verify input once,
supervise child via exec argument array (no shell), allowlisted env and hidden Windows
helper. Parse bounded result safely, check expected fixed artifacts/model count/hash,
prepare bundle, publish synchronously, return committed result before finally cleanup.
terminate/wait5s/kill/reap before cleanup; no inherited application settings or DB clients.
Stream/cap child diagnostic reads so full pipe cannot deadlock parent.
- [ ] **Step 4: GREEN:** actual process lifecycle + real native worker/bundle
regressions/static checks. No living child or file writes after cancellation test ends.
- [ ] **Step 5: Commit:** `feat: supervise cancellable pinned offline model training`.

### Task 12: Causal feature prediction service and JSONL

**Files:** Create `application/modeling/prediction_service.py`,
`infrastructure/files/model_predictions.py`,
`tests/unit/application/test_prediction_service.py`,
`tests/unit/infrastructure/files/test_model_predictions.py`,
`tests/fixtures/nba/modeling/predictions.expected.jsonl` (LF).

**Interfaces:** Consumes Task5 ModelFeatureSource/PredictionExporter,
Task10 LoadedModelBundle/ModelPredictor. Produces `AtomicModelPredictionExporter`;
`PredictModelsService(source: ModelFeatureSource, bundles: ModelBundleStore, exporter: PredictionExporter).predict(request: PredictModelsRequest)->PredictModelsResult` (async).

- [ ] **Step 1: RED causality/source-race tests**

```python
async def test_predict_uses_feature_path_never_future_targets(tmp_path):
    source, service = make_prediction_service(tmp_path)
    result = await service.predict(make_prediction_request(tmp_path))
    assert source.training_reads == 0
    assert result.states == source.expected_states
```

Task-local helpers compose real writer, Task5 source fake and checked native bundle.
`test_final_pointer_race_preserves_old_prediction_file` fails final recheck after
prepared bytes. Future state mutations leave current rates/input hash unchanged;
manifest/provenance may change. Invalid/terminal rows retained with exact reasons,
cold valid history predicts, negative flag/overflow failure, duplicate games/order,
one-game memory, zero selected games and canceled native/file worker cleanup tested.
- [ ] **Step 2: RED:** prediction service/writer test files.
- [ ] **Step 3: Implement:** load/verify bundle once; resolve/read_features only;
validate source versions vs model contract, predict per state in bounded native I/O,
append each game/release/recheck; prepare canonical manifest/body checksums/counts,
all-source final recheck then sync publish. Final result reports quality without
claiming terminal/invalid estimates. No baseline config override or latest model lookup.
- [ ] **Step 4: GREEN:** prediction/native bundle/source/Phase6 export regressions,
literal golden byte checks and static checks.
- [ ] **Step 5: Commit:** `feat: export causal offline predictions from verified models`.

### Task 13: CLIs, shared interrupt runner and operator workflow

**Files:** Create `scripts/{build_training_dataset,train_models,predict_models,async_runner}.py`,
`docker/ml.Dockerfile`, `tests/unit/test_modeling_cli.py`,
`tests/unit/modeling_interrupt_probe.py`; modify `scripts/build_baselines.py`,
`Makefile`, `README.md`; retain Phase6 interrupt regression.

**Interfaces:** Consumes three application services and spec §16 requests/results.
Produces CLI `parse_args(argv: Sequence[str]|None=None)`/`main(argv, *, run_command=run)->int`
per module; async `run(request)` composition for its respective service.
`run_owned_async(command: Callable[[],Awaitable[T]]) -> T` in scripts/async_runner.py
extracts owned-task SIGINT policy from Phase6; baseline `_run_command` delegates,
keeping its injectable public main contract and existing exit behavior.

- [ ] **Step 1: RED flags/exits/real SIGINT tests**

```python
def test_training_cli_is_database_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "invalid-not-a-db-url")
    assert train_models.main(["--help"]) == 0
```

Tests validate exact selectors/date UTC interpretation/embargo/defaults/decimal bounds,
early_stop>iterations, alias collisions, missing dependencies, no source/schema writes,
bundle versions, quality/operational/secret-safe exits and summary digest. Actual child
SIGINT probes: precommit two→130/old file; postcommit two→published 0 or1/summary;
original signal handler restored. Reuse Phase6 probe and add model pipeline probe.
- [ ] **Step 2: RED:** new CLI tests before scripts exist; missing help returns expected failure.
- [ ] **Step 3: Implement:** source scripts configure/close PG+CH and validate four
schemas without migration; trainer script has no Settings/DB dependency; lazy ML
imports keep help/dataset CLI usable without extra. Make targets required paths/selectors,
Windows direct commands, embargo/as-of/quality notes, model units/no accuracy claims.
ML Dockerfile follows existing Python3.14.8/uv0.11.18/nonroot API pattern, frozen
`--extra ml --no-dev`, no ML change to API Dockerfile or default Compose services.
- [ ] **Step 4: GREEN:** new CLI/operator tests plus baseline CLI/actual SIGINT
regressions; CLI help each, Ruff/format/mypy. Build ML image, verify import/native fit
and UID999; do not introduce public routes.
- [ ] **Step 5: Commit:** `feat: add offline modeling CLIs and operator runbook`.

### Task 14: Real pipeline acceptance and checkpoint

**Files:** Create `tests/integration/modeling_harness.py`,
`tests/integration/test_modeling_source.py`, `tests/e2e/test_modeling.py`,
`tests/fixtures/nba/modeling/` full-period/regulation/2OT fixtures,
`docs/superpowers/reports/2026-10-13-phase-7-acceptance.md`; update README/spec/plan status
only after verified implementation. Existing Phase6 harness stays unchanged.

**Interfaces:** Consumes all production services/CLIs, existing ownership-journal
pattern and tests/runtime_config.py loopback guards. Produces owned acceptance harness
with >=2 train games/distinct targets, one validation game, one test game, an embargo
game and 2OT case; explicit timestamps/cutoffs/source versions supplied to CLI.

- [ ] **Step 1: RED real E2E assertions**

```python
async def test_offline_modeling_end_to_end_is_read_only(owned_modeling, tmp_path):
    before = await owned_modeling.storage_projection()
    dataset, bundle, predictions = await owned_modeling.run_clis(tmp_path)
    assert bundle.manifest.label_version == "remaining-horizon-labels-v1"
    assert dataset.summary.trainable
    assert predictions.states == owned_modeling.feature_state_count
    assert await owned_modeling.storage_projection() == before
```

Expose harness `run_clis` typed artifacts and `storage_projection` full relevant PG
rows+four CH row counts; assert actual hashes/model metadata/metrics/matched cohorts/
round-trip rates separately. Test dataset byte identity across paths, source race,
embargo/all-state group assignment, first-OT exclusion of future second OT,
post-recovery valid state, cold-history predict and terminal reason. No fake scores
used to claim accuracy or baseline superiority.
- [ ] **Step 2: RED:** run focused integration/E2E on fresh explicit project/ports;
expected absent fixtures/new adapter path, not permission failure against user stores.
- [ ] **Step 3: Implement harness:** create far-future unique identities, full
PeriodEnd markers and two OT; journal owned primary keys before INSERT/upsert,
refuse collisions, use real Phase2→3→4→5 services and production Phase7 CLI.
Use separately tagged explicit lineup reset fixture only for recovery test; no new
production NBA semantics. Child subprocesses time out/terminate/reap before DB cleanup.
- [ ] **Step 4: Final acceptance:** Windows frozen ML extra and Linux ML image
native fit/save/load, nonroot UID, focused real tests;
full `uv run --extra ml pytest -q -m 'not live' --tb=short -rs`,
Ruff check/format, strict mypy, git diff --check.
Record actual counts/skips/runtime/accuracy limitations; resolve exact Compose name
and volume ownership before removing only this project's containers/volumes/network.
Fresh final whole-change review and one verified fix pass follow execution skill;
do not skip suite after product fixes or replace real review with self-review.
- [ ] **Step 5: Commit:** `test: accept the offline CatBoost pipeline end to end`.

## Spec Coverage and Self-Review

| Spec | Owning tasks |
|---|---|
| 1–3: scope/three heads/layering | 5, 8, 9, 11–14 |
| 4–6: exact targets/known horizon/verified endpoints | 1, 6, 7, 14 |
| 7: literal common numeric projection | 2, 8, 10, 12 |
| 8–10: temporal masks/eligibility/weights/memory | 3, 5, 7–9 |
| 11: fixed CPU dependency/config/validation only | 3, 8, 9, 13, 14 |
| 12–13: rates/postprocessing/cohort metrics | 4, 9, 10, 12 |
| 14–15: bundle identities/readback/process/commit point | 7, 10–13 |
| 16–17: CLIs/modules/runbook | 5–13 |
| 18–20: actual tests/platform gate/review handoff | every task, 14 |

Self-review checks: producer/consumer types match, HeadRows never leaks TEST to
fit, reduced TrainingBaseline can reconstruct canonical rows, eligibility reasons
are separate from target reasons, native round-trip uses known runtime properties,
reader/bundle/source checks protect whole publication. Every Review Focus item
names explicit regression owners above. Plan contains signatures/assertions and
testable commits, not native implementations or an undefined helper dependency.

Self-review completed 2026-10-13: 14 numbered tasks/70 ordered RED→GREEN/commit steps,
15 Python example blocks parse; spec coverage mapped to every section. NumPy2.4
CPython3.14 wheel availability verified in [published files](https://pypi.org/project/numpy/2.4.0/#files).
No product code or dependencies installed while writing this plan.

## Execution Handoff

План ожидает пользовательского review; implementation не начат.
Сохранённое предпочтение пользователя — native execution в текущем чате/main.
После approval плана использовать `superpowers:executing-plans`: выполнить 14 задач
последовательно, затем один fresh whole-change reviewer и один RED→GREEN fix pass.
Это сохраняет единые interfaces вдоль dataset→worker→bundle→prediction и не требует
отдельного implementer/reviewer context на каждый task. No push/merge/Phase8 work.
