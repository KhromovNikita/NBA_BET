# Phase 8 Offline Monte Carlo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Получить воспроизводимые распределения итоговых home/away scores и тотала до конца матча, включая будущие OT, из checked Phase 7 bundle и опубликованных состояний, с offline CLI и проверенным атомарным JSONL.

**Architecture:** Pure domain задаёт config/query, causal frame, integer score PMF, identities и exact summaries. Lazy NumPy kernel считает последовательные batches владений и будущих OT в одном owned cancellable thread. Application читает одну игру за раз, выполняет inference и simulation последовательно, проверяет источники и публикует streaming artifact через единственный synchronous replace.

**Tech Stack:** Python >=3.14,<3.15, frozen slotted dataclasses, Fraction/Decimal/isqrt, canonical JSON/SHA-256, asyncio/threading.Event, NumPy из существующего frozen `ml` extra, checked CatBoost 1.2.10 bundle, pytest/Ruff/strict mypy, Windows и Linux ML image/UID999.

**Spec:** [Approved Phase 8 specification](../specs/2026-10-14-phase-8-monte-carlo-design.md). Пользователь одобрил письменную спецификацию и поручил составить план. План одобрен пользователем 2026-10-15; native implementation in progress.

## Global Constraints

- Сохранить выбранный способ: сам в текущем чате/main, задачи последовательно, один свежий независимый reviewer в конце. До выполнения проверить AGENTS.md/status; не создавать worktree, не reset/push/merge и не включать чужие изменения.
- Horizon — весь остаток матча с возможными будущими OT. Current R берётся из current state: regulation remainder либо remainder текущего OT; каждый NEW OT =300000 ms. Rates заморожены на всю trajectory.
- Versions: `possession-simulation-v1`, `tilted-integer-score-pmf-v1`, `numpy-poisson-multinomial-v1`, `state-pcg64-seed-v1`, `exact-simulation-summary-v1`, `simulation-results-jsonl-v1`.
- S=1000000. Persisted numbers — exact signed Int64/null, кроме base seed UInt64 0..2^64-1. Derived seed — 64-character SHA-256; native floats только в tilt/sampling. UTF-8/LF, без JSON floats/NaN/Infinity.
- Defaults/bounds: simulations10000/1..1000000; seed42/0..2^64-1; batch_size1024/1..65536 и <=N; max_overtimes10/1..20 NEW OT; max_possessions_per_horizon10000/1..1000000.
- Prior positive для consecutive 0..K, K2..32, sum=S; default `(460000,80000,330000,120000,9000,900,90,9,1)`. Это заданный некалиброванный prior. Exponential shape фиксирован V1.
- Query отдельно от config/RNG: sorted unique lines0..64, nonnegative Int64 multiples500000; quantiles1..32 в0..S, defaults(50000,500000,950000); bool вместо int запрещён.
- Available PPP вне0..K*S → unavailable `ppp_outside_score_support`; pace>1000*S → `pace_outside_runtime_limit`. Invalid/R0 → `invalid_feature_state`/`no_future_time`; R0/tie тоже unavailable. Zero pace/PPP допустимы. Structural/hash/version/native errors прерывают весь export.
- `MAX_SCORE=(2**63-1)//S`; current home+away и каждый observed final total <=MAX_SCORE. Python-int moments; histogram bin budget100000 на каждую marginal; JSON record/manifest <=16MiB.
- Combined Poisson λ=P_scaled*R_ms/(1440000*S); случайная первая сторона и чередование. Counts выше cap → unresolved `possession_limit`, без clamp/resample. Tie после последнего нового OT → unresolved `overtime_limit`.
- Tilt: stable max-shifted log weights, bracket[-64,64], 80 bisections, equality в lower branch. Integer PMF: largest remainders/tie ascending k, adjacent mean repair <=1024 moves. Native modal-last projection/tie lowest k; persisted PMF ascending integer.
- Explicit Generator(PCG64), один per-state stream. Draw order: Poisson, first-side bits admitted paths, home multinomial, away multinomial; include n=0 admitted paths. λ=0 пропускает все четыре стадии. Original active-path order, без score sorting/stream resets между batches.
- Seed включает config/runtime/model/frame; исключает query/path/selection scope/операционные UUID/time/future suffix. Batch size входит в config. Reproducibility только при одинаковом verified runtime; cross-version/platform byte identity не обещается.
- Executed: resolved+unresolved=executed=requested=N. Unavailable: executed=resolved=unresolved=0. Incomplete сохраняет только resolved marginals, все полные means/stdev/quantiles/probabilities null; unknown line count=unresolved. Нельзя renormalize по resolved.
- Три marginals, exact first-moment identity, new_ot_counts completed paths; joint grid/winner/spread не добавлять. Half-away final rounding, population stdev, nearest-rank quantiles; line counts — authority, independently rounded probabilities могут дать S±1.
- Read-only public CURRENT/INVALID feature snapshots и P/L/F version checks; не вызывать read_training, rebuild/claims/migrations/NBA HTTP. Не менять Phase 7 targets/projection/fit/bundle. Нет replay/calibration/API/live/feeds/ставок в этой фазе.
- Один game dataset и один state outcome в работе; не копить game/season outcomes. Owned CPU/inference/I/O threads joined до cleanup; stop Event между batches/OT. Same-parent temps: присвоить ownership после КАЖДОГО allocation, fsync/readback/final source recheck, synchronous os.replace.
- Output не совпадает с model/config input через resolve/symlink/samefile hardlink. Precommit failure сохраняет destination; postcommit cleanup/два SIGINT сохраняют published result.
- Новой dependency нет; frozen optional `ml` extra. Help/pure tests без native imports; default API image остаётся без ML. Exit0 complete, exit1 published partial, exit2 precommit failure, exit130 precommit interruption.
- Config file canonical/versioned; с ним kernel override flags запрещены. Без файла N: CLI > MONTE_CARLO_SIMULATIONS >10000; omitted batch=min(1024,N). Decimal line/quantile parsing без float.
- TDD для нового поведения, relevant regressions/static checks перед commits. Новые ресурсы только owned Phase8; retained Phase6/7 scratch не читать/менять/удалять как часть cleanup Phase8.

## Review Focus

1. Почти граничный PPP и K=32: exact PMF mass/mean и modal-last sampling не дают отрицательной residual probability или лишних очков. Tasks2/6.
2. Все trajectories unresolved либо R0/tie: нет fake zero forecast, conditional probabilities или потери denominator. Tasks1/4/7.
3. Query/selection/future suffix/операционный snapshot UUID меняются при том же current frame и pinned bundle: seed/population сохраняются; exact provenance и semantic revision различаются по контракту. Tasks3/10/11/13.
4. Второй temp allocation, output hardlink на input или source race после prepare: старый destination/input сохранён, owned temp удалён, publish не вызван. Tasks5/10/11.
5. Повторная отмена во время native batch и два реальных SIGINT после replace: stop установлен, worker joined, cleanup не гоняется с worker, committed summary/exit сохраняется. Tasks8/12/14.

---

## File Structure and Dependencies

Все production paths ниже относительно `src/nba_live_totals/`, кроме явно названных `scripts/`, `tests/`, `docs/`, `README.md`, `Makefile`.

| Task | Files / ответственность |
|---|---|
| 1 | domain/simulation.py — config/query/runtime/frame и строгие units/availability |
| 2 | domain/simulation_pmf.py — integer PMF и tilt; отдельный небольшой numeric модуль |
| 3 | domain/simulation_hashing.py — projections/config/query/seed/population/run identities |
| 4 | domain/simulation.py + simulation_summary.py — outcomes/histograms/exact queried summaries |
| 5 | application/simulation/{models,ports,errors,__init__}.py — typed selectors/results/ownership boundaries |
| 6 | infrastructure/simulation/{numpy_engine,__init__}.py — lazy typed RNG и один horizon |
| 7 | infrastructure/simulation/numpy_engine.py — batches/OT/caps/full outcomes |
| 8 | infrastructure/simulation/executor.py + application/modeling/owned_io.py — stop/join |
| 9 | infrastructure/files/simulation_codec.py — strict row/manifest schema и semantic projections |
| 10 | infrastructure/files/simulation_export.py — streaming atomic writer и checked reader |
| 11 | application/simulation/service.py — sequential source/inference/simulation orchestration |
| 12 | scripts/simulate_totals.py, Makefile, README.md — CLI/runbook/interrupt contract |
| 13 | tests/integration/simulation_harness.py, test_simulation_source.py; tests/e2e/test_simulation.py — real source/native pipeline |
| 14 | acceptance probes/report + README/roadmap/spec/plan status — Windows/Linux/full regression/review |

Dependencies: 1→2→3→4→5→6→7→8; 5+4→9→10; 8+10→11→12→13→14.
Native execution сохраняет этот порядок. Setup и docs включены в работающие deliverables.
Перед RED Task1 capture Git base SHA и проверить tooling. Команды `uv run ...`
ниже означают frozen project environment; если uv отсутствует, использовать
уже проверенную `.venv/Scripts/python.exe -m pytest`, `-m ruff`, `-m mypy`.
При необходимости bootstrap uv — только в собственном scratch этого плана с
ledger после approval, не из retained Phase7 scratch. Task13 дополняет ledger
новым Compose project; задачи1–12 не требуют запуска хранилищ.

### Task 1: Strict config/query/runtime и causal frame

**Files:** Create `domain/simulation.py`, `tests/unit/domain/simulation_helpers.py`, `tests/unit/domain/test_simulation.py`.

**Interfaces:** Consumes existing `StateModelPrediction`, `PredictedRate`, `horizon_period(state: GameStateFeature)->int`.
Produces frozen `SimulationConfig(simulations:int=10000,seed:int=42,batch_size:int=1024,max_overtimes:int=10,max_possessions_per_horizon:int=10000,score_prior_weights:tuple[int,...]=DEFAULT_SCORE_PRIOR_WEIGHTS,policy_version:str="possession-simulation-v1")`;
`SimulationQuery(lines_scaled:tuple[int,...]=(),quantiles_scaled:tuple[int,...]=(50000,500000,950000),policy_version:str="exact-simulation-summary-v1")`;
`SimulationRuntime(python_version:str,numpy_version:str,system:str,machine:str,engine_version:str="numpy-poisson-multinomial-v1")`.
`SimulationFrame` fields: game_id:GameId; state_number/possession_number:int; state_kind:FeatureStateKind; boundary:EventBoundary; period/seconds_remaining_game_ms/seconds_remaining_period_ms/home_score/away_score:int; is_valid:bool; pace/home_ppp/away_ppp:PredictedRate; input_sha256/model_revision_sha256/model_artifact_sha256:str; unavailable_reason:str|None. Property `is_available:bool`.
`make_simulation_frame(prediction:StateModelPrediction,config:SimulationConfig)->SimulationFrame`;
helper `make_simulation_prediction(*,pace_scaled:int=100000000,home_ppp_scaled:int=1141111,away_ppp_scaled:int=1141111,period:int=4,remaining_ms:int=60000,home_score:int=10,away_score:int=9)->StateModelPrediction` uses `make_target_case`, valid clocks, actual `model_input_sha256(project_model_features(state,BaselineConfig()))`, fixed 64-character model hashes. R0/invalid fixture creates three unavailable PredictedRate values with the matching Phase7 reason; numeric arguments apply only to available states.

- [x] **Step 1: RED strict bounds/availability tests.**

```python
def test_defaults_and_zero_clock_tie():
    config = SimulationConfig()
    assert (config.simulations, config.seed, config.batch_size) == (10000, 42, 1024)
    assert sum(config.score_prior_weights) == 1000000
    frame = make_simulation_frame(
        make_simulation_prediction(remaining_ms=0, home_score=10, away_score=10), config
    )
    assert frame.unavailable_reason == "no_future_time"
    assert not frame.is_available
```

Parametrize bool/float/bounds/unknown versions, unsorted/duplicate/empty quantiles, 65 lines, non-half line, seed2^64 vs2^64-1, batch>N, invalid lineup, clocks/pregame/current OT, negative/overflow score, clipped zero flags. Assert PPP8000001 → `ppp_outside_score_support`, pace1000000001 → `pace_outside_runtime_limit`; zero rates remain available. Structural failures raise ValueError before availability masking.
- [x] **Step 2: RED:** `uv run pytest tests/unit/domain/test_simulation.py -q`; expected missing module/contracts.
- [x] **Step 3: Implement the named dataclasses/factory in `domain/simulation.py`.** Validate strict exact types and Global Constraints; use Phase7 clock validator on original state, copy only current causal fields. Availability priority: invalid_feature_state, no_future_time, ppp_outside_score_support, pace_outside_runtime_limit. Model hashes/identity/score/clock remain structural checks before these masks. No future data/native import.
- [x] **Step 4: GREEN:** same command plus `tests/unit/domain/test_model_predictions.py tests/unit/domain/test_model_targets.py`; all pass.
- [x] **Step 5: Commit:** `feat: define causal simulation configuration and frames`.

### Task 2: Exact tilted integer score PMF

**Files:** Create `domain/simulation_pmf.py`, `tests/unit/domain/test_simulation_pmf.py`.

**Interfaces:** Consumes `SimulationConfig.score_prior_weights`. Produces `ScorePmf(counts:tuple[int,...],target_scaled:int,policy_version:str="tilted-integer-score-pmf-v1")`, property `support_max:int`; `build_score_pmf(target_scaled:int,prior_weights:tuple[int,...])->ScorePmf`. PMF validates exact mass S and weighted mean target, not floats.

- [x] **Step 1: RED independent numerical goldens.**

```python
def test_binomial_prior_tilt_has_exact_known_solution():
    weights = (250000, 500000, 250000)
    assert build_score_pmf(500000, weights).counts == (562500, 375000, 62500)
    assert build_score_pmf(1500000, weights).counts == (62500, 375000, 562500)
    assert build_score_pmf(0, weights).counts == (1000000, 0, 0)
    assert build_score_pmf(2000000, weights).counts == (0, 0, 1000000)
```

Add default prior identity at1141111, boundary targets1/K*S-1, K32, remainder ties, positive/negative adjacent repair, invalid prior/support, nonfinite/bracket/repair failure. Assert `sum(c)==1000000` and `sum(k*c[k])==T` for each accepted target. Pin <=1024 repair guard with controlled internal projection input; no alternate guessed PMF.
- [x] **Step 2: RED:** `uv run pytest tests/unit/domain/test_simulation_pmf.py -q`; missing PMF builder.
- [x] **Step 3: Implement `ScorePmf`/`build_score_pmf` in `domain/simulation_pmf.py`.** Follow spec §8 exactly: endpoint/prior shortcuts, log-weight tilt, 80 bisections/equality lower, largest remainders/ascending ties, adjacent repair policy. Keep Python math local; no NumPy.
- [x] **Step 4: GREEN:** same command plus Task1 tests; all exact goldens/invariants pass.
- [x] **Step 5: Commit:** `feat: project scoring priors to exact integer PPP distributions`.

### Task 3: Query-independent state seed and semantic identities

**Files:** Create `domain/simulation_hashing.py`, `tests/unit/domain/test_simulation_hashing.py`.

**Interfaces:** Consumes Tasks1/2 and existing `CanonicalObject`, `canonical_json_bytes/sha256`.
Produces `simulation_config_projection(config:SimulationConfig)->CanonicalObject`, `simulation_query_projection(query:SimulationQuery)->CanonicalObject`, `simulation_frame_projection(frame:SimulationFrame)->CanonicalObject`, `simulation_runtime_projection(runtime:SimulationRuntime)->CanonicalObject`;
`simulation_config_sha256(config:SimulationConfig)->str`, `simulation_query_sha256(query:SimulationQuery)->str`;
`state_seed_sha256(frame:SimulationFrame,config:SimulationConfig,runtime:SimulationRuntime)->str`;
`simulation_revision_sha256(frame:SimulationFrame,config:SimulationConfig,runtime:SimulationRuntime)->str`;
`simulation_run_revision(selectors:CanonicalObject,config:SimulationConfig,query:SimulationQuery,runtime:SimulationRuntime,semantic_sources:tuple[CanonicalObject,...],semantic_body_sha256:str)->str`.

- [x] **Step 1: RED causality/hash tests.** Use literal canonical projection keys, explicit version domain separators and hashlib reference bytes. Assert changing seed/batch/runtime/current score/rate/model artifact/input hash changes state seed; changing query never reaches `state_seed_sha256` arguments. Two different operation UUIDs attached to the same prediction produce equal frames/seeds. Mutation of a `TrainingGameInput` future suffix/final Game score with the current state/bundle held fixed leaves frame/seed equal. Malformed SHA/unknown version fails.

```python
def test_causal_frame_contains_current_scores_and_no_final_box():
    frame = make_simulation_frame(
        make_simulation_prediction(home_score=10, away_score=9), SimulationConfig()
    )
    projection = simulation_frame_projection(frame)
    assert (projection["home_score"], projection["away_score"]) == (10, 9)
    assert "final_home_score" not in projection and "source_snapshot_id" not in projection
```
- [x] **Step 2: RED:** `uv run pytest tests/unit/domain/test_simulation_hashing.py -q`; missing hashing functions.
- [x] **Step 3: Implement the named functions in `domain/simulation_hashing.py`.** Hash only explicit current frame fields; all256 digest bits big-endian initialize PCG64 later. Population revision has its own versioned domain separator, query excluded; run revision includes semantic source/body and selectors/query, no operational UUID/time/path. Hash projections include applicable versions and no introspection over all dataclass fields.
- [x] **Step 4: GREEN:** same command plus Tasks1/2 tests; assertions cover changed current inputs and unchanged future/provenance/query.
- [x] **Step 5: Commit:** `feat: version simulation seeds and semantic identities`.

### Task 4: Outcome mass and exact histogram summaries

**Files:** Modify `domain/simulation.py`; create `domain/simulation_summary.py`, `tests/unit/domain/test_simulation_summary.py`; extend `tests/unit/domain/simulation_helpers.py`.

**Interfaces:** Produces `Histogram=tuple[tuple[int,int],...]`; `SimulationStatus` complete/incomplete/unavailable;
`SimulationOutcome(frame:SimulationFrame,status:SimulationStatus,requested:int,executed:int,resolved:int,unresolved:int,unavailable_reason:str|None,unresolved_counts:tuple[tuple[str,int],...],new_ot_counts:tuple[int,...],home_pmf:ScorePmf|None,away_pmf:ScorePmf|None,seed_sha256:str,simulation_revision_sha256:str,home_histogram:Histogram|None,away_histogram:Histogram|None,total_histogram:Histogram|None)` in `simulation.py` (TYPE_CHECKING PMF import where needed).
`MarginalSummary(mean_scaled:int,stdev_scaled:int,quantiles:tuple[tuple[int,int],...])`;
`LineSummary(line_scaled:int,count_under/count_equal/count_over/count_unknown:int|None,prob_under_scaled/prob_equal_scaled/prob_over_scaled:int|None)`;
`summarize_histogram(histogram:Histogram,quantiles_scaled:tuple[int,...])->MarginalSummary`,
`summarize_lines(outcome:SimulationOutcome,query:SimulationQuery)->tuple[LineSummary,...]`,
`validate_simulation_outcome(outcome:SimulationOutcome,config:SimulationConfig)->None`,
`make_unavailable_outcome(frame:SimulationFrame,config:SimulationConfig,runtime:SimulationRuntime)->SimulationOutcome`.

- [x] **Step 1: RED hand-counted summaries.**

```python
def test_exact_population_statistics_and_nearest_rank():
    result = summarize_histogram(((0, 1), (1, 1), (2, 2)), (0, 250000, 500000, 1000000))
    assert (result.mean_scaled, result.stdev_scaled) == (1250000, 829156)
    assert result.quantiles == ((0, 0), (250000, 0), (500000, 1), (1000000, 2))
```

Add complete line1: counts(1,1,2,0), probabilities(250000,250000,500000), half-line1.5 equality0. Three equal counts N3 yield333333 each, sum999999 unchanged. Tie rounding N128 gives1/128→7813. Incomplete all-unresolved uses empty hists, unknown=N, probabilities null; unavailable hists/line counts null and no executed paths. Reject zero/duplicate bins, contradictory mass/status/reasons/OT counts, first-moment mismatch, score>MAX_SCORE and >100000 bins.
- [x] **Step 2: RED:** `uv run pytest tests/unit/domain/test_simulation_summary.py -q`; missing outcome/summaries.
- [x] **Step 3: Implement named DTOs/functions.** Python-int weighted moments/Fraction and existing `domain.model_evaluation.round_sqrt_fraction` for scaled population stdev; nearest rank/integer line comparison. Completed marginals only; validate exact H+A first moments without claiming joint reconstruction. Unavailable still has versioned seed/revision identity but draws no RNG. Derived full marginal summaries are computed only when status complete, enforced by codec in Task9.
- [x] **Step 4: GREEN:** same command plus `tests/unit/domain/test_model_evaluation.py` and Tasks1–3; exact assertions pass.
- [x] **Step 5: Commit:** `feat: summarize simulation populations without losing unresolved mass`.

### Task 5: Typed requests, artifact metadata and application ports

**Files:** Create `application/simulation/{__init__,models,ports,errors}.py`, `tests/unit/application/test_simulation_models.py`, `tests/unit/application/simulation_fakes.py`.

**Interfaces:** Consumes domain DTOs and existing `ModelSourceReference`, `LoadedModelBundle`, `ModelBundleStore`, `FeatureProjectionDescriptor`, P/L/F versions.
Produces `SimulationError(code:str)` with safe allowlisted code; `SimulationStopped` internal exception; `SimulateTotalsRequest(seasons:tuple[SeasonId,...],model_path:Path,output_path:Path,config:SimulationConfig,query:SimulationQuery,game_id:GameId|None=None,state_number:int|None=None,config_path:Path|None=None)`;
`SelectedSimulationStates(total_states:int,state_number:int|None=None)` with properties `count:int`, `numbers:range|tuple[int,...]` (all=`range(total_states)`, one=`(state_number,)`);
`SimulationSourceSelection(source:ModelSourceReference,selection:SelectedSimulationStates)`;
`SimulationModelIdentity(recipe_sha256:str,artifact_sha256:str,descriptor:FeatureProjectionDescriptor,feature_versions:FeatureVersions,possession_versions:PossessionVersions,lineup_versions:LineupVersions)`;
`SimulationCounters(games:int,states:int,complete:int,incomplete:int,unavailable:int,unresolved:int,unresolved_counts:tuple[tuple[str,int],...])`;
`SimulationManifest(seasons:tuple[SeasonId,...],game_id:GameId|None,state_number:int|None,config:SimulationConfig,query:SimulationQuery,model:SimulationModelIdentity,runtime:SimulationRuntime,sources:tuple[SimulationSourceSelection,...],counts:SimulationCounters,source_sha256:str,body_sha256:str,semantic_body_sha256:str,run_semantic_revision:str,schema_version:str="simulation-results-jsonl-v1")`;
`ValidatedSimulationExport(path:Path,manifest:SimulationManifest,artifact_sha256:str)`;
`SimulateTotalsResult(counts:SimulationCounters,run_semantic_revision:str,artifact_sha256:str)` with `exit_code:int` (0 iff no incomplete/unavailable; otherwise1).
`ensure_simulation_paths_distinct(request:SimulateTotalsRequest)->None` checks resolve and existing samefile aliases, at request validation and publication.

Ports: `SimulationFeatureSource` is the read-only subset of existing source with exact `select_games(seasons,game_id=None)->tuple[GameId,...]`, `resolve(game_id)->ModelSourceReference`, `read_features(source)->FeatureDataset`, `recheck(source)->None`, all async; deliberately no `read_training` requirement.
`SimulationEngine.runtime()->SimulationRuntime`, synchronous `simulate(frame:SimulationFrame,config:SimulationConfig,*,stop_requested:Callable[[],bool])->SimulationOutcome`;
`SimulationExecutor.runtime()->SimulationRuntime`, `simulate(frame:SimulationFrame,config:SimulationConfig)->SimulationOutcome`, both async.
`SimulationExporter`: async `begin(request:SimulateTotalsRequest,bundle:LoadedModelBundle,runtime:SimulationRuntime)->None`, `begin_game(source:ModelSourceReference,selection:SelectedSimulationStates)->None`, `append_state(outcome:SimulationOutcome)->None`, `end_game()->None`, `prepare(sources:tuple[ModelSourceReference,...])->SimulateTotalsResult`, `abort()->None`; synchronous `publish()->None`.

- [x] **Step 1: RED selectors/path/metadata tests.** Assert `SelectedSimulationStates(5).numbers==range(5)`, one state3→(3,), state5 rejected; no per-state list allocation. State number without game/single season rejected. Model/output identical, symlink alias and existing hardlink alias all fail while original bytes remain unchanged. Config/output alias also fails. Counters require states=complete+incomplete+unavailable and valid reason totals. Fake source `read_training` raises AssertionError if ever called; fake executor records concurrent calls and enforces maximum1.

```python
def test_selection_stays_bounded_for_all_states():
    selection = SelectedSimulationStates(1000000)
    assert selection.count == 1000000 and selection.numbers == range(1000000)
    assert SelectedSimulationStates(5, 3).numbers == (3,)
```
- [x] **Step 2: RED:** `uv run pytest tests/unit/application/test_simulation_models.py -q`; missing application models.
- [x] **Step 3: Implement the exact contracts in the named files.** Reuse `BuildDatasetRequest(...,mode=VERIFY)` selectors and existing resolved-path conventions. No native imports/application array DTOs. Metadata explicitly excludes model/config/output paths from persisted projections. Fakes wrap `MemoryModelSource` through its public methods, with deterministic bounded outcomes and recorded begin/end/recheck order.
Allowlisted simulation error codes: `simulation_invalid_input`, `simulation_empty_selection`, `simulation_source_order`, `simulation_source_mismatch`, `simulation_feature_versions_incompatible`, `simulation_model_mismatch`, `simulation_missing_state`, `simulation_duplicate_state`, `simulation_native_error`, `simulation_numeric_overflow`, `simulation_histogram_limit`, `simulation_artifact_invalid`, `simulation_io_error`, `simulation_executor_busy`, `ml_dependencies_missing`. Existing ModelingError codes retain their current safe handling.
- [x] **Step 4: GREEN:** same command plus `tests/unit/application/test_modeling_models.py`; accepted/rejected path tests and counter invariants pass.
- [x] **Step 5: Commit:** `feat: define simulation service and export boundaries`.

### Task 6: Lazy NumPy RNG boundary and one-horizon possession sampling

**Files:** Create `infrastructure/simulation/{__init__,numpy_engine}.py`, `tests/unit/infrastructure/simulation/{__init__,test_numpy_horizon}.py`.

**Interfaces:** Consumes frame/config/ScorePmf. Infrastructure-only aliases `IntArray=NDArray[np.int64]`, `FloatArray=NDArray[np.float64]`, `BoolArray=NDArray[np.bool_]` under TYPE_CHECKING; imports at actual native boundary.
Produces `SimulationRng` Protocol: `poisson(lam:float,size:int)->IntArray`, `first_side_bits(size:int)->IntArray`, `multinomial(n:IntArray,pvals:FloatArray)->IntArray`;
`make_numpy_rng(seed:int)->SimulationRng` wraps explicit Generator(PCG64(seed)); bits from integers(0,2,size,dtype=int64), bit1=home first;
`capture_simulation_runtime()->SimulationRuntime` from Python/NumPy versions and platform system/machine;
`NativePmf(outcome_order:tuple[int,...],probabilities:FloatArray)` and `native_pmf(pmf:ScorePmf)->NativePmf`;
`HorizonBatch(home_scores:IntArray,away_scores:IntArray,possession_limited:BoolArray)`;
`sample_horizon(rng:SimulationRng,frame:SimulationFrame,config:SimulationConfig,home_pmf:ScorePmf,away_pmf:ScorePmf,home_scores:IntArray,away_scores:IntArray,remaining_ms:int)->HorizonBatch`.

- [x] **Step 1: RED controlled draw/parity/native tests.** Script counts[0,1,2,3], bits[1,1,0,0], atom2 for both teams, current10:9. Assert final home[10,12,12,12], away[9,9,11,13]; recorded draw calls in exact Poisson/bits/home/away order, including n=0 scoring rows. With cap2, last path limited and only three paths enter bits/scoring. Assert P100000000/R2880000 gives λ200; P0 invokes no RNG. Native modal-last tests cover tiny tail1/S, K32, ties lowest k, p>=0, fsum≈1 and remapping weighted outcomes. Malformed native shapes/dtypes/negative/nonfinite/count mismatch are operational errors.

```python
def test_tiny_tail_pmf_moves_mode_to_native_residual_slot():
    native = native_pmf(ScorePmf((999999, 1, 0), 1))
    assert native.outcome_order == (1, 2, 0)
    assert native.probabilities[-1] > 0.999
    assert all(p >= 0 for p in native.probabilities)
```
- [x] **Step 2: RED:** `uv run --extra ml pytest tests/unit/infrastructure/simulation/test_numpy_horizon.py -q`; missing adapter/functions.
- [x] **Step 3: Implement the named functions/wrapper.** Move modal outcome last; remaining p=1-fsum(previous), validate before multinomial. Guard count dtype/shape/range before arithmetic; admitted rows retain original order and n=0. Multinomial matrices bounded batch*(K+1), no event trace. Validate final scores against MAX_SCORE before histogram insertion. Module import remains native-free; missing extra maps to `ml_dependencies_missing`, no silent substitute.
- [x] **Step 4: GREEN:** same command plus pure domain tests; real NumPy array-n multinomial and PCG64 repeat test pass. Subprocess blocking NumPy import can import pure/application modules and this adapter module without executing its functions.
- [x] **Step 5: Commit:** `feat: sample exponential possession horizons with NumPy`.

### Task 7: Full batched simulation, future OT and unresolved accounting

**Files:** Modify `infrastructure/simulation/numpy_engine.py`; create `tests/unit/infrastructure/simulation/{simulation_rng_fakes,test_numpy_engine}.py`.

**Interfaces:** Consumes Task6 helpers and Task4 outcome validator. Produces `NumpySimulationEngine(*,rng_factory:Callable[[int],SimulationRng]|None=None)` implementing synchronous engine port from Task5. `runtime()` caches one captured immutable identity; `simulate(frame,config,*,stop_requested)` initializes one state stream from Task3 digest and returns a validated `SimulationOutcome`. Stop raises internal `SimulationStopped` exception defined in `application/simulation/errors.py` and consumed by executor.

- [x] **Step 1: RED trajectories/mass/resource/native distribution tests.**
Controlled script initial n0, OT1 n2 (atom2 both), OT2 n1 home first, initial0:0 → final4:2 and new_ot_counts=(0,0,1) with max_overtimes2; repeat starting period5, current OT does not consume either new slot. Zero pace/non-tie → complete point mass with no draws; zero pace/tie → all-unresolved `overtime_limit`, empty completed hists and no RNG draws. Possession cap produces mutually exclusive reason mass, never a clamp/resample. Assert N not divisible by batch executes exactlyN and one continuous stream, queries absent from kernel API, skipped/unavailable state does not shift another state. Stop checked before each batch and OT stage; histogram budget failure aborts, no truncation.
Native analytical test: N100000, P100 per48min, R2880000, identical default PPP1141111, initial1000000:0 (gap exceeds cap*K, thus no OT). Increment total follows compound Poisson λ200: expected mean=λ*E[X], variance=λ*E[X²] from literal prior. Mean tolerance=5*sqrt(variance/N); unbiased sample variance tolerance=5*sqrt((λ*E[X⁴]+(2+2/(N-1))*variance²)/N). Assert both and massN on fixed seed42; no model accuracy claim. Also exact same-runtime duplicate run and controlled draw-order goldens.

```python
def test_zero_pace_tie_keeps_all_paths_unknown():
    config = SimulationConfig(simulations=3, batch_size=2, max_overtimes=2)
    frame = make_simulation_frame(
        make_simulation_prediction(pace_scaled=0, home_score=10, away_score=10), config
    )
    outcome = NumpySimulationEngine().simulate(frame, config, stop_requested=lambda: False)
    assert (outcome.executed, outcome.resolved, outcome.unresolved) == (3, 0, 3)
    assert outcome.total_histogram == ()
    assert outcome.unresolved_counts == (("overtime_limit", 3),)
```
- [x] **Step 2: RED:** `uv run --extra ml pytest tests/unit/infrastructure/simulation/test_numpy_engine.py -q`; missing full engine.
- [x] **Step 3: Implement engine `runtime`/`simulate`.** Consecutive batches, initial horizon then up to configured NEW OT, boolean masks original order; only completed scores enter three integer histograms/new_ot_counts. Python-int accumulators, each marginal budget100000. Tie cap and possession cap mutually exclusive per path. Unavailable uses pure factory, no RNG construction. Config/runtime/frame seed only, all256 digest bits; no query-dependent calculation or independently seeded batches.
- [x] **Step 4: GREEN:** Task6/7 test files plus Tasks1–4; native analytic tolerances, overflow, all-unresolved, two-OT and same-runtime checks pass.
- [x] **Step 5: Commit:** `feat: simulate full-game totals with bounded overtime trajectories`.

### Task 8: Cooperative cancellation and owned worker join

**Files:** Create `infrastructure/simulation/executor.py`, `tests/unit/infrastructure/simulation/test_executor.py`; modify `application/modeling/owned_io.py`; extend `tests/unit/application/test_prediction_service.py` with unchanged-default owned-I/O regression.

**Interfaces:** Extend existing `async run_owned_io[T](operation:Callable[[],T],*,on_cancel:Callable[[],None]|None=None)->T`, preserving default behavior.
Produce `OwnedSimulationExecutor(engine:SimulationEngine)` implementing `async runtime()->SimulationRuntime`, `async simulate(frame:SimulationFrame,config:SimulationConfig)->SimulationOutcome`. Each simulate call owns a fresh threading.Event; callback=Event.set, engine predicate=Event.is_set. Reject overlap with `SimulationError("simulation_executor_busy")`.

- [x] **Step 1: RED cancellation/join tests.** Block controlled engine until Event; cancel once and twice while still running. Assert stop observed, worker finalizer runs before awaited cancellation returns, no remaining worker activity, next call succeeds. Engine error with pending cancellation does not become unobserved task exception; no callback invocation on successful run. Old run_owned_io(operation) still waits for uncooperative I/O and retrieves failure. Runtime capture/load uses joined I/O too.

```python
async def test_owned_stop_callback_is_not_called_on_success():
    signals = []
    assert await run_owned_io(lambda: 7, on_cancel=lambda: signals.append("stop")) == 7
    assert signals == []
```
- [x] **Step 2: RED:** `uv run pytest tests/unit/infrastructure/simulation/test_executor.py tests/unit/application/test_prediction_service.py -q`; missing executor/on_cancel keyword.
- [x] **Step 3: Implement named executor/optional callback.** Invoke synchronous no-throw stop callback on cancellation before public asyncio.wait/result join loop (ledger Ruling: Python3.14 shield logs cancelled worker errors even when retrieved). Retrieve worker completion/exception and then re-raise caller cancellation; SimulationStopped is internal stop result, never an exported partial artifact. No thread kill, file deletion from callback or concurrent state execution.
- [x] **Step 4: GREEN:** same command plus `tests/unit/test_modeling_cli.py tests/unit/test_build_baselines_cli.py tests/unit/application/test_baseline_service.py`, retaining pre/postcommit behavior. No artificial sleep-based timing assertions; use Events/barriers.
- [x] **Step 5: Commit:** `feat: stop and join owned simulation workers on cancellation`.

### Task 9: Strict canonical simulation row and manifest codecs

**Files:** Create `infrastructure/files/simulation_codec.py`, `tests/unit/infrastructure/files/test_simulation_codec.py`.

**Interfaces:** Consumes Tasks1–5, existing public `model_source_projection`, `model_source_semantic_projection`, `parse_model_source` from `infrastructure/files/training_dataset.py`.
Produces `simulation_manifest_projection(manifest:SimulationManifest)->CanonicalObject`, `parse_simulation_manifest(record:CanonicalObject)->SimulationManifest`;
`simulation_row_projection(source:ModelSourceReference,outcome:SimulationOutcome,query:SimulationQuery)->CanonicalObject`,
`simulation_row_semantic_projection(source:ModelSourceReference,outcome:SimulationOutcome,query:SimulationQuery)->CanonicalObject`,
`parse_simulation_row(record:CanonicalObject,manifest:SimulationManifest)->SimulationOutcome`.
Body row fixed keys: `record_type`, `source`, `frame`, `population`, `summaries`, `lines`. `source` carries game_id, feature snapshot UUID, input token SHA, state output SHA and player output SHA; full lightweight source refs live in manifest. `frame` is Task3 causal projection. `population` carries Task4 outcome fields except frame, plus PMF policies/counts/targets. `summaries` has home/away/total, null unless complete; line rows derive from query. Manifest contains schema, all six policy versions, config/query and SHA fields, model identity/descriptor, runtime, selectors, ordered source selections, counters and four revision/digest fields defined in Task5. No paths/time-of-run/final scores in frame/seed.

- [ ] **Step 1: RED canonical and adversarial row tests.** Round-trip complete/incomplete/unavailable outcomes; recompute PMF, mass/moments, full stats/ranks/line values/seed/population revision. Change a mean, unknown count, histogram bin or seed without updating semantic contents → reject. Reject unknown keys, bool numeric, float, unsupported version, null-vs-empty misuse, source/model/clock mismatch. An incomplete all-unresolved row keeps empty hists, null summaries/probabilities and unknown=N. Two operational snapshot UUIDs yield unequal exact rows but equal semantic rows and seeds; query changes semantic row summaries/run identity but never sampled population.

```python
def test_manifest_rejects_missing_contract_and_unknown_schema():
    with pytest.raises(ValueError):
        parse_simulation_manifest({})
    with pytest.raises(ValueError):
        parse_simulation_manifest({"schema_version": "simulation-results-jsonl-v2"})
```
- [ ] **Step 2: RED:** `uv run pytest tests/unit/infrastructure/files/test_simulation_codec.py -q`; missing codecs.
- [ ] **Step 3: Implement named projections/parsers with explicit key sets/types.** Parse bounded scalar structures into typed DTOs and compare every derived field to recomputed values. Use existing public source codecs without modifying their policies. Summary/query policy and PMF versions explicit. Semantic projection replaces operational source references with semantic references, retains actual sampled hists. Parser checks causal hash syntax/provenance consistency; it does not claim to reconstruct 96 features or the joint score distribution from partial frame/marginals.
- [ ] **Step 4: GREEN:** same command plus existing training dataset/model prediction codec tests; all exact projections and rejection cases pass.
- [ ] **Step 5: Commit:** `feat: encode and validate canonical simulation artifacts`.

### Task 10: Streaming exporter and atomic checked readback

**Files:** Create `infrastructure/files/simulation_export.py`, `tests/unit/infrastructure/files/test_simulation_export.py`.

**Interfaces:** Produce `AtomicSimulationExporter()` implementing Task5 exporter port; `CanonicalSimulationReader.validate(path:Path)->ValidatedSimulationExport`, `iter_outcomes(artifact:ValidatedSimulationExport)->Iterator[SimulationOutcome]`. Reader streams records; exporter stores source metadata/selection and counters, never all state outcomes. `prepare` returns ready result containing exact whole-file SHA; `publish` performs one synchronous replace; `abort` is joined/idempotent and preserves committed destination.

- [ ] **Step 1: RED streaming/ownership/readback tests.** Observe append of each row before next inference, correct begin/end/game chronology/all-state range/one-state sequence, no missing/duplicate/unexpected state and no giant outcome list. Same pinned source/runtime/config/query to two paths → byte-identical files and artifact SHA. Tamper bytes/duplicate JSON key/CRLF/noncanonical key order/record>16MiB/truncated LF or manifest/hash/counters → reject. Second temp allocation fails: first assigned temp is cleaned, destination unchanged. Replace PermissionError and prepare/readback error preserve old bytes. Before publish, model/config hardlink/symlink aliases reject again. Postcommit abort failure keeps published result.

```python
def test_reader_rejects_noncanonical_record_without_replacing_it(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{ "record_type": "manifest" }\r\n')
    with pytest.raises(SimulationError, match="simulation_artifact_invalid"):
        CanonicalSimulationReader().validate(path)
    assert path.read_bytes() == b'{ "record_type": "manifest" }\r\n'
```
- [ ] **Step 2: RED:** `uv run pytest tests/unit/infrastructure/files/test_simulation_export.py -q`; missing exporter/reader.
- [ ] **Step 3: Implement the named classes.** Same-parent body/ready temps, record size checked before write, incremental exact and semantic body SHA, source SHA from ordered explicit source projections. Assign ownership sequentially, fsync, construct manifest, stream checked readback and compare complete prepared metadata. Recompute counters and each game selection through reader; bound manifest and each line. Publish rechecks path aliases, os.replace once; no post-replace await. Do not copy Phase7 deferred tuple-allocation bug or fix unrelated old exporter here.
- [ ] **Step 4: GREEN:** same command plus Task9 and existing atomic model prediction export regressions; mutation/cleanup/byte identity tests pass.
- [ ] **Step 5: Commit:** `feat: publish streamed Monte Carlo results atomically`.

### Task 11: Sequential read-only simulation service

**Files:** Create `application/simulation/service.py`, `tests/unit/application/test_simulation_service.py`; extend `tests/unit/application/simulation_fakes.py`.

**Interfaces:** Consumes Task5 ports and existing bundle predictor/owned-I/O. Produces `SimulateTotalsService(source:SimulationFeatureSource,bundles:ModelBundleStore,executor:SimulationExecutor,exporter:SimulationExporter)` with `async simulate(request:SimulateTotalsRequest)->SimulateTotalsResult`.
Task11 test helper `async make_simulation_service(directory:Path)->tuple[MemoryModelSource,SimulateTotalsService]` creates a checked native test bundle `directory/"m.nbamodel"` with existing `tests.unit.infrastructure.files.test_model_bundle.make_bundle` and real exporter/executor; same public fixture pattern as Phase7. Native-dependent tests alone skip without extra; fake-port cancellation/orchestration tests stay native-free.

- [ ] **Step 1: RED orchestration/causality tests.** Assert bundle loaded once, native runtime captured once, exactly one FeatureDataset live, inference→simulation→append_state happens before next state, no overlapping executor calls, and one bounded all-state range. Exact state selection equals same row from season run; missing requested state, duplicate/out-of-order source/state, wrong model/input hash or P/L/F versions fail before publication. Expected input hash uses `model_input_sha256(project_model_features(state,bundle.manifest.baseline_config))`. Invalid/R0 rows preserved without executor draw; fake source.read_training/NBA/write calls are forbidden. Source pointer mutation both after game and after prepare causes abort and old destination survival. Cancellation joins inference/CPU/I/O before abort; publish occurs only after all final rechecks. Postcommit cleanup failure preserves prepared result; precommit error is not replaced by cleanup error.

```python
async def test_simulation_service_never_reads_future_training_targets(tmp_path):
    source, service = await make_simulation_service(tmp_path)
    request = SimulateTotalsRequest(
        (SeasonId("2025-26"),),
        tmp_path / "m.nbamodel",
        tmp_path / "out.jsonl",
        SimulationConfig(simulations=8, batch_size=4),
        SimulationQuery(),
    )
    result = await service.simulate(request)
    assert source.events.count("read_training") == 0
    assert result.counts.states == 8 and result.counts.unavailable >= 1
    assert result.exit_code == 1
```
- [ ] **Step 2: RED:** `uv run pytest tests/unit/application/test_simulation_service.py -q`; missing service.
- [ ] **Step 3: Implement constructor/`simulate` in the named module.** Load bundle/runtime through owned calls; compare feature descriptor and public P/L/F versions. Validate prediction identity, three model/input hashes and returned state against original state; create frame once. Available → executor; unavailable → pure factory. Stream each result and release state outcome before next; end/recheck/release dataset per game, retain only source refs. `prepare`, final source recheck, synchronous publish and immediate committed-result assignment; joined finally/abort with existing safe cleanup conventions.
- [ ] **Step 4: GREEN:** same command plus `tests/unit/application/test_prediction_service.py tests/unit/application/test_baseline_service.py`; all order/race/cancellation/hash checks pass.
- [ ] **Step 5: Commit:** `feat: orchestrate read-only full-game simulation exports`.

### Task 12: Offline CLI, exact decimal queries and operator commands

**Files:** Create `scripts/simulate_totals.py`, `tests/unit/test_simulate_totals_cli.py`, `tests/unit/simulation_interrupt_probe.py`; modify `Makefile`, `README.md`. Existing `docker/ml.Dockerfile` needs no change unless its actual CLI/import probe demonstrates one.

**Interfaces:** Produces `parse_args(argv:Sequence[str]|None=None,*,environ:Mapping[str,str]|None=None)->SimulateTotalsRequest`;
`parse_total_line(value:str)->int`, `parse_quantile_level(value:str)->int`, `load_simulation_config(path:Path)->SimulationConfig`;
`async run(request:SimulateTotalsRequest)->SimulateTotalsResult` composed through existing `run_source_work` and new engine/executor/exporter;
`main(argv:Sequence[str]|None=None,*,run_command:Callable[[SimulateTotalsRequest],Awaitable[SimulateTotalsResult]]=run)->int` through existing `run_owned_async`.

- [ ] **Step 1: RED parser/exit/import/interrupt tests.**
CLI `--line 210 --line 210.5 --quantile 0 --quantile 0.5 --quantile 1` → lines(210000000,210500000), levels(0,500000,1000000). Reject210.25, negative/nonfinite/overflow line, quantile0.0000001 and values>1, bad selectors, state without game, missing output parent/input aliases. Duplicate flags values normalize sorted unique query; domain DTOs remain strict.
N explicit3 overrides invalid/other env; omitted batch3; env17 givesN17/batch17; neither→10000/1024; invalid env without override fails. Canonical config bytes+LF load exactly, unknown/duplicate/noncanonical/oversized/bool/float fields rejected. With config any kernel flag forbidden, query allowed and env ignored. Help/import succeeds with invalid DATABASE_URL and blocked NumPy/CatBoost imports. Main returns0/1/2/130 as specified; summary includes all counts/hashes, errors expose safe codes only. Assert source composition validates four schemas and closes clients without writes.
Actual subprocess probe uses barriers: before-commit SIGINT exits130/old bytes; cancellation while CPU active waits for stop/join; two SIGINT after synchronous replace preserve published bytes/summary/exit0 or1 and restore prior signal handler. Reuse existing probe pattern, no sleeps as race proof.

```python
def test_query_decimals_are_exact_scaled_integers():
    assert parse_total_line("210.5") == 210500000
    assert parse_quantile_level("0.95") == 950000
    with pytest.raises(ValueError):
        parse_total_line("210.25")
```
- [ ] **Step 2: RED:** `uv run pytest tests/unit/test_simulate_totals_cli.py -q`; missing CLI.
- [ ] **Step 3: Implement named CLI/parsers and Make/runbook.** Decimal conversion exact to scaled int with no rounding, validate support then sort/dedupe. Read only MONTE_CARLO_SIMULATIONS from supplied/default environment for N precedence; no Settings/native/store construction on help/parser errors. Config read bounded16MiB, exact canonical projection+LF. Catch SimulationError and existing ModelingError as safe operational codes. Add guarded `simulate-totals` .PHONY target with SEASON/SEASON_FROM/SEASON_TO, MODEL, OUTPUT, GAME_ID, STATE_NUMBER, CONFIG, SIMULATIONS, SEED, BATCH_SIZE, MAX_OVERTIMES, MAX_POSSESSIONS_PER_HORIZON, LINES, QUANTILES. Document direct PowerShell invocation, repeated query flags, exit1/unresolved meaning and uncalibrated prior; replace reserved-setting README label with actual precedence. Existing ML image invokes `-m scripts.simulate_totals`; no new API/Compose service.
- [ ] **Step 4: GREEN:** same command plus `tests/unit/test_modeling_cli.py tests/unit/test_build_baselines_cli.py`; actual SIGINT probes pass. `uv run python -m scripts.simulate_totals --help` exits0 without native loading. Validate Make guards using available shell/Make tooling; direct PowerShell remains supported if Make absent.
- [ ] **Step 5: Commit:** `feat: expose offline totals simulation CLI and operator runbook`.

### Task 13: Real published-source and native end-to-end acceptance

**Files:** Create `tests/integration/simulation_harness.py`, `tests/integration/test_simulation_source.py`, `tests/e2e/test_simulation.py`.

**Interfaces:** Produce `SimulationAcceptance(engine:AsyncEngine,client:ClickHouseClient,*,recovery:bool=False)` with `async seed_pipeline()->tuple[ModelSourceReference,...]`, `async storage_projection()->tuple[dict[str,list[dict[str,Any]]],list[object]]`, `async run_cli(module:str,*arguments:str)->tuple[int,str,str]`, `async run_clis(directory:Path)->SimulationArtifacts`, `async cleanup()->None`.
`SimulationArtifacts(dataset_path:Path,bundle_path:Path,all_states_path:Path,selected_state_path:Path,all_states:ValidatedSimulationExport,selected_state:ValidatedSimulationExport)`.
`@asynccontextmanager async simulation_acceptance(*,recovery:bool=False)->AsyncIterator[SimulationAcceptance]` owns engine/client/journal lifetime using existing runtime_config loopback guards. `run_clis` builds/trains fixtures and then invokes simulation; it does not rebuild source snapshots.
Use existing `OwnershipJournal`, public pipeline services/readers and NBA-shaped regulation/two-OT fixtures. New owned identities: season2095-96, games0029595101..0029595105, dates2095-10-01/10-02/10-31/11-05/12-05, retarget team/player IDs to91395xxx namespace. Recovery fixture has explicit reset normalizer version `fixture-simulation-reset-v1`; never modify live production normalizer/version. No reuse of retained Phase7 artifact/scratch ownership.

- [ ] **Step 1: RED real-source/native tests.** Seed real Phase2→3→4→5, build Phase7 dataset/CPU bundle through real CLIs, run new CLI with N128/batch64 and integer/half lines. Assert checked output contains cold-history, regulation/current OT/terminal states; terminal/invalid rows unavailable, later recovered valid state uses executor. Resolved+unknown mass conserved; actual future two-OT suffix is not an input. Select one available state from exported frame identities, rerun exact state and compare histogram/seed/PMFs; changing query only changes derived summaries. Export identical selectors/query to another path and compare bytes/SHA. Read pinned bundle once for causality probes; do not compare retrained native artifact bytes.
Capture all PostgreSQL metadata rows plus four ClickHouse table counts before/after simulation-only CLI; equality proves no mutation. Assert no NBA fixture HTTP calls during simulation. Inject real source checksum/version/pointer failure after preparation → old destination remains. Ownership journal refuses preexisting IDs and cleanup touches only captured owned IDs/snapshot UUIDs; no default-database/global truncate.

```python
async def test_native_simulation_preserves_source_storage(tmp_path):
    async with simulation_acceptance() as harness:
        await harness.seed_pipeline()
        before = await harness.storage_projection()
        artifacts = await harness.run_clis(tmp_path)
        assert artifacts.all_states.manifest.counts.games == 5
        assert artifacts.all_states.manifest.counts.unavailable > 0
        assert await harness.storage_projection() == before
```
- [ ] **Step 2: RED:** after provision described below, `uv run --extra ml pytest tests/integration/test_simulation_source.py tests/e2e/test_simulation.py -q -m 'integration or e2e'`; missing harness/new feature assertion. First failing test is recorded before product fixes.
- [ ] **Step 3: Implement named harness/probes and any demonstrated integration correction.** Mirror existing harness pattern with new IDs, not copied artifact evidence. Train split validation2095-11-01/test2095-12-01/embargo1, two train games, fixture-only reset separated. Every spawned CLI has timeout/reap and safe log capture. Pin outputs and compare simulation-only storage projections separately from intentional fixture ingestion/training setup.
At this task's execution gate, create or verify THIS plan's scratch `.superpowers/sdd/2026-10-15-phase-8-monte-carlo` and ownership ledger, preserving any Task1 tooling evidence; use frozen existing `.venv`/available uv or bootstrap uv only inside this owned scratch (no global/PATH change). Allocate fresh Compose project `nba-phase8-<8hex>` and four available loopback ports; capture exact ID/ports/labels/volumes in ledger. Use `compose.yaml` plus owned override, production-compatible migrations and API build. Set TEST_DATABASE_URL/TEST_CLICKHOUSE_URL/TEST_REDIS_URL/TEST_API_URL explicitly in EVERY test shell invocation. No default stores or old Phase6/7 project/scratch. This setup is authorized by plan approval, no additional routine approval gate.
- [ ] **Step 4: GREEN:** same integration/E2E command, regular and explicit-reset cases; native bundle load/simulation, no-write projections, selected-state/query/path checks pass. Record actual artifact SHAs/counts and owned infrastructure coordinates in scratch evidence for Task14.
- [ ] **Step 5: Commit:** `test: verify native simulation against published feature snapshots`.

### Task 14: Platform, regression and final independent review checkpoint

**Files:** Create `docs/superpowers/reports/2026-10-15-phase-8-acceptance.md`; extend `tests/unit/simulation_interrupt_probe.py` and `tests/unit/test_simulate_totals_cli.py` only if platform evidence exposes a missing assertion. Modify README/roadmap `docs/superpowers/specs/2026-08-30-phase-1-infrastructure-design.md`, Phase8 spec and this plan status only after observed acceptance.

**Interfaces:** Consumes all shipped CLI/reader ports and Task13 owned resource ledger. Produces archived acceptance evidence with actual commands/runtime/test outcomes/SHA/counts, fresh final review disposition and verified cleanup scope. No calibration/model-accuracy promise and no automatic transition to Phase9.

- [ ] **Step 1: Write/run platform acceptance probes before accepting the implementation.** Windows frozen ML extra: actual CLI bundle load/native simulation, same-runtime repeated histograms and readback; cancellation during live CPU work and real pre/postcommit SIGINT. Linux image built from current `docker/ml.Dockerfile`: run help and actual bundle→simulation/reader under image UID999 using owned bind-mounted model/results; capture Python/NumPy/OS/architecture and mass/hashes. Assert within-runtime repeatability, not Windows/Linux equal draws. Default API image cannot import CatBoost and exposes unchanged routes/dependency scope; do not assume NumPy absent because existing NBA dependencies can provide it transitively. Any missing behavior gets a focused RED test before a product fix.
- [ ] **Step 2: Run comprehensive gates with owned explicit environment.** `uv sync --frozen --extra ml`; `uv run --extra ml pytest -m 'not live' -q`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy`; `git diff --check`. Expected: all pass, expected live/destructive skips itemized, no unexplained failure. Run full suite only against Task13 fresh owned project; phase6/7 SIGINT/owned-I/O regressions included. Read complete redirected logs, not only exit tail. Fix actual failures by evidence, rerun affected checks; do not fabricate baseline numbers or claim runtime results from pure fakes.
- [ ] **Step 3: Independent review and verified correction pass.** Follow preserved native workflow: one fresh whole-change reviewer using `superpowers:requesting-code-review`, most capable available model gpt-6-astra/high with no inherited full history. Provide approved spec/plan, base SHA captured before Task1, current diff and acceptance evidence; request correctness/causality/numerics/cancellation/atomicity/resource review. Address confirmed Critical/Important findings with focused RED→GREEN tests and relevant regressions/native recheck; archive any justified deferral. No extra review per task or repeated whole-review loop. If new failures remain, resolve them before claiming completion.
- [ ] **Step 4: Archive verified acceptance/status and clean owned resources.** Write actual dates, commands, result counts, runtime identities, model/artifact hashes, no-write projections, numerical limits, unresolved semantics, final review/fix evidence and remaining limitations. Update README/roadmap to Phase8 accepted only after gates. Commit evidence before removing owned artifacts. Remove only Task13 project/volumes and THIS plan's scratch after verifying exact absolute target under workspace, no reparse escape and ledger match; verify no remaining matching Docker resources. If recursive deletion is blocked by policy, report exact retained path/reason and do not use alternate deletion tooling. Retained Phase6/7 scratch is excluded.
- [ ] **Step 5: Final commit/status:** `docs: record verified Phase 8 simulation acceptance`; check Git status and return concise outcome, report link, actual checks and any retained resources. Stop before Phase9 replay/calibration design.

## Plan Self-Review and Execution Handoff

Spec coverage: §§1–4 → Global Constraints/Tasks1,3,5,12; §5 → Tasks1,12;
§6 → Tasks1,11; §7 → Tasks6,7; §8 → Tasks2,6; §9 → Task7;
§10 → Tasks3,6,7,13; §11 → Tasks4,7,9; §12 → Tasks6–8,10,11;
§13 → Tasks5,10,11; §14 → Tasks3,5,9,10; §15 → Tasks5,12;
§16 → file map/interfaces; §17 → Tasks1–14; §18 → Tasks6,14/runtime evidence.

Self-review completed 2026-10-15: all14 deliverables have owned files/interfaces
and 70 unchecked steps; names/dependencies checked against existing code and spec.
Thirteen Python assertion examples parse; Markdown links resolve; independent
Fraction/isqrt checks confirm prior sum1000000/mean1141111 and summary1250000/829156.
Five Review Focus entries each have owning rejection/acceptance tests; literal
defaults/units/mass/seed/cap rules checked against approved spec.
PMF isolated into its own pure module to avoid
mixing numeric algorithm with DTO definitions. Plan adds no dependency or product
scope and does not implement product code. Do not treat this self-review as user
approval or final independent code review.

Execution method already selected: **сам в текущем чате/main**, one final fresh
independent reviewer. Plan approved by user 2026-10-15; native execution in progress.
Use `superpowers:executing-plans`; preserve method without asking
the execution-choice question again. Before approval, product code/dependencies,
runtime scaffolding and Docker resources remain unchanged.
