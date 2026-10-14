# Phase 8 — Offline Monte Carlo Totals

Дата: 2026-10-14 (Europe/Moscow).
Статус: conversational design approved; written specification awaiting user review.
Product implementation и новые зависимости в этой стадии не меняются.

## 1. Цель и согласованные решения

Из текущего состояния матча и трёх rates Phase 7 получить распределения итоговых
очков home/away и тотала, средние, квантили и вероятности относительно заданных
тотальных линий. Пользователь одобрил:

- весь остаток матча, включая возможные будущие OT;
- модель отдельных владений с заданными versioned параметрами, не обучение sampler;
- экспоненциальную длительность, чередование сторон и дискретные очки со средним PPP;
- фиксированные текущие rates на весь сценарий, включая OT;
- seed, явные resource limits и учёт незавершённых сценариев;
- checked bundle + published states, NumPy kernel, offline CLI и atomic JSONL.

Цель фазы — проверяемый механизм вероятностного прогноза. Дисперсия и вероятности
не объявляются откалиброванными: точность распределений проверяется на replay в
Phase 9. Defaults score prior не являются оценками, полученными из NBA-истории.

## 2. Контекст и границы

Основа: [roadmap](2026-08-30-phase-1-infrastructure-design.md),
[Phase 7 design](2026-10-13-phase-7-catboost-design.md) и
[Phase 7 acceptance](../reports/2026-10-13-phase-7-acceptance.md).
Phase 7 принят: 2371 passed, 18 expected skips, Windows/Linux native acceptance.

Не добавляются prediction API, Redis cache, live polling, исторический replay,
обучение распределений, букмекерские feeds, подбор ставок, player/lineup synergy.
Не меняются targets, 96-column projection, fit recipe или формат model bundle.
Нет исторического as-of обещания: опубликованные источники остаются canonical.

Детальные броски/штрафные/подборы/фолы, смены составов и игровая стратегия не
симулируются. Дискретные очки — итоговые очки аналитического владения, а не число
очков одного броска. Число будущих OT неизвестно и не читается из исторического PBP.

## 3. Архитектура и зависимости

Pure domain содержит frozen DTOs, config/query validation, score-PMF projection,
hashes, histogram validation и exact summaries. Он не импортирует NumPy,
CatBoost, Settings, базы или filesystem.

Application orchestrates read-only sources, один checked model bundle, sequential
states, cancellable CPU executor и streaming exporter. Infrastructure содержит
lazy NumPy adapter, owned thread executor и atomic JSONL I/O.

Повторно используются `PublishedModelFeatureSource.read_features`, source recheck,
`VerifiedModelBundleStore`, `ModelPredictor.predict_state`, `run_source_work` и
`scripts.async_runner`. `read_training` и horizon endpoints не нужны.
Prediction JSONL Phase 7 не используется как самостоятельный input: он не содержит
полного текущего счёта; source feature state даёт необходимый causal input.

Используется существующий optional `ml` extra: NumPy уже входит в frozen lockfile,
CatBoost нужен для выбранного bundle. Новой зависимости и изменения default API
image dependency scope не требуется. Help и pure-domain tests работают без extra.

## 4. Версии и единицы

Versions V1:

| Contract | Identifier |
|---|---|
| Sample policy/config | `possession-simulation-v1` |
| Score-PMF policy | `tilted-integer-score-pmf-v1` |
| Numeric engine | `numpy-poisson-multinomial-v1` |
| RNG seed policy | `state-pcg64-seed-v1` |
| Summary/query policy | `exact-simulation-summary-v1` |
| JSONL | `simulation-results-jsonl-v1` |

Scale `S=1_000_000`. Rates, probabilities и mean/stdev сохраняются как Int64 scaled
integers или null. Scores и histogram keys — целые очки, время — integer ms.
Quantile levels и total lines — scaled integers. Canonical UTF-8/LF JSON не содержит
float, NaN, Infinity. Base seed — объявленный UInt64 exception к signed Int64
numeric полям; derived seed сохраняется как SHA-256 string, не как 256-bit JSON int.
Runtime floats разрешены только для tilt/native sampling.

## 5. SimulationConfig и SimulationQuery

`SimulationConfig` включает policy versions и следующие exact integer поля:

| Поле | Default | Bounds |
|---|---:|---|
| simulations | 10000 | 1..1000000 |
| seed | 42 | 0..2^64-1 |
| batch_size | 1024 | 1..65536 и <= simulations |
| max_overtimes | 10 | 1..20 новых OT после текущего horizon |
| max_possessions_per_horizon | 10000 | 1..1000000 combined possessions на trajectory |

`score_prior_weights` — tuple positive integers для всех consecutive outcomes
`0..K`, `2 <= K <= 32`, сумма строго S. Default:
`(460000, 80000, 330000, 120000, 9000, 900, 90, 9, 1)` для `0..8` очков.
Это явный некалиброванный shape prior с небольшой ненулевой mass на редких outcomes.

Форма длительности в V1 фиксирована exponential; её scale задаётся pace. Gamma
shape, correlation knobs и learned priors не добавляются скрыто. Все изменения
policy/defaults, способные изменить samples, требуют versioned contract.

`SimulationQuery` независим от config/RNG: sorted unique `lines_scaled` (0..64
линий) и sorted unique `quantiles_scaled` (1..32 levels). Линии неотрицательны,
Int64 и кратны S/2; default lines пусты. Levels 0..S включительно; defaults
`(50000, 500000, 950000)`. Bool не принимается вместо integer.

## 6. Causal frame и availability

`SimulationFrame` строится из одного `StateModelPrediction`: GameStateFeature,
его score/time/boundary, три rates с clipping flags, feature input SHA и обе
model identities. Frame проверяет Phase 7 clocks/horizon, source identity,
неотрицательные scores и отсутствие float/маскированных structural errors.

Состояния с invalid lineup → `invalid_feature_state`; R=0 → `no_future_time`.
Они сохраняются как unavailable без RNG и не становятся терминальными forecasts.
В частности, R=0/tie не приводит к выдуманным rates или чтению следующего OT.
Будущие OT симулируются только внутри trajectories, начатых с available R>0.

Available rate вне support не обрезается: PPP вне `[0,K*S]` даёт unavailable
`ppp_outside_score_support`; pace > `1000*S` даёт `pace_outside_runtime_limit`.
Это объявленные границы V1, не accuracy thresholds. Zero pace и zero PPP допустимы.
Phase 7 negative-clipping flags сохраняются без повторного postprocessing.

Ошибки versions, feature hashes, clocks, identity, native nonfinite/overflow —
operational errors всего экспорта, а не quality masking одной строки.
Home+away current score должен помещаться в `MAX_SCORE = Int64_max // S`.
Наблюдённый итог выше MAX_SCORE также отвергается до summary/publication.

## 7. Время и число владений

R — `seconds_remaining_game_ms`: остаток regulation для p<=4, текущего OT для p>=5.
Pregame использует 2880000 ms. Frozen rate P — pace одной команды за 48 минут.
Средняя длительность combined possession при P>0:

`mean_duration_ms = 1440000 * S / P_scaled`.

Количество завершённых владений до истечения R имеет Poisson mean:

`lambda = P_scaled * R_ms / (1440000 * S)`.

Это эквивалент подсчёта iid exponential interarrival times, а не переход к normal
distribution суммарных очков. При fixed rates и отсутствии зависимости outcome
от порядка engine может интегрировать clocks и считать Poisson counts напрямую.
Отдельный event trace не создаётся и не сохраняется.

Первое offense каждой trajectory/horizon выбирается Bernoulli(1/2). Стороны
чередуются: combined count n даёт floor(n/2) каждой стороне и дополнительное
владение выбранной первой стороне при нечётном n. Source не содержит confirmed
next-offense field; V1 не делает вид, что эта сторона известна. Это coarse horizon
model, без реконструкции будущих quarter-opening possession rules.

При P=0 counts=0 без деления на ноль. Counts выше max_possessions_per_horizon
останавливают только соответствующую trajectory как unresolved `possession_limit`:
их не clamp/resample и не удаляют из denominator.

## 8. Дискретные очки со средним PPP

Для каждой команды target T = PPP_scaled; support `0..K`. Priors w_k положительны.
Для 0<T<K*S используется exponential tilt:

`q_k(beta) = w_k * exp(beta*k) / sum_j(w_j * exp(beta*j))`.

Beta выбирается так, чтобы `sum(k*q_k)=T/S`. Stable max-shifted log weights,
bracket [-64,64], 80 bisection iterations; midpoint equality идёт в lower branch.
T=0 даёт atom0; T=K*S — atomK; T=sum(k*w_k) возвращает исходный integer prior.
Native nonfinite, невалидный bracket или невозможное
normalization отвергаются; запрещены fallback на guessed scoring means.

PMF сохраняется как integer counts c_k с `sum c_k=S` и `sum k*c_k=T` точно:
floor(S*q_k), затем remaining units по largest fractional remainder, tie по k
ascending. Residual mean исправляется переносом единиц между соседними outcomes:
для положительного residual — из smallest nonempty k<K в k+1; для отрицательного
— из largest nonempty k>0 в k-1. Каждый перенос сохраняет mass и меняет mean на
одну scaled unit. До 1024 переносов; невозможность выполнить invariants — ошибка.

Это versioned integer projection близкого tilt, не independently rounded PMF.
Численные tests отдельно проверяют обе exact identities и boundary cases.

Для n владений команды engine получает histogram outcomes через multinomial
с этим PMF и складывает `sum(k*count_k)`. Это эквивалент n независимым categorical
outcomes при frozen rates. Для native call modal outcome (максимальный c_k, tie —
минимальный k) переносится последним, остальные остаются ascending. Это гарантирует
remaining mass >=1/(K+1) до float rounding и устраняет near-zero negative residual
от суммирования. Float conversion c_k/S ограничена native boundary; последняя
probability =1-fsum(previous), finite/range checks обязательны. Counts возвращаются
к исходному ascending outcome order до score reduction. Persisted PMF остаётся
integer. Очки добавляются только своей команде.

## 9. Конец horizon и будущие OT

После residual horizon сравниваются simulated scores, не actual final scores.
Non-tie завершает trajectory. Tie запускает новый 300000-ms OT с теми же rates,
новой первой стороной и новыми counts/outcomes. Пока tie сохраняется, процесс
повторяется. `max_overtimes` ограничивает именно новые OT: уже начавшийся OT
является initial residual horizon и не расходует этот лимит.

Если после последнего разрешённого OT всё ещё tie, trajectory unresolved с причиной
`overtime_limit`. Не добавляются случайный победитель, half-point или extra score;
не выполняются silent rejection sampling и условная перенормировка оставшихся paths.
Технический лимит не является правилом NBA и не выдаётся за окончание матча.

Правила 12-minute regulation periods, 5-minute OT и продолжения при tie проверены
по [NBA Rule 5](https://official.nba.com/rule-no-5-scoring-and-timing/).
Используются game-clock minutes, без wall-clock intermissions.

## 10. RNG и воспроизводимость

Используется явный `Generator(PCG64(...))`, не global RNG/default generator.
Runtime identity содержит Python, NumPy, OS/architecture и engine version.
Per-state seed digest — canonical SHA-256 от base seed, config hash, runtime
identity, model recipe/artifact hashes и causal frame projection (game/state IDs,
boundary, clock/score/rates, model input hash). Полные 256 bits интерпретируются
как unsigned big-endian integer для PCG64 initialization.

Query/lines, output path, source UUIDs/timestamps, другие selected states, порядок
запуска и selection scope не входят в seed. Одно состояние даёт те же histograms
при отдельном и season export с теми же config/runtime/model/frame.
Batch size входит в config: независимость samples от его изменения не обещается.
Чтение от skipped/unavailable states не сдвигает stream других states.

Versioned draw order для каждого consecutive trajectory batch: initial horizon,
затем new OT1..limit; только active paths в original trajectory order. Для каждого
positive-lambda horizon — vector combined Poisson counts, vector first-side bits
для путей внутри possession limit, home multinomial, away multinomial.
При lambda=0 эти четыре draw stages пропускаются. Scoring вызывается для admitted
paths, включая n=0, в полном original order; vectors не фильтруются по n=0.
Переходы/masks не sort по счёту. Не создаются streams между batches.

Гарантия: повтор на том же verified runtime/algorithm/config/frame. NumPy не
обещает general stream compatibility между версиями: записывается runtime identity;
cross-platform/cross-version byte identity не обещается.
[NumPy compatibility](https://numpy.org/doc/stable/reference/random/compatibility.html).

## 11. Outcomes, mass и exact summaries

На каждом состоянии сохраняется один `SimulationOutcome` со status
`complete`, `incomplete` или `unavailable`. Available paths запрошены числом N.
`resolved + unresolved = N` точно; unresolved разбиты по mutually exclusive reason.
Для unavailable: `requested=N`, `executed=resolved=unresolved=0`, reason обязателен.
Для executed outcomes `executed=N`. Missing не трактуется как нулевой score.

Три marginal integer histograms: home, away, total; каждый sorted по score,
positive count, сумма counts=resolved. Exact first moment total histogram равен
сумме first moments home+away; это проверяется без rounded mean fields. Нельзя
восстановить joint grid из marginals, и readback не обещает такой reconstruction.
`new_ot_counts` (0..max_overtimes) содержит completed paths по числу добавленных OT,
сумма=resolved; текущий OT сюда не включается. Joint score grid, победитель и spread
probabilities в формат V1 не входят. Гистограммы содержат только completed paths,
не scores незавершённых OT. Это явно записано в schema.

Полные mean/stdev/quantiles и line probabilities доступны только при resolved=N.
Для incomplete они null; resolved histograms не выдаются за полное распределение.
В каждой line row: count_under, count_equal, count_over, count_unknown, сумма=N
для executed states. Unknown=unresolved. Для unavailable все counts null.

При complete mean считается exact Fraction(sum(score*count),N), stdev — population
sqrt(E[x^2]-E[x]^2) через integer/rational sqrt с final half-away rounding. Нет float
persisted variance. Mean/stdev scaled by S; quantile score — integer points.
Nearest-rank quantile: для q=0 minimum, q=S maximum; иначе rank=ceil(q*N/S).

Line comparison выполняется в integer arithmetic against line_scaled: integer
тотал S*t сравнивается с линией. Equality означает только равенство порогу;
букмекерский settlement не моделируется. Probability fields = round(count*S/N),
half-away-from-zero; counts — authority. Три independently rounded probabilities
могут суммарно отличаться от S на одну unit; нельзя тихо менять equality mass.

## 12. Memory, native boundary и cancellation

Kernel vectorized по trajectory batch, не materializes (N x possessions) или
все event traces. Native matrices максимум batch x (K+1); остальное — bounded
batch vectors и integer histograms. Aggregate histogram bin budget 100000 distinct
keys на каждую marginal; превышение — operational `simulation_histogram_limit`.
Весь источник держится по одному game; результаты не копятся как список всех states.

Counts/outcomes arrays dtype Int64, checked nonnegative shape/counts. λ float finite
и неотрицателен; rate/domain bounds применяются до native Poisson. Histogram/moment
accumulators используют Python integers; суммы и квадраты не вычисляются в Int64
с риском wrap. Любой numeric overflow прекращает экспорт, сохраняя destination.

Sequential application вызывает owned CPU executor для одного state. Он владеет
одним thread task и cancellation Event; kernel проверяет его между batches и OT
stages. Отмена сигнализирует stop, затем joins worker до освобождения его данных.
Model inference и file I/O также joined до cleanup. Нет процесса, продолжающего
писать/считать после CLI exit. Симулятор не запускает несколько states параллельно.

Допустимо добавить optional synchronous `on_cancel` callback к существующему
`run_owned_io`, с default None и прежним контрактом Phase 7; callback сигнализирует
Event, не удаляет файлы и не бросает exceptions. Regression Phase 7 обязателен.

## 13. Service и streaming exporter

`SimulateTotalsService`:

1. Загружает checked bundle один раз; capture simulation runtime/config/query.
2. Выбирает source games через public Phase 5/7 boundary; pin refs, проверить P/L/F.
3. Читает один verified FeatureDataset; выбирает все или requested state_number.
4. Для каждого selected state: owned model inference → pure frame → CPU executor
   (или unavailable outcome) → append одной bounded outcome row.
5. Проверяет state sequence/selection, end_game и повторно source reference.
6. Подготавливает artifact, проверяет readback, rechecks все источники, publish.
7. Joins/cleans owned resources; committed result сохраняется при cleanup failure.

Exporter port: begin(request,bundle,runtime), begin_game(source, selected_state_numbers),
append_state(outcome), end_game(), prepare(sources), synchronous publish(), abort().
State numbers selection хранится как bounded tuple для одного source game; при
all-states предпочтителен expected count/range, не дополнительный список outcomes.
Begin/end контролируют chronology, отсутствие duplicates, counts и identity.
Это intentionally не `append_game(list_of_all_simulations)`: память не зависит от
числа sampled results всего game.

Только published CURRENT/INVALID snapshots; quality обрабатывается per state.
Никаких rebuild/claims/migrations/NBA HTTP из simulation source или CLI.
Pointer/dependency race на final recheck отменяет целый artifact.
CLI composition validate четыре схемы, как в Phase 7.

## 14. JSONL и identities

Первый record — manifest: schema/versions, full config/query и hashes, model recipe
и exact bundle SHA, feature descriptor/versions, simulation runtime, selected
sources/provenance, counts complete/incomplete/unavailable, unresolved-reason totals,
source_sha256, body_sha256, run semantic revision. Sources используют существующий
lightweight PBP/P/L/F provenance; final Game scores в source metadata не являются
simulation inputs и не входят в per-state seed/frame.

Каждая body row содержит causal frame identity/score/time/rates/clipping, PMFs
(для executed), state seed digest, simulation revision, status/reason, N/counts,
три marginals/summaries, line rows и точные source feature/model references.
Unavailable PMFs/histograms/summaries null, не пустые успешные результаты.
Incomplete histogram может быть пустым при resolved=0; status всё равно incomplete.

`simulation_revision_sha256` идентифицирует population recipe/config/runtime/frame,
не query. `run_semantic_revision` включает selectors, config/query, semantic source
identities и semantic body digest; excludes operational source UUIDs/timestamps.
`artifact_sha256` — exact whole file SHA, возвращается после публикации, не включается
в собственный manifest. Exact body includes operational provenance.

Canonical sorted schema projections, LF, 16 MiB max каждый record/manifest; unknown
keys, duplicate keys и noncanonical bytes rejected by readback. Record counters,
histogram mass, PMF identities, summaries, probabilities и hashes rederived при
validation. Semantic body projection заменяет operational source identities semantic
references, не выбрасывает sampled histograms. Paths/launch time не влияют на bytes.

Temporary files в destination parent, incremental hash/body append, fsync, checked
readback, final source recheck и одна os.replace. Пути temporary ownership присваиваются
пошагово после каждого allocation: не копировать deferred tuple-allocation minor Phase 7.
Существующий model/config input не может быть output: resolved-path equality,
symlink aliases и samefile/hardlink equality для существующих файлов проверяются
до publication. Нет input/output alias workaround через atomic replacement.

## 15. CLI и operator contract

`python -m scripts.simulate_totals`:

- `--season` либо полный `--season-from/--season-to`, как Phase 7;
- optional `--game-id` только с одним season;
- optional `--state-number` только с game-id; exact state должен существовать;
- required `--model`, `--output`; destination parent существует;
- optional `--simulations`, `--seed`, `--batch-size`, `--max-overtimes`,
  `--max-possessions-per-horizon`, `--config`;
- repeated `--line` и `--quantile`, Decimal parser без binary-float conversion.

Config-file optional canonical JSON с versioned SimulationConfig, не Python/pickle.
Если config задан, kernel override flags запрещены (никакого неясного merge).
Query flags независимы от config и разрешены всегда; без quantile flags — defaults.
Если config не задан, N: explicit flag > MONTE_CARLO_SIMULATIONS >10000. Остальные
flags > defaults; при batch_size omission default min(1024,N).
Help/import не подключаются к stores и не требуют native extra.

Exit0: complete atomic artifact, все selected rows complete.
Exit1: artifact published, есть incomplete/unavailable rows.
Exit2: validation/source/schema/native/I/O error до publication.
Exit130: cancellation до publication. Два SIGINT после replace сохраняют published
summary/exit0/1. Generic safe error codes, credentials/model contents не печатаются.
Summary: games, states, complete/incomplete/unavailable, total unresolved,
run revision и artifact SHA. Полный all-state export обычно partial из-за R=0.

Make target `simulate-totals`, direct PowerShell runbook и существующий ML Dockerfile
поддерживают новый CLI. Нет нового Compose service/API route. DefaultN совпадает с
существующим reserved setting; теперь оно используется operator CLI по правилу выше.

## 16. Modules и interfaces для будущего плана

| Layer | Responsibility |
|---|---|
| domain/simulation.py | Config/query/frame, exact PMF projection, typed outcomes |
| domain/simulation_summary.py | Histograms, moments, quantiles, line counts |
| domain/simulation_hashing.py | Canonical versions/projections/seed/revision |
| application/simulation/models.py,ports.py,errors.py | Typed requests/results/ownership boundaries |
| application/simulation/service.py | Sequential read-only orchestration |
| infrastructure/simulation/numpy_engine.py | Lazy typed RNG/native vectorization |
| infrastructure/simulation/executor.py | Owned cancellation/join boundary |
| infrastructure/files/simulation_export.py | Streaming atomic JSONL and readback |
| scripts/simulate_totals.py | Guards, existing source composition/interrupt runner |
| integration/E2E owned harness | Real published-source + native model/simulation pipeline |

Kernel takes only frame/config/stop predicate; returns outcome, not files/DB values.
CPU executor async simulates one frame; unavailable paths can remain pure.
Service/exporter никогда не импортируют NumPy arrays как application DTOs.
Точные signatures и task boundaries уточняются в отдельном implementation plan
после written-spec approval, без изменения описанных contracts.

## 17. Tests и acceptance

Unit: strict exact integer/default/weight/query bounds; PMF mass/mean identities,
interior/edge/extreme rates; literal independent goldens. Analytic Poisson expectation
и known categorical means/variance проверяются на fixed seed с обоснованной tolerance,
не arbitrary model-accuracy threshold. Малая hand-built histogram проверяет nearest
rank, equality/half-lines, mean/population stdev, rounding, null при unresolved.

Controlled RNG/kernel tests: alternation/parity, zero rates, tie/no-tie, current OT
и several future OT, cap semantics, counts за resource limit без clamp/resample,
mass conservation, empty completed histograms, Int64 overflow before wrap.
Native NumPy tests обязательны наряду с controlled fixtures.

Causality при одном pinned model bundle и одинаковом current feature state:
changed future suffix/final score/known future OT не меняют frame/seed. Изменение
доступных current/pre-game features не объявляется неизменным, а исправление
source provenance может потребовать rematerialization по существующему протоколу.
Standalone state и season selection дают те же samples; изменённые lines/quantiles
не меняют histograms. Batch change не тестируется как promised bit identity.
Same pinned runtime/config/frame repeats; operational source UUID change affects
exact provenance bytes, не RNG. Cross-path exact file test uses same pinned refs.

Orchestration: version/hash failures before publish, source race after prepare,
missing/duplicate selected state, one-game memory release, streamed rows, begin
allocation failure cleanup, cooperative cancellation/join, PermissionError replace,
postcommit cleanup failure и actual one/two SIGINT probes. Phase 6/7 interrupt и
owned_io regressions retained. No storage mutations/NBA calls from simulation CLI.

Integration/E2E: fresh owned Compose project/volumes/loopback ports, local NBA-shaped
fixtures через real Phase 2→3→4→5, checked trained Phase 7 bundle, native simulation
CLI; before/after PG+четыре CH count projections. Include regulation/2OT, invalid
and later valid state with separate explicit-reset fixture, cold history, terminal
rows, exact selected-state and different output paths. Не использовать actual final
scores как forecast inputs и synthetic outcomes как доказательство calibration.

Gates: Windows frozen ML extra + Linux ML image native execution/UID999; full
`pytest -m 'not live'`, Ruff check/format, strict configured mypy, git diff --check.
Expected live/destructive skips explained. Independent final whole-change review
and one RED→GREEN fix pass follow selected inline execution workflow. Delete only
new owned resources after evidence archive; existing retained Phase6/7 scratch
belongs to those plans and is not a Phase8 cleanup target.

## 18. Проверенные внешние API и дальнейший шаг

На стадии дизайна проверены installed NumPy2.5.2 array-n multinomial и array-lambda
Poisson (shape/counts), без product code/dependency changes. Это не подменяет
acceptance implementation на Windows/Linux.

- [Poisson API](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.poisson.html)
  accepts scalar/array expected counts; λ/native Int64 range требует guarding.
- [Multinomial API](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.multinomial.html)
  supports broadcast n/pvals; последний pval трактуется как remaining probability.
- [RNG compatibility](https://numpy.org/doc/stable/reference/random/compatibility.html)
  не даёт общей гарантии одинаковых sampled streams между версиями/окружениями.
- [NBA Rule 5](https://official.nba.com/rule-no-5-scoring-and-timing/)
  определяет duration OT и продолжение игры при tie.

Следующий шаг: user review этого written spec. После approval — отдельный plan
через `superpowers:writing-plans`, затем выполнение выбранным ранее способом
(сам в текущем чате/main с одним независимым reviewer в конце), если пользователь
не изменит preference. После acceptance остановиться перед Phase9 replay design.

Spec self-review 2026-10-14: 18 sections; placeholder scan clean; config bounds,
clock/PPP units, exact PMF mass/mean, native modal-last projection, unresolved
denominators, query-independent RNG, streaming ownership и review gates проверены
на внутреннюю согласованность. Изменён только этот документ; implementation не начат.
