# NBA Live Totals Prediction Engine — Phase 4 Lineup Reconstruction Design

**Дата:** 2026-09-15
**Статус:** утверждён для implementation plan
**Предыдущая фаза:** Phase 3 possession parser

## 1. Цель

Phase 4 детерминированно восстанавливает составы обеих команд на площадке для
завершённых исторических NBA-матчей. Исходное состояние задаётся стартовыми пятёрками
из применённого box score, последующие состояния — подтверждёнными substitution-событиями
из применённого PlayByPlayV3.

Результат — versioned immutable snapshot непересекающихся lineup-сегментов. Для каждого
дискретного PBP boundary можно определить состояние home и away team. Валидная сторона
всегда содержит ровно пять уникальных player ID. Повреждённые либо неоднозначные данные
явно дают `invalid`, а не предполагаемый состав.

Phase 4 не меняет семантику или schema Phase 3 possessions. Phase 5 сможет соединить
опубликованные lineup-сегменты с конечными boundaries владений и построить требуемый
possession dataset с `home_lineup` и `away_lineup`.

## 2. Утверждённые решения

1. Реконструкция строго причинная: state на boundary использует только стартовую пятёрку
   и события не позже этого boundary. Lookahead и исправление прошлого будущими действиями
   запрещены.
2. Текущий состав переносится через границы периодов и overtime. Period boundary сам по
   себе не сбрасывает и не переопределяет lineup.
3. Ошибка локализуется по команде. Другая команда продолжает восстанавливаться, но общий
   5x5-сегмент валиден только при двух валидных сторонах.
4. Невалидное состояние стороны поглощающее до авторитетного полного lineup reset из пяти
   игроков. NBA PlayByPlayV3 normalizer V1 такие resets не создаёт, но provider-neutral
   state machine допускает их для будущего источника.
5. Lineups хранятся отдельно от `possessions_v1`: PostgreSQL является authoritative
   control plane, ClickHouse хранит `nba.lineup_segments_v1` immutable snapshots.
6. Snapshot с корректно представленными invalid-сегментами является публикуемым
   аналитическим результатом. Неразбираемый или внутренне несогласованный input не
   публикуется и не заменяет предыдущий pointer.
7. Domain/application не видят NBA `actionType`, `subType`, `description`, SQLAlchemy,
   ClickHouse/httpx, FastAPI или Redis. Provider-specific parsing остаётся в
   `infrastructure/nba` и имеет явную версию.
8. Lineup snapshot зависит одновременно от применённых box-score и PBP revisions.
   Смешанный либо устаревший dual-input snapshot не публикуется.
9. Порядок определяется только canonical `sequence_number`. Clock, `action_number` и
   nullable `action_id` не используются как identity или средство сортировки.
10. Phase 4 работает только с завершёнными историческими матчами. Live/incremental lineup
    state относится к Phase 11.

## 3. Scope

### 3.1 Входит в Phase 4

- immutable provider-neutral lineup contracts;
- NBA PlayByPlayV3 substitution normalizer;
- starter normalization и raw/canonical cross-check применённого box score;
- deterministic 5x5 state machine;
- per-team invalid state и безопасная error taxonomy;
- causal period/overtime и event-boundary semantics;
- PostgreSQL run/resource/attempt state с dual-input fencing;
- versioned ClickHouse lineup-segment schema;
- immutable write, exact readback, publication и crash recovery;
- `build-lineups` CLI для game/season/range, rebuild и verify;
- unit, fixture, PostgreSQL, ClickHouse, E2E, Docker и opt-in live tests;
- README и operator runbook Phase 4.

### 3.2 Не входит

- изменение `possessions_v1` или Phase 3 parser/normalizer rules;
- materialized possession-to-lineup join;
- pregame, rolling, player, team, pace, PPP или live features — Phase 5;
- lineup efficiency, pair/synergy и regularization — Phase 12;
- baseline/ML models, Monte Carlo, replay/backtest и prediction endpoints;
- live polling, partial game checkpoints и Redis lineup state;
- fuzzy player-name matching либо восстановление состава по будущим shot/foul/rebound
  участникам;
- автоматическое удаление orphan ClickHouse snapshots;
- отдельный NBA lineup endpoint.

Существующие `/health` и `/health/ready` не меняются. Новые HTTP routes не добавляются.

## 4. Рассмотренные варианты

### 4.1 Отдельный lineup snapshot pipeline — выбран

PostgreSQL хранит lineage, claims, fencing и published pointer. ClickHouse хранит
`lineup_segments_v1`. Lineup и possession revisions могут независимо пересобираться, а
Phase 5 соединяет их по event boundary.

Плюсы: сохраняет подтверждённый Phase 3 contract, даёт отдельную freshness model,
воспроизводимость и безопасную публикацию invalid-интервалов. Цена — отдельные control
tables и необходимость boundary join в Phase 5.

### 4.2 Добавить arrays в `possessions_v1` — отклонён

Этот вариант связывает версии possession parser и lineup reconstructor, требует переписать
Phase 3 output hashing/schema и пересобирать possessions при любом исправлении starter data.
Также одна lineup после possession не описывает substitutions внутри владения.

### 4.3 PostgreSQL-only либо вычисление на чтении — отклонён

Mutable relational rows или повторный расчёт на чтении ухудшают аналитические scan-запросы,
точную воспроизводимость и согласованность с существующей immutable ClickHouse model.

## 5. Архитектурная граница

```text
PostgreSQL applied box score raw + canonical player rows
PostgreSQL applied PBP raw + canonical event rows
                         │
                         ▼
NBA lineup normalizer (provider-specific, versioned, fail-closed)
                         │ starters + NormalizedLineupEvent[]
                         ▼
pure lineup state machine (provider-neutral, causal)
                         │ LineupReconstructionResult
                         ▼
ClickHouse immutable write + exact snapshot readback
                         │
                         ▼
PostgreSQL fenced publication pointer
```

Предлагаемая структура:

```text
src/nba_live_totals/
    domain/
        lineups.py
    application/
        lineups/
            models.py
            ports.py
            service.py
    infrastructure/
        nba/
            lineup_normalizer.py
        postgres/
            lineup_store.py
        clickhouse/
            lineups.py

alembic/versions/
    <revision>_phase_4_lineup_reconstruction.py

clickhouse/migrations/
    0002_lineup_segments.sql
    0002_lineup_segments.schema.json

scripts/
    build_lineups.py
```

Повторно используются общие `GameContext`, `EventBoundary`, `BoundarySide`, `GameId`,
`SeasonId`, canonical JSON hashing и безопасные logging conventions. Lineup domain не
импортирует possession parser и не создаёт общий framework для произвольных derived
datasets.

## 6. Термины и дискретная временная модель

Для canonical события `n` существуют две упорядоченные границы:

```text
BEFORE(n) < AFTER(n) < BEFORE(n + 1)
```

`BEFORE(n)` — state перед применением события `n`; `AFTER(n)` — state сразу после него.
Substitution является атомарным переходом между этими двумя boundaries.

Lineup segment задаётся включительными `start_boundary` и `end_boundary`. Первый сегмент
начинается на `BEFORE(first_event)`, последний заканчивается на `AFTER(last_event)`.
При substitution `n` предыдущий segment заканчивается на `BEFORE(n)`, следующий начинается
на `AFTER(n)`. В дискретном boundary domain между ними нет неизвестного состояния.

Несколько substitutions с одинаковым clock не образуют скрытую транзакцию. Каждое событие
применяется в `sequence_number` order, поэтому промежуточный state между `AFTER(n)` и
`BEFORE(n+1)` существует и остаётся наблюдаемым для точного boundary join.

Clock и period хранятся как описательные поля segment endpoints. Они не переупорядочивают
события и не объединяют substitutions.

## 7. Входной snapshot и атомарность двух ресурсов

`GameLineupInputSnapshot` содержит:

- final `GameContext`;
- canonical game-scoped `player_box_scores` projections обеих команд;
- canonical ordered `PlayByPlayEvent` rows;
- immutable applied box-score `SourceDocument`;
- immutable applied PBP `SourceDocument`;
- IDs и checksums обоих документов.

PostgreSQL work store в одной transaction:

1. блокирует `ingestion_resources` для `box_score:<game_id>` и
   `play_by_play:<game_id>` в фиксированном lexical resource-key order;
2. блокирует final game row после обоих Phase 2 resources, сохраняя действующий
   cross-plane deadlock order Phase 3;
3. требует `status='succeeded'` и ненулевой `applied_document_id`; durable applied pointer
   является входом Phase 4, а `latest_document_id` не подменяет его;
4. читает оба immutable source documents;
5. читает canonical game-scoped player box rows и PBP rows, относящиеся к этим applied
   revisions;
6. строит input token до выделения attempt.

Phase 2 хранит canonical game-scoped tables как текущую применённую projection. Поэтому
resource locks обязаны удерживаться до чтения соответствующих rows. Это исключает смесь
старого box score с новым PBP либо наоборот.

Перед `publish_snapshot`, `poison_attempt` и `fail_game` store повторно захватывает те же
locks и строит current token. Несовпадение делает attempt `superseded`; pointer не меняется.

## 8. Starter normalization

NBA normalizer сопоставляет applied raw box-score players с canonical player box rows по
`(game_id, team_id, player_id)`. Требуются:

- ровно две команды, совпадающие с `GameContext`;
- одинаковые множества player IDs в raw и canonical representations;
- согласованные team IDs, `played`, comment/DNP state и starter signal;
- отсутствие дубликатов player ID;
- ровно пять уникальных eligible starters на каждую команду.

Исторические player names/aliases берутся только из immutable applied raw box-score
document. Глобальная таблица `players` намеренно не читается, не хешируется и не участвует
в resolution: она может обновляться box score другого матча и не является game-revisioned.

Starter signal выбирается по наблюдаемой versioned NBA форме:

1. если raw player содержит `starter`, используется exact provider flag;
2. если поле отсутствует, legacy BoxScoreTraditionalV3 использует непустую `position`;
3. одновременные противоречащие signals дают structural normalization failure, а не
   предпочтение одного из них.

Starter обязан принадлежать команде, иметь `played=true` и не иметь DNP/comment state,
противоречащего участию. Невозможность получить ровно пять starters не ломает сам input
snapshot: соответствующая team state начинается как `invalid_initial_lineup`, чтобы
качество источника было опубликовано явно.

## 9. NBA substitution normalizer V1

Normalizer получает raw action и canonical `PlayByPlayEvent` в одной позиции. Cardinality,
game identity, sequence-to-position, action number/ID, period/clock, team/person identity и
substitution classification cross-check друг друга. Необъяснимое raw/canonical расхождение
является structural failure всего attempt.

Наблюдаемая PlayByPlayV3 форма одной замены:

```text
actionType = "Substitution" | versioned lowercase equivalent
subType    = "" | documented "out"
personId   = outgoing player ID
playerName = outgoing display label
description = "SUB: <incoming label> FOR <outgoing label>"
```

Разбор description допускается только внутри `infrastructure/nba` по case-insensitive
anchored fullmatch `^SUB:\s+(?<incoming>.+?)\s+FOR\s+(?<outgoing>.+?)$` после Unicode
NFKC. Captures должны быть непустыми, bounded и без control characters. Свободный поиск
подстроки и угадывание запрещены. `personId` должен разрешаться в roster этой команды,
находиться в outgoing label aliases и совпадать с canonical event. После trim/casefold
`actionType` обязан быть `substitution`, а `subType` — `""` либо `out`; другие значения
дают unsupported transition, пока новый profile не зафиксирует их semantics.

Incoming label разрешается только внутри roster той же команды. Для каждого игрока
генерируются конечные aliases:

- `family_name`;
- `first_name family_name`;
- `first initial + family_name`.

Перед exact comparison применяется одна функция: Unicode NFKC и casefold; Unicode
whitespace заменяется одним ASCII space; curly apostrophes и dash variants приводятся к
ASCII apostrophe/hyphen; точки и запятые удаляются; края повторно trim-ятся. Остальные
символы и порядок tokens сохраняются. Та же функция применяется к generated aliases и
captures. Перестановка слов, удаление apostrophe/hyphen, edit distance, phonetic matching и
future-event inference запрещены. Если alias соответствует нулю или нескольким eligible
roster players, signal становится `InvalidTeamTransition` с безопасным code.

Provider-specific normalizer выдаёт ровно один `NormalizedLineupEvent` на каждое canonical
PBP event. Envelope сохраняет только provider-neutral boundary metadata:

```text
NormalizedLineupEvent
    sequence_number
    period
    seconds_remaining_period_ms
    transition: LineupTransition
```

Так pure reconstructor знает complete event range и period/clock для первого/последнего
boundary даже в игре без единой замены. `LineupTransition` является одним из закрытых
provider-neutral сигналов:

```text
Substitute(team_id, outgoing_player_id, incoming_player_id)
InvalidTeamTransition(team_id | unknown, code)
FullLineupReset(team_id, player_ids)  # не создаётся NBA V1
NoLineupChange
```

Raw description, playerName и aliases не переходят в domain result и никогда не логируются.

## 10. Provider-neutral state machine

На старте каждая команда независимо получает `ValidLineup(frozenset[player_id])` либо
`InvalidLineup(first_reason, since_boundary)`.

Для `Substitute` над valid state проверяется:

1. signal team является home либо away team;
2. outgoing принадлежит roster этой команды и находится on court;
3. incoming принадлежит roster этой команды, eligible/played и отсутствует on court;
4. outgoing и incoming различны;
5. атомарная замена даёт ровно пять уникальных IDs.

Успешная замена создаёт новый valid state на `AFTER(event)`. Нарушение любой проверки
создаёт invalid state этой команды на `AFTER(event)`. Предыдущий valid segment заканчивается
на `BEFORE(event)`.

Substitution для уже invalid team не изменяет state и не пытается постепенно восстановить
пятёрку. `FullLineupReset` может вернуть valid state только если содержит ровно пять
уникальных eligible players одной команды; новый state начинается на указанном AFTER
boundary. Unsupported либо invalid reset сохраняет invalid state.

Если invalid transition не содержит доказуемого team ID, нельзя безопасно выбрать одну
сторону: обе team states становятся invalid на `AFTER(event)`. При известном home/away team
ошибка затрагивает только соответствующую сторону.

Period start/end, timeout, foul, replay, shot, rebound и прочие PBP actions сами по себе не
меняют lineup. State переносится через regulation periods и overtime без reset.

## 11. Причинность, пропущенные события и invalid semantics

State machine не проверяет итоговые minutes, shot actors или будущие substitutions для
исправления уже вычисленного state. Эти данные могут свидетельствовать о проблеме при
последующем событии, но не позволяют переписать прошлый boundary.

Если substitution был пропущен upstream, первая наблюдаемая ошибка часто возникает позже,
например outgoing player отсутствует в текущем state. В таком случае:

- предшествующие segments остаются такими, какими их можно было доказать причинно;
- invalid начинается после первого противоречивого события;
- retroactive invalidation по будущему evidence не выполняется;
- issue code указывает место обнаружения, а не предполагаемое место пропуска.

Invalid state означает «точная пятёрка недоказуема». Для invalid стороны output array пуст.
Нельзя публиковать частичные 4/6-player arrays или последнюю известную пятёрку как текущую.
Valid другая сторона сохраняет свою точную пятёрку. Combined segment valid только если обе
team states valid.

Первый fatal reason стороны сохраняется до valid reset. Последующие события invalid team не
создают неограниченный diagnostic stream. Operator projection `last_issue_codes` выводится
из первых 32 уникальных `(team, invalid_reason, invalid_since)` в ordered segments. Она не
является отдельной semantic частью snapshot и не нужна для пересчёта `output_sha256`.

## 12. Error taxonomy

Минимальный V1 allowlist:

```text
invalid_initial_lineup
unknown_substitution_team
missing_outgoing_player_id
unsupported_substitution_form
outgoing_label_mismatch
unknown_incoming_player
ambiguous_incoming_player
roster_team_mismatch
ineligible_incoming_player
outgoing_not_on_court
incoming_already_on_court
same_player_substitution
invalid_full_lineup_reset
```

Structural attempt errors, которые не становятся lineup segments:

```text
input_resource_not_applied
input_document_identity_mismatch
input_document_checksum_mismatch
raw_canonical_cardinality_mismatch
raw_canonical_event_mismatch
raw_canonical_roster_mismatch
unsupported_document_schema
empty_completed_game_pbp
```

Domain issue codes и exception types должны соответствовать safe identifier regex. Exception
messages, raw payloads, descriptions, SQL, URLs, DSNs, credentials и proxy values не
записываются в logs или PostgreSQL error fields.

## 13. Domain contracts

Основные frozen/slots types:

```text
LineupVersions
    normalizer_version
    reconstructor_version
    rules_version
    output_schema_version

LineupPlayerBox
    game_id, team_id, player_id
    position, starter, played, comment

TeamRoster
    team_id
    roster_player_ids
    eligible_player_ids
    starter_player_ids

LineupTransition = Substitute | InvalidTeamTransition | FullLineupReset | NoLineupChange

NormalizedLineupEvent
    sequence_number
    period
    seconds_remaining_period_ms
    transition

TeamLineupState
    status: valid | invalid
    player_ids: frozenset[int]        # ровно 5 только для valid
    invalid_reason: optional safe code
    invalid_since: optional EventBoundary

LineupSegment
    segment_number
    start_boundary
    end_boundary
    start_period, end_period
    start_seconds_remaining_period_ms, end_seconds_remaining_period_ms
    home_state
    away_state

LineupReconstructionResult
    status: valid | invalid
    revision
    ordered segments
    issues: computed bounded projection of unique invalid state transitions
```

Public constructors валидируют exact primitive types, positive IDs, safe/version codes,
boundary ordering, contiguous 1-based `segment_number`, отсутствие overlap и canonical
player ordering при сериализации.

Порядок игроков в lineup не имеет семантики. Domain использует set/frozenset; canonical
JSON и ClickHouse arrays сортируют IDs по возрастанию. Поэтому один и тот же 5-player set
имеет один hash независимо от порядка raw box-score rows.

## 14. Segment construction и lookup contract

Segment создаётся только при изменении хотя бы одного team state либо его validity. Обычные
PBP actions не создают по строке на событие.

Инварианты snapshot:

- хотя бы один segment для непустого complete-game PBP;
- `segment_number` contiguous, начиная с 1;
- первый start равен `BEFORE(first_sequence_number)`;
- последний end равен `AFTER(last_sequence_number)`;
- start/end каждого segment относятся к существующим canonical events;
- соседние segments разделены одним state-changing event: предыдущий заканчивается на
  `BEFORE(n)`, следующий начинается на `AFTER(n)`;
- каждый `BEFORE/AFTER` boundary от первого до последнего события покрыт ровно одним
  segment;
- valid side содержит ровно пять уникальных IDs, invalid side — пустой array и safe reason;
- combined result `valid`, только если все segments имеют две valid sides.

Phase 5 lookup для possession end boundary выбирает единственный segment, включительно
покрывающий boundary. Если ни одного либо более одного — dataset build fail-closed. Phase 4
не реализует сам join, но tests фиксируют lookup semantics как public contract.

Одинаковый `sequence_number` имеет смысл только внутри одной canonical PBP revision. Перед
любым possession-to-lineup lookup consumer обязан разрешить оба current pointers через
PostgreSQL и потребовать точное равенство:

```text
game_id
input_pbp_document_id
input_pbp_sha256
canonical_event_sha256
game_context_sha256
```

Несовпадение любого lineage field запрещает join, даже если обе ClickHouse tables содержат
boundary с тем же номером. Consumer не выбирает snapshot по времени, UUID или `max()`.

## 15. Revision и hashing

Canonical hashes:

```text
box_score_document_sha256
pbp_document_sha256
canonical_player_box_sha256
canonical_event_sha256
game_context_sha256
input_token_sha256
lineup_revision_id
output_sha256
```

`canonical_player_box_sha256` — SHA-256 canonical object
`{"schema":"canonical-lineup-player-box-v1","players":[...]}`. Rows сортируются по
`(team_id, player_id)` и содержат ровно:

```text
game_id, team_id, player_id, position, starter, played, comment
```

Nullable strings кодируются JSON `null`, booleans — только JSON `true|false`; отсутствующие
и пустые strings не отождествляются. Глобальные player names и statistics не входят в эту
projection. Их неизменяемый raw source при необходимости уже покрыт box-score document
checksum.

`canonical_event_sha256` переиспользует без изменений Phase 3 projection/schema
`canonical-pbp-events-v1`, ordering и checksum implementation. `game_context_sha256` также
переиспользует Phase 3 projection. Дублировать или слегка изменять эти helpers запрещено.

`input_token_sha256` покрывает canonical object `lineup-input-token-v1` с полями:

```text
game_id
applied_box_score_document_id, applied_box_score_sha256
applied_pbp_document_id, applied_pbp_sha256
canonical_player_box_sha256, canonical_event_sha256, game_context_sha256
normalizer_version, reconstructor_version, rules_version, output_schema_version
```

`lineup_revision_id` покрывает отдельный `lineup-revision-v1` с теми же content checksums,
canonical hashes и versions, но без applied document UUIDs. Поэтому два immutable documents
с одинаковым содержимым имеют одну semantic revision, но разные operational input tokens.
Изменение starter data, PBP, normalizer, state-machine rules или output schema всегда создаёт
новую revision.

`output_sha256` вычисляется по canonical JSON semantic result: schema/version marker,
revision и ordered segments, включая invalid reason/since state. Attempt UUID, generation,
timestamps и сам
`output_sha256` исключаются, поэтому forced rebuild одинакового результата даёт тот же
output hash.

Canonical JSON использует UTF-8, отсортированные object keys, compact separators, integer
IDs и lowercase enum values. Encoding не содержит BOM/newline/trailing whitespace. Floats в
Phase 4 output отсутствуют. Unit tests фиксируют exact projection keys, row order, explicit
nulls, canonical bytes и известные SHA-256 vectors для каждого нового schema tag.

## 16. PostgreSQL control plane

Alembic migration добавляет независимые таблицы:

### 16.1 `lineup_rebuild_runs`

Повторяет безопасный run contract Phase 3 и добавляет счётчик `invalid`:

```text
run_id, selectors, mode, forced_rebuild
status: running | succeeded | partial | failed | interrupted
discovered, materialized, invalid, skipped_current, verified, failed, busy
started_at, finished_at
```

### 16.2 `lineup_snapshot_attempts`

```text
snapshot_id, game_id, generation
lineup_revision_id, input_token_sha256
input_box_score_document_id, input_box_score_sha256
input_pbp_document_id, input_pbp_sha256
canonical_player_box_sha256, canonical_event_sha256, game_context_sha256
normalizer_version, reconstructor_version, rules_version, output_schema_version
expected_row_count, expected_output_sha256, expected_issue_codes, result_status
state: allocated | writing | verified | published | poisoned | superseded
created_by_run_id, created_by_claim_version
insert_started_at, verified_at, published_at, created_at
safe_error_code, safe_error_type
```

`result_status` становится `valid|invalid` при переходе в `writing` и далее неизменен.
`expected_issue_codes` одновременно фиксирует первые 32 уникальных safe
`(team_id, reason, sequence_number, boundary_side)` projections, детерминированно выведенных
из result segments; это resumable metadata, а не независимый источник semantic hash.
Для `allocated` вся expected metadata равна `NULL`; для `writing|verified|published` вся группа
обязательна. `poisoned|superseded` сохраняют состояние точки отказа, поэтому их expected metadata
либо вся `NULL`, либо вся заполнена.
Unique constraints защищают `(game_id, generation)` и `(game_id, snapshot_id)`. Hash,
version, state/timestamp и safe error constraints эквивалентны строгости Phase 3.

### 16.3 `lineup_rebuild_resources`

```text
game_id
publication_state: absent | current | stale | invalid
attempt_count, claim_version, next_generation
last_run_id
active run/claim/snapshot/revision/input tuple + claimed_until
published snapshot/revision/input tuple + published_at
last_issue_codes, last_error_code, last_error_type
created_at, updated_at, completed_at
```

`publication_state='current'` означает свежий опубликованный valid snapshot;
`publication_state='invalid'` — свежий опубликованный snapshot с invalid segments;
`stale` — pointer существует, но input/version token больше не current. Pointer tuple
сохраняется при stale/invalid и остаётся единственным способом адресовать ClickHouse rows.
`last_issue_codes` является bounded safe JSON projection из опубликованных segment states;
authoritative reason/since значения находятся в самих immutable ClickHouse rows.

Claims имеют lease и monotonically increasing `claim_version`; generation и claim counters
ограничены signed BIGINT. Все мутации attempts/resources требуют полного fence tuple.

## 17. ClickHouse schema

`nba.lineup_segments_v1` использует:

```text
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, segment_number)
```

Группы колонок:

```text
# immutable snapshot metadata
snapshot_id UUID
insert_version UInt64
lineup_revision_id FixedString(64)
normalizer_version LowCardinality(String)
reconstructor_version LowCardinality(String)
rules_version LowCardinality(String)
input_box_score_document_id UUID
input_box_score_sha256 FixedString(64)
input_pbp_document_id UUID
input_pbp_sha256 FixedString(64)
canonical_player_box_sha256 FixedString(64)
canonical_event_sha256 FixedString(64)
game_context_sha256 FixedString(64)
output_schema_version LowCardinality(String)
output_sha256 FixedString(64)
result_status Enum8('valid' = 1, 'invalid' = 2)

# game dimensions
game_id FixedString(10)
season FixedString(7)
season_start_year UInt16
season_type Enum8('regular_season' = 1, 'playoffs' = 2)
game_date Date
home_team_id UInt64
away_team_id UInt64

# segment identity and inclusive boundaries
segment_number UInt32
start_sequence_number UInt32
start_boundary_side Enum8('before' = 1, 'after' = 2)
end_sequence_number UInt32
end_boundary_side Enum8('before' = 1, 'after' = 2)
start_period UInt16
end_period UInt16
start_seconds_remaining_period_ms UInt32
end_seconds_remaining_period_ms UInt32

# team states
home_status Enum8('valid' = 1, 'invalid' = 2)
home_player_ids Array(UInt64)
home_invalid_reason LowCardinality(String)
home_invalid_since_sequence_number UInt32
home_invalid_since_boundary_side Enum8('none' = 0, 'before' = 1, 'after' = 2)
away_status Enum8('valid' = 1, 'invalid' = 2)
away_player_ids Array(UInt64)
away_invalid_reason LowCardinality(String)
away_invalid_since_sequence_number UInt32
away_invalid_since_boundary_side Enum8('none' = 0, 'before' = 1, 'after' = 2)
is_valid UInt8
```

Empty string reason означает valid side; invalid side требует allowlisted non-empty reason.
Для valid side `invalid_since_sequence_number=0` и side=`none`; invalid side хранит
положительный sequence и `before|after`. Application, encoder и readback validator
обеспечивают array cardinality/order, status/reason/since и `is_valid` consistency.
ClickHouse не считается источником current identity.

Один snapshot записывается одним ordered `JSONEachRow` block. Один snapshot ID получает не
более одного INSERT request; retry неопределённой записи сначала выполняет exact readback и
никогда не пересылает тот же ID.

## 18. Multi-table ClickHouse migrations

Существующий Phase 3 validator не должен выбирать просто последний migration manifest.
Migration layer становится table-aware:

- каждый manifest объявляет stable schema ID, database и table;
- schema ID и `(database, table)` образуют взаимно-однозначное отображение, но одна таблица
  может иметь несколько ordered additive migration revisions с тем же schema ID;
- loader проверяет ordered migration set и неизменность schema-ID-to-table mapping;
- `validate_schema(client, schema_id=...)` выбирает явно требуемый manifest;
- `build-possessions` продолжает проверять только possession schema ID;
- `build-lineups` проверяет lineup schema ID;
- `migrate-clickhouse` применяет все pending ordered SQL migrations и проверяет latest head
  projection каждой известной таблицы, сохраняя поддержку additive revisions одной таблицы;
- golden schema JSON остаётся точной projection из `system.tables/system.columns`.

Добавление `0002_lineup_segments` не меняет expected projection `possessions_v1`.

## 19. Publication protocol и recovery

Materialize flow для игры:

1. выбрать final game;
2. захватить fenced claim либо возобновить его только в контексте того же `run_id`;
3. прочитать атомарный dual-input snapshot и вычислить revision;
4. вернуть `skip_current`, если published token/revision уже current и нет `--rebuild`;
5. normalizer выдаёт starters/normalized event envelopes либо structural failure;
6. state machine строит valid/invalid result;
7. записать expected row count/output hash и перевести attempt `allocated → writing`;
8. выполнить единственный ClickHouse INSERT;
9. выполнить exact `FINAL` readback по `(game_id, snapshot_id)`;
10. проверить cardinality, keys, metadata, semantic rows и output hash;
11. перевести attempt `writing → verified`;
12. повторно проверить dual-input token и fence;
13. атомарно перевести attempt `verified → published`, обновить pointer и освободить claim.

Exact readback outcomes:

- `exact` — публикация разрешена;
- `absent` до/после подтверждённого transport failure — новый attempt/generation, тот же ID не
  переиспользуется;
- `different` или duplicate logical rows — attempt `poisoned`, pointer не меняется;
- `unavailable` — attempt остаётся `writing`, а live claim принадлежит текущему run;
- expired/stale fence — attempt `superseded`, pointer не меняется.

Resume не пересекает границу service/CLI invocation: каждый новый вызов создаёт новый
`run_id`. Пока lease исходного run жив, другой run получает `BUSY`. После expiry другой run
атомарно переводит старый attempt в `superseded` и создаёт fresh attempt/snapshot ID; он не
продолжает uncertain write и не пересылает прежний snapshot ID. Exact readback того же
`writing` attempt допустим только при повторном входе владельца с тем же `run_id` и fence.

Structural normalizer failure освобождает claim как failed и сохраняет предыдущий pointer.
Domain invalid result проходит обычные write/verify/publish steps и устанавливает resource
publication state `invalid`.

Distributed transaction между PostgreSQL и ClickHouse отсутствует. Orphan snapshots являются
audit-only и не удаляются normal path.

## 20. Verify semantics

`--verify` не materialize-ит, не claims game resource и не меняет publication pointer. Для
каждой выбранной игры он:

1. строит current dual-input token под короткой consistent read transaction;
2. разрешает pointer только через PostgreSQL resource;
3. проверяет pointer revision/token против current inputs и requested versions;
4. читает exact ClickHouse snapshot ID;
5. проверяет rows/hash/status/lineage;
6. возвращает verified либо безопасный stale/missing/different/unavailable code.

Verify run пишет только собственную run audit row/counters. Snapshot/resource state не
«исправляется» чтением.

## 21. Application service и CLI

`LineupBuildService` использует dependency-injected ports:

```text
LineupNormalizer
LineupReconstructor
LineupWorkStore
LineupSnapshotStore
```

Service обрабатывает games с bounded concurrency, не запускает новые claims после отмены,
дожидается/cancel-ит локальные tasks по принятому Phase 3 contract и best-effort завершает
run как interrupted.

`scripts/build_lineups.py` поддерживает:

```text
--season YYYY-YY [--game-id NNNNNNNNNN]
--season-from YYYY-YY --season-to YYYY-YY
--rebuild
--verify
```

`--rebuild` и `--verify` взаимоисключающие. Game ID допустим только с одним season.
Structured summary:

```text
run, status, discovered, materialized, invalid,
skipped_current, verified, failed, busy
```

`invalid` — ортогональный quality counter: число выбранных games, чей свежий current либо
только что опубликованный snapshot содержит invalid segments. Game одновременно учитывается
в своём operational outcome (`materialized`, `skipped_current` или `verified`) и в `invalid`.
Для нормально завершённого run наличие `invalid > 0` делает итоговый status `partial`, даже без storage failures. При отмене status остаётся `interrupted`, а уже наблюдённое значение `invalid` сохраняется.

Exit codes:

- `0`: все выбранные games valid/current/verified, без failed/busy;
- `1`: partial, failed, busy, stale verification либо хотя бы один опубликованный/current
  invalid snapshot;
- `2`: invalid selector/settings/schema;
- `130`: Ctrl+C.

Публикуемый invalid snapshot не считается storage failure, но даёт exit `1`, чтобы качество
данных не было незаметным в automation.

Make targets:

```text
make build-lineups SEASON=2025-26
make build-lineups SEASON=2025-26 GAME_ID=0022500123
make build-lineups SEASON_FROM=2021-22 SEASON_TO=2025-26
make build-lineups SEASON=2025-26 REBUILD=1
make build-lineups SEASON=2025-26 VERIFY=1
```

## 22. Configuration, observability и безопасность

Добавляются только Phase 4 version/settings и безопасные operational limits; connection
settings переиспользуются:

```text
LINEUP_NORMALIZER_VERSION
LINEUP_RECONSTRUCTOR_VERSION
LINEUP_RULES_VERSION
LINEUP_OUTPUT_SCHEMA_VERSION
LINEUP_BUILD_MAX_CONCURRENCY
LINEUP_CLAIM_LEASE_SECONDS
```

Settings валидируются до создания runtime adapters. Versions соответствуют safe version
regex, concurrency/lease имеют bounded positive values.

Structured logs содержат только event name, run/snapshot/game IDs, generation, counters,
state/status и safe error codes/types. Запрещены raw descriptions/payloads, player names,
SQL, response bodies/headers, DSNs, credentials и exception messages.

Новые metrics/endpoints не обязательны. Существующие API startup, health readiness и Redis
contracts не меняются.

## 23. Тестовая стратегия

### 23.1 Domain unit tests

- обязательный synthetic `A B C D E`, затем `A → F`, итоговый set `B C D E F`;
- несколько последовательных substitutions;
- несколько substitutions в один clock с разными sequences;
- home/away independence;
- exact 5 unique players для valid state;
- outgoing absent, incoming present, same player и wrong team;
- invalid от `AFTER(problem_event)` и сохранение valid `BEFORE`;
- invalid initial state с `BEFORE(first_event)`;
- absorbing invalid state и recovery только full valid reset;
- period/overtime carry-forward;
- inclusive segment lookup, adjacency, first/last coverage;
- отказ boundary lookup при несовпадающем possession/lineup PBP lineage;
- deterministic canonical player sorting и hashing;
- bounded `last_issue_codes` projection и validation типов/codes/versions.

### 23.2 NBA normalizer unit/golden tests

- observed title-case/empty-subtype и approved lowercase/`out` forms;
- exact anchored `SUB: incoming FOR outgoing`;
- full name, surname, initial, suffix, apostrophe, hyphen и Unicode normalization;
- duplicate surname/alias ambiguity;
- outgoing raw label/person ID consistency;
- ineligible/DNP incoming;
- malformed/unknown substitution form;
- raw/canonical cardinality, identity, sequence, roster и checksum mismatch;
- explicit starter flag и legacy position starter form;
- exact golden normalized signal JSON/SHA.

### 23.3 Application unit tests

- selection, current skip, forced rebuild и verify;
- valid и publishable invalid results;
- structural normalization failure;
- bounded concurrency и busy claims;
- cancellation/Ctrl+C и cleanup failures;
- no raw description/name/secret leakage in logs and CLI errors.

### 23.4 PostgreSQL integration tests

- schema constraints и Alembic upgrade;
- dual-resource locks в стабильном order;
- box-only, PBP-only и simultaneous freshness changes;
- claim collision, lease expiry, fencing и generation exhaustion;
- allocated/writing/verified/published/poisoned/superseded transitions;
- prior pointer preservation on failure;
- current valid, current invalid и stale pointer resolution;
- verify is non-mutating;
- cleanup изолированного test schema/database.

### 23.5 ClickHouse integration tests

- migration и exact schema projection для обеих tables;
- один ordered block на snapshot и отсутствие resend;
- exact/absent/different/unavailable readback;
- duplicates, wrong metadata, wrong lineup cardinality/order/status/reason/hash;
- partial write poisoning и uncertain-write resume;
- `FINAL` exact snapshot isolation;
- сохранение unrelated possession rows и cleanup только test snapshot IDs.

### 23.6 E2E и live

- fixture game проходит ingestion → lineup build → exact published snapshot;
- повтор без rebuild не добавляет rows;
- rebuild создаёт новую generation при том же semantic output hash;
- malformed fixture публикует ожидаемый invalid segment и CLI exit `1`;
- opt-in live canary делает bounded requests для одной закреплённой final game, проверяет
  starters, substitutions, 5x5 invariants, deterministic output и безопасный teardown;
- обычный suite не требует NBA network.

## 24. Acceptance criteria

1. Для каждого valid segment обе стороны содержат ровно пять уникальных roster player IDs.
2. Synthetic `A→F` и multiple/same-clock substitution tests проходят.
3. Ни normalizer, ни reconstructor не используют future events для предыдущего boundary.
4. Period/overtime boundary без substitution сохраняет lineup.
5. Неоднозначность не создаёт предполагаемый состав; публикуется explicit invalid state.
6. Ошибка одной команды не стирает доказанный valid state другой команды.
7. Raw/canonical mismatch не публикует новый pointer и сохраняет предыдущий.
8. Любое изменение applied box score, PBP, canonical rows или versions делает snapshot stale.
9. Один snapshot ID получает не более одного INSERT; exact readback обязателен до publish.
10. PostgreSQL pointer является единственным способом выбрать current lineup rows.
11. Possession-to-lineup lookup требует равного PBP/game lineage и fail-closed после
    correction, даже если sequence numbers совпадают.
12. `build-possessions` продолжает валидировать и использовать неизменённый
    `possessions_v1` после добавления второй ClickHouse migration.
13. Default/rebuild/verify, exit codes, Ctrl+C и crash recovery документированы и покрыты.
14. Полные non-live pytest, Ruff check/format, strict mypy, migration checks и Docker
    acceptance проходят.
15. Opt-in live canary подтверждает хотя бы одну реальную игру и не оставляет test data.
16. Phase 5+ features, models, API и live-worker behavior не добавлены.

## 25. Implementation sequencing constraints

Implementation plan обязан разделить работу на небольшие TDD-задачи с review checkpoints.
Сначала фиксируются domain contracts/tests, затем NBA normalization, application orchestration,
PostgreSQL control plane, ClickHouse schema/store, CLI/docs и финальная acceptance.

Любое изменение текущих Phase 2/3 contracts выполняется отдельным focused шагом с полным
регрессионным прогоном. Особенно это относится к multi-table ClickHouse migration validator:
до добавления `0002` должны существовать tests, доказывающие, что `build-possessions`
по-прежнему выбирает schema `possessions_v1` явно.

Нельзя ослаблять существующий Phase 3 normalizer для поддержки lineups. Phase 4 читает
applied raw/canonical PBP через собственный adapter и не меняет классификацию substitution
как administrative action для possession parser.

## 26. Явно отложено

- physical possession rows с `home_lineup/away_lineup` — Phase 5 dataset assembly;
- lineup/player rolling features и leakage-safe windows — Phase 5;
- исторический lineup PPP baseline и model features — Phases 6–7;
- replay-time as-of joins — Phase 9;
- HTTP exposure — Phase 10;
- live partial lineup updates/reset transport — Phase 11;
- pair/full-lineup synergy — Phase 12;
- alternate provider profiles beyond a defined adapter/version;
- orphan snapshot garbage collection.
