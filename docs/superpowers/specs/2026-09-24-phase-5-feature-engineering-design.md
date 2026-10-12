# Phase 5 — Leakage-Safe Feature Engineering Design

Дата: 2026-09-24  
Статус: implemented; final acceptance commit `cfdf872` (2026-10-04).

После восстановления Windows runtime verification повторена 2026-10-12 на Python 3.14
и свежих isolated Docker stores. Результаты и ограничения:
[recovery verification report](../reports/2026-10-12-phase-5-recovery-verification.md).

## 1. Цель

Phase 5 создаёт воспроизводимый physical dataset для pregame и live-state feature
engineering поверх опубликованных Phase 3 possession snapshots и Phase 4 lineup snapshots.

Результат фазы:

- immutable per-game feature snapshot;
- одна synthetic pregame state и одна state после каждого завершённого владения;
- causal pregame team, historical player, lineup aggregate и live features;
- строгая защита от current/future leakage;
- PostgreSQL control plane и ClickHouse analytical rows;
- offline CLI `scripts.build_dataset` и `make dataset`;
- deterministic versions, lineage, hashing, readback и rebuild semantics.

Phase 5 не обучает модели и не делает prediction.

## 2. Binding decisions

1. Dataset grain — game state. `state_number=0` означает synthetic pregame state;
   `state_number=N>0` соответствует состоянию после `possession_number=N`.
2. Dataset материализуется per game как immutable snapshot. PostgreSQL published pointer —
   единственный способ выбрать current feature snapshot.
3. Phase 3 `nba.possessions_v1` и Phase 4 `nba.lineup_segments_v1` не меняются.
4. Possession-to-lineup join выполняется по possession end boundary. Ноль или больше одного
   covering segment — structural failure и запрет публикации всей игры.
5. Possession и lineup inputs обязаны иметь exact shared PBP lineage.
6. Официально опубликованный invalid lineup не угадывается и не удаляется: state row
   сохраняется с invalid status, пустым lineup и `NULL` player/lineup aggregates.
7. Structural failure отличается от quality-invalid state. Structural failure не публикует
   candidate; quality-invalid snapshot может быть опубликован со статусом `invalid`.
8. Pregame/team/player history использует только final games с
   `history_game.started_at < target.started_at`. Равное время не считается прошлым.
9. Current target game box-score statistics запрещены как feature input. Target box score
   может влиять только транзитивно через уже опубликованный lineup snapshot, где Phase 4
   использует starter/roster identity, а не final statistics.
10. Player history — до 20 предыдущих appearances. Team pregame windows — последние
    5, 10 и 20 eligible games.
11. Неполные early windows используют доступный causal prefix и публикуют sample count.
    При нуле observations значение равно `NULL`.
12. Нулевой знаменатель всегда даёт `NULL`, а не ноль, infinity или imputation.
13. Rates и ratings хранятся fixed-point `Int64` scale `1_000_000`; float не входит в
    domain, storage, canonical JSON или hashes.
14. Pair/full-lineup synergy остаётся Phase 12. Baselines, targets, models, replay, API и
    live polling остаются последующим фазам.
15. Phase 5 не пытается восстановить historical source availability на момент прошлого
    матча. Она использует causal game cutoff и текущие опубликованные corrected sources.
    Availability-aware replay принадлежит Phase 9.

## 3. Не входит в Phase 5

- target columns и remaining-score labels;
- train/validation/test split;
- baseline predictions;
- CatBoost, model artifacts и training CLI;
- Monte Carlo;
- historical replay/backtest;
- prediction API, Redis cache и live worker;
- injury ingestion;
- travel distance и timezone delta;
- team fouls, bonus и timeouts remaining;
- player-pair, five-player lineup и matchup synergy;
- SHAP, embeddings, FM или GNN;
- изменение provider ingestion, possession parsing или lineup reconstruction semantics.

## 4. Рассмотренные архитектуры

### 4.1 Per-game immutable feature snapshots — выбрано

Каждая игра имеет собственные state/player rows, dependency manifest, attempt lifecycle и
published pointer. Архитектура повторяет доказанные Phase 3/4 fencing/readback patterns,
подходит для offline dataset и сохраняет calculator reusable для будущего live worker.

### 4.2 Season-range dataset snapshot — отклонено

Единый snapshot всего диапазона удобен для batch reproducibility, но любое исправление одной
игры заставляет пересобирать большой dataset и плохо переносится на будущий single-game live
consumer.

### 4.3 Query-time ClickHouse views — отклонено

Views уменьшают объём orchestration code, но не дают достаточно сильного published identity,
lineage audit, formula versioning и exact readback. Они также усложняют доказательство causal
cutoff.

## 5. Компоненты

```text
src/nba_live_totals/
    application/features/
        __init__.py
        models.py
        ports.py
        service.py
    domain/
        features.py
        feature_hashing.py
    infrastructure/
        nba/feature_event_normalizer.py
        postgres/feature_store.py
        clickhouse/features.py

alembic/versions/0005_feature_engineering.py
clickhouse/migrations/0003_game_state_features.sql
clickhouse/migrations/0003_game_state_features.schema.json
clickhouse/migrations/0004_player_pregame_features.sql
clickhouse/migrations/0004_player_pregame_features.schema.json
scripts/build_dataset.py
```

Responsibilities:

- `domain/features.py` — pure boundary join, fixed-point arithmetic, rolling windows,
  player/team/live formulas и invariants;
- `domain/feature_hashing.py` — canonical projections, revisions и output hashes;
- `application/features/models.py` — frozen request, source snapshot, dependency, attempt,
  result и summary models;
- `application/features/ports.py` — source resolver, calculator, work store и snapshot store
  protocols;
- `application/features/service.py` — bounded orchestration, cancellation и run accounting;
- `feature_event_normalizer.py` — provider-specific raw/canonical PBP cross-check и
  provider-neutral causal stat events;
- PostgreSQL adapter — selection, atomic source resolution, manifests, fencing и pointers;
- ClickHouse adapter — immutable inserts, exact readback и two-table verification;
- CLI — composition only, без feature/business logic.

## 6. Source contracts

### 6.1 Target inputs

Для target game PostgreSQL resolver в одной consistent transaction получает:

- final game metadata;
- current published possession attempt/reference;
- current published lineup attempt/reference;
- applied raw/canonical PBP required for stat-event normalization;
- resource/attempt fencing fields needed for a later recheck.

Consumer никогда не выбирает ClickHouse snapshot через `max(snapshot_id)`, timestamp,
generation или lexical UUID order.

### 6.2 Shared PBP lineage

Перед любым ClickHouse read должны точно совпасть:

```text
game_id
input_pbp_document_id
input_pbp_sha256
canonical_event_sha256
game_context_sha256
```

Используется общий `PbpLineage` и `require_matching_pbp_lineage`. Несовпадение —
`source_lineage_mismatch`, structural failure.

### 6.3 Exact source read

Possession и lineup adapters читают только PostgreSQL-published snapshot IDs, с `FINAL`,
exact partition/game/snapshot filters, deterministic order, row-count check, lineage check и
output-hash check.

### 6.4 Historical inputs

History resolver выбирает final games строго раньше target `started_at`:

- до 20 последних games для каждой target team;
- до 20 последних appearances для каждого player, встречающегося хотя бы в одном valid
  target lineup segment;
- union выбранных games образует bounded dependency manifest.

Для каждого dependency game manifest фиксирует:

```text
dependency_game_id
started_at
game_context_sha256
applied_box_score_document_id
applied_box_score_sha256
canonical_team_box_sha256
canonical_player_box_sha256
possession_snapshot_id
parse_revision_id
possession_input_token_sha256
possession_output_sha256
```

Historical feature calculation читает только строки, соответствующие этому manifest.
`latest_document_id` запрещён; используется applied identity.

`canonical_team_box_sha256` использует schema `canonical-feature-team-box-v1`, сортирует
rows по numeric `team_id` и включает только formula inputs:

```text
game_id, team_id, seconds_played
field_goals_made, field_goals_attempted
three_pointers_made, three_pointers_attempted
free_throws_made, free_throws_attempted
rebounds_offensive, rebounds_defensive, rebounds_total
assists, turnovers, points
```

`canonical_player_box_sha256` использует schema `canonical-feature-player-box-v1`, сортирует
по `(team_id, player_id)` и включает тот же stat subset плюс `player_id`, `played` и
`seconds_played`. Provider percentages, plus/minus и поля, не участвующие в формулах, в эти
hashes не входят. `NULL` сохраняется как `null`; calculator не заменяет его нулём.

Prior lineup snapshots не используются для player efficiency attribution: end-boundary lineup
не доказывает participation в течение всего владения.

## 7. Boundary model и state grain

Phase 4 boundary order остаётся:

```text
(sequence_number, BEFORE) < (sequence_number, AFTER)
```

Lineup segment имеет inclusive start/end. Для каждой possession state используется:

```python
find_lineup_segment(lineup_segments, possession.end_boundary)
```

Lookup обязан вернуть ровно один segment.

### 7.1 Pregame state

Synthetic state `0`:

```text
state_number = 0
state_kind = pregame
possession_number = 0
period = 0
home_score = 0
away_score = 0
home_possessions = 0
away_possessions = 0
seconds_remaining_game_ms = 2_880_000
seconds_remaining_period_ms = 720_000
boundary = BEFORE(first_sequence_number)
```

Pregame lineup берётся из единственного segment, покрывающего эту boundary. Live features в
state `0` равны `NULL`; pregame/player/lineup features доступны по обычным правилам.

### 7.2 After-possession states

Для каждой possession row создаётся state с тем же `possession_number`, after-possession
score, clocks и possession counters. `state_number` contiguous и равен possession number.

End lineup описывает состояние после boundary. Он не используется, чтобы приписать прошедшее
владение конкретным игрокам.

## 8. Numeric contract

### 8.1 Fixed point

Все ratios/ratings используют:

```text
FEATURE_SCALE = 1_000_000
```

Pure calculator принимает integer counts и возвращает `int | None`. Division выполняет
round-half-away-from-zero. Поведение для отрицательных difference features симметрично.

Canonical JSON содержит scaled integers, а не decimal strings или floats.

### 8.2 Missing values

`NULL` означает только «значение нельзя вычислить из causal inputs»:

- observations отсутствуют;
- denominator равен нулю;
- required player profile component отсутствует;
- lineup side invalid.

Zero остаётся реальным нулём, когда numerator равен нулю, а denominator положителен.

### 8.3 Ordering

- states — по `state_number`;
- player profiles — по numeric `player_id`;
- lineup player IDs — numeric ascending;
- dependency games — по `(started_at, game_id)`;
- issue codes — deterministic causal order, capped at 32.

## 9. Pregame team features

Для `N in {5, 10, 20}` выбираются последние `min(N, available)` team games. Публикуется
`games_used_N`.

```text
off_rating_N = 100 * sum(team_points) / sum(team_possessions)
def_rating_N = 100 * sum(opponent_points) / sum(opponent_possessions)
pace_N = 48min * sum(home_possessions + away_possessions)
         / (2 * sum(game_duration))
```

`game_duration = 48min + 5min * completed_overtime_periods`.

Aggregate-of-counts используется вместо среднего от per-game ratios.

Для каждой стороны также публикуются:

```text
rest_days
back_to_back
is_home
```

`rest_days = max(0, target.game_date - previous.game_date - 1 day)`. Если предыдущей игры
нет, `rest_days=NULL`, `back_to_back=false`. `back_to_back=true`, только если calendar-date
difference равен одному дню.

## 10. Provider-neutral live stat events

NBA adapter cross-check-ит applied raw actions и canonical events так же строго, как Phase 3/4,
но выдаёт только stat deltas:

```text
sequence_number
boundary = AFTER(sequence_number)
team_id
field_goal_attempts
three_point_attempts
free_throw_attempts
turnovers
offensive_rebounds
defensive_rebounds
```

Unsupported/non-stat action даёт zero delta, но сохраняет sequence alignment. Identity,
cardinality, clock или team mismatch — structural failure.

Для state boundary:

- `BEFORE(n)` включает events с sequence `< n`;
- `AFTER(n)` включает events с sequence `<= n`.

Нельзя читать target final team/player box-score counters для live rates.

## 11. Live features

### 11.1 Pace

```text
current_pace = 48min * completed_combined_possessions
               / (2 * elapsed_game_time)
```

Для current period scheduled duration равна 48 минутам в regulation и увеличивается на
5 минут за каждый уже начатый overtime. Future overtime не предполагается.

`pace_last_N`, `N in {5,10,20}`, использует последние `min(N, completed)` combined
possessions и elapsed time между началом oldest possession и current end. Публикуется
`pace_last_N_possessions_used`. Zero/non-positive elapsed time даёт `NULL`.

### 11.2 PPP

```text
team_ppp = sum(offense_points on team possessions) / team_possessions
team_ppp_last_N = sum(offense_points on last N team possessions)
                  / number_of_selected_team_possessions
```

Для `N in {5,10,20}` используется доступный causal prefix и публикуется sample count.
`defense_points` не приписываются offensive possession и не входят в PPP numerator.

### 11.3 Event rates

Для каждой стороны prefix-inclusive:

```text
three_point_attempt_rate = 3PA / FGA
free_throw_rate = FTA / FGA
turnover_rate = turnovers / own_possessions
offensive_rebound_rate = OREB / (OREB + opponent_DREB)
```

Fouls, bonus и timeouts не входят в `feature-formulas-v1`.

## 12. Player pregame features

Player profile использует до 20 предыдущих appearances (`played=true` и
`seconds_played>0`). Публикуются `games_used`, `seconds_played` и scaled estimated
possessions/exposures.

Для dependency game `g`:

```text
player_exposure_g = player_seconds_g / game_duration_seconds_g
estimated_player_possessions_g = team_possessions_g * player_exposure_g
player_used_possessions_g = FGA_g + 0.44 * FTA_g + TOV_g
```

Coefficient `0.44` представлен rational `44/100`.

Features:

```text
minutes_avg = sum(player_seconds) / (60 * games_used)
points_per_100 = 100 * sum(points) / sum(estimated_player_possessions)
off_rating = 100 * sum(points) / sum(player_used_possessions)
def_rating = 100 * sum(opponent_points * player_exposure)
             / sum(opponent_possessions * player_exposure)
usage_rate = sum(player_used_possessions)
             / sum(team_used_possessions * player_exposure)
assist_rate = sum(assists)
              / sum(max(team_FGM - player_FGM, 0) * player_exposure)
turnover_rate = sum(turnovers) / sum(player_used_possessions)
three_point_attempt_rate = sum(3PA) / sum(FGA)
three_point_percentage = sum(3PM) / sum(3PA)
free_throw_rate = sum(FTA) / sum(FGA)
rebound_rate = sum(total_rebounds)
               / sum((team_rebounds + opponent_rebounds) * player_exposure)
pace_context = seconds-weighted mean of dependency-game pace
```

`off_rating` — versioned box-score scoring-efficiency definition, а не claim о full
play-by-play individual offensive rating. `def_rating` — exposure-weighted team defensive
context, а не causal individual defender attribution. Имена и definitions документируются,
чтобы Phase 7 не интерпретировал их как provider advanced metrics.

## 13. Lineup aggregate features

Для valid side с пятью players calculator ищет пять pregame profiles.

Публикуются:

```text
profiled_player_count
lineup_avg_off_rating
lineup_avg_def_rating
lineup_avg_usage_rate
lineup_avg_three_point_attempt_rate
lineup_avg_free_throw_rate
lineup_avg_minutes
lineup_avg_pace_context
```

Каждый aggregate — arithmetic mean пяти scaled values. Если хотя бы один required component
равен `NULL`, соответствующий lineup aggregate равен `NULL`; другие independently computable
aggregates остаются доступны.

При двух available sides:

```text
offense_difference
defense_difference
pace_difference
```

Difference равен home minus away. `lineup_pair_synergy` отсутствует до Phase 12.

## 14. Invalid и structural behavior

### 14.1 Quality-invalid state

Если covering segment существует, но side status invalid:

- side player IDs остаются empty;
- original safe reason/since boundary копируются в state row;
- `profiled_player_count=0`;
- все side lineup aggregates равны `NULL`;
- snapshot `result_status=invalid`;
- другие causal team/live features сохраняются.

Snapshot может восстановиться в последующем segment: valid later state снова получает пять
players и aggregates. Snapshot-wide status остаётся invalid, если хотя бы одна state invalid.

### 14.2 Structural failures

Candidate не публикуется при:

- absent/stale/invalid possession source pointer;
- отсутствующем lineup pointer;
- PBP lineage mismatch;
- source readback count/hash/lineage mismatch;
- missing или multiple covering lineup segments;
- non-contiguous possession/state numbers;
- raw/canonical stat-event mismatch;
- historical manifest row missing или изменившемся перед publish;
- canonical/hash contract violation.

Предыдущий fresh published feature pointer сохраняется. Safe issue code и category хранятся в
control plane; raw payload и secrets в error/log не попадают.

## 15. Identity, manifests и hashes

Versions:

```text
feature_join_version = feature-join-v1
feature_formula_version = feature-formulas-v1
feature_event_normalizer_version = nba-feature-events-v1
feature_output_schema_version = game-state-features-v1
player_output_schema_version = player-pregame-features-v1
```

### 15.1 Dependency hashes

`dependency_input_sha256` покрывает operational manifest, включая document/snapshot UUIDs,
tokens, checksums и ordering.

`dependency_revision_sha256` покрывает semantic manifest без operational UUIDs, но с content
checksums/revisions, game cutoff fields и formula-relevant box/possession content.

### 15.2 Feature input token

`feature-input-token-v1` включает:

```text
target game_id
target game_context_sha256
possession snapshot/revision/input token/output hash
lineup snapshot/revision/input token/output hash/result status
dependency_input_sha256
all feature versions
```

### 15.3 Feature revision

`feature-revision-v1` включает те же semantic inputs, но заменяет operational target snapshot
UUIDs/tokens соответствующими semantic revisions/output hashes и использует
`dependency_revision_sha256`.

Одинаковый content даёт одну revision при разных materialization UUIDs.

### 15.4 Output hashes

- `state_output_sha256` — ordered state rows;
- `player_output_sha256` — ordered player rows;
- `feature_output_sha256` — canonical object с обоими hashes, counts, result status и
  bounded issue codes.

Attempt/generation/timestamps не входят в semantic output hash.

## 16. PostgreSQL control plane

Alembic revision `0005_feature_engineering` добавляет четыре таблицы.

### 16.1 `feature_build_runs`

Повторяет run contract Phase 4:

```text
run_id, request identity/selectors, requested versions
status: running|succeeded|partial|failed|interrupted
discovered, materialized, invalid, skipped_current, verified, failed, busy
started_at, finished_at
```

### 16.2 `feature_build_resources`

Одна row на game:

```text
game_id primary key
publication_state: absent|current|stale|invalid
claim_version
active run/claim/snapshot/revision/input tuple + claimed_until
published snapshot/revision/input tuple + published_at
last_error_category, last_issue_codes
created_at, updated_at
```

### 16.3 `feature_snapshot_attempts`

```text
snapshot_id UUID primary key
game_id, generation
feature_revision_id, feature_input_token_sha256
source possession/lineup snapshot and revision identities
dependency_input_sha256, dependency_revision_sha256
all feature versions
expected_state_row_count, expected_state_output_sha256
expected_player_row_count, expected_player_output_sha256
expected_feature_output_sha256, expected_issue_codes, result_status
state: allocated|writing|verified|published|poisoned|superseded
created_by_run_id, created_by_claim_version
insert_started_at, verified_at, published_at, created_at
```

Metadata completeness CHECKs следуют Phase 4: allocated не имеет expected output; writing и
позже имеет полную expected group; verified/published имеют `verified_at`; только published
имеет `published_at`.

### 16.4 `feature_snapshot_dependencies`

Immutable child rows keyed by:

```text
(snapshot_id, dependency_game_id)
```

Columns содержат §6.4 identities/checksums, ordinal и причины inclusion bitmask
(`home_team_window`, `away_team_window`, `player_history`). Manifest immutable после перехода
attempt в writing.

## 17. ClickHouse schemas

### 17.1 `nba.game_state_features_v1`

Exact logical columns:

```text
snapshot_id, insert_version
feature_revision_id, feature_input_token_sha256
dependency_input_sha256, dependency_revision_sha256
feature_join_version, feature_formula_version
feature_event_normalizer_version, output_schema_version
state_output_sha256, feature_output_sha256

possession_snapshot_id, parse_revision_id, possession_output_sha256
lineup_snapshot_id, lineup_revision_id, lineup_output_sha256

game_id, season, season_start_year, season_type, game_date, started_at
home_team_id, away_team_id

state_number, state_kind, possession_number
boundary_sequence_number, boundary_side
period, seconds_remaining_game_ms, seconds_remaining_period_ms
home_score, away_score, score_diff
home_possessions, away_possessions

home_lineup_status, home_player_ids
home_invalid_reason, home_invalid_since_sequence_number
home_invalid_since_boundary_side
away_lineup_status, away_player_ids
away_invalid_reason, away_invalid_since_sequence_number
away_invalid_since_boundary_side

home_games_used_5, home_games_used_10, home_games_used_20
away_games_used_5, away_games_used_10, away_games_used_20
home_off_rating_5, home_off_rating_10, home_off_rating_20
home_def_rating_5, home_def_rating_10, home_def_rating_20
home_pace_5, home_pace_10, home_pace_20
away_off_rating_5, away_off_rating_10, away_off_rating_20
away_def_rating_5, away_def_rating_10, away_def_rating_20
away_pace_5, away_pace_10, away_pace_20
home_rest_days, away_rest_days
home_back_to_back, away_back_to_back

current_pace
pace_last_5, pace_last_10, pace_last_20
pace_last_5_possessions_used
pace_last_10_possessions_used
pace_last_20_possessions_used

home_ppp, away_ppp
home_ppp_last_5, home_ppp_last_10, home_ppp_last_20
away_ppp_last_5, away_ppp_last_10, away_ppp_last_20
home_ppp_last_5_possessions_used
home_ppp_last_10_possessions_used
home_ppp_last_20_possessions_used
away_ppp_last_5_possessions_used
away_ppp_last_10_possessions_used
away_ppp_last_20_possessions_used

home_three_point_attempt_rate, away_three_point_attempt_rate
home_free_throw_rate, away_free_throw_rate
home_turnover_rate, away_turnover_rate
home_offensive_rebound_rate, away_offensive_rebound_rate

home_profiled_player_count, away_profiled_player_count
home_lineup_avg_off_rating, away_lineup_avg_off_rating
home_lineup_avg_def_rating, away_lineup_avg_def_rating
home_lineup_avg_usage_rate, away_lineup_avg_usage_rate
home_lineup_avg_three_point_attempt_rate
away_lineup_avg_three_point_attempt_rate
home_lineup_avg_free_throw_rate, away_lineup_avg_free_throw_rate
home_lineup_avg_minutes, away_lineup_avg_minutes
home_lineup_avg_pace_context, away_lineup_avg_pace_context
offense_difference, defense_difference, pace_difference

is_valid
```

Home/away role columns encode the requested home-game feature without redundant constant
columns: home-side features have `is_home=true` semantics, away-side features false.

IDs/counters/clocks use exact integer/enum types matching source schemas. Feature values and
differences are `Nullable(Int64)` scaled by `FEATURE_SCALE`; sample counts are unsigned
integers. Back-to-back and `is_valid` are `UInt8` constrained to `0|1` by adapter validation.

Engine:

```text
ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, state_number)
```

### 17.2 `nba.player_pregame_features_v1`

Exact logical columns:

```text
snapshot_id, insert_version
feature_revision_id, feature_input_token_sha256
dependency_input_sha256, dependency_revision_sha256
feature_formula_version, output_schema_version
player_output_sha256, feature_output_sha256

game_id, season, season_start_year, season_type, game_date, started_at
player_id, team_id, is_home
history_cutoff_started_at
games_used, seconds_played, estimated_possessions

minutes_avg
points_per_100
off_rating
def_rating
usage_rate
assist_rate
turnover_rate
three_point_attempt_rate
three_point_percentage
free_throw_rate
rebound_rate
pace_context
```

`estimated_possessions` и все feature values — `Nullable(Int64)` scaled by
`FEATURE_SCALE`. `games_used` и `seconds_played` — exact unsigned counts. Player row может
существовать с partial `NULL` metrics; это не quality-invalid, если inputs causal и
structurally consistent.

Engine:

```text
ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, player_id)
```

Correct read всегда использует PostgreSQL pointer, `FINAL` и exact game/snapshot filters.

## 18. Build lifecycle

Для каждой selected final game:

1. начать/продолжить run и получить fenced claim;
2. в одной transaction разрешить target possession+lineup pointers и applied PBP;
3. проверить exact shared lineage;
4. построить bounded historical manifest;
5. вычислить input token/revision и вернуть `skip_current`, если exact current уже published;
6. сохранить allocated attempt и immutable dependency rows;
7. прочитать exact source snapshots и manifest-bound history;
8. normalize target stat events;
9. построить player profiles, pregame state и ordered after-possession states;
10. вычислить counts/hashes/status/issues;
11. fenced transition `allocated -> writing` с expected metadata;
12. вставить player и state rows;
13. exact readback обеих таблиц;
14. fenced transition `writing -> verified`;
15. под locks повторно разрешить target pointers и пересчитать manifest hashes;
16. при exact equality атомарно publish pointer и освободить claim.

Games обрабатываются последовательно по `(started_at, game_id)`. Межигровая параллельность в
Phase 5 не вводится, поскольку history dependencies делают её ordering-sensitive.

## 19. Recovery и fencing

- Same-run live claim может resume allocated/writing/verified attempt.
- Другой run получает BUSY, пока lease жив.
- После expiry новый run supersede-ит старый attempt и создаёт fresh snapshot ID.
- Uncertain ClickHouse transport вызывает exact readback до любого resend.
- Обе таблицы exact и hashes/counts совпали — attempt может advance без повторной вставки.
- Одна таблица absent, partial или different при persisted insert intent — attempt poisoned;
  тот же snapshot ID не переиспользуется.
- Stale owner не может publish/poison/fail новый owner state.
- Verify mode не пишет и проверяет current pointer, обе ClickHouse tables и current source/
  dependency identities.

## 20. CLI, settings и docs

CLI:

```text
uv run python -m scripts.build_dataset --season 2025-26
uv run python -m scripts.build_dataset --season 2025-26 --game-id 0022500123
uv run python -m scripts.build_dataset --season-from 2021-22 --season-to 2025-26
uv run python -m scripts.build_dataset --season 2025-26 --rebuild
uv run python -m scripts.build_dataset --season 2025-26 --verify
```

Selectors и exit/accounting contract повторяют lineup CLI. `--rebuild` и `--verify`
взаимоисключающие. NBA network fallback отсутствует.

Settings добавляют exact safe-identifier versions из §15. Environment и Compose передают их
в API image для CLI execution. Публичные HTTP routes не меняются.

Makefile добавляет `dataset` с теми же selector guards. README описывает dependency order:

```text
backfill -> build_possessions -> build_lineups -> build_dataset
```

## 21. Logging и performance

Structured logs содержат только safe scalar fields:

```text
run_id, game_id, snapshot_id, generation, state
feature_revision_id, source snapshot IDs
dependency_count, state_row_count, player_row_count
result_status, issue_codes, elapsed_ms
```

Payloads, player arrays, raw events, DSNs и secrets не логируются.

Calculator использует:

- linear two-pointer merge для ordered possession/lineup join;
- one-pass prefix accumulators для live features;
- bounded deques для 5/10/20 windows;
- batched historical reads;
- no dataframe dependency и no quadratic segment scan.

Phase 5 не обещает production live SLA, но pure state update остаётся пригодным для будущего
incremental consumer.

## 22. Test strategy

### 22.1 Unit

- inclusive end-boundary join и 0/>1 fail-closed;
- substitution внутри possession и post-possession lineup semantics;
- current/future game exclusion;
- future mutation не меняет earlier features;
- strict equal-`started_at` exclusion;
- early 5/10/20 windows и sample counts;
- regulation/OT pace и no future overtime;
- prefix-only PPP/event rates;
- zero denominator/missing history → `NULL`;
- player formulas, rational `0.44` и no current box stats;
- invalid segment, invalid→valid recovery и snapshot-wide invalid status;
- fixed-point positive/negative tie rounding;
- canonical ordering, known SHA-256 vectors и input permutations;
- frozen protocol signatures.

### 22.2 PostgreSQL integration

- revision upgrade/downgrade и exact schema constraints;
- atomic dual-source resolution;
- applied-vs-latest document selection;
- claim collision, lease expiry и full-fence mutation matrix;
- target source race before publish;
- dependency manifest race before publish;
- current/invalid/stale pointer resolution;
- same-run resume и other-run BUSY;
- finish_run counters.

### 22.3 ClickHouse integration

- both schema projections and table-aware destructive guards;
- exact two-table write/readback;
- absent/different/duplicate/partial classification;
- uncertain transport exact resume without resend;
- valid/invalid/recovered row flags;
- deterministic ordering and combined output hash;
- source lineage corruption rejection.

### 22.4 E2E

Synthetic fixture содержит несколько prior games и target game. Test выполняет:

```text
ingestion -> possession build -> lineup build -> dataset build
```

Assertions:

- state `0` плюс exactly one row per possession;
- exact end-boundary lineups;
- target final box score mutation не влияет на target features;
- future game mutation не влияет на target features;
- prior dependency mutation делает target stale;
- invalid interval сохраняется, recovery возвращает valid later rows;
- second default run has zero writes;
- forced rebuild deterministic;
- verify exact;
- CLI exit/accounting correct.

Fixture preflights ownership, uses far-future identities and deletes only journaled rows.

## 23. Final acceptance

В fresh uniquely named Compose project на unused loopback ports:

1. PostgreSQL `alembic upgrade head` дважды;
2. ClickHouse migrations дважды;
3. four services healthy и API readiness green;
4. full non-live pytest;
5. feature E2E и possession/lineup regressions;
6. destructive ClickHouse migration suite только после explicit empty-store checks;
7. Ruff check, Ruff format check и strict mypy;
8. CLI help и Makefile contract;
9. no live NBA variables и no NBA calls;
10. removal только exact temporary containers/network/volumes.

## 24. Success criteria

Phase 5 complete, когда:

1. Published feature snapshot нельзя получить иначе чем через PostgreSQL pointer.
2. State table содержит pregame state и каждую published possession ровно один раз.
3. Каждая valid state имеет exact five-player home/away lineups at end boundary.
4. Invalid source lineup никогда не превращается в guessed 5v5.
5. PBP lineage mismatch или ambiguous join никогда не публикуется.
6. Current/future game data не влияет на historical/pregame features.
7. Live features используют только prefix до state boundary.
8. Same inputs/versions дают identical revisions, rows и hashes.
9. Изменение target source, dependency или formula version делает current feature pointer stale.
10. Partial/uncertain writes не могут заменить предыдущий current pointer.
11. Dataset CLI offline, idempotent и fully verified.
12. Phase 1–4 tests и schemas остаются совместимыми.
13. Models, targets, replay, API, live worker и synergy не добавлены преждевременно.
