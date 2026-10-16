# Phase 8 — offline Monte Carlo acceptance

Дата проверок: **2026-10-16**, Europe/Moscow. Реализация начата 2026-10-15.
Статус: **Phase 8 принята** по implementation/platform/full-suite/review gates.
Docker cleanup выполнен; собственный scratch сохранён из-за policy block удаления.

Основание: [одобренная спецификация](../specs/2026-10-14-phase-8-monte-carlo-design.md)
и [план](../plans/2026-10-15-phase-8-monte-carlo.md).
Implementation base: `5b2884e0276e9c782788fdf3541683c0c7129d3d`.
Реализация выполнена в согласованном текущем checkout/main.

## Реализовано

Pure config/query/frame/PMF/summary/hash contracts, NumPy PCG64 simulation до конца
матча с будущими OT, owned CPU executor, strict canonical JSONL reader и атомарный
streaming exporter, read-only service, CLI `scripts.simulate_totals`, Make target и
[операторская инструкция](../../../README.md#phase-8-offline-monte-carlo-totals).
Phase 7 targets, 96-column projection, обучение и model bundle format сохранены.

Численная модель: integrated exponential durations → combined Poisson possessions,
чередование сторон с fair first-side draw, integer-score PMFs с exact PPP mean,
фиксированные causal rates для всех будущих горизонтов. Sampling идёт по batches;
память не содержит N×possessions или все state outcomes. Симулятор хранит три
bounded marginal histogram, которые не восстанавливают joint score distribution.

Версии: `possession-simulation-v1`, `tilted-integer-score-pmf-v1`,
`numpy-poisson-multinomial-v1`, `state-pcg64-seed-v1`,
`exact-simulation-summary-v1`, `simulation-results-jsonl-v1`.

## Проверки и команды

Команды запускаются из корня проекта. `uv` 0.11.18 установлен только в owned
scratch; глобальные Python/PATH не изменялись. Существующая frozen `.venv`
проверена через `uv sync --frozen --extra ml` (64 packages).
Во всех тестовых shell явно заданы четыре TEST_* адреса отдельного проекта ниже.
Полный redirected log прочитан целиком; необъяснённых failures/errors нет.
21 expected skip: 3 opt-in live canaries без TEST_NBA_* / isolated-live opt-in,
15 destructive ClickHouse migration cases без TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1,
3 Windows symlink-creation cases без требуемых privileges. Реальные input symlink
initial/late guards отдельно выполнены в Linux. Никаких live NBA запросов не было.
Focused test counts — подмножества suite, они не прибавляются к2722.

| Проверка | Наблюдаемый результат |
|---|---|
| `uv run --extra ml pytest -m 'not live' -q -ra` | 2722 passed, 21 skipped in 1073.75s (0:17:53) |
| `uv run ruff check .` / `uv run ruff format --check .` | Прошли до финальной правки документации |
| `uv run mypy` | 327 source files, без ошибок |
| `git diff --check` | Прошёл |
| Real-source/native integration + E2E | 5 passed, 172.47 s |
| Реальные SIGINT: before/fake CPU/native NumPy CPU/after replace | 4 passed, 11.26 s |
| Linux GNU Make 4.3 | 7 invalid guards + 2 valid command expansions прошли |
| Fresh API image | UID999, CatBoost отсутствует, routes `/health`, `/health/ready` сохранены |

NumPy в API image присутствует транзитивно через существующие NBA dependencies;
отсутствие NumPy не является условием этой фазы. ML image строится из существующего
`docker/ml.Dockerfile`: `nba-bet-phase8-ml:20261016-6c203d3`, image ID
`sha256:c9698d4d8ba2424d80a16023c9108c19d21ed59f84310299205bc4e51c098f5d`.
Production files image совпадают с текущей реализацией; последующие commits добавляют
тесты и acceptance docs. `-m scripts.simulate_totals --help` выполнен в image.

Численные тесты отдельно проверяют exact PMF mass/mean и endpoints, largest-remainder
repair, modal-last residual, compound-Poisson moments на N=100000, deterministic
PCG64 draw order, multi-OT/caps, all-unresolved, R0/tie, nearest-rank quantiles,
Fraction/isqrt summaries и independently rounded line probabilities.

Adversarial проверки охватывают unknown/duplicate keys, float/bool/Int64 overflow,
неподдерживаемые версии, подмену derived mean/line counts/PMF/hash/seed,
CRLF/noncanonical/truncated/oversized JSONL, missing/duplicate states и model/source
identity. Второй temp allocation и ошибки readback/replace сохраняют старый output.
Реальный NumPy SIGINT probe выполняет native horizon до Event barrier; два SIGINT
проверяют stop flag, join и exit130. Postcommit probe сохраняет summary/exit1.

## Windows и Linux на одном pinned bundle

Один bundle из owned полного offline цикла, SHA:
`c1b09871d95dec24ea592b10e5ac2fc09e7c75681db022f6c96aebd7855c246a`.
Linux использует этот же проверенный Windows bundle через read-only mount.

| Runtime | Python | NumPy | CatBoost | OS / architecture | UID |
|---|---|---|---|---|---|
| Windows | 3.14.8 | 2.5.2 | 1.2.10 | Windows / AMD64 | Host user |
| Linux ML image | 3.14.8 | 2.5.2 | 1.2.10 | Linux / x86_64 | 999 |

В обоих случаях N128/batch64, seed42, default prior/caps, линии210/210.5.
Проверены same-runtime byte identity, reader readback, exact selected-state equality,
неизменность populations при другой query и model/config input alias guards.
В Linux реальные symlink initial/late publication aliases отклонены, input SHA сохранён.
Draws и bytes между Windows/Linux не сравниваются: runtime входит в seed identity.

| Artifact | Games / states | Complete | Incomplete | Unavailable | Unresolved |
|---|---|---|---|---|---|
| Windows | 5 / 49 | 40 | 0 | 9 | 0 |
| Linux | 5 / 49 | 37 | 3 | 9 | 3 |

Windows artifact SHA: `6e6b838e1733506dbbebb19786e7af9a6f0fc6f2fe40265bedc2d953dc7bf916`.
Windows semantic revision: `f6ab50cb24ff2113207d0d3a1a5f5c9026b7df32f27d8423a95daaa787b3dce4`.
Linux artifact SHA: `9573039253bac80148ffc1ba20e5e34a71b4837b3d1fdb2d9064681953d6e701`.
Linux semantic revision: `c1e02b1abdde4064cf5704edcb45537eb6ad815ef31835fd6f397e8714e46ac4`.
Linux unresolved reasons: `overtime_limit=3`; эта масса явно остаётся unknown.

## Реальный pipeline и отсутствие записей

С нуля выполнены Phase2 ingestion → Phase3 possessions → Phase4 lineups → Phase5
published features → Phase7 dataset/CPU training → Phase8 simulation CLI.
Использованы только local NBA-shaped fixtures: season2095-96, games0029595101..5,
team/player namespace91395xxx. Training split: validation2095-11-01,
test2095-12-01, embargo1. Проверены regulation, два известных OT, cold history,
terminal states и invalid→recovered lineup с отдельной fixture version
`fixture-simulation-reset-v1`; production NBA normalizer не менялся.

До/после simulation-only CLI сравниваются **все PostgreSQL metadata rows** и counts
четырёх ClickHouse tables: possessions, lineups, state features, player pregame.
Windows и Linux projections равны. Fixture fetch counter не меняется; read-only source
composition не создаёт NBA HTTP clients, claims, rebuilds или writes.
Первый OT current frame/population на pinned bundle сохраняется при изменении
будущего terminal score. Simulation source `read_training` запрещён отдельной проверкой.
Реальная source checksum mutation после checked prepare отклоняет publication и
сохраняет предыдущие bytes. OwnershipJournal отвергает preexisting team ID;
пустой чужой journal не удаляет созданные другим владельцем строки.

## Границы результата

N1..1000000, batch1..65536 и ≤N; max future OT1..20, possession cap1..1000000;
prior support K2..32; Pmax1000, PPP0..K. JSONL record/manifest ≤16MiB,
≤100000 bins на marginal; все persisted numeric values signed Int64, кроме UInt64
base seed. Текущий total/observed final score ≤floor(Int64max/1000000).

Resource-limit paths учитываются как unresolved, без clamp/resample. Неполные строки
содержат completed-only histograms и null full-population summaries/probabilities.
Unavailable state сохраняет строку с нулём выполненных trials и null histograms.
Exit0 означает полное выполнение, exit1 — опубликованный partial artifact,
exit2/130 до commit сохраняют старый output. Freeze rates/prior duration model
пока не откалиброваны; Phase9 historical replay/calibration ещё не выполнялась.

## Rulings I made

1. Task8: shared `run_owned_io` использует public `asyncio.wait` и явное извлечение
   результата/исключения вместо `shield`. Python3.14.8 shield callback логировал
   worker error после cancellation даже при owned retrieval; это воспроизведено
   RED→GREEN. Цена ошибочного решения: нарушение shared Phase7/file-I/O cancellation
   или error propagation. Проверены repeated cancellation, retrieval и регрессии.
2. Task12: существующий `run_source_work` получил optional keyword-only `settings`;
   simulation передаёт Settings с уже выбранным N. Иначе повторный Settings() читал
   invalid env и ломал approved explicit/config precedence. Остальные callers
   сохраняют прежний default, environment глобально не меняется. Цена ошибки:
   regression initialization/cleanup общих source clients; покрыто source composition
   и explicit-invalid-env RED→GREEN тестами.
3. Final / calibration: approved fixed-rate/exponential/prior policy остаётся
   вычислительным предположением, empirical calibration и learned priors относятся
   к Phase9. Пользователь получает versioned distributions без обещания точности.
   Цена ошибки: неверная интерпретация вероятностей до эмпирической проверки.
4. Final / live integration: live API/cache/polling/replay/bookmaker feeds/settlement
   исключены согласованной областью. Результат доступен через offline CLI/JSONL.
   Цена ошибки: live consumers потребуют последующей интеграции.
5. Final / joint scores: V1 публикует marginals, не winner/spread probabilities или
   восстановленное joint distribution. Цена ошибки: потребитель может ошибочно
   вывести неподдерживаемые вероятности из marginal histograms.
6. Final / reproducibility: гарантия относится к recorded runtime, не к совпадению
   draws между OS/versions. Цена ошибки: cross-runtime byte comparisons могут
   ошибочно трактоваться как regression; runtime identity включён в seed/artifact.
7. Final / as-of: sources используют currently published canonical snapshots,
   historical as-of reconstruction отсутствует. Цена ошибки: использование этих
   snapshots как исторически доступных данных может исказить future replay.
8. Final / authenticity: checksums/checked readback проверяют consistency/integrity
   в local ownership model, signed-artifact authentication не добавляется.
   Цена ошибки: actor с возможностью переписать artifact и все hashes может
   подделать согласованный файл; это не обещанный authentication contract.

9. Final / teardown order: closing ledger и evidence check выполняются перед
   удалением собственного scratch, поскольку completion script/log живут в нём.
   Фактический cleanup outcome затем отдельно фиксируется в Git-report.
   Цена ошибки: преждевременная ledger отметка могла бы скрыть ошибку cleanup;
   проверка реального удаления и итоговый report остаются обязательными.

## Final independent review

Один свежий reviewer **gpt-6-astra/high**, без inherited history, проверил весь
диапазон `5b2884e..3011424`, approved spec/plan, production code, focused tests,
ledger и этот draft report. Каждый из пяти Review Focus рассмотрен отдельно.
Review выполнялся read-only параллельно comprehensive gate; reviewer явно получил
статус full-suite pending и не запускал дополнительные integration/store operations.

**Critical: 0; Important: 0; Minor: 0.** Correction pass не требуется;
deferred minors отсутствуют. Reviewer сообщил независимый численный sweep560 cases
для K2/3/8/16/32, разных positive priors, endpoints/near-endpoints/interior targets:
exact mass/mean и native probability checks прошли, как и `git diff --check`.
HEAD/index/files им не изменялись. Это дополнительное review evidence, не замена
обязательного полного suite executor-а.

Вердикт: **Ready to merge — Yes from code review, subject to the pending
comprehensive acceptance gate.** Условие выполнено: полный suite прошёл2722/2722 non-skipped checks. Подтверждены numeric accounting, incomplete mass,
causal identities, temp/publication ownership, cancellation и compatibility обоих
documented rulings. Implementer подтвердил grading по эффекту на пользователя.

Все шесть пунктов `Declined to judge` (calibration/realism, live integrations,
joint/winner/spread outputs, cross-runtime identical draws, historical as-of,
signed authenticity) рассмотрены implementer-ом и архивированы как решения3–8 выше
с обоснованием и ценой ошибки. Повторное ревью не назначалось. После review/full suite production/test files
не менялись; последующие правки относятся только к acceptance/status docs.
Closing evidence check сверяет итог suite, hashes, все rulings и отсутствие code
изменений относительно tested/reviewed HEAD3011424; полный suite не повторяется
без изменения кода или новых failures.

## Owned infrastructure и cleanup

Compose project: `nba-phase8-3168b126`, label `nba.phase8.owner=3168b126`.
Loopback ports: PostgreSQL63341, ClickHouse63342, Redis63343, API63344.
API built из текущего `docker/api.Dockerfile`; production-compatible PG/CH migrations
применены только к этим fresh stores. Named volumes:
`nba-phase8-3168b126_postgres_data`, `nba-phase8-3168b126_clickhouse_data`,
`nba-phase8-3168b126_redis_data`.

Docker cleanup выполнен 2026-10-16 после archive commit3eff26e: сверены owner labels
четырёх containers и точные имена трёх volumes; удалены4 containers,3 volumes и
project network. Повторные queries по owner/project labels вернули0 containers,
0 volumes,0 networks. `buildx_buildkit_default` и чужие resources не затрагивались.

Closing evidence check прошёл: suite summary, hashes,9 rulings и отсутствие изменений
production/tests относительно reviewed/tested HEAD3011424 подтверждены; Task14 ledger
записан перед teardown согласно решению9.

**Сохранён собственный scratch:**
`C:/Users/khrom/Desktop/Other/Own/NBA_BET/.superpowers/sdd/2026-10-15-phase-8-monte-carlo`.
Рекурсивная команда `Remove-Item -LiteralPath <этот точный путь> -Recurse -Force`
с проверками containment/ledger/reparse была отклонена **до запуска** automatic
command review: `rejected: blocked by policy`. Отдельная предварительная проверка
absolute target/parents была успешной; завершающий rejected script не выполнялся.
Поэтому scratch не удалён; альтернативного инструмента для обхода запрета не было.
Это ограничение cleanup, а не оставшаяся implementation/test задача.
Retained Phase6/7 scratch исключён. Временные Linux probe containers работают с `--rm`.
Проверенные локальные image tags остаются для воспроизводимости.
