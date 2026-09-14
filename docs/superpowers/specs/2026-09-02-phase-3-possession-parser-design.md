# NBA Live Totals Prediction Engine — Phase 3 Possession Parser Design

**Дата:** 2026-09-02  
**Статус:** утверждён для implementation plan  
**Предыдущая фаза:** Phase 2 historical ingestion

## 1. Цель

Phase 3 детерминированно восстанавливает аналитические владения завершённых NBA-матчей
из уже сохранённого PlayByPlayV3, записывает воспроизводимые версионные snapshots в
ClickHouse и хранит управление работой, fencing и current-publication pointer в
PostgreSQL.

Единица результата — состояние матча **после завершённого аналитического владения**.
Phase 3 не строит составы, признаки или прогнозы.

## 2. Обязательные решения

1. PostgreSQL остаётся authoritative control plane: входная версия PBP, leases,
   fencing, run/resource status и опубликованный snapshot.
2. ClickHouse хранит immutable analytical snapshots. Он не решает, какой snapshot
   является текущим.
3. State machine provider-neutral и не видит NBA `actionType`, `subType` или
   `description`.
4. Отдельный NBA normalizer преобразует PlayByPlayV3 в закрытый набор семантических
   сигналов. Все provider-specific allowlists/regex находятся только в
   `infrastructure/nba` и имеют явную версию.
5. Порядок задаётся только `sequence_number`. `action_number`, `action_id` и clock не
   используются как уникальная identity или средство сортировки.
6. Входом normalizer служат одновременно:
   - canonical PBP rows из PostgreSQL;
   - immutable `SourceDocument`, на который указывает PBP
     `ingestion_resources.applied_document_id`.
7. Raw и canonical sequences cross-check друг друга. Raw нужен для полей и контекста,
   которые Phase 2 намеренно не переносит в domain, например `shotValue`, team-rebound
   representation и FT administration.
8. Никаких distributed transactions. ClickHouse snapshot сначала записывается и
   проверяется; PostgreSQL pointer публикуется только после этого под fence и повторной
   проверкой input PBP version.
9. Некорректный или неоднозначный complete game не публикует новый snapshot и не
   заменяет последний валидный результат.
10. Phase 3 работает только с завершёнными историческими играми. Partial/live checkpoint
    parsing относится к Phase 11.

## 3. Scope

### 3.1 Входит в Phase 3

- immutable possession-domain contracts;
- PlayByPlayV3 semantic normalizer;
- deterministic analytical-possession state machine;
- invalid-state and warning taxonomy;
- score and final-game reconciliation;
- PostgreSQL run/resource state для possession rebuild;
- versioned ClickHouse possession schema;
- safe crash recovery без cross-database transaction;
- one-game, one-season и range CLI;
- synthetic, fixture, PostgreSQL, ClickHouse, Docker и opt-in live acceptance.

### 3.2 Не входит

- lineup/substitution reconstruction — Phase 4;
- pregame/live/rolling features, pace и PPP — Phase 5;
- baseline/ML models — Phases 6–7;
- Monte Carlo — Phase 8;
- replay/backtest — Phase 9;
- games/predictions API — Phase 10;
- live polling и incremental open-possession state — Phase 11;
- player/lineup synergy — Phase 12+;
- WNBA, G League и исторические rule variants;
- удаление orphan ClickHouse snapshots в normal path.

Существующие `/health` и `/health/ready` не меняются. Новые game или prediction routes
не появляются.

## 4. Архитектурная граница

```text
PostgreSQL applied PBP raw + canonical rows
                    │
                    ▼
NBA semantic normalizer (provider-specific, versioned)
                    │ PossessionSignal[]
                    ▼
pure possession state machine (provider-neutral)
                    │ PossessionSnapshot
                    ▼
ClickHouse immutable write + exact readback verification
                    │
                    ▼
PostgreSQL fenced publication pointer
```

Предлагаемая структура:

```text
src/nba_live_totals/
    domain/
        possessions.py
    application/
        possessions/
            models.py
            ports.py
            service.py
    infrastructure/
        nba/
            possession_normalizer.py
        postgres/
            possession_store.py
        clickhouse/
            possessions.py

clickhouse/
    migrations/
        0001_possessions.sql

scripts/
    migrate_clickhouse.py
    build_possessions.py
```

Domain/application не импортируют `nba_api`, SQLAlchemy, ClickHouse/httpx, FastAPI или
Redis. Не создаётся общий framework для произвольных derived datasets.

## 5. Определение аналитического владения

Для model dataset используется не буквальный shot-clock segment, а аналитическое
владение:

- начинается при первом однозначном контроле команды после boundary;
- продолжается после offensive rebound;
- продолжается после penalty/free-throw procedure, если та же команда сохраняет мяч;
- заканчивается, когда противник получает/получит контроль, после made field goal,
  made final ordinary free throw, turnover/control award, defensive rebound, проигранного
  jump ball или конца периода;
- не создаётся только потому, что после броска на сирене противник формально получил бы
  право на inbound.

Это определение выбрано для будущих possession-level PPP и simulation. Официальное
правило shot clock может считать касание кольца окончанием team possession, но offensive
rebound не увеличивает аналитический possession counter.

## 6. Identity, revisions и boundaries

```text
PossessionKey = (game_id, possession_number)
SnapshotRowKey = (snapshot_id, possession_number)
```

- `possession_number` contiguous и 1-based внутри опубликованного snapshot;
- snapshot целиком заменяет логическую ревизию игры;
- clock, `action_number` и `action_id` не являются identity;
- correction/re-normalization может сдвинуть все последующие possession numbers.

Все identity-хэши используют один byte-level contract `nba-live-totals-cjson-v1`:

```python
json.dumps(
    projection,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
```

В projection разрешены только JSON `object/array/string/exact-int/exact-bool/null`; bool
проверяется до int, float (включая `1.0`), datetime/date/UUID/enum objects и unsupported
types запрещены. UUID — lowercase canonical string, SHA-256 — lowercase hex, enum —
lowercase stable value, дата — `YYYY-MM-DD`, clock — integer milliseconds. Строки не
trim/casefold/NFC-normalize; encoding не содержит BOM, newline или trailing whitespace.
Каждый корневой object содержит `schema`, поэтому изменение projection требует нового schema
tag, а не молчаливого переиспользования старого digest.

`canonical_event_checksum` — SHA-256 object
`{"schema":"canonical-pbp-events-v1","events":[...]}`. Events идут по
`sequence_number`; каждый содержит ровно поля:

```text
game_id, sequence_number, action_number, action_id, period, clock,
seconds_remaining_period_ms, team_id, person_id, action_type, sub_type, description,
home_score, away_score, shot_result, shot_distance, legacy_x, legacy_y, location,
is_field_goal, points_total, video_available
```

Nullable keys всегда присутствуют как `null`; dictionary key order не влияет на bytes,
array order влияет. Provider raw-only поля, включая `shotValue`, туда не добавляются.

`game_context_sha256` — SHA-256 object:

```json
{
  "schema": "possession-game-context-v1",
  "game_id": "0022500123",
  "season": "2025-26",
  "season_type": "regular_season",
  "game_date": "2026-01-02",
  "status": "final",
  "home_team_id": 10,
  "away_team_id": 20,
  "home_score": 110,
  "away_score": 108
}
```

`parse_revision_id` — SHA-256 object
`{"schema":"possession-parse-revision-v1", "game_id":..., "pbp_document_sha256":...,
"canonical_event_sha256":..., "game_context_sha256":..., "normalizer_version":...,
"parser_version":..., "rules_version":..., "output_schema_version":...}`. Document UUID,
snapshot/run/claim IDs и timestamps исключены: два raw documents с одинаковым payload
checksum имеют одинаковую content revision, но разные operational lineage/input tokens.

`output_sha256` — SHA-256 object
`{"schema":"possession-output-v1","parse_revision_id":...,"rows":[...]}`. Rows ordered by
`possession_number` and contain exactly:

```text
game_id, season, season_start_year, season_type, game_date, home_team_id, away_team_id,
possession_number, offense_possession_number, period, offense_team_id, defense_team_id,
start_sequence_number, start_boundary_side, end_sequence_number, end_boundary_side,
start_seconds_remaining_period_ms, end_seconds_remaining_period_ms,
seconds_remaining_game_ms, home_score_before, away_score_before, home_score, away_score,
score_diff, offense_points, defense_points, home_possessions, away_possessions, end_reason,
source_event_count
```

Publication/transport/lineage fields `snapshot_id`, `insert_version`, row-level version/input
hashes, `output_sha256` и timestamps исключены. ClickHouse values сначала восстанавливаются
в typed projection; JSON strings из driver response напрямую не хешируются. Normalizer
signals имеют аналогичный
`normalized-actions-v1` checksum used by golden tests, but it is not a publication identity.

`input_token_sha256` имеет exact root:

```json
{
  "schema": "possession-input-token-v1",
  "game_id": "...",
  "applied_pbp_document_id": "...",
  "applied_pbp_sha256": "...",
  "canonical_event_sha256": "...",
  "game_context_sha256": "...",
  "normalizer_version": "...",
  "parser_version": "...",
  "rules_version": "...",
  "output_schema_version": "..."
}
```

`normalized-actions-v1` root —
`{"schema":"normalized-actions-v1","game_id":...,"actions":[...]}`. Каждый action содержит
ровно поля envelope section 8.2 с именами `game_id`, `sequence_number`,
`provider_action_number`, `provider_action_id`, `period`, `clock_ms`, `actor_team_id`,
`actor_person_id`, `checkpoint_home_score`, `checkpoint_away_score`,
`checkpoint_combined_score`, `consumption`, `signals`; nullable keys присутствуют. Signal —
object с discriminator `signal_type` (lowercase имя vocabulary) и всеми аргументами соответствующего signal из
section 8.2; optional arguments присутствуют как null, enums — lowercase values, tuple — JSON
array. Изменение signal fields требует `normalized-actions-v2` и normalizer version bump.

Общий Unicode vector `{"a":"Café 🏀","b":[true,null,2]}` имеет SHA-256
`e96bb88b7b8b40fdde3b618cf689fe690659af59f599b35875d9eb3ec990fa92`;
показанный выше `possession-game-context-v1` —
`6a31d3483e743e09df8c5fcb1d405d6bcd1a4d0a0ae7476bef0a947ff2d29e34`.
Implementation добавляет fixed projection-specific golden vectors. Python domain,
PostgreSQL metadata and ClickHouse readback verification call the same canonical encoder;
никакая сторона не собирает digest конкатенацией строк.

Boundary задаётся точно:

```python
EventBoundary(sequence_number: int, side: BEFORE | AFTER)
```

Это позволяет одному rebound/turnover event завершить старое владение на `AFTER(event)`,
а новый контроль начать с той же границы без двойного consumption события.

## 7. Входной snapshot

PostgreSQL claim возвращает immutable `GamePbpSnapshot`:

```text
game, season, season_type, final score
home_team_id, away_team_id
game_context_sha256
applied source_document_id + checksum
canonical PlayByPlayEvent[] in sequence order
canonical_event_checksum
raw SourceDocument
```

В одной короткой PostgreSQL transaction store:

1. locks Phase 2 PBP resource;
2. проверяет `status=succeeded` и non-null `applied_document_id`;
3. читает raw document, game metadata и canonical events и вычисляет
   `game_context_sha256`;
4. проверяет contiguous positive `sequence_number` и raw/canonical game identity;
5. locks/claims possession resource в фиксированном lock order;
6. сохраняет candidate input IDs/checksums/context/version и fenced lease.

Snapshot не содержит mutable mappings. Тот же input должен давать byte-identical signals,
result rows и output hash.

## 8. NBA semantic normalizer

### 8.1 Контракт

```python
class PossessionNormalizer(Protocol):
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedGameActions: ...
```

Normalizer обязан:

- перечислять raw actions в provider array order и сопоставлять их canonical
  `sequence_number` один-к-одному;
- exact-match/case-normalize только документированные NBA values;
- cross-check game, period, clock, teams, scores и action IDs с canonical rows;
- локализовать все NBA-specific parsing rules;
- вернуть semantic signals либо bounded safe issues;
- не использовать description как неограниченный natural-language parser.

PlayByPlayV3 не предоставляет отдельные структурированные поля для всех FT/rebound/replay
семантик. Поэтому разрешены только anchored, versioned description patterns для заранее
перечисленных случаев. Неизвестная control-relevant форма — ошибка, а не догадка.

### 8.2 `NormalizedAction` и закрытый semantic vocabulary

Normalizer возвращает ровно один envelope на каждый raw/canonical action:

```text
NormalizedAction
    game_id: GameId
    sequence_number: positive int
    provider_action_number: int                 # annotation, не identity
    provider_action_id: int | null              # annotation, не identity
    period: positive int
    clock_ms: nonnegative int
    actor_team_id: TeamId | null
    actor_person_id: int | null
    checkpoint_home_score: nonnegative int | null
    checkpoint_away_score: nonnegative int | null
    checkpoint_combined_score: nonnegative int | null
    signals: non-empty ordered tuple[Signal, ...]
    consumption: TRANSITION | ANNOTATION | ADMINISTRATIVE | UNSUPPORTED
```

Порядок envelopes — строго `sequence_number`; порядок нескольких signals внутри action
определяет V1 normalization matrix. Event boundary всегда ссылается на envelope, поэтому
одно provider action может атомарно, например, начислить очки и разрешить контроль без
повторного consumption. `signals` не содержат provider strings или descriptions.

```text
PeriodStart
PeriodEnd
FieldGoalAttempt(team, value, made, and_one_candidate)
Rebound(rebounding_team, actor=PLAYER|TEAM, provider_side=OFFENSIVE|DEFENSIVE|UNKNOWN,
        disposition=NORMAL_LIVE|FT_PLACEHOLDER|RETAIN_TEAM|AWARD_OPPONENT)
Turnover(committing_team, gained_by?, kind)
StealAnnotation(team, related_sequence_number?)
Foul(committing_team, kind, shooting_team?, related_sequence_number?)
FreeThrowTripStart(trip_id, shooting_team, kind, attempts, resume_disposition,
                   resume_team?, interrupted_trip_id?)
FreeThrowAttempt(trip_id, attempt, total, made, awarded_points)
FreeThrowTripEnd(trip_id, resolution, resume_team?)
JumpBall(winner_team?)
Violation(team?, disposition)
Goaltending(team, offensive|defensive, awarded_points)
ControlAward(team, reason)
Administrative(kind)
Unsupported(control_relevant)
```

`Rebound` намеренно не содержит `offensive|defensive`. Normalizer валидирует только команду
и structured disposition; pure parser сравнивает `rebounding_team` с текущим offense и тем
самым без циклической зависимости классифицирует normal live rebound. Dead-ball team rebound
обязан иметь `FT_PLACEHOLDER` внутри незавершённого FT trip либо explicit
`RETAIN_TEAM`/`AWARD_OPPONENT`; иначе action unsupported. `provider_side` служит только
consistency check и не определяет transition.

`resume_disposition`:

```text
OPPONENT_INBOUND
LIVE_REBOUND
RETAIN_TEAM(team_id)
RESUME_INTERRUPTED
JUMP_BALL
```

State machine никогда не выводит disposition из слов `technical`, `flagrant` или
`clear path`; это обязанность NBA normalizer.

### 8.3 Provider-specific V1 rules

- made/missed FG определяется структурированными `isFieldGoal`, `shotResult` и raw
  `shotValue`; score checkpoint служит reconciliation, а не классификатором события;
- `scoreHome/scoreAway` — cumulative team-score checkpoints; `pointsTotal` — combined
  cumulative game score **только на scoring action** и zero sentinel на non-scoring action;
  он не означает shot value и не заменяет `shotValue`;
- FT attempt/total берутся из anchored `subType` form;
- FT make/miss определяется allowlisted structured form и reconciles с exact score delta;
- rebound team определяется по validated team/person representation и game teams;
  `offensive|defensive` вычисляет только state machine относительно active owner;
- supplementary steal с повторным provider action number annotates turnover и не создаёт
  вторую смену контроля;
- jump-ball winner может быть разрешён следующим explicit control event; parser не
  пытается угадать его по jumper `team_id`;
- substitution, timeout и ordinary foul являются administrative context и сами по себе
  не завершают владение;
- Instant Replay является administrative no-op, если effective score/control stream уже
  согласован; неявная retroactive correction без target приводит к invalid result.

Полная нормативная matrix exact values и anchored patterns находится в Appendix A. Любая
реализация V1 обязана использовать её как data table; добавление provider form меняет
`normalizer_version` и требует golden fixture, а не локального regex guess.

## 9. Domain model

Минимальные frozen types:

```text
GameContext
EventBoundary
PossessionSignal (closed union)
Possession
PossessionIssue
PossessionParseResult
```

`Possession` содержит:

```text
game_id
possession_number
offense_team_id
defense_team_id
offense_possession_number
period
start_boundary
end_boundary
start/end period clock milliseconds
seconds_remaining_game_ms
home/away score before
home/away score after
offense_points
defense_points
end_reason
source event range/count
```

Не содержит players-on-court, lineup arrays, pace, PPP, ratings, rolling windows или
labels.

`PossessionParseResult` содержит revision/version lineage, ordered possessions, bounded
issues и canonical output checksum. Для Phase 3 valid complete game не имеет open
possession. Partial input возвращает `INVALID/incomplete_play_by_play`, а не resumable
checkpoint.

## 10. State machine

Состояния:

| State | Смысл |
|---|---|
| `WAITING_FOR_CONTROL` | Нет активного владения; возможен entitlement hint. |
| `CONTROLLED` | Есть offense/defense и одно активное владение. |
| `AWAITING_REBOUND` | Missed FG/final live FT; владелец ещё не сменился. |
| `FT_PROCEDURE` | Dead-ball FT trip временно приостановил underlying control. |
| `JUMP_PENDING` | Контроль неизвестен до winner/control award. |
| `COMPLETE` | Final period закрыт, новые transitions запрещены. |

FT procedures поддерживают stack: technical FT может временно прервать ordinary trip и
после завершения восстановить его state. `trip_id` детерминирован:
`ft:<game_id>:<first_attempt_sequence_number>:<stack_depth>`. `FreeThrowTripStart` открывает ровно один trip,
все attempts обязаны ссылаться на top-of-stack, `FreeThrowTripEnd` закрывает его; вложенный
trip имеет `interrupted_trip_id` top-of-stack до push. Одинаковая sequence не может открыть
два trip одного depth.

### 10.1 Ownership

- offense/defense — две разные команды текущей игры;
- foul team не считается автоматически offense;
- technical FT shooter не меняет ownership;
- новый период не наследует offense предыдущего;
- первый unambiguous shot/turnover может определить offense с warning
  `offense_inferred`;
- неоднозначный старт не угадывается.

### 10.2 Field goals и rebounds

- made FG завершает владение `made_field_goal`, кроме явно связанного and-one;
- miss переводит в `AWAITING_REBOUND` и сам владение не завершает;
- offensive player/team rebound продолжает то же владение;
- defensive player/team rebound завершает владение `defensive_rebound` и устанавливает
  opponent control на `AFTER(rebound)`;
- dead-ball team rebound следует explicit control disposition; неоднозначный owner —
  invalid.

Team rebound с unknown award после missed shot остаётся pending до explicit control. Если
следующий effective signal — `PeriodEnd`, active shooting possession закрывается только
`period_end`; rebound считается annotation, opponent possession не создаётся. Team rebound
без связанного missed FG/final FT либо без последующего control/period end invalid.

And-one linkage допустим только между made FG с `and_one_candidate=true` и ближайшим
ordinary one-attempt trip той же shooting team до любого control-changing action. Link
хранит `related_sequence_number`; отсутствие или два кандидата — invalid, совпадение clock
само по себе недостаточно.

### 10.3 Turnovers, violations, steals

- turnover/offensive violation завершает владение ровно один раз;
- linked steal — annotation, не дополнительный switch;
- unlinked steal меняет контроль только при explicit `gained_control`;
- defensive violations с retained offense не завершают владение;
- offensive basket interference/goaltending следует turnover/control-award semantics;
- defensive goaltending с awarded score следует made-FG semantics.

### 10.4 Jump balls

- winner=current offense: владение продолжается;
- winner=opponent: завершение `jump_ball_change`;
- неизвестный winner: `JUMP_PENDING` до следующего однозначного control event;
- re-jump не создаёт владение;
- unresolved jump в complete game — invalid.

Если action, следующий за `JUMP_PENDING`, одновременно даёт explicit owner и содержит
shot/turnover, parser сначала разрешает pending jump на `BEFORE(action)`, затем применяет
основной signal этого же envelope. Это один consumption с двумя упорядоченными transitions,
не phantom possession. Иной multi-transition action обязан быть прямо разрешён Appendix A.

### 10.5 Free throws

- ordinary shooting/bonus: только final resolution может завершить владение;
- `FreeThrowTripEnd.resolution` задаётся normalizer matrix и обязан согласоваться с
  `resume_disposition` и последним effective attempt;
- final make + `OPPONENT_INBOUND`: `final_free_throw`;
- final miss + `LIVE_REBOUND`: `AWAITING_REBOUND`;
- `RETAIN_TEAM`/`RESUME_INTERRUPTED` никогда не выводятся parser-ом из результата броска;
- and-one остаётся одним владением вместе с made FG;
- technical FT начисляет score, сохраняет interrupted ownership и не завершает
  владение;
- flagrant/clear-path следует explicit retained-team disposition;
- defensive lane violation/retry voids miss и не создаёт boundary;
- offensive violation/forfeited final attempt следует explicit opponent award;
- double violation следует explicit retry/jump/control disposition;
- неизвестная trip length/kind/disposition — invalid, без description guess.

`RESUME_INTERRUPTED` восстанавливает полный suspended frame: state, active possession/owner,
pending rebound/jump, entitlement hint и parent FT trip. Для technical FT «между
владениями» suspended frame может быть `WAITING_FOR_CONTROL` с opponent-inbound entitlement
после make либо `JUMP_PENDING`/opening `WAITING_FOR_CONTROL` без active possession. Очки тогда
обновляют game-score baseline, новый possession не создаётся; после trip восстанавливается
тот же frame. Отсутствие самого suspended frame невозможно — normalizer всегда открывает
technical trip поверх текущего state; conflicting explicit control во время trip invalid.

### 10.6 Periods и overtime

- period end закрывает активное/pending владение `period_end`;
- после buzzer make/miss не создаётся phantom opponent possession;
- missing `PeriodEnd` можно synthesize только перед первым action периода `p+1`, когда
  последний action периода `p` имеет clock `0`, новый period ровно `p+1`, а между ними нет
  unresolved FT/rebound/jump; synthetic boundary — `AFTER(last action p)`. Missing final
  period end не synthesizes;
- periods `>=5` — overtime без basketball-rule cap; storage contract принимает
  `1..65535`, большее значение invalid;
- clock tie нормален; clock increase — warning, но ordering остаётся sequence-based.

## 11. Score и time reconciliation

Score state начинается с `0-0`, обновляется semantic scoring effects и сверяется с
cumulative non-null `scoreHome/scoreAway` checkpoints. `pointsTotal` имеет provider-specific
dual form: `0` sentinel у non-scoring action; у scoring action — cumulative
`home_score + away_score` **после** action. Очки самого события всегда берутся из
`shotValue`/FT/goaltending effect и score delta, не из `pointsTotal`.

Для valid complete game:

- effective score не уменьшается;
- каждый delta объяснён ровно одним signal;
- scoring `pointsTotal == home_score + away_score`, non-scoring `pointsTotal == 0`;
- последний score совпадает с final `games.home_score/away_score`;
- offense/defense points каждой строки совпадают с score before/after;
- unexplained regression/delta делает snapshot invalid.

Score attribution задаётся active boundaries, а не типом штрафного. Scoring signal между
start/end active possession попадает в его delta; технический штрафной команды defense
поэтому может дать `defense_points > 0`. Opponent-inbound entitlement после made FG — только
hint в `WAITING_FOR_CONTROL`, не start нового владения. Dead-ball technical в этом состоянии,
как и administrative score до opening control, накапливается в `pending_baseline_delta`, уже
входит в `score_before` следующего possession и не fabricates possession points.
Если period/game заканчивается до следующего possession, pending nonzero baseline невозможно
представить текущей row schema и complete parse fail closed с
`unattributed_administrative_score`; scoring вообще без active state/entitlement также invalid.

`seconds_remaining_game_ms`:

- periods 1–4: remaining current quarter + remaining regulation quarters;
- period >=5: remaining time только текущего overtime;
- будущий overtime никогда не добавляется задним числом, чтобы не создавать leakage.

## 12. Issues и fail-closed policy

```text
severity = warning | error
code = bounded snake_case
sequence_number = optional positive integer
related_sequence_number = optional positive integer
```

Issues не содержат raw description, payload, SQL, response body или exception message.

Fatal/error examples:

```text
mixed_game
sequence_gap
raw_canonical_mismatch
invalid_game_teams
decreasing_period
unknown_control_action
unknown_owner
conflicting_control
unresolved_rebound
unresolved_free_throw_trip
unresolved_jump_ball
invalid_score_delta
score_regression
final_score_mismatch
missing_final_period_end
incomplete_play_by_play
```

Warnings:

```text
duplicate_provider_action
clock_tie
clock_increase
offense_inferred
missing_score_checkpoint
synthesized_period_boundary
unsupported_administrative_action
orphan_steal_annotation
```

Unknown administrative no-op может быть warning. Unknown control/scoring-relevant action
всегда error. Invalid parse не записывает/publishes новый ClickHouse snapshot.

## 13. Invariants

1. Possession numbers contiguous и 1-based.
2. У каждого possession ровно один offense и другая game team как defense.
3. Boundaries монотонны по sequence; clock не сортирует события.
4. Каждое effective event consumed ровно один раз как transition, annotation, no-op или
   issue.
5. Offensive rebound не увеличивает possession count.
6. Defensive rebound/turnover/control change переключает offense ровно один раз.
7. Одновременно существует не более одного active possession.
8. Complete valid game не оставляет pending rebound/FT/jump/control state.
9. Score полностью reconciled с final game.
10. Одинаковый input и versions создают identical revision ID, rows и hash.
11. Invalid spans никогда не преобразуются в fabricated valid possessions.

## 14. Application ports

```python
class PossessionWorkStore(Protocol):
    async def start_run(...) -> UUID: ...
    async def select_games(...) -> tuple[GameId, ...]: ...
    async def claim_game(...) -> ClaimDecision: ...
    async def publish_snapshot(...) -> None: ...
    async def fail_game(...) -> None: ...
    async def finish_run(...) -> None: ...

class PossessionSnapshotStore(Protocol):
    async def write_snapshot(...) -> None: ...
    async def verify_snapshot(...) -> SnapshotVerification: ...
    async def read_snapshot(...) -> tuple[Possession, ...]: ...
```

Normalizer и parser остаются synchronous pure CPU components. I/O orchestration — async.

## 15. PostgreSQL control plane

Alembic `0003_possession_reconstruction` добавляет:

### 15.1 `possession_rebuild_runs`

```text
run_id UUID PK
selectors JSONB
mode materialize|verify
forced_rebuild bool
status running|succeeded|partial|failed|interrupted
discovered/materialized/skipped_current/verified/failed/busy nonnegative counts
started_at/finished_at UTC
```

### 15.2 `possession_rebuild_resources`

```text
game_id PK/FK games
publication_state absent|current|stale|invalid
attempt_count BIGINT DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 9223372036854775807)
claim_version BIGINT DEFAULT 0 CHECK (claim_version BETWEEN 0 AND 9223372036854775807)
last_run_id FK
next_generation BIGINT DEFAULT 1
    CHECK (next_generation BETWEEN 1 AND 9223372036854775807)

active_run_id/active_claim_version/active_snapshot_id nullable
active_parse_revision_id/active_input_token_sha256 nullable
claimed_until nullable

published_snapshot_id nullable
published_parse_revision_id nullable
published_input_token_sha256 nullable
published_at nullable

last_issue_codes JSONB (bounded safe codes/sequence refs only)
last_error_code/last_error_type
created_at/updated_at/completed_at
```

Initial row: `publication_state=absent`, counters/`claim_version=0`, `next_generation=1`,
active/published/error fields null. `active_*` вместе с `claimed_until` либо все non-null,
либо все null; active claim positive и равен durable `claim_version`. `current` требует
полного `published_*` набора; `absent` требует null pointer, а `stale|invalid` могут сохранять
старый published pointer только для audit. `last_issue_codes` — array не более 32 объектов
`{code, sequence_number?, related_sequence_number?}` без description.
`last_error_code/type` проходят тот же bounded safe-identifier validator, что Phase 2.

Unsuffixed versions не хранятся: candidate versions входят в active parse revision/input
token и snapshot-attempt row, published versions — в referenced published attempt. Поэтому
failed candidate никогда не перезаписывает metadata предыдущей публикации.

### 15.3 `possession_snapshot_attempts`

```text
snapshot_id UUID PK
game_id FK games
generation BIGINT CHECK (generation BETWEEN 1 AND 9223372036854775807)
UNIQUE(game_id, snapshot_id), UNIQUE(game_id, generation)
parse_revision_id CHAR(64)
input_token_sha256 CHAR(64)
input_pbp_document_id FK source_documents
input_pbp_sha256/input_event_sha256/game_context_sha256 CHAR(64)
normalizer_version/parser_version/rules_version/output_schema_version
expected_row_count nullable
expected_output_sha256 nullable
state allocated|writing|verified|published|poisoned|superseded
created_by_run_id/created_by_claim_version
insert_started_at/verified_at/published_at nullable
safe_error_code/safe_error_type nullable
created_at
```

`snapshot_id` — random UUIDv4 materialization-attempt identity; `parse_revision_id` —
deterministic content identity. Новый claim, который может отправить новый INSERT, выделяет
новый `snapshot_id` и строго возрастающую per-game `generation`; `insert_version` в
ClickHouse равен generation. Forced `--rebuild` поэтому имеет новый snapshot/generation, но
при одинаковом input те же parse revision, analytical rows и output hash. Default run
идентичный опубликованный revision только skip-ит.

Resource содержит deferrable composite FKs
`(game_id, active_snapshot_id)` и `(game_id, published_snapshot_id)` → attempt
`(game_id, snapshot_id)`; run/document FKs также реальные PostgreSQL FKs. Circular
claim insert/update выполняется одной transaction с `DEFERRABLE INITIALLY DEFERRED`, поэтому
application-only referential integrity не требуется.

`allocated` требует null expected hash/count/timestamps; `writing` требует positive expected
count, expected hash и `insert_started_at`; `verified|published` дополнительно требуют
`verified_at`; только `published` имеет `published_at`; `poisoned|superseded` никогда не имеют
`published_at`. Hash columns имеют lowercase-hex CHECK, version/error fields — safe identifier
CHECK. `expected_row_count` positive: complete valid game без владений не публикуется.

### 15.4 Input token, states и lock order

`input_token_sha256` — canonical hash object
`possession-input-token-v1` из PBP document ID/checksum, canonical-event checksum,
`game_context_sha256` и normalizer/parser/rules/output-schema versions. Phase 2 не обязан
знать о Phase 3. Freshness вычисляется сравнением token при selection, claim, publication и
current read; изменившийся PBP **или game context** немедленно делает старый pointer stale
для consumers даже без eager invalidation.

Persisted `publication_state=current` — cached control metadata, не разрешение читать без
token comparison. При mismatch effective state всегда stale, даже если `--verify` по своему
read-only contract не обновляет cached resource row.

Каждая claim/publish/current-resolution transaction использует одинаковый lock order:

1. Phase 2 PBP `ingestion_resources` row `FOR UPDATE`;
2. `games` row `FOR SHARE`;
3. `possession_rebuild_resources` row `FOR UPDATE` (после idempotent insert-if-missing);
4. соответствующий `possession_snapshot_attempts` row;
5. run row, только если этой transaction нужны counters/finalization.

Phase 2 PBP resource должен быть `succeeded` с applied document, game — `final`. Под этими
locks читаются raw/canonical PBP и game context. `games.updated_at` не используется как
fence. Publication повторно строит полный token и требует равенство candidate token плюс
fence `(active_run_id, active_claim_version, active_snapshot_id)`.

Под resource lock новый claim выполняет checked signed-64-bit increments:
`claim_version += 1`, `generation = next_generation`, `next_generation += 1`,
`attempt_count += 1`; затем копирует claim version в active/attempt fields. Достижение max
BIGINT — explicit exhausted error. Resume внутри ещё действующего lease не increment-ит
ничего. Exact-expiry reclaim сначала supersedes old attempt, затем всегда выполняет эти
increments и получает новый snapshot ID; «diagnostic claim» без durable fence не существует.

Lifecycle:

| Событие | Resource | Attempt |
|---|---|---|
| claim | active lease заполнен | `allocated` |
| перед INSERT | без изменения | `writing`, expected count/hash сохранены |
| exact readback | без изменения | `verified` |
| fenced publish | pointer/token atomically заменены, lease очищен, `current` | `published` |
| invalid parse до INSERT | lease очищен; fresh old pointer остаётся current, stale old pointer становится `invalid` | `poisoned` с safe issue |
| input/token изменился | lease очищен; старый pointer не current | `superseded` |
| uncertain write | lease сохраняет attempt до диагностики | `writing` |

После отправки INSERT тот же snapshot ID никогда повторно не вставляется. Пока исходный claim
и lease действуют, его resume сначала делает ordered `FINAL` readback: exact result →
`verified`; zero/partial/different rows → `poisoned`; недоступный ClickHouse оставляет
`writing` и run завершается partial. Lease reclaim никогда не усыновляет и не публикует ID
старого claim: old attempt становится `superseded` (его late write остаётся orphan), новый
claim получает новый ID/generation. Это закрывает и stale late write, и случай, когда retry
даёт меньше rows и старые sorting keys иначе пережили бы `ReplacingMergeTree`.

Current read — двухфазный optimistic protocol: (1) PostgreSQL разрешает published pointer и
fresh input token; (2) ClickHouse читает exact ordered rows/hash; (3) PostgreSQL повторно
разрешает pointer/token и требует их равенство исходным. При изменении reader повторяет или
возвращает stale, но никогда не выдаёт rows под новым token. Это as-of-final-check
consistency, а не бессрочная linearizable lease после возврата результата.

## 16. ClickHouse analytical plane

DDL `clickhouse/migrations/0001_possessions.sql` создаёт `nba.possessions_v1`:

```sql
CREATE TABLE IF NOT EXISTS nba.possessions_v1
(
    snapshot_id UUID,
    insert_version UInt64,
    parse_revision_id FixedString(64),
    normalizer_version LowCardinality(String),
    parser_version LowCardinality(String),
    rules_version LowCardinality(String),
    input_pbp_document_id UUID,
    input_pbp_sha256 FixedString(64),
    input_event_sha256 FixedString(64),
    game_context_sha256 FixedString(64),
    output_schema_version LowCardinality(String),
    output_sha256 FixedString(64),

    game_id FixedString(10),
    season FixedString(7),
    season_start_year UInt16,
    season_type Enum8('regular_season' = 1, 'playoffs' = 2),
    game_date Date,
    home_team_id UInt64,
    away_team_id UInt64,

    possession_number UInt16,
    offense_possession_number UInt16,
    period UInt16,
    offense_team_id UInt64,
    defense_team_id UInt64,

    start_sequence_number UInt32,
    start_boundary_side Enum8('before' = 1, 'after' = 2),
    end_sequence_number UInt32,
    end_boundary_side Enum8('before' = 1, 'after' = 2),
    start_seconds_remaining_period_ms UInt32,
    end_seconds_remaining_period_ms UInt32,
    seconds_remaining_game_ms UInt32,

    home_score_before UInt16,
    away_score_before UInt16,
    home_score UInt16,
    away_score UInt16,
    score_diff Int16,
    offense_points UInt16,
    defense_points UInt16,
    home_possessions UInt16,
    away_possessions UInt16,

    end_reason Enum8(
        'made_field_goal' = 1,
        'defensive_rebound' = 2,
        'turnover' = 3,
        'final_free_throw' = 4,
        'period_end' = 5,
        'jump_ball_change' = 6,
        'control_award' = 7
    ),
    source_event_count UInt16
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, possession_number);
```

Правила:

- никаких lineup arrays, feature/model columns или nullable domain values;
- один game snapshot записывается одним ordered `JSONEachRow` block;
- `insert_version == generation`; один snapshot ID получает не более одного INSERT request;
  `ReplacingMergeTree` защищает только от transport-level exact duplicate, а не выбирает
  latest game snapshot;
- все строки snapshot лежат в одной season partition;
- correct read всегда фильтрует по PostgreSQL-published `snapshot_id`, использует `FINAL`
  и `ORDER BY possession_number`; известные published `game_id` и `season_start_year` также
  обязательны в `WHERE` для sorting-key/partition pruning;
- raw `max(snapshot_id)` или «последняя строка в ClickHouse» не является publication;
- old/orphan snapshots сохраняются для audit; normal path не выполняет mutations или
  `OPTIMIZE ... FINAL`.

`home_score`, `away_score`, `score_diff`, `offense_possession_number`, `home_possessions`,
`away_possessions` и `seconds_remaining_game_ms` описывают состояние **after-possession**.
`offense_points` и
`defense_points` — delta score before→after относительно команд этой строки. Маркер
`is_final_game_possession` не хранится: он однозначно выводится из max possession number и
не может случайно попасть в Phase 5 features.

Exact derived semantics:

- `score_diff = home_score - away_score`;
- `home_possessions/away_possessions` — 1-based cumulative count строк, где соответствующая
  команда была offense, включая текущую строку;
- `offense_possession_number` равен соответствующему home/away counter текущей offense;
- boundary order: `BEFORE(n) < AFTER(n) < BEFORE(n+1)`;
- `source_event_count` — число envelopes, для которых
  `start_boundary <= BEFORE(sequence)` и `AFTER(sequence) <= end_boundary`; следовательно,
  start `AFTER(n)` исключает event `n`, end `AFTER(n)` включает его, end `BEFORE(n)` исключает;
- `start_sequence_number/end_sequence_number` — sequence components самих boundaries, даже
  когда boundary side исключает action из count.

### 16.1 Миграции ClickHouse

PostgreSQL `0003` также создаёт ledger:

```text
clickhouse_schema_migrations
    migration_id TEXT PK
    checksum_sha256 CHAR(64)
    expected_schema_fingerprint_sha256 CHAR(64)
    state applying|applied
    applied_at nullable
```

Отдельный `scripts.migrate_clickhouse` требует уже применённый Alembic `0003` и:

1. получает session-scoped PostgreSQL advisory lock на весь bootstrap;
2. проверяет ledger checksum; mismatch существующего migration ID — fatal drift;
3. помечает migration `applying`;
4. применяет ordered idempotent DDL: `CREATE DATABASE IF NOT EXISTS nba`, `CREATE TABLE IF
   NOT EXISTS` и только additive `ALTER ... IF NOT EXISTS` в будущих migrations;
5. вычисляет fingerprint из `system.tables/system.columns` (типы, order/partition keys,
   engine) и требует exact expected value;
6. transactionally помечает migration `applied` и сохраняет fingerprint;
7. освобождает lock.

Crash после DDL до ledger mark безопасен: rerun повторяет idempotent DDL и verification.
Даже `applied` ledger на каждом запуске сверяется с фактической схемой; missing/drifted
table — schema error. ClickHouse migrations forward-only: автоматического downgrade нет,
rollback требует новой forward repair migration и явно документированной operator action.

`expected_schema_fingerprint_sha256` использует общий CJSON encoder и exact projection:

```json
{
  "schema": "clickhouse-possession-schema-v1",
  "database": "nba",
  "table": "possessions_v1",
  "table_contract": {
    "engine": "...",
    "engine_full": "...",
    "partition_key": "...",
    "sorting_key": "...",
    "primary_key": "...",
    "sampling_key": "..."
  },
  "columns": [
    {
      "position": 1,
      "name": "snapshot_id",
      "type": "UUID",
      "default_kind": "",
      "default_expression": "",
      "is_in_partition_key": false,
      "is_in_sorting_key": true,
      "is_in_primary_key": true,
      "is_in_sampling_key": false
    }
  ]
}
```

Table values читаются одной строкой `system.tables`; columns — из `system.columns ORDER BY
position`. Empty strings сохраняются, booleans приводятся к exact bool, никакой whitespace
normalization не выполняется. Projection содержит такую запись для **каждой** DDL column;
пример выше сокращён только многоточием table values и одной column row. Expected complete
projection/hash генерируется один раз из pinned Docker ClickHouse version и коммитится рядом
с migration; drift любого включённого поля fatal. Upgrade ClickHouse version требует
повторной явной верификации/fingerprint migration, а не runtime acceptance нового текста.

FastAPI startup не запускает schema migration. Docker image копирует `clickhouse/` и
scripts; readiness продолжает проверять dependency ping, а не schema bootstrap.

## 17. Crash consistency и fencing

Workflow:

1. PG выдаёт claim, фиксирует exact input token и новый attempt ID/generation.
2. Normalizer/parser строит deterministic result.
3. Invalid result вызывает fenced `fail_game`; current pointer не меняется.
4. Expected row count/hash фиксируются до I/O, attempt становится `writing`, valid rows одним
   block вставляются в ClickHouse не более одного раза.
5. Store читает `FINAL WHERE season_start_year=... AND game_id=... AND snapshot_id=...
   ORDER BY possession_number`, проверяет row count, contiguous numbers, boundaries, all-row
   canonical SHA-256 и переводит attempt в `verified`.
6. PG publication повторно locks PBP+possession resources.
7. Candidate PBP document/event checksum **и game-context fingerprint** обязаны всё ещё
   совпадать с current input token.
8. `(active_run_id, active_claim_version, active_snapshot_id)` fence обязан совпадать.
9. Только тогда current snapshot pointer и success metadata меняются atomically.

Crash outcomes:

- до отправки ClickHouse write: PG pointer не изменён; attempt можно безопасно poison и
  выделить новый;
- timeout/обрыв после отправки: тот же ID не reinsert-ится; ordered readback классифицирует
  его как verified/poisoned либо оставляет writing до восстановления ClickHouse;
- после ClickHouse write до PG publish: snapshot orphan, но недоступен как current;
- после PG publish до client response: rerun проверяет published result и skip;
- stale worker может создать orphan/duplicate rows, но не может publish;
- PBP или game-context change во время build делает publication stale и требует новый
  rebuild.

## 18. CLI

```text
python -m scripts.build_possessions --season 2025-26
python -m scripts.build_possessions --season 2025-26 --game-id 0022500123
python -m scripts.build_possessions --season-from 2021-22 --season-to 2025-26
python -m scripts.build_possessions --season 2025-26 --rebuild
python -m scripts.build_possessions --season 2025-26 --verify
```

Selectors повторяют Phase 2 validation: ровно один из `--season` либо полного
`--season-from/--season-to`; `--game-id` допустим только вместе с ровно одним `--season`.
`--verify` и `--rebuild` mutually exclusive.

- default: missing/stale/failed/version-mismatched builds, successful current snapshot
  skips;
- `--rebuild`: новый immutable local snapshot из текущего stored PBP без NBA network;
- `--verify`: не меняет ClickHouse rows, publication pointer или resource freshness;
  единственная запись — audit run/counters;
- никакого `--refresh`: network refresh выполняется `backfill_games --refresh`, затем
  отдельно `build_possessions`;
- только final Regular Season/Playoffs games с successful applied PBP;
- Ctrl+C: новые claims прекращаются, active tasks отменяются, run best-effort становится
  `interrupted`, exit `130`;
- materialize accounting: `materialized` только после publish, `skipped_current` только для
  exact fresh revision, invalid/CH failure → `failed`, чужой live lease → `busy`;
- verify accounting: exact current readback → `verified`; missing/stale/invalid/poisoned,
  hash mismatch или unavailable ClickHouse → `failed`; `materialized=0`;
- exit `0` только при `failed=busy=0`, `1` при partial/failed/busy/stale verify, `2` при
  invalid CLI/settings/schema, Ctrl+C — `130`.

## 19. Concurrency и observability

- `POSSESSION_RESOURCE_LEASE_SECONDS=300`, integer `1..86400`;
- `POSSESSION_BUILD_MAX_CONCURRENCY=4`, integer `1..32`;
- `CLICKHOUSE_OPERATION_TIMEOUT_SECONDS=30`, positive finite number;
- `POSSESSION_NORMALIZER_VERSION=nba-pbp-v2`,
  `POSSESSION_PARSER_VERSION=analytical-v1`, `POSSESSION_RULES_VERSION=nba-2025-v1` и
  `POSSESSION_OUTPUT_SCHEMA_VERSION=possessions-v1` — non-empty bounded safe identifiers;
- operation timeout отделён от readiness timeout; lease не переиспользует Phase 2 NBA
  resource lease;
- claim lease начинается после финального lock;
- exact-expiry reclaim разрешён;
- stale finish/fail/publish запрещён fence;
- cancellation никогда не маскирует primary failure;
- one-game snapshot write остаётся одним block; multi-game orchestration может batching
  только без смешения row identities/publication decisions.

Lifespan по-прежнему создаёт readiness ClickHouse client для `/health/ready`. CLI создаёт
отдельный analytical client с operation timeout и закрывает его в `finally`; shared global
client между event loops не вводится.

Structured log allowlist:

```text
event, run_id, game_id, snapshot_id, source_document_id
normalizer_version, parser_version, rules_version
attempt, row_count, duration_ms, status, error_code, error_type
```

Запрещено логировать descriptions, action/subtype arrays, raw payload, SQL, HTTP body,
headers, credentials, proxies и DSNs.

## 20. Test strategy

### 20.1 NBA normalizer unit tests

- exact supported action/subtype/description forms;
- raw/canonical one-to-one sequence cross-check;
- `shotValue`, scoring-only cumulative `pointsTotal`, zero sentinel, team checkpoints и
  score deltas;
- player/team rebounds, supplementary steals и jump-ball lookahead;
- ordinary/and-one/technical/flagrant/clear-path FT context;
- violations/goaltending/replay;
- unknown control-relevant form fails closed;
- provider descriptions never reach domain result/issues.
- exact Appendix A action→envelope golden rows, включая null/zero representations;

Существующий трёхсобытийный PBP fixture остаётся mapper-order fixture. Для Phase 3
создаётся отдельный realistic synthetic corpus.

### 20.2 State-machine unit matrix

| Сценарий | Ожидание |
|---|---|
| Opening jump, miss, offensive rebound, make | Одно владение, end at make. |
| Miss + defensive rebound | End/switch ровно один раз. |
| Offensive/defensive/dead-ball team rebound | Continue/end/explicit award. |
| Turnover + linked steal same clock | Один end, steal annotation. |
| Ordinary 2/3-shot FT | Только final resolution завершает. |
| Final FT miss + offensive/defensive rebound | Continue/end. |
| Made FG + and-one make/miss | Одно владение через trip. |
| Technical FT active/between possessions | Ownership unchanged. |
| Flagrant/clear-path + retained ball | Awarded offense continues. |
| Lane violations | Retry/forfeit/jump according to disposition. |
| Offensive/defensive goaltending | Turnover/awarded make. |
| Jump won same/opposite team and re-jump | Continue/switch/pending. |
| Duplicate action numbers and clock ties | Exact sequence order. |
| Buzzer make/miss | No phantom possession. |
| Period 5+ and multiple OTs | No rule-level OT cap; UInt16 bound validated. |
| Malformed owner/rebound/FT/jump/score | Stable invalid issue. |
| Same input twice | Identical revision/hash/rows. |
| Fixed canonical hash vectors | Identical UTF-8 bytes/digests across domain and readback. |

### 20.3 PostgreSQL integration

- migration/schema contract;
- claim, BUSY, exact-expiry reclaim;
- stable lock order and missing-row race;
- unchanged input/version skip;
- parser-version invalidation;
- PBP changes mid-build prevents publish;
- game score/team/season/date changes mid-build prevents publish;
- two-phase current read detects PBP/game-context race after ClickHouse read;
- stale fence cannot success/fail/publish;
- invalid parse preserves old current snapshot;
- cancellation/finalization semantics.

### 20.4 ClickHouse integration

- idempotent DDL and exact schema/engine keys;
- authenticated JSONEachRow insert;
- retry/duplicate visibility through `FINAL`;
- equal/lower/higher insert-version fixtures и exact-duplicate behavior;
- poisoned fewer-row/different-row attempt rotates snapshot ID/generation;
- exact `ORDER BY possession_number` readback and output hash;
- incomplete/orphan snapshot never selected by PG pointer;
- old snapshots retained after changed build;
- no dependency on background merge or `OPTIMIZE FINAL`.
- migration crash before ledger mark, safe replay, checksum/schema drift и missing database;

### 20.5 E2E and live

Fixture E2E:

```text
Phase 2 stored PBP
    -> normalize
    -> parse
    -> ClickHouse write/verify
    -> PostgreSQL publish
```

Проверяет unchanged rerun with zero writes, parser failure preserving old current,
changed PBP producing a new current snapshot, crash-point resume и `--verify`.

Opt-in live canary использует explicit season/game IDs и пустые isolated PG/ClickHouse:

1. Phase 2 ingests one completed game;
2. Phase 3 builds and reconciles a valid snapshot;
3. second Phase 3 run makes zero NBA calls and skips ClickHouse write;
4. output is read back exactly through the PG pointer;
5. cleanup leaves both stores empty.

Docker acceptance: four services healthy, PostgreSQL and ClickHouse migrations, CLI help,
non-root UID, fixture build/verify и unchanged health routes.

## 21. Acceptance criteria

Phase 3 готова, когда одновременно:

1. Provider strings существуют только в NBA normalizer.
2. Parser является pure deterministic function.
3. Все required fixture cases проверяют exact count, teams, boundaries, reason, scores и
   issues.
4. Duplicate provider actions/clock ties не влияют на ordering/identity.
5. Complete valid games имеют contiguous possessions, no open state и final score match.
6. Unknown possession-relevant semantics fail closed.
7. Same input/version даёт same revision/hash; default persistence skip/idempotent, а explicit
   `--rebuild` создаёт новый attempt identity с тем же content hash.
8. Changed PBP или parser/rules version создаёт новый immutable snapshot.
9. Failed/stale worker не может заменить current pointer.
10. ClickHouse rows доступны как current только через PostgreSQL publication metadata.
11. Crash между stores безопасно возобновляется.
12. CLI selectors, rebuild, verify, interruption и exit codes документированы/проверены.
13. README сообщает Phase 3 status и документирует ClickHouse migration, build/rebuild,
    verify, publication pointer и orphan/poisoned recovery runbook.
14. Phase 1/2 tests, Ruff, format, strict mypy, frozen sync, migrations и Docker gates
    остаются зелёными.
15. Ни lineup, ни features/models/API/live-worker logic не добавлены.

## 22. Явно отложено

- description-only неизвестные FT/foul/correction формы;
- unlinked retroactive scorebook corrections;
- exotic simultaneous/offsetting foul procedures без explicit resumption;
- partial/live incremental parsing и persisted open state;
- in-place mutation опубликованных possessions;
- orphan snapshot GC/TTL;
- ClickHouse materialized feature views;
- box-score formula reconciliation, которое «изобретает» недостающие владения;
- lineup/substitution state;
- pace, PPP, rolling features и leakage policy.

## 23. Appendix A — нормативный NBA PlayByPlayV3 normalizer V1

Appendix описывает **закрытый** контракт `nba-pbp-v1` для Stats PlayByPlayV3 `meta.version=1`.
Он не является утверждением, что provider никогда не пришлёт иных форм: всё неуказанное
сохраняется как `Unsupported`, control/scoring-relevant форма делает parse invalid и может
быть добавлена только новой версией matrix плюс golden fixture.

Baseline corpus pinned к `nba_api v1.11.4` response game `0022400001`:
`docs/nba_api/stats/endpoints/responses/playbyplayv3.json`; его Phase 2 canonical JSON
SHA-256 — `12737b1aae4f880263aedd832a561f9ad0fbbb5addeed6dac6112b3221edf6cc`.
Различающие примеры из него:

| actionNumber | `actionType` | score after | `shotValue` | `pointsTotal` | description meaning |
|---:|---|---|---:|---:|---|
| 21 | `Made Shot` | `0-3` | 3 | 3 | event 3, player total 3, combined game score 3 |
| 23 | `Made Shot` | `3-3` | 3 | 6 | event/player total 3, combined game score 6 |
| 36 | `Made Shot` | `3-5` | 2 | 8 | event/player total 2, combined game score 8 |
| 121 | `Free Throw` | `14-16` | 0 | 30 | event 1, player total 5 in description, combined score 30 |

Поэтому `pointsTotal` semantics подтверждается случаями, где event points, player cumulative
points и combined score различаются. Authoritative team checkpoints всё равно только
`scoreHome/scoreAway`; `pointsTotal` — дополнительная scoring-action consistency check.
Локальный Phase 2 fixture с `actionType=2pt`, `shotValue=3` намеренно synthetic и внутренне
противоречив: он проверяет lossless mapping/order, но не является normalizer golden corpus.

Pinned action inventory: empty annotation, `Foul`, `Free Throw`, `Instant Replay`,
`Jump Ball`, `Made Shot`, `Missed Shot`, `period`, `Rebound`, `Substitution`, `Timeout`,
`Turnover`. Наблюдаемые FT subtypes — ordinary `1 of 1`, `1/2 of 2`, `1/2/3 of 3`;
rebound — `Unknown|Normal Rebound`; jump — empty; replay — `Coach Challenge Support Ruling`,
`Replay Center`, `Support Ruling`. Каждая из этих форм имеет extracted golden action. Compact
FG aliases и редкие special FT/violation forms — отдельные explicit synthetic goldens; release
canary corpus обязан подтвердить их либо fail closed, но не расширять matrix автоматически.

### A.1 Общие raw rules

| Raw поле | V1 normalization |
|---|---|
| `actionType`, `subType` | `strip()` + `casefold()`, затем exact lookup; contains/fuzzy запрещены. |
| `teamId`, `personId` | positive integer; `0|null` → absent, кроме team-rebound rule ниже. |
| `scoreHome`, `scoreAway` | `""|null` → absent; ASCII digit string → nonnegative int; другое invalid. |
| `shotResult` | `""|null` → absent; только exact normalized `Made|Missed`. |
| `shotValue` | `0|null` → absent для non-FG; для FG только `2|3`. |
| `isFieldGoal`, flags | только provider `0|1`/documented boolean representation. |
| `pointsTotal` | nonnegative int; non-scoring `0` → normalized combined checkpoint null; scoring value → `checkpoint_combined_score`. |
| `actionNumber`, `actionId` | audit only; duplicate/null допустимы, ordering не меняют. |
| `description` | только перечисленные ниже `fullmatch`; без substring parsing. |

Raw action array и canonical rows сопоставляются 1:1 по позиции. `gameId`, period, clock,
IDs, scores и все текущие canonical fields обязаны совпасть после уже определённого Phase 2
mapping. Raw-only `shotValue` читается из того же applied `SourceDocument`; другой document
или refetch запрещён.

Каждый action возвращает один `NormalizedAction`; молчаливый drop запрещён. Если один action
порождает несколько signals, порядок фиксирован: `score effect → control annotation →
administrative annotation`. Relationships всегда используют `sequence_number`.

### A.2 Exact action mapping

| Normalized `actionType` | Дополнительные условия | Signal/disposition |
|---|---|---|
| `period` | subtype только `start|end`, neutral IDs | `PeriodStart|PeriodEnd`. |
| `made shot` | `isFieldGoal=1`, result `made`, team — game team, value `2|3` | `FieldGoalAttempt(..., made=true)`; checkpoint delta ровно value. |
| `missed shot` | `isFieldGoal=1`, result `missed`, team — game team, value `2|3` | `FieldGoalAttempt(..., made=false)`; score не меняется. |
| `2pt` | compact alias; `shotValue=2`, result made/missed, team — game team | Тот же FG signal; конфликт value/type invalid. |
| `3pt` | compact alias; `shotValue=3`, result made/missed, team — game team | Тот же FG signal; конфликт value/type invalid. |
| `free throw` | subtype из A.3, `isFieldGoal=0` | Signals из A.3/A.4. |
| `rebound` | subtype `unknown|normal rebound` | Signal из A.5. |
| `turnover` | team — game team; subtype из allowlist ниже | `Turnover`; linked blank-action steal — annotation. |
| `foul` | subtype из allowlist ниже | Administrative FT/control context; сам foul не переключает владение. |
| `violation` | subtype из allowlist ниже | `Violation` с explicit V1 disposition; ambiguous invalid. |
| `jump ball` | subtype empty/`recovered`; description shape fullmatch | `JumpBall`; `teamId` jumper не считается winner без explicit provider field. |
| `instant replay` | subtype `coach challenge support ruling|coach challenge overturn ruling|support ruling|overturn ruling|replay center|review` либо empty | Administrative; unexplained retroactive correction invalid. |
| `timeout` | subtype `regular|short|official` | Administrative. |
| `substitution` | subtype `in|out` либо documented empty | Administrative; lineup не строится. |
| empty | только anchored block/steal description | `Administrative(block)` или `StealAnnotation`; иное unsupported. |

Compact `2pt|3pt` aliases входят только с exact matching `shotValue` и structured
`shotResult`; они не исправляют противоречивый synthetic fixture. Pinned real corpus использует
`Made Shot|Missed Shot`. Phase 3 realistic corpus проверяет обе явно определённые формы.

V1 turnover subtype allowlist:

```text
bad pass, lost ball, traveling, offensive foul turnover, double dribble,
discontinue dribble, 3 second violation, 5 second violation, 8 second violation,
shot clock violation, shot clock, inbound turnover, backcourt turnover, backcourt,
offensive goaltending, lane violation, kicked ball violation, palming,
5 second inbound, step out of bounds, out of bounds lost ball turnover,
out of bounds - bad pass turnover
```

V1 foul subtype allowlist:

```text
personal, shooting, loose ball, offensive, offensive charge, intentional, away from play, clear path,
double technical, technical, flagrant 1, flagrant type 1, flagrant 2, flagrant type 2,
defense 3 second, taunting, excess timeout, charge, block, personal take, shooting block
```

`offensive|charge` требует linked turnover/control award; без него complete sequence invalid.
Остальные foul forms только формируют FT context/administrative signal и сами не означают
конец владения.

V1 violation subtype allowlist:

```text
delay of game, defensive goaltending, lane violation, jump ball violation,
kicked ball, kicked ball violation, double lane violation
```

Kicked ball/delay retains current offense. Defensive goaltending требует exact positive
score delta `2|3` и следует made-FG semantics. Lane/double-lane disposition задаёт A.4;
jump-ball violation требует explicit `ControlAward|JUMP_BALL`, иначе invalid.

Anchored blank-action annotations:

```regex
^(?P<player>[^\r\n]+) STEAL \((?P<count>[0-9]+) STL\)$
^(?P<player>[^\r\n]+) BLOCK \((?P<count>[0-9]+) BLK\)$
```

Steal связывается с ближайшим preceding turnover той же clock cluster и противоположной
team, без пересечения другого control event. Два кандидата или отсутствие explicit turnover
оставляют `orphan_steal_annotation`; steal не создаёт самостоятельный switch.

Jump-ball description допускается только по fullmatch:

```regex
^Jump Ball (?P<jumper_a>[^\r\n:]+) vs\. (?P<jumper_b>[^\r\n:]+): Tip to (?P<recipient>[^\r\n:]+)$
```

Имена не маппятся на команды без structured identity. Winner остаётся null и разрешается
следующим control event; description лишь подтверждает поддержанную форму.

### A.3 Exact free-throw subtype matrix

Ordinary:

```text
Free Throw 1 of 1
Free Throw 1 of 2 | Free Throw 2 of 2
Free Throw 1 of 3 | Free Throw 2 of 3 | Free Throw 3 of 3
```

Special retained/interrupted procedures:

```text
Free Throw Technical
Free Throw Technical 1 of 2 | Free Throw Technical 2 of 2
Free Throw Flagrant 1 of 1
Free Throw Flagrant 1 of 2 | Free Throw Flagrant 2 of 2
Free Throw Flagrant 1 of 3 | Free Throw Flagrant 2 of 3 | Free Throw Flagrant 3 of 3
Free Throw Clear Path 1 of 2 | Free Throw Clear Path 2 of 2
Free Throw Away From Play 1 of 1
Free Throw Transition Take 1 of 1
```

Эти строки — exact lookup table, не открытый regex. `Free Throw Technical` означает attempt
`1/1`. Номер/total обязаны быть `1..3`, начинаться с 1 и двигаться последовательно; повтор
того же ordinal допустим только после explicit lane-violation retry.

Результат определяется только одним из anchored patterns, где `<subtype>` — `re.escape`
exact raw subtype:

```regex
^(?P<player>[^\r\n]+) <subtype> \((?P<player_points>[0-9]+) PTS\)$
^MISS (?P<player>[^\r\n]+) <subtype>$
```

Первый — made, второй — missed. `player_points` является display/cumulative player metadata
и не участвует в game score. `pointsTotal` проверяется по A.1. Не совпавшая description при
FT control-relevant и поэтому invalid.

Первый effective attempt envelope эмитит `TripStart` перед `Attempt`; final effective
attempt — `Attempt` перед `TripEnd`. Synthetic signals используют boundary исходного action,
не получают отдельный sequence number. `trip_id =
ft:<game_id>:<first_attempt_sequence_number>:<stack_depth>`.

### A.4 FT kind, linkage и resume

| Kind | Resume disposition |
|---|---|
| ordinary 2/3-shot | final make → `OPPONENT_INBOUND`; final miss → `LIVE_REBOUND`. |
| ordinary 1-of-1 | только structurally linked and-one; иначе ambiguity error. |
| technical | `RESUME_INTERRUPTED`; ownership до trip сохраняется. |
| flagrant/clear path/away-from-play/transition-take | `RETAIN_TEAM(shooting_team)`. |

And-one требует одновременно: ordinary `1 of 1`; та же shooting team; предыдущий effective
made FG той же period/clock dead-ball cluster; intervening `shooting|block` foul другой
команды; ни одного control-changing action. Link — `related_sequence_number` made FG.
Ноль/два кандидата invalid; clock equality без foul недостаточна.

Nesting разрешён только technical trip: push с parent trip, attempts, pop, resume parent без
изменения ordinal. Новый ordinary/flagrant trip при открытом ordinary trip без explicit
provider relation invalid.

Lane violation rules закрыты:

- defensive lane violation + повтор того же ordinal → предыдущий miss `VOID_RETRY`;
- offensive lane violation на final miss → `OPPONENT_INBOUND` только при explicit control
  award/turnover pair;
- double lane violation → explicit retry либо jump ball;
- отсутствие следующего ожидаемого attempt/control делает trip invalid.

Team rebound между non-final missed FT и повтором/следующим attempt того же trip получает
`FT_PLACEHOLDER` и не влияет на control. На final miss применяется обычный live rebound.

### A.5 Rebound normalization без ownership cycle

```text
player rebound:
    teamId = game team, personId = positive non-team identity
    rebounding_team = teamId, actor = PLAYER

team rebound:
    teamId = 0|null, personId = one of game team IDs
    rebounding_team = personId, actor = TEAM
```

Иная identity invalid. `subType=unknown` не сообщает side. `normal rebound` также не
переключает владение самостоятельно. Если fullmatch player description содержит
`REBOUND (Off:N Def:M)`, delta counters может дать `provider_side` только как consistency
annotation; parser всё равно сравнивает rebound team со своим active offense. Description
team name не используется для team mapping.

```regex
^(?P<player>[^\r\n]+) REBOUND \(Off:(?P<off>[0-9]+) Def:(?P<def>[0-9]+)\)$
^(?P<team_name>[^\r\n]+) Rebound$
```

Live player rebound подразумевает control rebounding team. Team rebound получает
`FT_PLACEHOLDER` по A.4 либо требует следующего explicit control action; normalizer не
угадывает dead-ball award по credit team.

### A.6 Corrections, completeness и matrix evolution

Instant Replay/administrative events сохраняются envelope-ами. Overturn без target sequence,
score regression или delta, не объяснённый effective signals, invalidates result; normalizer
не переставляет и не удаляет более ранние actions. Complete game требует final period marker,
final-score equality и закрытые FT/rebound/jump states.

Каждая строка/ветвь Appendix имеет positive и negative golden fixtures. Перед расширением
allowlist отдельный offline corpus audit перечисляет новые `(actionType, subType)` forms;
изменение проходит новой normalizer version и regression fixtures. Runtime никогда не
самообучает matrix и не пишет raw description в issue/log.

## 23.1 Appendix B — нормативная дельта NBA PlayByPlayV3 normalizer V2

`nba-pbp-v2` сохраняет закрытый контракт V1 и добавляет только две подтверждённые
структурированные формы. Изменение версии обязательно делает snapshots, построенные с
`nba-pbp-v1`, stale; автоматического переиспользования старого normalized output нет.

Blank anchored `BLOCK` может наследовать `shotValue` только от непосредственно
предшествующего missed field goal. Обе записи обязаны иметь одинаковые period, clock,
provider `actionNumber` и `shotValue` (`2|3`), а preceding action обязан быть валидным
missed FG. У block `isFieldGoal=false`, `pointsTotal=0`, пустой `shotResult` и отсутствуют
`scoreHome/scoreAway`. Любой mismatch, made/non-FG predecessor, отсутствие пары либо
scoring/checkpoint data оставляет action control-relevant `Unsupported` и даёт error.
Другие non-FG actions по-прежнему не принимают ненулевой `shotValue`.

Team rebound disposition scan в V2 может переходить на другой clock внутри того же
period. Он пропускает только уже разрешённые безопасные administrative/non-control
actions и использует первую валидную structured control action. Scan останавливается и
fail-closed на period boundary, player rebound, jump ball, неизвестной/невалидной control
форме и любом неоднозначном barrier. Raw description не участвует в выборе команды.

## 24. Источники решений

- Исходное техническое задание: sections 5, 6, 23, 38 и roadmap Phase 3.
- Phase 1 design: modular-monolith boundaries и roadmap.
- Phase 2 design: lossless PBP order, raw versioning и applied-document semantics.
- NBA PlayByPlayV3 documented payload:
  https://github.com/swar/nba_api/blob/v1.11.4/docs/nba_api/stats/endpoints/responses/playbyplayv3.json
- NBA Rule 4 (team possession definitions):
  https://official.nba.com/rule-no-4-definitions/
- NBA Rule 6 (putting ball in play):
  https://official.nba.com/rule-no-6-putting-ball-in-play-live-dead-ball/
- NBA Rule 7 (shot clock/control transitions):
  https://official.nba.com/rule-no-7-24-second-clock/
- NBA Rule 9 (free throws and violations):
  https://official.nba.com/rule-no-9-free-throws-and-penalties/
- NBA Rule 10/12 (violations, fouls, retained control and jump balls):
  https://official.nba.com/rule-no-10-violations-and-penalties/
  https://official.nba.com/rule-no-12-fouls-and-penalties/
- ClickHouse ReplacingMergeTree/`FINAL` operational guidance:
  https://clickhouse.com/resources/engineering/clickhouse-optimize-table-final
- ClickHouse single-block insert atomicity:
  https://clickhouse.com/blog/update-performance-clickhouse-vs-postgresql
