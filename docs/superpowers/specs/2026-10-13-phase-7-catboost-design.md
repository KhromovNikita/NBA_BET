# Phase 7 — Offline CatBoost Models

Дата: 2026-10-13 (Europe/Moscow)

Статус: written specification approved by user on 2026-10-13; implementation planning.

## 1. Согласованный результат

Пользователь одобрил полный offline-цикл для трёх независимых CPU-моделей:
темп, PPP хозяев и PPP гостей. Цели относятся к оставшемуся основному времени,
а после начала OT — к остатку текущего овертайма. Pipeline готовит проверенную
обучающую выборку, разделяет целые матчи по времени, обучает модели, сравнивает
их с Phase 6 baselines, сохраняет и загружает комплект моделей и предоставляет
CLI для прогнозирования по опубликованным feature snapshots.

Успех: оператор может воспроизвести подготовку данных, обучить настоящий CatBoost,
проверить его на более поздних матчах и получить те же прогнозы после загрузки
сохранённого комплекта. Источники и версии явно зафиксированы. Работоспособность
pipeline не означает доказанное превосходство модели или пригодность к ставкам.

Основа: [roadmap Phase 1](2026-08-30-phase-1-infrastructure-design.md),
[Phase 5 features](2026-09-24-phase-5-feature-engineering-design.md),
[Phase 6 baselines](2026-10-12-phase-6-baselines-design.md) и
[Phase 6 acceptance](../reports/2026-10-13-phase-6-acceptance.md).

## 2. Границы

Включены domain-контракты targets/projection/split/predictions, read-only source
adapter, dataset JSONL, CatBoost adapter, обучение/evaluation, проверенный model
bundle, три CLI entry points, operator runbook и реальные локальные проверки.

Не включены GPU, автоматический подбор параметров, MLflow/remote registry,
Monte Carlo, вероятности линии, прогноз окончательного счёта со всеми будущими OT,
historical replay/backtest, калибровка, API/Redis/live worker, новые player ID
embeddings или pair/full-lineup synergy. Phase 8 и Phase 9 остаются отдельными фазами.

Схемы, hashes, publication rules и формулы Phase 1–6 не меняются. Нет новых таблиц,
migrations, claims или build runs в базах. NBA.com не вызывается. Источники читаются
по существующим PostgreSQL pointers и проверяются существующими ClickHouse readers.

## 3. Выбранная архитектура и альтернативы

Выбраны три отдельных `CatBoostRegressor`: независимая доступность целей,
понятные masks/metrics и прямой контракт для следующей фазы. Одна `MultiRMSE`
модель связала бы цели и усложнила работу с отсутствующим PPP одной стороны.
Общая PPP-модель с признаком стороны потребовала бы нового симметричного mapping
и другого модельного контракта. Для V1 эти варианты не добавляют необходимой функции.

Domain не импортирует CatBoost/NumPy и не обращается к хранилищам. Application
управляет последовательностью через ports. Infrastructure читает snapshots,
адаптирует матрицы к CatBoost и публикует файлы. Composition roots находятся в scripts.

Поток: published sources → targets + common feature projection → training JSONL
→ three CPU regressors → validation selection → held-out evaluation → model bundle.
Prediction: bundle + one current feature state → the same projection → three rates.

Обучение читает зафиксированный файл и не перепроверяет latest pointers в базах:
изменение latest после подготовки не отменяет корректную pinned training dataset.
Prediction CLI проверяет свои текущие источники до публикации результата.

## 4. Версии и представление чисел

Новые версии: `remaining-horizon-labels-v1`, `model-feature-projection-v1`,
`game-temporal-split-v1`, `training-dataset-jsonl-v1`, `catboost-training-v1`,
`model-bundle-v1`, `model-predictions-jsonl-v1`.

Targets и persisted predictions используют scale `1_000_000`, как Phase 5/6.
Счёт, количества, clocks и исходные feature values сохраняются как exact signed
Int64; отсутствующие values — JSON null с reason. Bool не допускается вместо
числового параметра. Targets считаются через integer/Fraction arithmetic,
округление half-away-from-zero выполняется один раз на конечную метрику.

Canonical JSON остаётся существующим UTF-8, sorted keys, compact separators, LF
контрактом, без JSON floats, NaN или infinity. Новая ML-граница преобразует rates
из fixed-point в float для CatBoost; runtime NaN разрешён только там как missing
numeric feature. Это не меняет persisted контракты прежних фаз.

## 5. Проверенные источники и границы периодов

`ModelSourceReference` содержит Game и exact `CurrentFeatureSnapshotReference`.
Source adapter использует public `resolve_target`, `read_target`, `resolve_current`
и `read_snapshot` Phase 5, с явными configured Possession/Lineup/Feature versions.
Допускаются CURRENT и INVALID feature snapshots; stale/absent/busy и mismatches
являются operational failure всего экспорта. Оба feature tables проверяются.

Для labels `TrainingGameInput` дополнительно содержит проверенные possessions
из того же `FeatureTargetSnapshot` и `HorizonEnd` для regulation/каждого OT.
`HorizonEnd` фиксирует period, inclusive end boundary, два score и два cumulative
possession counters. PeriodEnd boundaries получаются через public Phase 3 normalizer
из того же applied PBP; нормализатор обязан совпадать с configured version.
Score/counters берутся из последнего проверенного possession до соответствующего
PeriodEnd, включая предыдущие периоды. Пустой период не создаёт владения.

Обязательны последовательные PeriodEnd markers, regulation end и корректное
окончание final game. Boundary/period/clock/counters/score каждого feature state
сверяются с соответствующим possession; pregame имеет zero counters/score и
2_880_000 ms. Final endpoint сверяется с GameContext final score. Для проверенного
parser output нет неатрибутированных административных очков после последнего
possession; несогласованность нельзя исправлять из target box scores.

`read_features` для prediction не строит endpoints/targets. Идентификаторы PBP,
possession/lineup/feature snapshots и их checksums попадают в source manifest.
Adapter не выбирает newest row в ClickHouse и не использует private reconstructors.

## 6. Формулы целей

Для state в периодах 1–4 endpoint — PeriodEnd(4). Для state в периоде p >= 5
endpoint — PeriodEnd(p). Pregame относится к regulation. Время R берётся из
`state.seconds_remaining_game_ms`: до конца regulation или текущего известного OT.
Не используются число будущих OT или duration до фактического конца матча.

Из cumulative endpoint минус current state:

- N_home/N_away — будущие владения каждой стороны;
- P_home/P_away — будущие очки каждой стороны.

Цели в обычных единицах:

```text
pace     = 2_880_000 * (N_home + N_away) / (2 * R)
home_ppp = P_home / N_home
away_ppp = P_away / N_away
```

После этого каждая доступная цель кодируется в scale 1_000_000. Pace — владения
одной команды за 48 минут, совпадает по единицам с Phase 6. PPP учитывает всю
прибавку счёта соответствующей стороны, включая корректно атрибутированные FT.
Овертайм не делит pace на пять минут вместо 48: меняется только R.

Доступность определяется отдельно по каждому head:

| Условие | Результат |
|---|---|
| Invalid current lineup state | Все targets null, `invalid_feature_state` |
| R = 0 | Все targets null, `no_future_time` |
| N_side = 0 при R > 0 | Только PPP этой стороны null, `no_future_possessions` |
| P_side = 0 при N_side > 0 | PPP = 0, доступен |
| N_home + N_away = 0 при R > 0 | Pace = 0, доступен |

Отрицательные suffix deltas, endpoint раньше state, несовпадающие game/team IDs,
clocks, boundaries или отсутствующие обязательные endpoints — structural/source
failure. Они не превращаются в обычную missing row и не подменяются нулём.
Будущие invalid lineup states не отменяют labels текущего valid state: цели
опираются на валидный possession/PBP source, а не на известность будущего состава.

## 7. Общая проекция признаков

`project_model_features(state, baseline_config)` — pure-функция с одним state;
она не получает FeatureDataset, endpoints, Game final score или targets.
Baselines рассчитываются существующим pure calculator Phase 6 для этого state.
Projection имеет фиксированный порядок 96 числовых columns, одинаковый для всех
heads и обоих workflows. Список literal и versioned, не автоматически все поля dataclass.

1. State scalars (11): `period`, `seconds_remaining_game_ms`,
   `seconds_remaining_period_ms`, `home_score`, `away_score`, `score_diff`,
   `home_possessions`, `away_possessions`, `offense_difference`,
   `defense_difference`, `pace_difference`.
2. `home_pregame`, затем `away_pregame` (14 на сторону): `games_used_5`,
   `games_used_10`, `games_used_20`, `off_rating_5/10/20`, `def_rating_5/10/20`,
   `pace_5/10/20`, `rest_days`, `back_to_back`.
3. `home_lineup`, затем `away_lineup` (8 на сторону): `profiled_player_count`,
   `lineup_avg_off_rating`, `lineup_avg_def_rating`, `lineup_avg_usage_rate`,
   `lineup_avg_three_point_attempt_rate`, `lineup_avg_free_throw_rate`,
   `lineup_avg_minutes`, `lineup_avg_pace_context`.
4. Live (29) в порядке: `current_pace`, `pace_last_5/10/20`,
   `pace_last_5/10/20_possessions_used`, `home_ppp`, `away_ppp`,
   `home_ppp_last_5/10/20`, `away_ppp_last_5/10/20`,
   `home_ppp_last_5/10/20_possessions_used`,
   `away_ppp_last_5/10/20_possessions_used`, `home_three_point_attempt_rate`,
   `away_three_point_attempt_rate`, `home_free_throw_rate`, `away_free_throw_rate`,
   `home_turnover_rate`, `away_turnover_rate`, `home_offensive_rebound_rate`,
   `away_offensive_rebound_rate`.
5. Baselines (12): historical, live, shrinkage — у каждого `pace`, `home_ppp`,
   `away_ppp`, затем shrinkage live weights для этих трёх metrics.

Имена nested columns имеют prefix `home_pregame.`, `away_pregame.`,
`home_lineup.`, `away_lineup.`, `live.`, `baseline.<method>.`.
Weights имеют имена `baseline.shrinkage.<metric>_live_weight`.
BaselineConfig default 20/20/40 сохраняется в dataset и bundle; prediction CLI
использует config из bundle и не допускает override, меняющий projection.

Per-column descriptor содержит name и divisor. Для rates/ratings/minutes/differences
и weights divisor = 1_000_000; clocks/counts/rest_days/period/scores divisor = 1.
`back_to_back` превращается в 0/1 или null с divisor 1. При отсутствующем nested
object все его columns null. Отсутствующие values не заменяются нулём.

Game/team/player IDs, UUID, season, started_at, state/possession number, validity
metadata, endpoint, будущие counters, targets, eligibility и training weights
не входят в matrix. В V1 нет categorical columns или target encodings; уже
рассчитанные lineup aggregates входят, сырые player IDs — нет.

Descriptor checksum защищает порядок, names, divisors и baseline config.
Добавление dataclass field не расширяет projection без новой версии.

## 8. Temporal split

Dataset request задаёт один season или inclusive season range, обязательные
`validation_from` и `test_from` как ISO dates и `embargo_days` (default 1,
integer 1–30). Dates трактуются как 00:00:00 UTC; validation_from < test_from.
Классификация опирается на UTC `Game.started_at`, а не provider `game_date`:

```text
train       : started_at < validation_from - embargo_days
embargo     : validation_from - embargo_days <= started_at < validation_from
validation  : validation_from <= started_at < test_from - embargo_days
embargo     : test_from - embargo_days <= started_at < test_from
test        : started_at >= test_from
```

Оба окна до cutoffs исключаются целиком. Validation interval обязан быть
непустым по датам; request с перекрывающимися embargo windows запрещён.
Матчи с одинаковым started_at попадают в одну часть. Все states матча сохраняют
одну partition; random row split, shuffling partitions и подмешивание validation/test
в fit запрещены. В выбранном range не бывает повторных game IDs.

Canonical Game/PBP V1 не содержит фактический wall-clock finished_at. Поэтому
day embargo уменьшает риск пересечения длинного матча с cutoff, но этот split
является offline chronological holdout, не point-in-time replay или доказательством
доступности исправленного provider snapshot в прошлом. Документировать это в
runbook/metrics; Phase 9 отдельно решает historical replay/as-of semantics.

Проверенные causal histories validation/test могут включать более ранние матчи
этих частей: к моменту соответствующего state они являются наблюдаемой историей.
Это не разрешает использовать их targets при fit. Pregame rolling windows Phase 5
не пересчитываются через общий season dataframe или на полном будущем датасете.

## 9. Training dataset и eligibility

`TrainingRow`: game_id, started_at, state_number, boundary, period, horizon_period,
is_valid (diagnostic metadata), partition,
remaining_time_ms, feature_values[96], три nullable targets с reasons,
future possession/point counts и три baseline method outcomes. Последние counts,
targets и partition — diagnostics/labels, не model inputs. Для embargo rows все
eligible masks false с причиной `temporal_embargo`, независимо от доступных targets.
Target unavailable reason и eligibility reason — разные поля: доступная цель
может иметь eligible=false из-за embargo; её value/reason при этом не переписываются.
В records `targets.<head>` имеет `value_scaled`/`unavailable_reason`,
`eligibility.<head>` — `eligible`/`reason`. У доступного target/eligible row reason=null.

Одна row на каждый state, включая invalid/terminal/embargo; fixed chronological
order `(started_at, game_id, state_number)`. Source reference list хранится в manifest.
Builder читает и освобождает одну игру за раз, spools canonical rows на диск,
считает checksum и counters потоково. Не удерживает весь сезон в памяти.

Manifest — первая JSONL row с `record_type=manifest`, затем `record_type=state`.
Manifest содержит selectors, cutoffs/embargo, все versions, projection descriptors,
baseline config/hash, exact source identities/versions/quality/checksums, per-partition
game/state/eligible counts, excluded counts по reason, outcome body SHA-256.
Path, время запуска и random run ID не входят в canonical identity.

`dataset_semantic_revision` включает versions, split, config, semantic feature/PBP/
possession revisions/checksums и canonical semantic body, исключает operational
UUIDs. `dataset_artifact_sha256` — точные bytes всего JSONL, включая source UUIDs.
Один и тот же pinned source при одинаковых selectors даёт побайтово равный JSONL
на разных output paths. Semantics не обещает одинаковые bytes после нового snapshot UUID.

Trainer проверяет canonical bytes, row/schema/order/partition, descriptor hash,
counts, targets/reasons/masks и body checksum до вызова CatBoost. Публикация dataset
сама по себе не обязывает иметь пригодные train/validation/test для каждого head.
Для обучения каждого head нужны: минимум два train games, минимум два разных train
targets, хотя бы один eligible game в validation и test. При несоблюдении — безопасный
`insufficient_training_data`, никаких частичных моделей. Constant train target не
включает `allow_const_label` и не превращает workflow в фиктивный predictor.

## 10. Weights и matrix

Для каждого head/partition eligible означает: non-embargo, valid current state,
R > 0 и доступная цель соответствующего head. Sample weights не являются features.
В train каждой игре назначается равный суммарный вес, чтобы длинные матчи не
доминировали только числом possession states. Для n_g eligible rows игры g,
N eligible rows и G eligible games в соответствующей части:

```text
weight(row) = N / (G * n_g)
```

Средний вес равен 1. Validation использует свою такую же нормировку. Weights
считаются отдельно для head и partition без доступа к test при fit.
CatBoost adapter получает matrix в chronological order, head labels в обычных
единицах, finite positive float weights. Numeric missing values превращаются в NaN
только внутри adapter; остальные float inputs/targets/weights обязаны быть finite.

Trainer обрабатывает heads последовательно и освобождает их matrices. CatBoost
может материализовать текущие train/validation pools в RAM; это явно отличается
от streaming dataset builder. `max_rows_per_partition` default 1_000_000,
positive integer <= 10_000_000, проверяется до allocation. Это ceiling на rows,
не обещание memory budget в bytes. Test evaluation работает bounded batches 4096.

## 11. CPU training V1

ML dependency — optional extra `ml`, CatBoost exactly 1.2.10 с frozen transitive
dependencies в uv.lock. Production API image остаётся без extra. Добавляется
отдельный offline ML Dockerfile для acceptance/операторского запуска; Python 3.14
не меняется. CLI help/dataset preparation не требуют импортировать CatBoost.
Training/prediction при отсутствующем extra возвращают `ml_dependencies_missing`
с инструкцией `uv sync --extra ml`. Строгая типизация достигается собственным
узким typed adapter boundary, не отключением проверки всего проекта.

Fixed defaults для всех трёх heads:

| Parameter | Значение |
|---|---|
| task_type | CPU |
| loss_function / eval_metric | RMSE / RMSE |
| iterations | 1000 |
| depth | 6 |
| learning_rate | 0.03 |
| l2_leaf_reg | 3 |
| random_seed | 42 |
| thread_count | 1 |
| has_time | True |
| boosting_type | Plain |
| bootstrap_type | No |
| random_strength | 0 |
| nan_mode | Min |
| use_best_model | True |
| early_stopping_rounds | 100 |
| allow_writing_files / verbose | False / False |

CLI overrides: iterations 1–100_000, depth 1–10, learning_rate finite decimal
(0,1], l2_leaf_reg finite decimal [0,1000], random_seed integer 0–2^31-1,
early_stopping_rounds 1–iterations, max_rows_per_partition в указанном диапазоне.
Defaults iterations=1000/early_stopping=100; при меньшем iterations оператор
обязан явно передать подходящий early_stopping. Bool/nan/inf запрещены.
Decimal parameters в canonical config имеют нормализованные decimal strings;
float values создаются только внутри ML adapter. Thread_count=1/CPU/loss/schema
не переключаются flags в V1. Остальные resolved CatBoost parameters фиксируются
в training diagnostics и не используются как неподтверждённые пользовательские overrides.
Native resolved float parameters/diagnostic float metrics кодируются finite
нормализованными decimal strings, не JSON float. Operational paths и volatile
native training timestamps/GUIDs не переносятся в собственные manifest fields.

Fit получает только train и validation pools. Test не передаётся в eval_set,
callbacks, early stopping, feature selection или preprocessing. Best iteration и
actual tree_count сохраняются отдельно для каждого head. После выбора не выполняется
refit на train+validation: проверяется и сохраняется та же выбранная модель.

Reproducibility проверяется по matrix/order/config и prediction values на одинаковом
runtime. Не обещается побайтовое равенство `.cbm` повторных fit, либо bit equality
между Windows/Linux/CPU architectures: native metadata/численная реализация могут
различаться. Bundle указывает CatBoost, Python, NumPy версии и platform/architecture.

## 12. Prediction policy

`ModelPredictor.predict_state` получает только GameStateFeature и загруженный
bundle; projection config/versions берутся из bundle. Несовместимые descriptors,
feature versions, hashes или engine version отвергаются до predict. CatBoost
получает явно названные columns в зафиксированном порядке.

Invalid state: все три rates null, `invalid_feature_state`. R = 0: все null,
`no_future_time`. Valid state с R > 0: все три модели работают, даже при cold
pregame history; missing inputs остаются missing. Predictor не требует знать N/P
будущего и не делает fallback на baseline при ошибке модели.

Finite negative RMSE output преобразуется в 0 с явным per-head
`was_clipped_to_zero=true`. Прочие values округляются half-away-from-zero через
Decimal(str(prediction)) к scale 1_000_000. Нет произвольных upper/lower physical
caps. NaN/infinity/Int64 overflow — operational model failure всего prediction
export, не unavailable row. Метрики используют ту же postprocessing policy;
число negative outputs отражается в diagnostics.

Одна prediction row на state: identifiers, boundary, source feature reference,
model semantic revision/artifact SHA-256, state projection SHA-256, horizon period,
R, pace/home_ppp/away_ppp, nullable reasons, clipping flags и derived quality status.
Input hash относится к одному causal projection, не к future targets. Source
manifest содержит полный pinned snapshot hash; изменение suffix может изменить
provenance/artifact bytes, но не rates одинакового current projection.

## 13. Evaluation и сравнение с baselines

Для каждого head отдельно выдаются validation/test row_count, game_count,
MAE и RMSE в обычных target units, game-balanced MAE/RMSE с weights §10,
negative_prediction_count, mask coverage и counts exclusions. Train metrics
не используются как доказательство качества на будущих матчах.

Historical/live/shrinkage Phase 6 сравниваются с соответствующим target на
пересечении eligible rows и доступного baseline metric. Model scores на таком
пересечении пересчитываются на тех же rows; weights пересчитываются по оставшимся
games/rows. Поэтому сравнение не смешивает разные cohorts. Для пустого cohort
metrics null с `no_comparable_rows`, counts = 0, а не ложное значение 0.

Report включает отдельные regulation и OT cohorts, каждый с counts/coverage;
пустой OT cohort явно unavailable. Все persisted MAE/RMSE кодируются integer
scale 1_000_000 (названия заканчиваются `_scaled`); report указывает scale.
Absolute/squared error sums считаются exact по persisted target/prediction values,
weighted sums через Fraction. MAE округляется half-away-from-zero один раз.
Для RMSE пусть Q — exact weighted mean squared error в squared scaled units:
`k = isqrt(floor(Q))`; результат `k+1`, если `4*Q.numerator >=
Q.denominator*(2*k+1)^2`, иначе k. Это exact rounding без ошибки finite-precision
sqrt около половины шага. Overflow запрещён. Native float CatBoost eval metrics
являются training diagnostics, не заменой этим export metrics.

При отсутствии превосходства над baseline pipeline всё равно публикует корректный
bundle и фактический report. Нет искусственного acceptance threshold на синтетике,
подбора seed до победы, автоматического продвижения модели или обещания точности.

## 14. Model bundle и загрузка

Комплект — один ZIP_STORED файл с расширением `.nbamodel`, содержащий ровно пять
flat entries: `manifest.json`, `metrics.json`, `pace.cbm`, `home_ppp.cbm`,
`away_ppp.cbm`. Это позволяет использовать тот же один `os.replace` commit point
на Windows и Linux, вместо попытки атомарно заменить непустой каталог моделей.
ZIP entry timestamps фиксированы 1980-01-01, порядок entries фиксирован как выше.
Paths и launch timestamps не вносятся в собственные metadata.

Manifest содержит bundle/projection/label/split/training/prediction-policy versions,
feature source versions, полный projection descriptor/hash, BaselineConfig/hash,
canonical TrainingConfig/hash, dataset semantic revision и exact artifact checksum,
dataset selectors/split/source manifest, runtime versions/platform, head/tree/best
iteration metadata, checksums/sizes трёх `.cbm` и `metrics.json`.
Prediction policy version: `nonnegative-rmse-rates-v1`.

Model semantic revision вычисляется из dataset semantic revision, canonical
TrainingConfig, всех model/projection/label/split/policy versions и CatBoost version.
Exact `model_artifact_sha256` вычисляется по финальному bundle, хранится в CLI
result/prediction export и не является самоссылочным полем внутри ZIP.
Разные native `.cbm` bytes могут иметь одинаковый semantic recipe revision;
для exact model identity всегда используются оба значения, включая artifact SHA.

До публикации: native files закрыты, SHA-256/размеры рассчитаны; комплект fsync;
полная readback validation; загрузка всех трёх models и probe predictions на
фиксированных eligible validation rows. На том же runtime round-trip encoded
predictions обязаны точно совпадать с pre-save values. Нет публикации двух успешно обученных heads
при ошибке третьего, и нет отдельных latest pointers/remote registry.

Loader открывает и pins один bundle, проверяет allowed entries, duplicates,
ZIP_STORED/schema/version/descriptor hashes, content sizes/checksums до native
load. Не вызывает `extractall`: читает только точные names и пишет их под
собственными flat names в private temporary directory, после load удаляет её.
Directory traversal/extra entries, symlink ZIP entries или schema mismatch
отвергаются. Limits: bundle <= 512 MiB, один `.cbm` <= 128 MiB,
каждый JSON entry <= 16 MiB; проверяется реальный streamed length, не только ZIP metadata.

Engine CatBoost version обязана совпадать; Python major/minor — 3.14. Runtime
patch/platform/NumPy version записываются для provenance; portable loading не
обещает побайтовую повторяемость fit на другой платформе. Native files рассматриваются
как собственные локальные artifacts, не как формат произвольного remote executable.

## 15. Файлы, commit point и отмена

Dataset/prediction JSONL и model bundle пишутся через owned temporaries в том же
parent directory, что destination. Parent должен существовать; output path
обязателен, resolved и не совпадает с training input/model bundle/source artifacts.
Для training input создаётся проверенная private copy до fit; все heads читают
одни и те же bytes. Concurrent изменение original input не смешивает datasets.

Ни один workflow не удаляет старый destination перед публикацией. Content
readback и fsync завершаются до единственного синхронного `os.replace`.
`PermissionError` на Windows/любая precommit ошибка сохраняет старый artifact.
После replace committed result сохраняет итоговый exit и summary, даже если
cleanup или повторный SIGINT происходят позже. Shared owned-task CLI interrupt
handling сохраняет уже исправленный Phase 6 contract; изменение не возвращает
default Runner second-SIGINT escalation.

File/prediction native I/O выполняется в owned worker; cancellation joins worker
до удаления temporaries. CPU fit/evaluation/save выполняются в отдельном owned
subprocess без database credentials/clients. Parent передаёт job file с private
paths и config; child пишет только в owned workspace. Нет shell-built commands.
Child environment — allowlist системных переменных для Python/OS runtime плюс
PYTHONUNBUFFERED; DATABASE_URL, CLICKHOUSE/REDIS passwords и прочие application
credentials не наследуются. Windows child запускается без отдельного видимого окна.
Parent на cancellation terminate → wait до 5 s → kill при необходимости → wait/reap,
потом удаляет owned files. Staging workspace не удаляется при живом процессе.
Cancelled training не продолжает незаметно сохранять модель в фоне.

Child stdout/stderr не пересылается как arbitrary traceback; контролируемый
bounded diagnostics/result протокол использует безопасные error codes. Parent
получает metrics/metadata после exit 0, проверяет их и строит/publishes bundle.
Cleanup failures после commit логируются без исходных секретных exception text.

Dataset builder rechecks каждую игру после read и все references после готового
file readback, непосредственно до публикации. Prediction CLI делает такие же
rechecks. Это pinned snapshot guarantee, не distributed transaction между stores
и filesystem. Training из уже проверенной private copy не обращается к latest.

## 16. CLI и operator workflow

Три entry points:

```text
uv run python -m scripts.build_training_dataset --season-from 2021-22 --season-to 2025-26 --validation-from 2024-07-01 --test-from 2025-07-01 --output training.jsonl
uv run --extra ml python -m scripts.train_models --dataset training.jsonl --output models/v1.nbamodel
uv run --extra ml python -m scripts.predict_models --model models/v1.nbamodel --season 2025-26 --game-id 0022500123 --output predictions.jsonl
```

`build_training_dataset`: selectors `--season` либо полный range
`--season-from/--season-to`, required `--validation-from`, `--test-from`, `--output`;
optional `--embargo-days` и три BaselineConfig flags Phase 6. Нет `--game-id`:
тест одного матча покрывается domain tests, тренировочный split требует разных игр.
Нет refresh/rebuild/network fallback. Существующие четыре analytical schemas
валидируются read-only; migrations не запускаются.

`train_models`: required `--dataset`, `--output`, overrides §11.
Нет DB selectors или DB connection. Не требует доступных stores/NBA.
`--output` не равен dataset path; model parent уже существует.

`predict_models`: required `--model`, `--output`, selector single season/range;
`--game-id` допускается только с одним season. Exports все выбранные states,
не предсказывает из last state вместо заданной истории. Нет training targets
или baseline config override. Existing source versions явно передаются readers.

Make targets повторяют CLI guards: `training-dataset`, `train-models`,
`predict-models`. Windows runbook даёт прямые uv/Python commands; Make опционален.
Optional ML Dockerfile устанавливает frozen `ml` extra и запускается nonroot,
mounts inputs read-only, writes artifacts в явно выбранный output directory.
Acceptance не запускает training helper с видимым окном или background job вне CLI.

| CLI | Exit 0 | Exit 1 |
|---|---|---|
| build_training_dataset | Published dataset пригоден для всех трёх heads | Published dataset с недостаточными partition/target counts |
| train_models | Published полный model bundle и metrics | Не используется как рейтинг точности |
| predict_models | Все rows available | Published полный export с invalid/terminal rows |

Все CLI: exit 2 — validation/source/schema/dependency/I/O/model failure до
публикации; exit 130 — interruption до публикации. Train с недостаточной dataset
возвращает 2 `insufficient_training_data`. Нормальные отсутствующие inputs/terminal
targets не имитируют успешное обучение. CLI summary даёт status, game/state/head
counts и artifact checksum; никакого database URL/password/сырого traceback.
Пустой game selector result — `no_eligible_games`, exit 2 без пустого artifact.

## 17. Контракты и модули для следующего плана

| Layer | Responsibility |
|---|---|
| domain/model_features.py | Frozen 96-column descriptor, pure projection, hash |
| domain/model_targets.py | HorizonEnd/TrainingGameInput/TrainingRow, exact targets |
| domain/model_training.py | Typed TrainingConfig, split/eligibility/weights/metrics |
| domain/model_predictions.py | Typed rates/reasons/clipping/semantic identities |
| application/modeling/ | Dataset/training/prediction requests, results, ports, services |
| infrastructure/postgres/model_source.py | Read-only published feature/target resolution |
| infrastructure/files/training_dataset.py | Streaming canonical dataset writer/reader |
| infrastructure/files/model_bundle.py | Verified bundle publication/loading |
| infrastructure/files/model_predictions.py | Atomic prediction JSONL |
| infrastructure/ml/catboost_adapter.py | Typed lazy native adapter, matrix/load/predict |
| infrastructure/ml/training_worker.py | Owned child job executor, fit/evaluate/save |
| scripts/build_training_dataset.py | Source composition and dataset CLI |
| scripts/train_models.py | Parent process supervision and training CLI |
| scripts/predict_models.py | Bundle/source composition and prediction CLI |

Разделение по задачам не требует добавлять generic framework или менять source
ports прежних фаз. Shared CLI runner/atomic-file primitives могут быть вынесены
из Phase 6 только при необходимости reuse, с прежними regression tests и неизменными
внешними contracts. Domain features/baselines не импортируют modeling обратно.

## 18. Проверки и критерии приёмки

Unit checks:

- Exact fractional labels/rounding/zero vs missing; regulation/current OT endpoints;
  terminal R=0; 2OT future counts не попадают в target первого OT.
- Current feature projection не меняется при mutation suffix/final box/labels;
  literal 96 names/divisors/golden hash; training/prediction используют один mapping.
- Invalid current state исключается, поздний valid state после explicit fixture
  reset работает; future invalid lineup не портит target valid prefix.
- Structural mismatches, endpoint/counter/clock/ID/version errors не маскируются.
- UTC split, равные started_at, whole-game partition, оба embargo windows;
  недостаточные и constant train targets не создают bundle.
- Per-head weights по game; missing inputs не становятся нулём; test не передаётся
  fit/eval_set; изменение только test targets не меняет выбранные models/predictions.
- Baseline matched cohorts/coverage, exact error sums, weighted metric rounding,
  empty OT/no-comparable rows, negative clipping и rejection nonfinite/overflow.
- Dataset/bundle canonical readback/hash/entries/version tampering; input copy;
  artifact identities не содержат output path/launch timestamp.
- Реальный tiny CPU fit, повторный fit на same runtime, save/load round-trip.
  Тесты не заменяют реальный adapter только mock predictor.
- SIGINT до commit, два SIGINT после commit, PermissionError replace, failed head,
  worker termination/reaping и отсутствие background writes после cleanup.
- CLI guards, missing ml extra, error secrecy и Make/runbook consistency.

Integration/E2E: fresh isolated Compose project/volumes/loopback ports; synthetic
games с достаточными train games/разными targets, validation, test и embargo,
regulation и два OT. Fixtures проходят реальную Phase 2→3→4→5 pipeline, затем
dataset CLI → CPU trainer → bundle loader → prediction CLI. Синтетический reset
явно versioned отдельно: production NBA normalizer V1 не обещает lineup recovery.

Перед/после snapshots всех relevant PG tables и четырёх CH row counts доказывают
read-only modeling sources/CLI. Fixtures journal owned primary keys/snapshot IDs,
не overwrites чужие fixtures, и reap subprocesses до удаления данных. Source race
на final recheck сохраняет старый output; trainer работает с pinned dataset при
недоступных stores. Повторный build одинакового dataset source на другом path
побайтово совпадает; загруженный bundle выдаёт те же encoded predictions.

Acceptance gates: frozen dependencies Windows + ML image/Linux import/native fit;
nonroot ML image; unit/integration/E2E; полный `pytest -m 'not live'`, Ruff check
и format, strict mypy, git whitespace check. Expected live/destructive skips
объясняются в отдельном checkpoint. NBA live canary не требуется этой фазой.
Изолированные owned Docker resources удаляются после финальной проверки;
нельзя удалять обычные user volumes ради тестов.

Модельная точность измеряется на реальных выбранных данных отдельно от synthetic
acceptance. Отсутствие исторических данных не заполняется скачиванием по умолчанию,
придуманными labels или заявлением о выигранном baseline comparison.

## 19. Проверенные внешние контракты

По состоянию на 2026-10-13:

- CatBoost 1.2.10 имеет CPython 3.14 wheels для Windows x86-64 и Linux x86-64:
  [release files](https://pypi.org/project/catboost/1.2.10/#files).
- `has_time`, `nan_mode`, `use_best_model`, seed и RMSE описаны в
  [common parameters](https://catboost.ai/docs/en/references/training-parameters/common).
  Validation используется для выбора лучшей iteration; chronological partition
  всё равно реализуется собственным domain splitter, а не одним `has_time` flag.
- Numeric missing inputs поддерживаются:
  [missing values](https://catboost.ai/docs/en/concepts/algorithm-missing-values-processing).
- CPU fit/eval_set/early stopping:
  [Regressor.fit](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor_fit).
- Native `.cbm` load и required feature mapping при predict:
  [load_model](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor_load_model),
  [predict](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor_predict).
- `allow_writing_files=False` отключает побочные analytical/snapshot files:
  [output parameters](https://catboost.ai/docs/en/references/training-parameters/output).

Наличие wheel не заменяет проверку import/native fit frozen extra на обеих средах.
Новые dependencies пока не установлены; product code не менялся при проектировании.

## 20. Передача в планирование

Следующий шаг после письменного approval — отдельный implementation plan с
interfaces, RED→GREEN cases, ownership, commit boundaries и Review Focus.
Он должен особенно проверить label endpoint vs current-state causality, test
isolation, head masks/cohort weights, bundled source/model identities, worker
lifecycle и publication после повторного interrupt. В реализации не допускаются
скрытая замена target horizon или пропуск реального CPU adapter acceptance.

Spec self-review проверяет отсутствие placeholders, точность 96-column schema,
согласованность формул/units, split/embargo boundaries, CLI exits и atomic bundle
contract. После Phase 7 acceptance работа останавливается перед отдельным Phase 8 design.

Self-review completed 2026-10-13: 96-column count проверен по текущим contracts;
источники/clock semantics/PeriodEnd normalizer проверены; label masks отделены
от embargo eligibility; точный RMSE rounding не зависит от floating sqrt;
same-runtime round-trip требует exact encoded equality; native float diagnostics
не нарушают canonical JSON; shared publication/interrupt contract согласован с
Phase 6. Незавершённых требований и шаблонных заглушек нет. Product implementation не начат.
