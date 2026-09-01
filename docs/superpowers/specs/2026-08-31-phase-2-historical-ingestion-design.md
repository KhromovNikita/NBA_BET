# Phase 2 Historical NBA Ingestion — Design Specification

## 1. Назначение

Phase 2 добавляет возобновляемую историческую загрузку NBA-данных в существующий
модульный монолит. Система получает пять последних завершённых сезонов, включая
Regular Season и Playoffs, сохраняет исходные ответы NBA.com и формирует канонические
PostgreSQL-таблицы для следующих фаз.

Phase 2 реализует рабочий источник через `nba_api`, но domain и application code не
зависят от этой библиотеки. Замена NBA.com на другой provider не должна требовать
изменения будущих features, ML, simulation, prediction service или API.

## 2. Подтверждённые решения

1. Источник данных — NBA.com через `nba_api>=1.11.4,<1.12`.
2. Загружаются Regular Season и Playoffs; preseason и All-Star исключены.
3. Сохраняются нормализованные данные и неизменяемые исходные JSONB-документы.
4. Backfill продолжает работу после ошибки отдельного ресурса и возвращает частичный
   результат с exit code `1`.
5. Повторный запуск возобновляет загрузку: успешные ресурсы пропускаются, а failed или
   missing resources обрабатываются повторно.
6. Phase 2 предоставляет CLI, но не добавляет игровых HTTP API endpoints.
7. PostgreSQL хранит все данные Phase 2. ClickHouse начинает хранить
   реконструированные possessions только в Phase 3; Redis в Phase 2 не используется.
8. Possession parsing и lineup reconstruction остаются соответственно в Phase 3 и
   Phase 4. Phase 2 сохраняет необходимые для них исходные признаки.

## 3. Scope

### 3.1 Входит в Phase 2

- provider-neutral domain models для seasons, teams, players, games, traditional box
  scores и play-by-play events;
- replaceable historical data source protocol;
- NBA.com adapter через `nba_api`;
- чистые NBA payload mappers;
- retry, exponential backoff, jitter, rate limiting и ограниченная concurrency;
- Alembic migration для всех исторических и ingestion-таблиц;
- идемпотентные PostgreSQL operations;
- versioned raw JSONB documents;
- resumable ingestion state и run summaries;
- `scripts.backfill_games` CLI;
- structured ingestion logs;
- unit, integration, fixture-driven E2E и opt-in live canary tests;
- runbook для одного матча, одного сезона и пяти сезонов.

### 3.2 Не входит в Phase 2

- possession detection;
- восстановление 5x5 lineup по substitutions;
- ClickHouse historical possession schema;
- features, datasets, models, simulation, prediction и backtesting;
- live polling и Redis game state;
- публичные games или predictions API routes;
- background queue, Celery, Kafka или отдельный ingestion service;
- preseason, Summer League и All-Star games;
- автоматический регулярный scheduler.

## 4. Архитектура

Phase 2 продолжает границы Phase 1:

```text
scripts/backfill_games.py
        |
        v
application/ingestion/service.py
        |                         |
        v                         v
HistoricalDataSource       HistoricalIngestionStore
        |                         |
        v                         v
infrastructure/nba          infrastructure/postgres
        |                         |
        v                         v
nba_api / NBA.com              PostgreSQL
```

### 4.1 Domain

`nba_live_totals.domain` содержит immutable dataclasses, enums, value validation и
никаких импортов FastAPI, SQLAlchemy, Redis, ClickHouse или `nba_api`.

Основные модули:

```text
src/nba_live_totals/domain/
    identifiers.py
    games.py
    box_scores.py
    play_by_play.py
    source.py
```

### 4.2 Application

`nba_live_totals.application.ingestion` определяет:

- `HistoricalDataSource` — fetch и parse contract для заменяемого provider;
- `HistoricalIngestionStore` — атомарные persistence operations;
- `HistoricalIngestionService` — orchestration и failure isolation;
- request/result types и resource state machine.

Application layer знает о domain types и ports, но не знает о `nba_api` и SQLAlchemy.

### 4.3 Infrastructure

```text
src/nba_live_totals/infrastructure/
    nba/
        source.py
        transport.py
        mappers/
            schedule.py
            box_score.py
            play_by_play.py
    postgres/
        schema.py
        ingestion_store.py
```

`NBAApiHistoricalDataSource` — anti-corruption boundary. `transport.py` изолирует
синхронные endpoint constructors, а mappers являются чистыми функциями.

Transport использует `nba_api.stats.library.http.NBAStatsHTTP` с endpoint names и
parameters публичных V3/Schedule contracts. Он получает JSON dictionary без запуска
встроенного dataframe/parser path `nba_api`; только после сохранения raw document наши
чистые mappers преобразуют payload. Это обеспечивает требование raw-before-parse даже
при несовместимом изменении provider schema.

`PostgresHistoricalIngestionStore` реализует application port через SQLAlchemy Core и
async PostgreSQL transactions. ORM identity map для batch ingestion не требуется.

### 4.4 Composition roots

FastAPI composition root и публичный API Phase 1 не меняют контракт. CLI создаёт свои
`Settings`, async engine, source и store, а затем гарантированно закрывает engine даже
при cancellation или ошибке.

## 5. Source contracts

### 5.1 JSON types и source document

```python
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source: str
    endpoint: str
    resource_key: str
    requested_at: datetime
    request_parameters: JsonObject
    payload: JsonObject
    checksum_sha256: str
```

Checksum вычисляется из UTF-8 canonical JSON с сортированными keys и компактными
separators. Он не зависит от форматирования ответа.

### 5.2 Historical data source

```python
class HistoricalDataSource(Protocol):
    async def fetch_schedule(self, season: SeasonId) -> SourceDocument: ...
    async def fetch_box_score(self, game_id: GameId) -> SourceDocument: ...
    async def fetch_play_by_play(self, game_id: GameId) -> SourceDocument: ...

    def parse_schedule(self, document: SourceDocument) -> SeasonSchedule: ...
    def parse_box_score(self, document: SourceDocument) -> BoxScore: ...
    def parse_play_by_play(self, document: SourceDocument) -> tuple[PlayByPlayEvent, ...]: ...
```

Fetch и parse разделены, чтобы application service мог сохранить raw document до
проверки provider schema. Другой provider поставляется как другая реализация всего
`HistoricalDataSource`; orchestration не меняется.

### 5.3 NBA endpoints

- `ScheduleLeagueV2` — schedule, UTC datetime, arena, home/away teams, game status и
  scores;
- `BoxScoreTraditionalV3` — player, team и starter/bench traditional statistics;
- `PlayByPlayV3` — ordered actions.

`PlayByPlayV2`, `BoxScoreTraditionalV2`, `ScoreboardV2` и `BoxScoreSummaryV2` не
используются. В `nba_api` 1.11.x V2 endpoints deprecated или имеют известные пробелы в
актуальных данных.

Tournament classification детерминирована префиксом десятизначного NBA game ID:

- `002` — Regular Season;
- `004` — Playoffs;
- `001`, `003` и `005` — исключённые preseason, All-Star и Play-In;
- любой иной префикс — mapping failure, а не молчаливое включение.

Дополнительные label/subtype fields сохраняются только в raw document и не заменяют
это правило в V1.

## 6. Domain model

### 6.1 Identifiers

- `SeasonId` соответствует regex `^\d{4}-\d{2}$`; вторая часть равна последним двум
  цифрам следующего календарного года;
- `GameId` соответствует regex `^\d{10}$`;
- `TeamId` и `PlayerId` — положительные integers;
- все datetimes timezone-aware и нормализованы в UTC.

### 6.2 Season, team, player и game

`SeasonType` содержит `REGULAR_SEASON` и `PLAYOFFS`.

`GameStatus` содержит `SCHEDULED`, `LIVE`, `FINAL`, `POSTPONED`, `CANCELLED` и
`UNKNOWN`. Неизвестный provider status сохраняется как `UNKNOWN`, логируется и не
передаётся в per-game boxscore/PBP queue.

`Game` содержит:

- `game_id`, `season`, `season_type`;
- `game_date`, `started_at`;
- `home_team_id`, `away_team_id`;
- nullable `home_score`, `away_score`;
- `status`;
- nullable `arena_name`, `arena_city`, `arena_state`.

Home и away teams должны различаться. Scores неотрицательны. Для `FINAL` оба score
обязательны.

`Team` содержит NBA ID, city, name, tricode и slug. `Player` содержит NBA ID,
first/family/full name. Provider обновляет изменившиеся display metadata по ID, не
создавая новую identity.

### 6.3 Box score

`TeamBoxScore` и `PlayerBoxScore` сохраняют все поля Traditional V3, необходимые для
будущих features: seconds played, field goals, three-pointers, free throws, offensive,
defensive и total rebounds, assists, steals, blocks, turnovers, personal fouls, points
и plus/minus. Проценты хранятся как provider values и могут быть пересчитаны из
made/attempted counts.

`PlayerBoxScore` дополнительно содержит team ID, position, jersey number, starter flag,
played flag и nullable DNP/comment text. Отсутствующие числовые значения у DNP player
нормализуются в `None`, а не в ложный ноль.

Provider minutes сохраняют наблюдаемые empty, ISO-duration и `MM:SS` формы. Colon
форма должна точно соответствовать ASCII `[0-9]+:[0-5][0-9]`; malformed,
non-ASCII формы и total за пределами PostgreSQL signed `Integer` завершаются безопасным
`schema_mismatch`.

Каждый box score обязан:

- относиться к запрошенному game ID;
- содержать ровно две team rows, совпадающие с home/away teams;
- не содержать одного player дважды;
- связывать player row только с одной из двух команд;
- согласовывать итоговые team points с final game score.

### 6.4 Play-by-play

`PlayByPlayEvent` содержит:

- `game_id`, положительный `sequence_number`, `action_number`, nullable `action_id`;
- `period`, исходный ISO clock и `seconds_remaining_period_ms`;
- nullable team ID и person/player ID;
- `action_type`, `sub_type`, description;
- nullable home/away score;
- nullable shot result, shot distance, legacy x/y и location;
- `is_field_goal`, `points_total`, `video_available`.

`sequence_number` детерминированно назначается из 1-based raw provider array order.
Events возвращаются в sequence order, а `(game_id, sequence_number)` уникален.
Provider `action_number` и nullable `action_id` сохраняются точно; дубликаты
`action_number` допустимы и не удаляются или перенумеровываются. Для завершённого матча
пустой PBP является failure. Substitution
events сохраняются без реконструкции lineup.

## 7. PostgreSQL schema

### 7.1 Canonical tables

`seasons`

- primary key: `(season, season_type)`;
- `season_start_year`, `created_at`, `updated_at`.

`teams`

- primary key: `team_id BIGINT`;
- `city`, `name`, `tricode`, `slug`, timestamps;
- index по `tricode`; identity определяется только стабильным NBA team ID.

`players`

- primary key: `player_id BIGINT`;
- `first_name`, `family_name`, `full_name`, timestamps.

`games`

- primary key: `game_id VARCHAR(10)`;
- composite foreign key to `seasons`;
- foreign keys to home/away teams;
- fields from `Game`;
- checks for distinct teams, ID shape and nonnegative scores;
- indexes `(season, season_type, game_date)` and `game_date`.

`team_box_scores`

- primary key: `(game_id, team_id)`;
- foreign keys to game/team;
- normalized Traditional V3 fields;
- index `(team_id, game_id)`.

`player_box_scores`

- primary key: `(game_id, player_id)`;
- foreign keys to game/player/team;
- normalized Traditional V3 fields;
- indexes `(player_id, game_id)` and `(game_id, team_id)`.

`play_by_play_events`

- primary key: `(game_id, sequence_number)`;
- foreign keys to game и nullable team; `person_id` намеренно не имеет foreign key,
  потому что provider может указать official, неизвестного actor или player, которого
  нет в traditional box score;
- normalized PlayByPlay V3 fields;
- index `(game_id, period, sequence_number)`; `action_number` остаётся provider field.

No partitioning is introduced in Phase 2. The expected five-season volume is suitable
for an indexed PostgreSQL table; premature partitioning would complicate migrations.

### 7.2 Raw documents

`source_documents`

- `document_id UUID` primary key;
- `source`, `endpoint`, `resource_key`;
- `request_parameters JSONB`, `payload JSONB`;
- `checksum_sha256 CHAR(64)`, `requested_at`, `stored_at`;
- unique `(source, endpoint, resource_key, checksum_sha256)`;
- index `(source, endpoint, resource_key, stored_at DESC)`.

Identical payloads deduplicate. Изменившийся payload создаёт новую immutable version.
Raw documents не удаляются при canonical refresh в Phase 2.

### 7.3 Ingestion state

`ingestion_runs`

- `run_id UUID` primary key;
- request parameters JSONB;
- `started_at`, nullable `finished_at`;
- status: `running`, `succeeded`, `partial`, `failed`, `interrupted`;
- discovered, succeeded, skipped и failed counters.

`ingestion_resources`

- primary key: `(source, resource_type, resource_key)`;
- resource type: `schedule`, `box_score`, `play_by_play`;
- status: `pending`, `running`, `succeeded`, `failed`;
- `attempt_count`, nullable `latest_document_id`, nullable durable `applied_document_id`;
- nullable `last_run_id`, `last_error_code`, `last_error_type`;
- `claim_version BIGINT`, nullable `claimed_until`, timestamps.

Resource claim выполняется атомарно. Неистёкший чужой claim пропускается. Истёкший
claim доступен для resume и увеличивает монотонный `claim_version`. Default claim lease
— 600 seconds.

## 8. Persistence semantics

`HistoricalIngestionStore` предоставляет операции уровня use case, а не низкоуровневые
generic repositories:

```python
class ClaimAction(StrEnum):
    FETCH = "fetch"
    REPARSE = "reparse"
    SKIP = "skip"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class StoredSourceDocument:
    document_id: UUID
    document: SourceDocument


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    key: ResourceKey
    run_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    action: ClaimAction
    claim: ResourceClaim | None = None
    stored_document: StoredSourceDocument | None = None


class HistoricalIngestionStore(Protocol):
    async def start_run(self, request: BackfillRequest) -> UUID: ...
    async def finish_run(self, run_id: UUID, result: BackfillResult) -> None: ...
    async def claim_resource(
        self, run_id: UUID, key: ResourceKey, *, refresh: bool
    ) -> ClaimDecision: ...
    async def save_document(self, claim: ResourceClaim, document: SourceDocument) -> UUID: ...
    async def apply_schedule(
        self, claim: ResourceClaim, document_id: UUID, schedule: SeasonSchedule
    ) -> None: ...
    async def replace_box_score(
        self, claim: ResourceClaim, document_id: UUID, box_score: BoxScore
    ) -> None: ...
    async def replace_play_by_play(
        self,
        claim: ResourceClaim,
        document_id: UUID,
        events: tuple[PlayByPlayEvent, ...],
    ) -> None: ...
    async def fail_resource(
        self, claim: ResourceClaim, *, error_code: str, error_type: str
    ) -> None: ...
```

`FETCH` и `REPARSE` всегда возвращают `ResourceClaim`; `REPARSE` также возвращает
`StoredSourceDocument`. `SKIP` и `BUSY` не возвращают claim. `SKIP` успешного resource
возвращает immutable document, на который указывает durable `applied_document_id`,
прочитанный внутри той же locked claim transaction. Schedule skip discovery парсит
только этот decision-carried snapshot и не делает отдельный latest-document read.
Box-score и PBP skips ничего не парсят и не записывают. Все write operations
проверяют `(run_id, claim_version)` как fencing token. Поздно завершившийся worker с
истёкшей lease не может перезаписать данные нового владельца.

`save_document` имеет отдельную короткую transaction и завершается до parsing.
`apply_*` выполняет canonical write и перевод resource в `succeeded` в одной
transaction.

Boxscore и PBP являются полными snapshots. При изменившемся checksum соответствующие
game-scoped rows удаляются и вставляются заново в одной transaction. Это не оставляет
устаревших players/actions от старой версии. Raw versions сохраняют audit trail.

Если refresh получает тот же checksum для уже успешного resource, canonical rewrite не
выполняется; обновляется только run accounting.

`BUSY` означает, что resource обрабатывается другим неистёкшим claim. Такой resource не
считается успешно завершённым текущим run: итоговый status становится `partial`, а CLI
возвращает `1`, чтобы оператор повторил resume позже.

## 9. Ingestion algorithm

### 9.1 Schedule

Для каждого явно выбранного сезона:

1. Claim `schedule:{season}`.
2. Если failed resource имеет raw document и нет `--refresh`, выполнить local reparse.
3. Иначе fetch `ScheduleLeagueV2`, сохранить document, затем parse.
4. Исключить game ID prefixes `001` и `003`.
5. Преобразовать `002` и `004` в соответствующие `SeasonType`.
6. Upsert season, teams и games одной canonical transaction.
7. В per-game queue добавить только `FINAL` games.

Schedule failure одного сезона не останавливает другие сезоны.

### 9.2 Per-game resources

Для каждого final game независимо обрабатываются:

1. `box_score:{game_id}`;
2. `play_by_play:{game_id}`.

Оба resource проходят claim, reparse/fetch, raw persistence, validation и atomic
replacement. PBP failure не откатывает уже успешный box score и наоборот.

### 9.3 Resume и refresh

- `succeeded` пропускается по умолчанию;
- `failed` с raw document сначала reparses locally;
- `failed` без raw document повторяет network fetch;
- `pending` и expired `running` доступны для claim;
- `--refresh` всегда выполняет fetch, но не переписывает canonical data при неизменном
  checksum;
- `--game-id` требует уже существующий game или сначала получает его season schedule,
  если вместе передан `--season`; после schedule parsing в queue попадает только
  указанный game, а не весь сезон.

## 10. Networking

`nba_api` использует synchronous `requests`. Каждый endpoint call выполняется через
`asyncio.to_thread` под общим semaphore. Default concurrency — 1. Global rate limiter
обеспечивает минимальный интервал 0.75 seconds между началами запросов даже при
увеличенной concurrency.

Retry применяется только к:

- timeout;
- connection error;
- HTTP 429;
- HTTP 5xx;
- пустому или не-JSON transport response.

Default — 4 attempts с exponential backoff от 1 до 15 seconds и bounded random jitter.
Schema, domain validation и неизвестный tournament prefix не retry-ятся сетью.

Parent cancellation никогда не преобразуется в обычную resource failure. CLI отмечает
run как `interrupted`, освобождает локальные ресурсы и возвращает управление оболочке.

## 11. CLI contract

Поддерживаются взаимоисключающие selectors:

```bash
uv run python -m scripts.backfill_games --season 2025-26

uv run python -m scripts.backfill_games \
    --season-from 2021-22 \
    --season-to 2025-26

uv run python -m scripts.backfill_games \
    --season 2025-26 \
    --game-id 0022500123
```

Дополнительный `--refresh` включает forced fetch. Season range включителен и должен
быть непрерывным. Без season selector CLI завершает работу с exit code `2`; скрытого
зависящего от даты default нет.

Exit codes:

- `0` — все выбранные resources succeeded или были корректно skipped;
- `1` — есть failed resource или schedule целого сезона;
- `2` — invalid arguments или configuration;
- keyboard interruption сохраняет `interrupted` run и использует стандартный interrupt
  exit status оболочки.

CLI печатает компактную итоговую строку: run ID и discovered/succeeded/skipped/failed.
Детали доступны в JSON logs и PostgreSQL state.

## 12. Settings

Существующий `NBA_API_TIMEOUT_SECONDS=15.0` используется transport layer. Добавляются:

```text
NBA_API_MAX_CONCURRENCY=1
NBA_API_MIN_INTERVAL_SECONDS=0.75
NBA_API_MAX_ATTEMPTS=4
NBA_API_BACKOFF_BASE_SECONDS=1.0
NBA_API_BACKOFF_MAX_SECONDS=15.0
NBA_API_RESOURCE_LEASE_SECONDS=600
```

Concurrency, attempts и lease — positive integers; intervals/backoff — nonnegative or
positive floats согласно смыслу. Backoff max не может быть меньше base.

NBA.com не требует application API key. Raw payloads, request headers и environment
proxy values не логируются.

## 13. Errors and observability

Application использует стабильные error codes:

- `network_timeout`, `network_connection`, `rate_limited`, `upstream_server`;
- `empty_response`, `invalid_json`, `schema_mismatch`;
- `invalid_game`, `invalid_box_score`, `invalid_play_by_play`;
- `unknown_game_type`, `resource_busy`, `claim_lost`, `persistence_error`.

В database и logs сохраняются error code и exception class. Полный exception message,
payload или DSN не сохраняются в `ingestion_resources` и не попадают в обычные logs.

Каждое значимое событие логирует:

```text
event, run_id, season, game_id, resource_type,
attempt, status, duration_ms, error_code, error_type
```

Отсутствующие поля опускаются. Существующий request ID не используется CLI как run ID;
correlation обеспечивается отдельным `run_id`.

## 14. Testing strategy

### 14.1 Unit

- Season/Game ID validation и UTC normalization;
- schedule mapping для regular season, playoffs, excluded и unknown prefixes;
- boxscore mapping для starters, bench, DNP и nullable fields;
- overtime, score и ISO clock parsing;
- substitutions и ordered PBP;
- duplicate/mismatched data rejection;
- canonical JSON checksum;
- retry classification, backoff и rate limiter;
- orchestration resume, refresh, reparse, partial failure и cancellation;
- CLI selectors и exit codes.

Fixtures имеют реальную форму NBA V3 payload, но сокращены до минимального набора rows и
events, необходимого тесту. Полные сезонные ответы не коммитятся.

### 14.2 Integration

На реальном Docker PostgreSQL проверяются:

- Alembic upgrade head и повторный upgrade;
- foreign keys и checks;
- raw document deduplication и versioning;
- resource claim/expiry;
- idempotent schedule upsert;
- atomic boxscore/PBP replacement;
- rollback при invalid batch;
- local reparse после ранее сохранённого document.

### 14.3 Fixture-driven E2E

CLI запускается против fake `HistoricalDataSource` и реального PostgreSQL. Первый run
загружает минимальный набор regular/playoff games; второй run возвращает те же row
counts и skipped resources. Отдельный сценарий инъецирует ошибку одного game и
подтверждает продолжение, exit code `1` и успешный resume.

### 14.4 Live canary

Opt-in marker `live` выполняет запросы для одного явно заданного завершённого game ID и
проверяет непустые schedule/game, boxscore и PBP. Он исключён из обычного CI и полного
локального suite, поскольку NBA.com является внешним нестабильным upstream.

## 15. Migration and runtime rollout

Новая Alembic revision следует за `0001_baseline`. Downgrade удаляет только таблицы
Phase 2 в обратном dependency order. Migration не выполняет network backfill.

Docker API image включает `scripts` и `nba_api`, поэтому CLI может запускаться как с
host, так и внутри одноразового container process. Compose не получает постоянный
ingestion worker.

После migration acceptance выполняются:

1. fixture E2E;
2. opt-in live canary одного game;
3. повторный canary/backfill для доказательства idempotency;
4. документированная пятисезонная команда.

Полный пятисезонный network backfill является операторской data-loading operation и не
является обязательной частью быстрого CI gate.

## 16. Acceptance criteria

Phase 2 готова, когда одновременно выполнено следующее:

1. Domain/application modules не импортируют `nba_api`, SQLAlchemy, FastAPI, Redis или
   ClickHouse clients.
2. Source adapter использует только ScheduleLeagueV2, BoxScoreTraditionalV3 и
   PlayByPlayV3.
3. Migration создаёт описанные canonical/raw/state tables и проходит idempotent upgrade.
4. Raw document сохраняется до parsing и переживает parser failure.
5. Повтор одинакового backfill не создаёт duplicate canonical или raw rows.
6. Изменившийся source checksum создаёт raw version и атомарно заменяет game snapshot.
7. Failed game не останавливает остальные games; run завершается `partial` и CLI code
   `1`.
8. Resume использует имеющийся raw document до нового network fetch.
9. Regular Season и Playoffs обрабатываются; preseason/All-Star исключаются.
10. Fixture E2E, PostgreSQL integration tests и opt-in live canary проходят.
11. Все Phase 1 tests остаются зелёными.
12. Ruff, full format check, strict mypy, frozen lock sync и Docker runtime gates проходят.
13. README документирует запуск одного game, одного season и диапазона пяти сезонов,
    expected duration, resume, refresh и безопасную остановку.

## 17. Источники решений

- Исходное техническое задание: разделы 4, 5, 23, 41, 44, 46 и 47.
- Phase 1 design: roadmap и сохранённые modular-monolith boundaries.
- `nba_api` 1.11.4 project metadata:
  https://raw.githubusercontent.com/swar/nba_api/master/pyproject.toml
- `nba_api` 1.11.4 release notes:
  https://github.com/swar/nba_api/releases/tag/v1.11.4
- ScheduleLeagueV2 implementation:
  https://github.com/swar/nba_api/blob/master/src/nba_api/stats/endpoints/scheduleleaguev2.py
- BoxScoreTraditionalV3 contract:
  https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/boxscoretraditionalv3.md
- PlayByPlayV3 contract:
  https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/playbyplayv3.md
