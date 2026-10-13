# Phase 6 — Deterministic Offline Baselines

Дата: 2026-10-12

Статус: approved after written-spec review (2026-10-13); implementation not started

## 1. Цель и согласованный объём

Создать воспроизводимые baseline-оценки темпа и очков на владение (PPP) для каждого
состояния матча из опубликованного датасета Phase 5. Эти оценки станут ориентиром
для CatBoost в Phase 7 и входами для будущей симуляции в Phase 8.

Согласованный пользователем вариант: pure-калькулятор, offline CLI и JSONL-экспорт.
Baseline рассчитывается в трёх вариантах: historical, live, shrinkage.

Успех фазы: CLI читает проверенные снимки признаков, детерминированно рассчитывает
все три варианта, экспортирует идентичный результат при одинаковых входах и явно
объясняет отсутствие любой оценки. Обычный запуск не обращается к NBA.com.

В Phase 6 не входят итоговый счёт, вероятность пробития линии, labels, обучение,
подбор гиперпараметров, backtest, API, Redis, live worker и новые таблицы baseline.
Сохранённые фазы 3–5, их схемы, hashes и правила публикации не меняются.

## 2. Выбранная архитектура

Рассмотрены три варианта:

1. Только библиотека расчёта: минимальный объём, но отсутствует операторский
   workflow для применения к сохранённым сезонам.
2. Библиотека, CLI и JSONL: выбранный вариант. Доступен полноценный offline запуск,
   а файлы с точными source identities можно воспроизводить и сравнивать.
3. Новые PostgreSQL/ClickHouse baseline snapshots: удобны для последующих readers,
   но требуют нового publication protocol до появления потребителя.

Компоненты:

```text
src/nba_live_totals/
    domain/baselines.py
    domain/baseline_hashing.py
    application/baselines/
        models.py
        ports.py
        service.py
    infrastructure/postgres/baseline_source.py
    infrastructure/files/baseline_export.py
scripts/build_baselines.py
```

Domain зависит от provider-neutral `GameStateFeature`, `FeatureDataset` и чистых
числовых функций. Он не импортирует settings, SQLAlchemy, HTTP-клиенты или filesystem.
Application управляет выборкой, проверкой источников, расчётом и экспортом.
Адаптер источников переиспользует публичные readers Phase 5, не вызывает claims,
`start_run`, `finish_run` и не создаёт записи в feature control plane.
JSONL adapter отвечает только за каноническую сериализацию и атомарную замену файла.

## 3. Числовой контракт

Все оценки — signed fixed-point Int64 с `BASELINE_SCALE = FEATURE_SCALE = 1_000_000`.
PPP выражается в points / offensive possession. Pace — владения одной команды за
48 минут, как в Phase 5. JSON не содержит float, NaN, infinity или числовых строк.

Промежуточные выражения используют точные integer/rational операции. Округление
round-half-away-from-zero выполняется один раз при получении каждой конечной оценки.
Расчёт использует уже округлённые признаки Phase 5, без повторного чтения box scores.
Недопустимый тип, отрицательный счётчик, отрицательная оценка или выход за Int64
означает structural failure. Настоящее нулевое PPP допустимо; нулевой знаменатель
или отсутствие наблюдений дают `NULL`, а не искусственный ноль.

Фиксированные identities:

```text
baseline_formula_version = baseline-formulas-v1
baseline_output_schema_version = baseline-jsonl-v1
baseline_scale = 1000000
```

## 4. Настройки и их границы

Один общий historical window `W` выбирается из `5`, `10`, `20`; default `20`.
Все prior inputs берутся из соответствующих полей Phase 5.
Неполное окно используется как есть; `games_used_W` сохраняется в результате.

Shrinkage prior strengths задаются положительными целыми:

```text
ppp_prior_possessions = 20
pace_prior_combined_possessions = 40
```

Эти значения — начальная конфигурация для воспроизводимой проверки, а не результат
калибровки. Параметры входят в config hash. Их автоматический подбор относится к
последующему temporal evaluation, не к Phase 6.

## 5. Historical baseline

Обозначения ниже описывают физические величины до fixed-point encoding:

```text
home_ppp_prior = (home.off_rating_W + away.def_rating_W) / 200
away_ppp_prior = (away.off_rating_W + home.def_rating_W) / 200
pace_prior = (home.pace_W + away.pace_W) / 2
```

Рейтинг Phase 5 задан на 100 владений; деление на 200 одновременно переводит
его в PPP и усредняет атаку команды с защитой соперника.

Каждый prior требует оба своих входа. Отсутствие одного из них означает unavailable
для соответствующей метрики. Нет league constants, угадывания missing values,
односторонней подстановки, home advantage multiplier или lineup adjustment.

Для state 0 и каждого after-possession state historical baseline использует один
и тот же pregame prior. Historical inputs не меняются от событий target game.

## 6. Live baseline

Читаются только уже рассчитанные prefix features:

```text
pace_live = state.live.current_pace
home_ppp_live = state.live.home_ppp
away_ppp_live = state.live.away_ppp
```

Счётчики наблюдений:

```text
home_n = state.home_possessions
away_n = state.away_possessions
combined_n = home_n + away_n
```

При нуле соответствующего счётчика live estimate отсутствует, даже если повреждённый
вход содержит числовой rate. Для state 0 все счётчики должны быть нулевыми и live
baseline unavailable. Для положительного счётчика missing rate остаётся unavailable.

Не используются rolling live windows, target final box scores, будущие состояния,
итоговое количество владений или ещё не начавшиеся overtime. Единицы current pace
и правила overtime полностью наследуются от Phase 5.

## 7. Shrinkage baseline

Для каждой метрики:

```text
weight_live = n / (n + k)
estimate = (n * live_estimate + k * historical_prior) / (n + k)
```

Для home/away PPP `n` — offensive possessions соответствующей стороны,
`k = ppp_prior_possessions`. Для pace `n = combined_n`,
`k = pace_prior_combined_possessions`. Вес сериализуется fixed-point, но оценка
рассчитывается по точной дроби, без использования округлённого веса.

Если `n = 0` и prior доступен, оценка равна prior независимо от отсутствия live rate.
Если prior отсутствует, смешанная оценка unavailable даже при доступном live rate;
самостоятельный live baseline при этом остаётся доступен. Если `n > 0`, prior есть,
но live rate отсутствует, смешанная оценка unavailable. Никаких неявных fallback.

## 8. Качество и неполные данные

Каждый state создаёт ровно три outcome rows, по одному на метод. Каждая метрика
содержит значение либо `NULL`, количество наблюдений и bounded reason code.
Причина доступной метрики равна `NULL`.

Безопасные причины отсутствия:

```text
missing_historical_inputs
no_live_observations
missing_live_inputs
invalid_feature_state
```

Статус строки: `available`, если доступны все три метрики; `partial`, если доступна
хотя бы одна; `unavailable`, если не доступна ни одна.

State с invalid home/away lineup сохраняется как три unavailable rows с
`invalid_feature_state`. Игроки и lineup не угадываются. Поздний восстановленный
valid state обрабатывается нормально, даже если snapshot-wide статус Phase 5
остаётся invalid. Source snapshot status и issue codes сохраняются в manifest.
Отсутствие всей истории не считается operational failure и не запрещает live rows.

Несовпадающие game identities, отсутствующие обязательные nested feature objects,
повторяющиеся или непоследовательные states, несовместимые versions, source checksum
mismatch и повреждённый экспорт — structural/operational failures, не missing data.
Они запрещают экспорт всего выбранного диапазона.

## 9. Источники, freshness и порядок

CLI выбирает только completed Regular Season / Playoffs games по тем же selectors,
что offline dataset. Входы выбираются через PostgreSQL published feature pointers,
с проверкой всех source dependencies и конкретных configured feature versions.
Не используется `latest`, максимальная generation или UUID ordering в ClickHouse.

Для каждой игры адаптер:

1. Разрешает target и exact target player identities публичными readers Phase 5.
2. Вызывает существующий read-only `resolve_current`.
3. Для CURRENT или INVALID читает exact feature snapshot, включая проверку обеих
   таблиц, row counts и всех output checksums.
4. Рассчитывает состояния по порядку, читая только текущую строку и pregame inputs.
5. Повторно разрешает источник и сравнивает полный reference с первым.

До финального экспорта adapter ещё раз проверяет все selected references. Изменение
dependency или pointer, отсутствие либо stale snapshot запрещает экспорт. Нет
автоматической пересборки Phase 5, NBA network fallback и скрытого retry на новый source.

Одновременно в памяти находится не больше одного полного feature dataset; outcome
rows потоково пишутся на диск. В памяти допустим bounded-per-game buffer и список
source references для выбранного диапазона, но не все outcomes сезона.

Игры идут по `(started_at, game_id)`, states по `state_number`, методы в фиксированном
порядке `historical`, `live`, `shrinkage`. Для одной игры результат детерминирован и
не зависит от того, какие другие игры были выбраны в том же запуске.

Экспорт фиксирует конкретные immutable source identities. Он не объявляется новым
авторитетным current pointer: источники могут измениться после последней проверки.
Это pinned artifact с проверенными на момент чтения источниками, а не транзакция
между PostgreSQL, ClickHouse и filesystem.

## 10. JSONL artifact

Один UTF-8 файл с LF содержит сначала manifest line, затем outcome lines. Каждая
JSON line канонична: keys sorted, компактные separators, `ensure_ascii=False`,
никаких timestamps запуска или случайных run IDs. Повторный запуск по тем же
selectors, exact source snapshots и config даёт byte-identical artifact.

Manifest содержит:

- record type `manifest`, schema/formula versions, scale;
- normalized selectors и все baseline parameters;
- config SHA-256;
- ordered source game IDs, feature snapshot UUIDs, feature revision/input/output
  hashes, feature version strings, source status и issue codes;
- outcome row count и SHA-256 конкатенации outcome bytes, включая LF каждой строки.

Outcome содержит:

- record type `baseline`, game ID, state number, boundary sequence/side, method;
- source snapshot UUID, baseline revision SHA-256, state scores/period/time;
- status, home/away history sample counts, home/away/combined possession counts;
- `pace`, `home_ppp`, `away_ppp` и отдельные unavailable reason codes;
- live weight для каждой доступной метрики в shrinkage rows (ноль при `n=0`);
  для недоступных метрик и других методов `NULL`.

Semantic baseline revision hash включает source feature revision и output checksum,
state identity, formula/schema versions и config hash. Operational snapshot UUID
сохраняется отдельно и не входит в semantic revision. Full artifact checksum зависит
от exact source snapshot UUIDs в manifest и outcome rows.

Outcome bytes сначала потоково записываются во временный body file рядом с destination;
row count и checksum накапливаются при записи. Затем второй временный file получает
manifest line и body, скопированный фиксированными chunks. Оба temporary files находятся
в том же filesystem, что destination. Flush/fsync выполняются до readback-проверки
row count и checksum. После последней source recheck готовый file атомарно заменяет
destination через `os.replace`. При успехе, исключении или Ctrl+C оставшиеся temporary
files удаляются; при failure существующий destination не изменяется. Пустая выборка — operational failure с
`no_eligible_games`; не создаётся пустой успешный artifact.

## 11. CLI и операторский workflow

```text
uv run python -m scripts.build_baselines --season 2025-26 --output baselines.jsonl
uv run python -m scripts.build_baselines --season 2025-26 --game-id 0022500123 --output game.jsonl
uv run python -m scripts.build_baselines --season-from 2021-22 --season-to 2025-26 --output baselines.jsonl
uv run python -m scripts.build_baselines --season 2025-26 --history-window 10 --ppp-prior-possessions 20 --pace-prior-combined-possessions 40 --output baselines.jsonl
```

`--output` обязателен. Single game требует matching single season; season и range
взаимоисключающие. Нет `--refresh`, `--rebuild` и автоматических migrations.
Итоговый путь разрешается относительно текущей рабочей директории. Повторный запуск
атомарно заменяет явно выбранный output file.

Exit codes:

- `0`: artifact полностью экспортирован; все outcome rows available.
- `1`: artifact полностью экспортирован, но содержит partial/unavailable rows.
- `2`: arguments/settings, source, schema или I/O error; новый artifact не опубликован.
- `130`: interrupted; новый artifact не опубликован.

Structured summary сообщает status, games, states, available/partial/unavailable row
counts и artifact SHA-256. Логи не содержат raw events, player arrays, SQL, DSNs,
credentials или exception messages. Error codes и error types bounded/safe.
Make target `build-baselines` повторяет selectors и требует OUTPUT. README описывает
полный offline порядок `backfill -> build_possessions -> build_lineups -> build_dataset
-> build_baselines`, замену файла, fixed-point units и границу будущих фаз.

## 12. Verification и критерии приёмки

Domain tests проверяют exact rounding, rate-to-PPP conversion, симметрию при смене
home/away, partial windows, ноль, cold start, разные offensive counters, shrinkage при
`n=0` и `n=k`, invalid-to-valid recovery, overflow и отсутствие будущих inputs.
Изменение позднего состояния не изменяет численные оценки, weights, statuses и
unavailable reasons более ранних состояний. Source-wide hashes и revisions при этом
могут измениться: они описывают новую версию всего immutable feature snapshot.

Application/adapter tests проверяют exact source addressing, checksum mismatch,
stale/missing references, source races между reads и final recheck, отсутствие claims
и DB writes, selectors/order, безопасные errors и отмену.

Export tests проверяют golden canonical bytes, source/config/revision hashes,
byte-identical rerun, row count `3 * states`, atomic replacement и сохранение старого
файла при failure/Ctrl+C. Тестируются отсутствие временных файлов после сбоя и partial
exit status при успешно сохранённом artifact.

E2E на isolated PostgreSQL/ClickHouse проходит Phase 2–5 fixture pipeline и вызывает
реальный baseline CLI, включая cold history, invalid interval/recovery, повторный
экспорт и изменение previous dependency. NBA.com не вызывается.

Финальный gate: unit/API, integration/E2E, Ruff, strict mypy и `git diff --check`.
Численные baseline parameters не объявляются оптимальными без Phase 9 evaluation.
После приёмки Phase 6 работа останавливается перед отдельным дизайном Phase 7.

## 13. Совместимость с восстановленным проектом

Runtime — Python 3.14, текущий `uv.lock`, существующие PostgreSQL 17 и ClickHouse 26.3.
Новые product dependencies не нужны. Существующие Phase 1–5 планы остаются историческими
документами; implementation опирается на текущий код и настройки.

В domain `FeatureVersions()` join version default равен `feature-join-v1`, а settings,
Compose и E2E используют `possession-lineup-inclusive-v1`. Phase 6 всегда передаёт полный
`FeatureVersions` из settings в source resolver; она не меняет этот существующий
контракт и не использует domain defaults для чтения published snapshots.

Design scope и written spec одобрены пользователем в текущем чате. Implementation plan:
[Phase 6 baseline plan](../plans/2026-10-13-phase-6-baselines.md).
Product implementation ещё не начата; план передаётся на review и выбор метода исполнения.
