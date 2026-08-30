# NBA Live Totals Prediction Engine — Phase 1 Infrastructure Design

## 1. Назначение документа

Этот документ фиксирует общую архитектурную форму NBA Live Totals Prediction
Engine и подробный дизайн Phase 1. Phase 1 создаёт воспроизводимый
production-oriented фундамент: FastAPI, конфигурацию, lifecycle внешних
ресурсов, PostgreSQL, ClickHouse, Redis, Alembic, structured logging, Docker
Compose и тестовый toolchain.

Phase 1 не создаёт фиктивный prediction endpoint и не включает ingestion,
парсинг владений, ML, Monte Carlo или live worker. Эти возможности добавляются
отдельными фазами после прохождения собственных design, implementation и
verification gates.

## 2. Архитектурное направление всей системы

Система реализуется как модульный монолит в одном Python-репозитории. API,
historical ingestion, live worker, trainer и backtest runner являются разными
entry points, но используют общие domain-типы, application services и
инфраструктурные адаптеры.

Основные правила зависимостей:

```text
entry points (API / CLI / workers)
                ↓
application services and ports
                ↓
domain models and policies

infrastructure adapters implement application/domain ports
and are wired only at the composition root
```

Domain, feature engineering, simulation и prediction logic не импортируют
FastAPI, NBA.com client, SQLAlchemy, Redis или ClickHouse clients. Конкретный
NBA data provider, model implementation и artifact storage заменяются через
явные `Protocol`-контракты.

Модульный монолит выбран вместо ранних микросервисов, потому что он сохраняет
чёткие границы модулей без расходов на сетевые контракты, distributed tracing,
deployment coordination и eventual consistency. Независимые entry points
оставляют возможность выделить worker или trainer в сервис позднее.

## 3. Целевая структура репозитория

Структура развивается по фазам; пустые пакеты заранее не создаются.

```text
src/nba_live_totals/
    main.py
    settings.py
    lifecycle.py

    api/
        app.py
        dependencies.py
        routes/
            health.py
            games.py                 # Phase 10
            predictions.py           # Phase 10

    application/
        health.py
        ports/                        # добавляется вместе с первым consumer
        game_state.py                 # Phase 3+
        prediction.py                 # Phase 8+

    domain/
        models/                       # Phase 2+
        services/                     # Phase 3+

    ingestion/
        nba/                          # Phase 2
        live/                         # Phase 11

    repositories/
        postgres/                     # Phase 2+
        clickhouse/                   # Phase 3+
        redis/                        # Phase 10+

    features/                         # Phase 5
    ml/                               # Phase 6+
    simulation/                       # Phase 8
    backtesting/                      # Phase 9
    workers/                          # Phase 11

    infrastructure/
        postgres.py
        clickhouse.py
        redis.py
        model_storage.py              # Phase 7

    observability/
        logging.py

tests/
    unit/
    integration/
    e2e/

scripts/                              # Phase 2+
alembic/
docker/
docs/superpowers/specs/
docs/superpowers/plans/
```

Комментарии `Phase N` в дереве описывают момент появления модулей в roadmap;
они не переносятся в имена каталогов или production-файлы.

## 4. Scope Phase 1

Phase 1 создаёт:

- installable Python package `nba_live_totals`;
- FastAPI application factory;
- `/health` для liveness;
- `/health/ready` для readiness;
- immutable validated settings;
- async lifecycle PostgreSQL, ClickHouse HTTP и Redis clients;
- конкурентные dependency checks с индивидуальными timeout;
- structured JSON logging и request correlation;
- SQLAlchemy/Alembic foundation без игровых таблиц;
- Docker image и Compose services `api`, `postgres`, `clickhouse`, `redis`;
- unit, API, integration и container smoke tests;
- команды `make dev`, `make test`, `make lint`, `make typecheck`;
- документацию установки, запуска и диагностики.

Phase 1 явно не включает:

- NBA provider или сетевые вызовы к NBA endpoints;
- таблицы games, players, possessions или predictions;
- historical/live ingestion;
- feature calculation;
- baseline или CatBoost models;
- Monte Carlo simulation;
- prediction API;
- trainer, backtest runner или live worker.

## 5. Phase 1 runtime components

### 5.1 Application factory

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    ...
```

Переданный `Settings` используется напрямую. Если аргумент отсутствует,
settings загружаются из environment ровно один раз при создании приложения.
Это исключает глобальный mutable state и позволяет тестам создавать полностью
изолированные экземпляры приложения.

### 5.2 AppResources

Lifespan создаёт типизированный immutable holder `AppResources`, содержащий:

- SQLAlchemy `AsyncEngine`;
- `redis.asyncio.Redis` client;
- `httpx.AsyncClient` для ClickHouse;
- dependency checkers, использующие эти клиенты.

Holder прикрепляется к конкретному экземпляру FastAPI. На shutdown engine и
оба клиента закрываются даже при частично завершившемся startup.

### 5.3 Health application service

Application layer определяет контракт checker и агрегирует результаты:

```python
class DependencyChecker(Protocol):
    name: str

    async def check(self) -> None:
        ...
```

Успешный checker завершается без результата, а неуспешный поднимает исключение.
`HealthService` запускает все checks конкурентно. Каждый adapter отвечает за
свой I/O, а service отвечает за timeout, latency measurement и общий статус.

### 5.4 Infrastructure checks

```text
PostgreSQL  -> SELECT 1 через SQLAlchemy AsyncConnection
ClickHouse  -> GET /ping через общий httpx.AsyncClient
Redis       -> PING через redis.asyncio
```

SQLAlchemy используется только для PostgreSQL. ClickHouse в Phase 1 работает
через нативный асинхронный HTTP API; это не помещает синхронный driver в event
loop и не вводит дополнительную библиотеку без consumer beyond health checks.

## 6. HTTP contracts

### 6.1 GET /health

Endpoint проверяет только живость процесса и не обращается к внешним системам.

```json
{
  "status": "alive"
}
```

Успешный ответ всегда имеет HTTP `200`, пока FastAPI способен обработать
запрос.

### 6.2 GET /health/ready

Успешный ответ:

```json
{
  "status": "ready",
  "checks": {
    "postgres": {"status": "up", "latency_ms": 3.2},
    "clickhouse": {"status": "up", "latency_ms": 4.8},
    "redis": {"status": "up", "latency_ms": 1.1}
  }
}
```

Если хотя бы один dependency недоступен или превысил timeout, endpoint
возвращает HTTP `503`, общий статус `not_ready` и `down` для соответствующей
проверки. Внешний ответ не содержит DSN, credentials, query text, exception
message или stack trace.

Порядок ключей `checks` стабилен: `postgres`, `clickhouse`, `redis`. Latency
округляется до одной десятой миллисекунды и не используется тестами как точное
значение.

## 7. Lifecycle и failure policy

Clients создаются без обязательного сетевого handshake. Недоступность
PostgreSQL, ClickHouse или Redis не завершает API startup: liveness остаётся
доступным, readiness возвращает `503`, а следующий readiness request повторяет
проверку на тех же reconnect-capable pools/clients.

Это решение различает две категории отказов:

- invalid configuration или невозможность создать локальный client — startup
  failure;
- временная недоступность внешнего сервиса — running process with failed
  readiness.

Каждый dependency check ограничивается
`HEALTHCHECK_TIMEOUT_SECONDS`. Отмена check не подавляется и не оставляет
background task. Один неуспешный dependency не отменяет сбор статусов остальных.

Shutdown закрывает ресурсы в обратном порядке создания. Ошибка закрытия одного
ресурса логируется, но не препятствует попыткам закрыть остальные.

## 8. Configuration

Settings реализуются через `pydantic-settings` и являются immutable. Имена
environment variables и defaults для локального Compose:

| Variable | Type | Local default |
|---|---:|---|
| `APP_ENV` | string | `local` |
| `LOG_LEVEL` | enum | `INFO` |
| `DATABASE_URL` | URL | `postgresql+asyncpg://nba:nba@postgres:5432/nba` |
| `CLICKHOUSE_URL` | URL | `http://clickhouse:8123` |
| `CLICKHOUSE_USER` | string | `default` |
| `CLICKHOUSE_PASSWORD` | secret string | empty for local Compose |
| `REDIS_URL` | URL | `redis://redis:6379/0` |
| `HEALTHCHECK_TIMEOUT_SECONDS` | positive float | `2.0` |
| `NBA_API_TIMEOUT_SECONDS` | positive float | `15.0` |
| `LIVE_POLL_INTERVAL_SECONDS` | positive float | `3.0` |
| `MONTE_CARLO_SIMULATIONS` | positive integer | `10000` |
| `MODEL_PATH` | path | `/app/models` |

Phase 1 валидирует также будущие параметры, явно требуемые общим ТЗ, чтобы
deployment contract не менялся незаметно. Неиспользуемые настройки не влияют
на runtime Phase 1.

`.env.example` содержит только локальные несекретные значения. `.env`, model
artifacts, caches, coverage и graphify outputs исключаются из Git.

## 9. Observability

Стандартный `logging` получает JSON formatter без отдельного logging framework.
Каждая запись содержит:

```text
timestamp
level
logger
message
app_env
request_id (если запись относится к HTTP request)
```

HTTP middleware принимает валидный входной `X-Request-ID` или создаёт UUID,
помещает его в context-local storage и возвращает в response header. Значение
считается валидным, если содержит от 1 до 128 ASCII-символов из набора
`A-Z`, `a-z`, `0-9`, `.`, `_` и `-`; остальные значения заменяются новым UUID.
Request ID не хранится в process-global mutable variable.

Readiness failure логируется компактно с полями `dependency`, `error_type` и
`latency_ms`. Stack trace не пишется для ожидаемой недоступности dependency при
каждом polling request. Непредвиденные ошибки логируются один раз на API
boundary со stack trace.

Prediction-specific поля из общего ТЗ добавляются вместе с prediction service,
когда для них появится реальный producer.

## 10. Database and migration foundation

SQLAlchemy metadata и Alembic async environment создаются в Phase 1, но первая
migration не создаёт игровые сущности. `alembic upgrade head` успешно
выполняется на пустой базе и служит проверкой корректности migration runtime.

PostgreSQL позже хранит relational metadata и model versions. ClickHouse позже
получает possession dataset через отдельный versioned DDL path. Redis остаётся
ephemeral store и не использует migrations.

## 11. Docker design

Compose содержит четыре сервиса:

| Service | Image/build | Purpose |
|---|---|---|
| `api` | project Dockerfile, `python:3.12.14-slim-bookworm` | FastAPI runtime |
| `postgres` | `postgres:17.11-bookworm` | relational metadata |
| `clickhouse` | `clickhouse:26.3.25.2-jammy` | analytical storage |
| `redis` | `redis:8.2.9-bookworm` | cache/live state |

Теги проверены по официальным Docker image registries 2026-08-30:

- https://hub.docker.com/_/postgres/tags?name=17.&page=1
- https://hub.docker.com/_/redis
- https://hub.docker.com/_/clickhouse
- https://hub.docker.com/_/python/tags?name=3.12&page=1

Images фиксируют exact database patch versions; обновление выполняется явным
dependency maintenance change. Python dependencies фиксирует `uv.lock`.

Каждое хранилище имеет named volume и native healthcheck. API зависит от
healthiness трёх хранилищ для обычного Compose startup, но его runtime failure
policy всё равно допускает последующую потерю dependency. Повторный
`docker compose up -d` переиспользует volumes и не запускает destructive init.

API image запускается непривилегированным пользователем, содержит только
runtime dependency group и не копирует `.env`, tests или local model artifacts.

## 12. Testing strategy

### 12.1 Unit tests

Unit suite проверяет:

- корректные defaults и invalid settings;
- immutable settings;
- `HealthService` при all-up, partial-down, timeout и checker exception;
- независимый сбор всех результатов при одном failure;
- отсутствие секретных exception details в response models;
- JSON logging fields и request-id context isolation.

Checker fakes реализуют тот же `Protocol`, но assertions направлены на результат
реального `HealthService`, а не на поведение fake.

### 12.2 API tests

FastAPI тестируется через `httpx.ASGITransport`:

- `/health` не вызывает dependency checks;
- `/health/ready` возвращает `200` для all-up;
- `/health/ready` возвращает `503` для partial-down;
- `X-Request-ID` сохраняется или создаётся;
- response contract не раскрывает exception details.

### 12.3 Integration tests

Tests с marker `integration` используют настоящие Compose services и проверяют:

- PostgreSQL `SELECT 1` через async engine;
- ClickHouse `/ping` с configured authentication;
- Redis `PING`;
- успешное закрытие pools/clients;
- `alembic upgrade head` на PostgreSQL.

### 12.4 Container smoke test

После `docker compose up -d --build` smoke test ожидает healthy containers,
запрашивает `/health` и `/health/ready` и проверяет HTTP status plus JSON
contract. Ожидание основано на health state с конечным timeout, а не на
фиксированном sleep.

## 13. Developer commands

```text
make dev        -> uv sync и docker compose up -d для инфраструктуры/API
make test       -> поднять dependencies и выполнить весь pytest suite
make lint       -> ruff check . и ruff format --check .
make typecheck  -> mypy --strict src tests
```

Прямые эквиваленты на `uv` документируются в README, поэтому Makefile не
становится единственным способом разработки на Windows.

## 14. Quality gate Phase 1

Phase 1 завершена, только если одновременно выполняются условия:

1. `uv sync --frozen` воспроизводимо устанавливает lockfile.
2. Unit, API, integration и smoke tests проходят.
3. `ruff check`, `ruff format --check` и `mypy --strict` проходят без ошибок.
4. `docker compose up -d --build` поднимает четыре healthy services.
5. `/health` возвращает `200` независимо от состояния data stores.
6. `/health/ready` возвращает `200` при all-up и безопасный `503` при partial-down.
7. Повторный Compose startup сохраняет named-volume data.
8. README описывает setup, configuration, commands, endpoints и границы Phase 1.

## 15. Спорные решения и rationale

### 15.1 Modular monolith вместо microservices

V1 требует согласованных domain contracts и быстрых итераций, а не независимого
масштабирования ещё не существующих workloads. Отдельные entry points дают
достаточную deployability без distributed-system overhead.

### 15.2 API стартует при недоступных dependencies

Fail-fast применяется к invalid local configuration. Временная внешняя ошибка
выражается readiness status, иначе orchestration получает crash loop и теряет
диагностический liveness endpoint.

### 15.3 ClickHouse через HTTP в Phase 1

Нужна только `/ping`, а `httpx` уже входит в стек. Специализированный adapter
выбирается в Phase 3 по реальным требованиям bulk inserts и analytical queries.

### 15.4 Нет игровых таблиц в Phase 1

Schemas должны следовать подтверждённым domain models и ingestion semantics.
Создание speculative tables сейчас привело бы к migrations churn и раннему
связыванию с NBA provider payloads.

### 15.5 Standard logging вместо structlog

Требования Phase 1 покрывает небольшой JSON formatter и context variable.
Отдельный framework можно добавить только при доказанной нехватке возможностей.

### 15.6 Никаких фиктивных predictions

Endpoint, возвращающий константу или случайный total, создавал бы ложный API
contract и не проверял главную архитектуру remaining possessions × PPP.
Prediction routes появляются после simulation и inference contracts.

## 16. Roadmap после Phase 1

Каждая фаза начинается только после успешных tests, lint и typecheck предыдущей:

1. **Phase 2 — historical ingestion:** provider protocol, games, players,
   boxscore, play-by-play и idempotent persistence.
2. **Phase 3 — possession parser:** deterministic possession reconstruction,
   invalid-state handling и ClickHouse possession schema.
3. **Phase 4 — lineup reconstruction:** starting lineups, substitutions,
   synthetic 5v5 tests и invalid segments.
4. **Phase 5 — features:** leakage-safe rolling team/player/live features.
5. **Phase 6 — baselines:** historical PPP, live PPP и shrinkage combination.
6. **Phase 7 — CatBoost:** pace, home PPP и away PPP models, temporal split,
   artifacts and version metadata.
7. **Phase 8 — Monte Carlo:** vectorized possession simulation, distributions,
   quantiles and betting-line probabilities.
8. **Phase 9 — historical replay:** future-blind backtest and calibration metrics.
9. **Phase 10 — prediction API:** games/predictions routes, Redis state hash and
   prediction cache.
10. **Phase 11 — live worker:** configurable polling, state updates and
    idempotent inference triggering.
11. **Phase 12 — player/lineup synergy:** regularized pair and lineup features.
12. **Phase 13 — experimental FM/embeddings:** isolated experiments evaluated
    through the same replay; no GNN implementation in V1.

Phase 2 получает отдельную design specification после завершения Phase 1. Такой
цикл не позволяет поздним research requirements размыть текущий production
milestone.
