# Phase 5 Feature Engineering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking. Every behavior starts with a focused failing test; preserve
> RED/GREEN evidence and use a fresh worker per task.

**Goal:** Build a deterministic, leakage-safe physical feature dataset with one synthetic
pregame state and one state after every published possession, plus bounded historical player
profiles, immutable per-game snapshots, exact lineage, and offline rebuild/verify workflows.

**Architecture:** PostgreSQL atomically resolves and fences the authoritative current Phase 3
possession and Phase 4 lineup pointers, the applied target PBP, and a bounded causal history
manifest. Pure domain calculators join possession end boundaries to exactly one lineup segment,
derive fixed-point pregame/player/lineup/live features, and produce immutable ordered rows.
ClickHouse stores two snapshot tables; exact two-table readback must succeed before PostgreSQL
publishes the only current pointer. Games are processed sequentially by `(started_at, game_id)`.

**Tech Stack:** Python 3.12-3.13, frozen slotted dataclasses and `StrEnum`, asyncio,
SQLAlchemy Core/asyncpg, PostgreSQL 17, Alembic, ClickHouse 26.3 HTTP interface, httpx,
Pydantic Settings, pytest, Ruff, strict mypy, and Docker Compose. No pandas, NumPy, or ML
dependency is introduced.

**Spec:**
`docs/superpowers/specs/2026-09-24-phase-5-feature-engineering-design.md`

## Global constraints

- Read the authoritative spec before each task. The spec wins if this plan omits detail.
- Work directly in the current checkout because the user explicitly rejected worktrees. Do not
  push, merge, reset, or discard unrelated user changes.
- Phase 5 reads only locally applied/published data. Dataset builds and ordinary tests make no
  NBA network request and have no network fallback.
- Do not modify Phase 3 possession or Phase 4 lineup schemas, hashes, parsing, reconstruction,
  or pointer semantics. Reuse `PbpLineage`, `require_matching_pbp_lineage`,
  `find_lineup_segment`, and the published-reference read paths.
- A target source is addressable only through PostgreSQL current pointers. Never infer current
  identity with `max(snapshot_id)`, timestamps, generations, or UUID ordering in ClickHouse.
- Require exact equality of the five-field shared PBP lineage before any target ClickHouse read.
  Join every possession at `possession.end_boundary`; zero or multiple covering segments rejects
  the entire candidate.
- Historical eligibility is strict: `history.started_at < target.started_at`. Equal timestamps,
  the current game, and future games are excluded. Current target final box-score counters are
  forbidden feature inputs.
- All rates and ratings are `Int64` fixed point at `FEATURE_SCALE=1_000_000`, with integer
  round-half-away-from-zero. Floats and decimal strings are forbidden in domain values,
  canonical JSON, hashes, and storage.
- Missing observations or zero denominators produce `None`/ClickHouse `NULL`; a real zero
  numerator with a positive denominator remains zero. Never impute.
- A published quality-invalid lineup remains a dataset state: empty player IDs, copied safe
  invalid reason/boundary, zero profiled-player count, and null lineup aggregates. Never guess
  players. Later valid segments recover row validity, but snapshot status remains `invalid`.
- Structural failures never publish a candidate and never replace a fresh old pointer. Safe
  codes/categories only may be persisted or logged; never log payloads, player arrays, raw
  events, SQL, URLs, DSNs, headers, credentials, proxies, or exception messages.
- ClickHouse normal writes are immutable and single-send per snapshot UUID. A lost response is
  resolved by exact `FINAL` readback, never by resending the same UUID.
- Pair/full-lineup synergy, labels, baselines, models, training, replay, predictions, API routes,
  Redis, live polling, injuries, travel, and Phase 6+ work remain out of scope.
- Use `apply_patch` for manual edits. At each checkpoint run focused tests, relevant Phase 1-4
  regressions, Ruff, strict mypy, and `git diff --check` before committing.

## Frozen public names and versions

Use these names consistently; workers must not invent parallel aliases:

```text
domain.features:
  FEATURE_SCALE, FeatureVersions, FeatureStateKind, FeatureResultStatus,
  HistoricalTeamBox, HistoricalPlayerBox, FeatureDependency, FeatureDependencyManifest,
  FeatureStatDelta, NormalizedFeatureEvents, TeamPregameFeatures, PlayerPregameFeatures,
  LineupAggregateFeatures, LiveFeatures, FeatureCalculationInput, GameStateFeature,
  FeatureDataset, scaled_ratio, join_possessions_to_lineups,
  build_team_pregame_features, build_player_pregame_features,
  build_lineup_aggregate_features, DeterministicFeatureCalculator

application.features:
  FeatureBuildMode, FeatureBuildStatus, FeatureAttemptState, FeatureClaimAction,
  FeatureVerificationStatus, FeaturePublicationState, BuildDatasetRequest,
  BuildDatasetResult, FeatureTargetSnapshot, FeatureTargetRows,
  HistoricalPossessionSource, FeatureSourceSnapshot, FeatureInputToken, FeatureSnapshotAttempt,
  FeatureBuildClaim, CurrentFeatureSnapshotReference, FeatureClaimDecision,
  CurrentFeatureSnapshotResolution, FeatureSnapshotVerification,
  FeatureEventNormalizer, FeatureSourceReader, FeatureCalculator, FeatureWorkStore,
  FeatureSnapshotStore, FeatureBuildService
```

Freeze these exact values before creating golden hashes:

```text
FEATURE_SCALE=1000000
FEATURE_JOIN_VERSION=feature-join-v1
FEATURE_FORMULA_VERSION=feature-formulas-v1
FEATURE_EVENT_NORMALIZER_VERSION=nba-feature-events-v1
FEATURE_OUTPUT_SCHEMA_VERSION=game-state-features-v1
PLAYER_OUTPUT_SCHEMA_VERSION=player-pregame-features-v1
FEATURE_CLAIM_LEASE_SECONDS=300
```

## Review focus

The implementation review must explicitly verify these five high-risk contracts:

1. Task 11 proves a race changing either target pointer or any manifest dependency after claim
   and before publish supersedes the attempt while preserving the previous pointer.
2. Tasks 2 and 13 prove invalid-to-valid lineup recovery, snapshot-wide invalid status, empty
   invalid arrays, and the absence of guessed players in both domain and persisted rows.
3. Tasks 3 and 10 prove equal-`started_at`, current, and future games cannot enter a history
   window or dependency manifest.
4. Task 4 proves regulation/overtime elapsed-time rules and `NULL` for zero/non-positive rolling
   pace duration without assuming future overtime.
5. Task 13 proves one-table success plus other-table absent/partial/different poisons the attempt,
   never resends the UUID, and never publishes.

## Planned file structure

```text
src/nba_live_totals/
  domain/
    features.py
    feature_hashing.py
  application/features/
    __init__.py
    errors.py
    models.py
    ports.py
    service.py
  infrastructure/nba/feature_event_normalizer.py
  infrastructure/postgres/feature_store.py
  infrastructure/clickhouse/features.py
alembic/versions/0005_feature_engineering.py
clickhouse/migrations/
  0003_game_state_features.sql
  0003_game_state_features.schema.json
  0004_player_pregame_features.sql
  0004_player_pregame_features.schema.json
scripts/build_dataset.py
```

---

### Task 1: Define fixed-point and immutable feature-domain contracts

**Files:**

- Create: `src/nba_live_totals/domain/features.py`
- Create: `tests/unit/domain/feature_helpers.py`
- Create: `tests/unit/domain/test_features.py`

**Produces:** validated provider-neutral inputs/outputs and the only numeric primitive used by
all Phase 5 formulas.

- [ ] **Step 1: Write RED constructor and fixed-point tests**

Test frozen/slotted behavior, exact integers rejecting booleans, positive IDs, sorted immutable
tuples, SHA-256/version validation, issue cap 32, dependency order `(started_at, game_id)`, and
state/player ordering. Pin positive and negative half ties, zero denominator, and real zero:

```python
def test_scaled_ratio_rounds_half_away_from_zero() -> None:
    assert scaled_ratio(1, 2) == 500_000
    assert scaled_ratio(-1, 2) == -500_000
    assert scaled_ratio(1, 0) is None
    assert scaled_ratio(0, 7) == 0
```

Assert `FeatureVersions` stores exactly the five version values above. `FeatureDependency`
contains every §6.4 identity/checksum plus `ordinal` and `inclusion_reasons`; the manifest
rejects duplicate games and noncanonical ordinals.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/domain/test_features.py -q
```

Expected: collection fails because `nba_live_totals.domain.features` does not exist.

- [ ] **Step 3: Implement exact contracts**

Use integer arithmetic only:

```python
FEATURE_SCALE = 1_000_000


def scaled_ratio(numerator: int, denominator: int, *, multiplier: int = 1) -> int | None:
    """Return multiplier*numerator/denominator at FEATURE_SCALE, half away from zero."""


@dataclass(frozen=True, slots=True)
class FeatureVersions:
    join_version: str
    formula_version: str
    event_normalizer_version: str
    output_schema_version: str
    player_output_schema_version: str
```

Define all frozen public names listed above. `FeatureDataset` owns ordered `states`, ordered
`players`, deterministic `issues`, and derives `FeatureResultStatus.INVALID` if any state is
quality-invalid. No class accepts `float` or `Decimal`.

Freeze the pure calculator input independently of application/persistence types:

```python
@dataclass(frozen=True, slots=True)
class FeatureCalculationInput:
    target_game: Game
    possessions: tuple[Possession, ...]
    lineup_segments: tuple[LineupSegment, ...]
    historical_team_boxes: tuple[HistoricalTeamBox, ...]
    historical_player_boxes: tuple[HistoricalPlayerBox, ...]
    history_possessions: tuple[Possession, ...]
```

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_features.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/features.py tests/unit/domain/feature_helpers.py tests/unit/domain/test_features.py
git commit -m "feat: define feature domain contracts"
```

---

### Task 2: Join possession boundaries and assemble ordered game states

**Files:**

- Modify: `src/nba_live_totals/domain/features.py`
- Modify: `tests/unit/domain/feature_helpers.py`
- Create: `tests/unit/domain/test_feature_state_join.py`

**Produces:** the linear target possession/lineup join and exact pregame/after-possession state
grain.

- [ ] **Step 1: Write RED exact-boundary tests**

Cover pregame `BEFORE(first_sequence)`, every possession `end_boundary`, a substitution inside a
possession, same-clock sequential substitutions, period/OT boundaries, and final event. Assert
the joined lineup is the state *after* the possession boundary and is never used to attribute the
past possession.

```python
joined = join_possessions_to_lineups(game, possessions, segments)
assert joined[0].state_number == 0
assert joined[0].boundary == EventBoundary(1, BoundarySide.BEFORE)
assert [row.state_number for row in joined] == list(range(len(possessions) + 1))
assert joined[3].home_player_ids == tuple(sorted(expected_after_boundary_players))
```

Assert zero or two covering segments, noncontiguous possession numbers, wrong game/team IDs,
and a missing pregame segment raise a structural domain error before any dataset exists.

- [ ] **Step 2: Write RED invalid/recovery tests**

Create valid -> invalid -> valid segments. Assert invalid rows keep team/live features eligible,
but have empty player IDs, copied safe reason/since boundary, `profiled_player_count=0`, and null
lineup aggregates. Later rows recover five players; `FeatureDataset.status` remains invalid and
its bounded issue list retains the causal invalid transition.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/domain/test_feature_state_join.py -q
```

- [ ] **Step 4: Implement a linear two-pointer merge**

`join_possessions_to_lineups` advances monotonically through ordered nonoverlapping lineup
segments and calls the existing inclusive `find_lineup_segment` semantics as its correctness
oracle in focused tests. Construct state 0 exactly as §7.1 and one state per possession with the
same `possession_number`; never scan all segments for every possession.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_feature_state_join.py tests/unit/domain/test_lineup_boundaries.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/features.py tests/unit/domain/feature_helpers.py tests/unit/domain/test_feature_state_join.py
git commit -m "feat: join feature states to lineup boundaries"
```

---

### Task 3: Calculate causal team pregame windows

**Files:**

- Modify: `src/nba_live_totals/domain/features.py`
- Create: `tests/unit/domain/test_team_pregame_features.py`

**Produces:** aggregate-of-counts 5/10/20 ratings, pace, rest, and home-role features without
current/future leakage.

- [ ] **Step 1: Write RED window/formula tests**

Pin exact 5/10/20 samples and values for:

```text
off_rating = 100 * sum(team_points) / sum(team_possessions)
def_rating = 100 * sum(opponent_points) / sum(opponent_possessions)
pace = 48min * sum(home_possessions + away_possessions) / (2 * sum(game_duration))
```

Assert aggregate-of-counts differs from and wins over mean-of-game-ratios. Cover incomplete
windows with counts, zero denominators -> `None`, regulation and completed OT durations, first
game `rest_days=None/back_to_back=False`, one-day difference B2B, and nonnegative rest days.

- [ ] **Step 2: Write RED leakage tests**

Build histories containing earlier, equal-time, current, and future games. Assert only rows with
`started_at < target.started_at` participate, including when game IDs sort before the target at
the same timestamp. Mutating excluded rows must leave serialized output byte-identical.

```python
features = build_team_pregame_features(target, mixed_history, team_id=target.home_team_id)
assert features.games_used_20 == earlier_count
assert features == build_team_pregame_features(
    target, mutated_excluded, team_id=target.home_team_id
)
```

- [ ] **Step 3: Run RED and implement bounded windows**

```powershell
uv run pytest tests/unit/domain/test_team_pregame_features.py -q
```

Sort eligible games by `(started_at, game_id)`, take suffixes of 5/10/20, sum integer counts,
and call `scaled_ratio`; do not calculate provider percentages or per-game averages.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_team_pregame_features.py tests/unit/domain/test_features.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/features.py tests/unit/domain/test_team_pregame_features.py
git commit -m "feat: calculate causal team pregame features"
```

---

### Task 4: Normalize prefix stat events and calculate live features

**Files:**

- Create: `src/nba_live_totals/application/features/__init__.py`
- Create: `src/nba_live_totals/application/features/errors.py`
- Create: `src/nba_live_totals/infrastructure/nba/feature_event_normalizer.py`
- Modify: `src/nba_live_totals/infrastructure/nba/__init__.py`
- Modify: `src/nba_live_totals/domain/features.py`
- Create: `tests/unit/infrastructure/nba/test_feature_event_normalizer.py`
- Create: `tests/unit/domain/test_live_features.py`

**Produces:** one provider-neutral stat delta per canonical event and one-pass prefix pace, PPP,
and event-rate calculations.

- [ ] **Step 1: Write RED normalizer tests**

Using minimal raw PBP fixtures, assert exact raw/canonical identity, cardinality, sequence,
period, clock, and team cross-checks. A non-stat action emits an all-zero delta at
`AFTER(sequence_number)`; shots/free throws/turnovers/rebounds emit only the §10 integer fields.
Mismatch raises `FeatureNormalizationError(code=<safe code>)` without embedding descriptions.

```python
events = NBAPlayByPlayV3FeatureEventNormalizer().normalize(snapshot)
assert len(events.deltas) == len(snapshot.canonical_events)
assert events.deltas[4].boundary == EventBoundary(5, BoundarySide.AFTER)
```

- [ ] **Step 2: Write RED causal live-feature tests**

Assert `BEFORE(n)` sees `< n`, `AFTER(n)` sees `<= n`; future event mutation cannot change an
earlier state. Test current pace, rolling 5/10/20 pace and sample counts, team PPP and rolling
team-possession PPP, 3PA/FGA, FTA/FGA, TOV/own possessions, and OREB/(OREB+opponent DREB).
Prove defense points never enter offensive PPP.

- [ ] **Step 3: Write RED overtime/zero-duration tests**

Assert regulation schedules 48 minutes; started OT adds exactly five minutes; future OT is not
assumed. Equal-clock last-N bounds and zero/nonpositive elapsed duration return `None` while
preserving the causal sample count.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_feature_event_normalizer.py tests/unit/domain/test_live_features.py -q
```

Use prefix accumulators and bounded deques, never target final box counters and never a quadratic
event/state scan. State 0 live fields are all `None`.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_feature_event_normalizer.py tests/unit/domain/test_live_features.py tests/unit/infrastructure/nba/test_lineup_normalizer.py -q
uv run ruff check src/nba_live_totals tests/unit
uv run ruff format --check src/nba_live_totals tests/unit
uv run mypy src tests/unit
git diff --check
git add src/nba_live_totals/application/features/__init__.py src/nba_live_totals/application/features/errors.py src/nba_live_totals/infrastructure/nba/feature_event_normalizer.py src/nba_live_totals/infrastructure/nba/__init__.py src/nba_live_totals/domain/features.py tests/unit/infrastructure/nba/test_feature_event_normalizer.py tests/unit/domain/test_live_features.py
git commit -m "feat: calculate causal live features"
```

---

### Task 5: Calculate historical player profiles and lineup aggregates

**Files:**

- Modify: `src/nba_live_totals/domain/features.py`
- Create: `tests/unit/domain/test_player_pregame_features.py`
- Create: `tests/unit/domain/test_lineup_feature_aggregates.py`
- Create: `tests/unit/domain/test_feature_calculator.py`

**Produces:** bounded last-20 appearance profiles, five-player aggregates, differences, and the
composed `DeterministicFeatureCalculator`.

- [ ] **Step 1: Write RED player formula tests**

Pin all §12 formulas with integer fixtures: appearance eligibility, seconds and games used,
`exposure=player_seconds/game_duration`, estimated possessions, rational `44/100` used
possessions, minutes average, points/100, box scoring efficiency `off_rating`, exposure-weighted
defensive context, usage, assist, turnover, 3PA rate/percentage, free-throw rate, rebound rate,
and seconds-weighted pace context. Test last 20 appearances and partial null metrics.

```python
profile = build_player_pregame_features(target, history, player_id=101)
assert profile.games_used == 2
assert profile.estimated_possessions == expected_scaled_possessions
assert profile.three_point_percentage == scaled_ratio(3, 8)
```

Assert `played=false`, zero-second, equal-time, current, and future rows are excluded. Mutating
the target final player/team box rows must not change any target profile.

- [ ] **Step 2: Write RED lineup aggregate tests**

For each valid side require exactly five profiles. Average each scaled component independently;
one null component nulls only that aggregate. Differences are home minus away with symmetric
negative tie behavior. Invalid sides produce zero profiled count and all-null aggregates without
looking up or guessing players. Pair/full-lineup synergy must be absent.

- [ ] **Step 3: Write RED composed-calculator tests**

`DeterministicFeatureCalculator.calculate(inputs, events)` must emit state 0 plus one row per
possession, ordered player rows, exact boundary lineups, shared pregame features, prefix-only
live values, recovery after invalid intervals, and snapshot-wide invalid status.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/domain/test_player_pregame_features.py tests/unit/domain/test_lineup_feature_aggregates.py tests/unit/domain/test_feature_calculator.py -q
```

Build player profiles once per target player, cache by numeric ID, then decorate ordered joined
states. Do not use historical lineup snapshots for participation attribution.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_player_pregame_features.py tests/unit/domain/test_lineup_feature_aggregates.py tests/unit/domain/test_feature_calculator.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/features.py tests/unit/domain/test_player_pregame_features.py tests/unit/domain/test_lineup_feature_aggregates.py tests/unit/domain/test_feature_calculator.py
git commit -m "feat: calculate player and lineup features"
```

---

### Task 6: Canonicalize dependency, input, revision, and output hashes

**Files:**

- Create: `src/nba_live_totals/domain/feature_hashing.py`
- Create: `tests/unit/domain/test_feature_hashing.py`

**Produces:** byte-pinned operational and semantic identities for source history and both output
tables.

- [ ] **Step 1: Write RED canonical box projection tests**

Pin literal canonical bytes and independently calculated SHA-256 vectors for
`canonical-feature-team-box-v1` and `canonical-feature-player-box-v1`. Team rows sort by numeric
`team_id`; player rows by `(team_id, player_id)`. Include only §6.4 formula fields, preserve
`None` as JSON null, and prove provider percentages, plus/minus, input order, and unrelated fields
do not alter the digest.

- [ ] **Step 2: Write RED manifest/token/revision vectors**

Add and pin these projections/checksums:

```python
def dependency_input_projection(manifest: FeatureDependencyManifest) -> CanonicalObject: ...
def dependency_revision_projection(manifest: FeatureDependencyManifest) -> CanonicalObject: ...
def feature_input_token_projection(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    possession_snapshot_id: UUID,
    parse_revision_id: str,
    possession_input_token_sha256: str,
    possession_output_sha256: str,
    lineup_snapshot_id: UUID,
    lineup_revision_id: str,
    lineup_input_token_sha256: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_input_sha256: str,
    versions: FeatureVersions,
) -> CanonicalObject: ...


def feature_revision_projection(
    *,
    game_id: GameId,
    target_started_at: datetime,
    game_context_sha256: str,
    parse_revision_id: str,
    possession_output_sha256: str,
    lineup_revision_id: str,
    lineup_output_sha256: str,
    lineup_result_status: LineupStatus,
    dependency_revision_sha256: str,
    versions: FeatureVersions,
) -> CanonicalObject: ...
```

Operational dependency/input hashes include document and snapshot UUIDs, tokens, checksums, and
canonical ordering. Semantic dependency/revision hashes exclude operational UUIDs and include
content checksums/revisions, the explicit `target_started_at` cutoff, and all version values;
`game_context_sha256` alone is insufficient because it does not cover the start timestamp. Hashing
code imports domain types only and never imports `application.features`. Prove two materializations
of identical content have different input tokens but the same feature revision.

- [ ] **Step 3: Write RED output-hash vectors**

Pin `state_output_sha256`, `player_output_sha256`, and the combined
`feature-output-v1` projection. The combined object contains both hashes, both counts, result
status, and bounded causal issue codes. Attempt UUID, generation, insert version, and timestamps
must not affect any semantic output hash. Input permutations must canonicalize identically.

- [ ] **Step 4: Run RED and implement using canonical JSON**

```powershell
uv run pytest tests/unit/domain/test_feature_hashing.py -q
```

Reuse `domain.canonical` and let its float/Decimal rejection remain the final guard. Export
`canonical_team_boxes_checksum`, `canonical_player_boxes_checksum`,
`dependency_input_checksum`, `dependency_revision_checksum`, `feature_input_token_checksum`,
`feature_revision_id`, `state_output_checksum`, `player_output_checksum`, and
`feature_output_checksum`.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_feature_hashing.py tests/unit/domain/test_canonical.py tests/unit/domain/test_possession_hashing.py tests/unit/domain/test_lineup_hashing.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/feature_hashing.py tests/unit/domain/test_feature_hashing.py
git commit -m "feat: hash feature snapshots and dependencies"
```

---

### Task 7: Freeze feature application models and ports

**Files:**

- Modify: `src/nba_live_totals/application/features/__init__.py`
- Modify: `src/nba_live_totals/application/features/errors.py`
- Create: `src/nba_live_totals/application/features/models.py`
- Create: `src/nba_live_totals/application/features/ports.py`
- Create: `tests/unit/application/feature_fakes.py`
- Create: `tests/unit/application/test_feature_models.py`
- Create: `tests/unit/application/test_feature_ports.py`

**Produces:** immutable source/attempt/result contracts and dependency-injected boundaries before
any persistence implementation.

- [ ] **Step 1: Write RED source/token/request tests**

`FeatureTargetSnapshot` contains full final `Game` (including `started_at`), a `GamePbpSnapshot`,
and exact published `CurrentSnapshotReference` / `CurrentLineupSnapshotReference`. Validate
`GameContext.from_game(game)` against both attempts and exact three-way PBP lineage.
`FeatureTargetRows` contains exact target possessions, lineup segments, and the sorted union of
player IDs from valid target segment sides only. `FeatureSourceSnapshot` combines the target,
that validated `target_player_ids` tuple, bounded sorted dependency manifest, formula-relevant
historical team/player boxes, and one `HistoricalPossessionSource` per dependency.
`HistoricalPossessionSource` pairs its `FeatureDependency` with the exact Phase 3
`CurrentSnapshotReference`, including expected row count and complete attempt lineage/version
metadata. Validate one-to-one canonical ordering, both manifest hashes, and input/revision.
Invalid-only players are never substituted from roster or target box-score rows.

`BuildDatasetRequest` repeats lineup selectors: one season, one game within one season, or a
continuous inclusive season range; verify/rebuild are mutually exclusive.

- [ ] **Step 2: Write RED lifecycle/result tests**

Freeze these enum values:

```text
FeatureBuildMode: materialize|verify
FeatureBuildStatus: succeeded|partial|failed|interrupted
FeatureAttemptState: allocated|writing|verified|published|poisoned|superseded
FeatureClaimAction: build|resume|skip_current|busy
FeatureVerificationStatus: exact|absent|different|unavailable
FeaturePublicationState: absent|current|stale|invalid
```

Allocated attempts have an entirely null expected group. Writing and later have state/player
counts and hashes, combined hash, issues, and result status. Verified/published require
`verified_at`; only published has `published_at`. Poisoned/superseded accept the complete null or
complete populated group according to failure stage. Result counters sum to discovered;
`invalid` is orthogonal to successful materialized/skipped/verified outcomes and forces partial.

- [ ] **Step 3: Write RED protocol signature tests**

Freeze exactly:

```python
class FeatureEventNormalizer(Protocol):
    def normalize(self, snapshot: GamePbpSnapshot) -> NormalizedFeatureEvents: ...


class FeatureSourceReader(Protocol):
    async def read_target(self, target: FeatureTargetSnapshot) -> FeatureTargetRows: ...

    async def read_inputs(self, claim: FeatureBuildClaim) -> FeatureCalculationInput: ...


class FeatureCalculator(Protocol):
    def calculate(
        self, inputs: FeatureCalculationInput, events: NormalizedFeatureEvents
    ) -> FeatureDataset: ...


class FeatureWorkStore(Protocol):
    async def start_run(self, request: BuildDatasetRequest) -> UUID: ...
    async def select_games(self, request: BuildDatasetRequest) -> tuple[GameId, ...]: ...
    async def resolve_target(self, game_id: GameId) -> FeatureTargetSnapshot: ...
    async def claim_game(
        self,
        run_id: UUID,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
        *,
        rebuild: bool,
    ) -> FeatureClaimDecision: ...
    async def mark_writing(
        self,
        claim: FeatureBuildClaim,
        *,
        expected_state_row_count: int,
        expected_state_output_sha256: str,
        expected_player_row_count: int,
        expected_player_output_sha256: str,
        expected_feature_output_sha256: str,
        expected_issue_codes: tuple[str, ...],
        result_status: FeatureResultStatus,
    ) -> FeatureSnapshotAttempt: ...
    async def mark_verified(
        self, claim: FeatureBuildClaim, verification: FeatureSnapshotVerification
    ) -> FeatureSnapshotAttempt: ...
    async def publish_snapshot(
        self, claim: FeatureBuildClaim
    ) -> CurrentFeatureSnapshotReference: ...
    async def poison_attempt(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None: ...
    async def fail_game(
        self, claim: FeatureBuildClaim, *, error_code: str, error_type: str
    ) -> None: ...
    async def record_unclaimed_failure(
        self, run_id: UUID, game_id: GameId, *, error_code: str, error_type: str
    ) -> None: ...
    async def resolve_current(
        self,
        target: FeatureTargetSnapshot,
        target_player_ids: tuple[int, ...],
        versions: FeatureVersions,
    ) -> CurrentFeatureSnapshotResolution: ...
    async def finish_run(self, run_id: UUID, result: BuildDatasetResult) -> None: ...


class FeatureSnapshotStore(Protocol):
    async def write_snapshot(self, claim: FeatureBuildClaim, dataset: FeatureDataset) -> None: ...
    async def verify_snapshot(
        self, attempt: FeatureSnapshotAttempt
    ) -> FeatureSnapshotVerification: ...
    async def read_snapshot(self, reference: CurrentFeatureSnapshotReference) -> FeatureDataset: ...
```

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/application/test_feature_models.py tests/unit/application/test_feature_ports.py -q
```

Follow Phase 4 validation patterns, frozen tuples, exact enums, UUIDv4 snapshots, bounded safe
issue strings, and complete metadata groups. Do not import infrastructure packages.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/application/test_feature_models.py tests/unit/application/test_feature_ports.py tests/unit/application/test_lineup_models.py -q
uv run ruff check src/nba_live_totals/application tests/unit/application
uv run ruff format --check src/nba_live_totals/application tests/unit/application
uv run mypy src tests/unit/application
git diff --check
git add src/nba_live_totals/application/features tests/unit/application/feature_fakes.py tests/unit/application/test_feature_models.py tests/unit/application/test_feature_ports.py
git commit -m "feat: define feature application contracts"
```

---

### Task 8: Add the PostgreSQL feature control plane

**Files:**

- Create: `alembic/versions/0005_feature_engineering.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/schema.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/__init__.py`
- Create: `tests/integration/test_feature_schema.py`

**Produces:** four constrained control-plane tables with upgrade/downgrade compatibility.

- [ ] **Step 1: Write RED schema/constraint tests**

Pin `feature_build_runs`, `feature_build_resources`, `feature_snapshot_attempts`, and
`feature_snapshot_dependencies` column names, enums, primary/foreign/unique keys, indexes, and
safe identifier/checksum bounds. Assert run counter consistency and resource active/published
tuple all-null/all-populated checks.

For attempts, test every state against expected-metadata completeness, verified/published times,
positive state count, nonnegative player count, valid/invalid issue requirements, one generation
per game, and UUID ownership. A fully invalid target may legitimately have zero player rows.
For dependencies, test PK `(snapshot_id, dependency_game_id)`, unique ordinal per snapshot,
ordered nonnegative ordinals, and inclusion bitmask limited to home-team, away-team, and
player-history bits.

- [ ] **Step 2: Write RED migration replay/downgrade tests**

Upgrade from `0004_lineup_reconstruction`, upgrade head twice, downgrade to `0004`, and re-upgrade.
Assert Phase 1-4 sentinels survive byte-for-byte and only Phase 5 tables disappear on downgrade.
Test rejected invalid state/status values directly through PostgreSQL.

- [ ] **Step 3: Run RED and implement**

```powershell
uv run pytest tests/integration/test_feature_schema.py -q -m integration
```

Use named constraints and the existing SQLAlchemy metadata style. Dependency rows become
immutable at application transition to writing; schema constraints prevent reparenting and
duplicate identity while adapter tests in Tasks 10-11 prove post-allocation immutability.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/integration/test_feature_schema.py tests/integration/test_lineup_schema.py tests/integration/test_possession_schema.py -q -m integration
uv run ruff check alembic src/nba_live_totals/infrastructure/postgres tests/integration
uv run ruff format --check alembic src/nba_live_totals/infrastructure/postgres tests/integration
uv run mypy src tests/integration
git diff --check
git add alembic/versions/0005_feature_engineering.py src/nba_live_totals/infrastructure/postgres/schema.py src/nba_live_totals/infrastructure/postgres/__init__.py tests/integration/test_feature_schema.py
git commit -m "feat: add feature control plane schema"
```

---

### Task 9: Add both ClickHouse feature schemas and table-aware guards

**Files:**

- Create: `clickhouse/migrations/0003_game_state_features.sql`
- Create: `clickhouse/migrations/0003_game_state_features.schema.json`
- Create: `clickhouse/migrations/0004_player_pregame_features.sql`
- Create: `clickhouse/migrations/0004_player_pregame_features.schema.json`
- Modify: `src/nba_live_totals/infrastructure/clickhouse/migrations.py`
- Create: `tests/unit/infrastructure/clickhouse/test_feature_schemas.py`
- Modify: `tests/unit/infrastructure/clickhouse/test_migrations.py`
- Modify: `tests/integration/test_clickhouse_migrations.py`

**Produces:** exact `nba.game_state_features_v1` and `nba.player_pregame_features_v1` physical
tables without weakening possession/lineup migration safety.

- [ ] **Step 1: Write RED DDL/manifest contract tests**

Pin every logical column in spec §§17.1-17.2, exact ClickHouse types, nullability, engine,
partition, and ordering. Assert all feature metrics/differences are `Nullable(Int64)`, sample
counts are unsigned, IDs/clocks match source widths, arrays are integer arrays, and booleans are
`UInt8` adapter-constrained.

```text
game states: ReplacingMergeTree(insert_version), PARTITION BY season_start_year,
             ORDER BY (game_id, snapshot_id, state_number)
players:     ReplacingMergeTree(insert_version), PARTITION BY season_start_year,
             ORDER BY (game_id, snapshot_id, player_id)
```

- [ ] **Step 2: Write RED four-table migration regressions**

Add `GAME_STATE_FEATURES_SCHEMA_ID` and `PLAYER_PREGAME_FEATURES_SCHEMA_ID`. Assert ordered apply
and replay, latest projection per stable schema ID, missing/drifted ledger or catalog failure,
and that validating one selected schema is not blocked by an unrelated pending table. Existing
`0001` and `0002` manifests/fingerprints stay byte-identical.

Destructive tests first prove all four analytical tables empty, then drop/restore only exact
owned tables/ledger rows. Preserve possession and lineup sentinels while adding 0003/0004.

- [ ] **Step 3: Run RED and add generated golden manifests**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_feature_schemas.py tests/unit/infrastructure/clickhouse/test_migrations.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py -q -m integration
```

Generate the exact system projection using the pinned ClickHouse image; do not hand-normalize
engine strings or types.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse -q
uv run pytest tests/integration/test_clickhouse_migrations.py tests/integration/test_possession_schema.py tests/integration/test_lineup_schema.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add clickhouse/migrations/0003_game_state_features.sql clickhouse/migrations/0003_game_state_features.schema.json clickhouse/migrations/0004_player_pregame_features.sql clickhouse/migrations/0004_player_pregame_features.schema.json src/nba_live_totals/infrastructure/clickhouse/migrations.py tests/unit/infrastructure/clickhouse/test_feature_schemas.py tests/unit/infrastructure/clickhouse/test_migrations.py tests/integration/test_clickhouse_migrations.py
git commit -m "feat: add feature ClickHouse schemas"
```

---

### Task 10: Resolve target sources, causal manifests, and fenced claims

**Files:**

- Create: `src/nba_live_totals/infrastructure/postgres/feature_store.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/__init__.py`
- Create: `tests/integration/feature_harness.py`
- Create: `tests/integration/test_feature_source_resolution.py`
- Create: `tests/integration/test_feature_work_store_claims.py`

**Produces:** atomic dual-pointer resolution, applied target PBP, bounded manifest/history rows,
input/revision identities, and first-claim/resume/skip/BUSY decisions.

- [ ] **Step 1: Write RED exact target-resolution tests**

In one repeatable transaction resolve a full final `Game`, current valid possession pointer/attempt,
current valid-or-invalid lineup pointer/attempt, and applied raw/canonical PBP. Reject absent,
stale, or invalid possession; absent/stale lineup; corrupted pointers; and any one-field PBP
lineage mismatch with safe code `source_lineage_mismatch`. Prove `latest_document_id` is ignored
in favor of `applied_document_id`. Recompute Phase 3/4 freshness using the injected current
`PossessionVersions` and `LineupVersions`; never trust stored publication-state labels alone.

- [ ] **Step 2: Write RED causal manifest tests**

After `FeatureSourceReader.read_target` returns the sorted union of players from valid target
segments, `claim_game` re-locks and rechecks both target pointers, then selects at most 20 prior
team games per target side and at most 20 appearances for exactly those players. Use strict SQL
predicate `games.started_at < :target_started_at` and
stable `(started_at, game_id)` ordering. Equal timestamp, target, future, non-final, zero-second,
and `played=false` rows must not enter. Union reasons into the three-bit inclusion mask; read
formula boxes in batches and calculate both canonical box checksums.
Persist the sorted `target_player_ids` on `FeatureSourceSnapshot`/claim, including players with
zero history, and reconstruct one full historical Phase 3 reference per manifest row by joining
the exact published attempt. This gives Task 12 an expected row count and complete lineage without
opening a second PostgreSQL transaction.

```python
assert all(
    dep.started_at < claim.source.target.game.started_at for dep in claim.source.manifest.items
)
assert tuple(dep.ordinal for dep in claim.source.manifest.items) == tuple(range(dependency_count))
```

- [ ] **Step 3: Write RED claim/recovery tests**

Cover a race between preliminary target read and claim allocation: changing either pointer makes
the claim fail/re-resolve without allocating. Then cover first claim, exact current skip, forced
rebuild, stale current, invalid current, same-run
resume of allocated/writing/verified, other-run BUSY, lease expiry superseding old attempt with a
fresh UUID, simultaneous claims, and generation exhaustion. Allocation persists immutable
dependency rows in the same transaction as active claim/source token/revision.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/integration/test_feature_source_resolution.py tests/integration/test_feature_work_store_claims.py -q -m integration
```

Constructor:

```python
PostgresFeatureWorkStore(
    engine: AsyncEngine,
    *,
    possession_versions: PossessionVersions,
    lineup_versions: LineupVersions,
    source: str = "nba.com",
    lease_seconds: int = 300,
    utc_now: Callable[[], datetime] = _system_utc_now,
)
```

Lock the game, both source resource/pointer rows, historical applied-document resources, feature
resource, and attempt in one documented deterministic order. Batched queries must be bounded by
the selected manifest; avoid per-player/per-game query loops.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/integration/test_feature_source_resolution.py tests/integration/test_feature_work_store_claims.py tests/integration/test_lineup_work_store_claims.py tests/integration/test_possession_work_store_claims.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/postgres tests/integration
uv run ruff format --check src/nba_live_totals/infrastructure/postgres tests/integration
uv run mypy src tests/integration
git diff --check
git add src/nba_live_totals/infrastructure/postgres/feature_store.py src/nba_live_totals/infrastructure/postgres/__init__.py tests/integration/feature_harness.py tests/integration/test_feature_source_resolution.py tests/integration/test_feature_work_store_claims.py
git commit -m "feat: resolve causal feature build claims"
```

---

### Task 11: Fence feature transitions and recheck every dependency at publish

**Files:**

- Modify: `src/nba_live_totals/infrastructure/postgres/feature_store.py`
- Create: `tests/integration/test_feature_work_store_fencing.py`
- Create: `tests/integration/test_feature_current_resolution.py`
- Create: `tests/integration/test_feature_run_accounting.py`

**Produces:** complete attempt lifecycle, full-fence mutation matrix, publish-time source recheck,
authoritative current resolution, and durable run accounting.

- [ ] **Step 1: Write RED lifecycle/fence tests**

Test `allocated -> writing -> verified -> published`, exact state/player counts and hashes,
combined hash/status/issues, immutable dependency rows after writing, and stale run/claim version,
snapshot, generation, revision, token, lease, source pointer, and manifest fences for every
terminal method. A stale owner cannot publish, poison, or fail a newer owner's state.
Test `record_unclaimed_failure` under the feature-resource lock: it may persist a safe resolution
error only when no other run owns a live claim, never changes a current pointer, and never
overwrites a newer owner's active/error fields.

- [ ] **Step 2: Write RED target-pointer race tests**

After exact readback but before publish, independently change the current possession pointer and
the current lineup pointer. `publish_snapshot` must lock/re-resolve both, recompute the target
token, mark the attempt superseded, clear only its active tuple, and retain the previous fresh
feature pointer.

```python
await replace_current_possession_pointer(game_id)
with pytest.raises(FeatureSourceChangedError):
    await store.publish_snapshot(claim)
assert await published_feature_pointer(game_id) == old_pointer
```

- [ ] **Step 3: Write RED dependency-race tests**

Independently change one dependency's applied box document, canonical team box, canonical player
box, possession pointer/revision/token/output, and inclusion eligibility. Under locks, rebuild the
bounded manifest from the claim's persisted sorted `target_player_ids`, including players with
zero prior appearances, and recompute both dependency hashes. Any mismatch supersedes the attempt
and preserves the old pointer; an unrelated game outside the manifest does not block publication.

- [ ] **Step 4: Write RED current/run tests and implement**

Resolve absent/current/invalid/stale without mutation, requiring exact published attempt metadata
and fresh source/dependency identity. Corrupted pointer metadata fails closed. Test finish-run
counters/status for success, quality-invalid partial, operational partial/failure, busy, and
interruption.

```powershell
uv run pytest tests/integration/test_feature_work_store_fencing.py tests/integration/test_feature_current_resolution.py tests/integration/test_feature_run_accounting.py -q -m integration
```

Every terminal transition repeats the complete deterministic lock/read/hash sequence. Exact
publish atomically marks the attempt, updates the pointer/publication state, copies bounded issues,
clears active/error fields, and releases the claim.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/integration/test_feature_work_store_fencing.py tests/integration/test_feature_current_resolution.py tests/integration/test_feature_run_accounting.py tests/integration/test_lineup_work_store_fencing.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/postgres tests/integration
uv run ruff format --check src/nba_live_totals/infrastructure/postgres tests/integration
uv run mypy src tests/integration
git diff --check
git add src/nba_live_totals/infrastructure/postgres/feature_store.py tests/integration/test_feature_work_store_fencing.py tests/integration/test_feature_current_resolution.py tests/integration/test_feature_run_accounting.py
git commit -m "feat: fence feature snapshot publication"
```

---

### Task 12: Read exact target and historical analytical sources

**Files:**

- Create: `src/nba_live_totals/infrastructure/clickhouse/features.py`
- Modify: `src/nba_live_totals/infrastructure/clickhouse/__init__.py`
- Create: `tests/unit/infrastructure/clickhouse/test_feature_source_reader.py`
- Create: `tests/integration/test_feature_source_reader.py`

**Produces:** `ClickHouseFeatureSourceReader`, which first returns exact target rows needed for
claim construction, then materializes `FeatureCalculationInput` only from PostgreSQL-addressed
Phase 3/4 snapshots and manifest-bound history.

- [ ] **Step 1: Write RED target exact-read tests**

`read_target(FeatureTargetSnapshot)` uses `FINAL` and exact season/game/snapshot filters,
deterministic
possession/segment order, expected row count, output hash, and five-field PBP lineage. Reject
missing, extra, duplicate, noncontiguous, mixed-snapshot, mixed-game, hash-drifted, or corrupted
lineage rows before returning `FeatureTargetRows`. Derive `target_player_ids` only as the sorted
union from valid segment sides. Never issue a latest/max query.

- [ ] **Step 2: Write RED manifest-bound history tests**

Read historical possession rows only for dependency snapshot IDs in the immutable manifest and
combine them with the already claimed canonical team/player box rows. For each aligned
`HistoricalPossessionSource`, require its expected row count, attempt versions, PBP lineage,
input/output hashes, and snapshot ID. Reject a missing manifest game, missing/extra row,
output/hash mismatch, or altered dependency identity. One batched query per source table (chunked
only at a fixed bound) replaces N+1 reads.

```python
target_rows = await reader.read_target(target)
inputs = await reader.read_inputs(claim)
assert inputs.possessions == target_rows.possessions
assert {row.game_id for row in inputs.history_possessions} == manifest_game_ids
```

- [ ] **Step 3: Write RED source-isolation and complexity tests**

Seed unrelated newer snapshots and prove they are ignored. Use an instrumented client to assert
query count is constant for a small fixture and bounded/chunked for a 40-game manifest. Verify
shared lineage is checked before the first data query and a mismatch performs no ClickHouse read.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_feature_source_reader.py -q
uv run pytest tests/integration/test_feature_source_reader.py -q -m integration
```

Reuse the validated row decoders/invariants of `PossessionSnapshotStore` and
`ClickHouseLineupSnapshotStore` instead of duplicating their semantic checks. Add narrowly scoped
exact-ID batch readers where the existing single-reference ports are insufficient.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_feature_source_reader.py tests/unit/infrastructure/clickhouse/test_possession_snapshot_store.py tests/unit/infrastructure/clickhouse/test_lineup_snapshot_store.py -q
uv run pytest tests/integration/test_feature_source_reader.py tests/integration/test_possession_snapshot_store.py tests/integration/test_lineup_snapshot_store.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/clickhouse tests
uv run ruff format --check src/nba_live_totals/infrastructure/clickhouse tests
uv run mypy src tests
git diff --check
git add src/nba_live_totals/infrastructure/clickhouse/features.py src/nba_live_totals/infrastructure/clickhouse/__init__.py tests/unit/infrastructure/clickhouse/test_feature_source_reader.py tests/integration/test_feature_source_reader.py
git commit -m "feat: read exact feature source snapshots"
```

---

### Task 13: Persist and verify immutable two-table feature snapshots

**Files:**

- Modify: `src/nba_live_totals/infrastructure/clickhouse/features.py`
- Create: `tests/unit/infrastructure/clickhouse/test_feature_snapshot_store.py`
- Create: `tests/integration/test_feature_snapshot_store.py`

**Produces:** `ClickHouseFeatureSnapshotStore` with single-send semantics and complete two-table
readback classification.

- [ ] **Step 1: Write RED exact serialization tests**

Pin ordered JSONEachRow for every §17 state/player column, shared snapshot metadata, source
identities, lineage hashes, nullable fixed-point values, integer arrays, invalid reason/since
sentinels, and `0|1` booleans. Assert valid, invalid, and recovered states serialize exactly;
invalid arrays stay empty and no player ID is synthesized. State rows order by `state_number`,
player rows by numeric `player_id`.

- [ ] **Step 2: Write RED per-table readback classifications**

For each table independently classify EXACT, ABSENT, DIFFERENT, UNAVAILABLE, duplicate, and
partial. Reject missing/extra/mixed metadata, wrong counts/order, noncontiguous states, player
universe drift, wrong null/sentinel/validity flags, booleans as integers outside `0|1`, output hash
drift, and source lineage corruption. Combined EXACT requires both tables exact and the same
feature output hash/status/issues.

- [ ] **Step 3: Write RED partial-write and uncertain-transport tests**

Exercise state-success/player-absent, player-success/state-absent, either partial/different, and
lost responses after each INSERT. A process-local sent-ID set is populated before the first
transport. After any insert intent, verify before any retry; both exact may advance, while one
absent/partial/different poisons the attempt and the UUID is never sent again or published.

```python
await store.write_snapshot(claim, dataset)
assert client.insert_calls == [STATE_TABLE, PLAYER_TABLE]
assert (await store.verify_snapshot(claim.attempt)).status is FeatureVerificationStatus.EXACT
with pytest.raises(FeatureSnapshotStoreError):
    await store.write_snapshot(claim, dataset)
```

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_feature_snapshot_store.py -q
uv run pytest tests/integration/test_feature_snapshot_store.py -q -m integration
```

Normal path sends each table at most once for a snapshot UUID. A zero expected player count sends
no empty INSERT and an exact zero-row query is valid; state count is always positive.
`read_snapshot` reconstructs and
validates a complete `FeatureDataset`; `verify_snapshot` compares counts, state/player hashes,
combined hash, status, issues, source/dependency identities, and versions.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_feature_snapshot_store.py -q
uv run pytest tests/integration/test_feature_snapshot_store.py tests/integration/test_possession_snapshot_store.py tests/integration/test_lineup_snapshot_store.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/clickhouse tests
uv run ruff format --check src/nba_live_totals/infrastructure/clickhouse tests
uv run mypy src tests
git diff --check
git add src/nba_live_totals/infrastructure/clickhouse/features.py tests/unit/infrastructure/clickhouse/test_feature_snapshot_store.py tests/integration/test_feature_snapshot_store.py
git commit -m "feat: persist immutable feature snapshots"
```

---

### Task 14: Orchestrate sequential build, recovery, verification, and cancellation

**Files:**

- Create: `src/nba_live_totals/application/features/service.py`
- Modify: `src/nba_live_totals/application/features/__init__.py`
- Create: `tests/unit/application/test_feature_service.py`

**Produces:** `FeatureBuildService` implementing the §18 lifecycle without cross-game
parallelism.

- [ ] **Step 1: Write RED fresh-build ordering tests**

Assert exact order: start run; select already ordered `(started_at, game_id)`; resolve target;
exact-read target rows; claim with target player IDs while rechecking target; exact-read complete
inputs; normalize; calculate; hash; mark writing; send two tables; readback; mark verified;
publish. Reject duplicate/out-of-order selections rather than re-sorting by game ID. Later
selected games cannot begin until the earlier game completes.

```python
service = FeatureBuildService(
    normalizer=normalizer,
    source_reader=source_reader,
    calculator=calculator,
    work_store=work_store,
    snapshot_store=snapshot_store,
    versions=versions,
)
result = await service.build(request)
assert work_store.claimed_game_ids == selected_game_ids_in_started_at_then_id_order
```

- [ ] **Step 2: Write RED resume/uncertain-write tests**

Resume ALLOCATED by reading/calculating; WRITING by readback only; VERIFIED by publication only.
For uncertain transport, swallow only the transport outcome long enough to verify and never call
write twice. Exact two-table readback advances; absent/partial/different poisons.

The claim decision occurs after the required preliminary exact target read. BUSY and exact-current
decisions perform no full manifest-bound source read, normalization, calculation, or output write.

- [ ] **Step 3: Write RED verify/cancellation/accounting tests**

Verify mode resolves target, exact-reads target player IDs, then resolves the current pointer,
rechecks current target sources and manifest, and reads
both tables without claim/allocation/write. Test materialized, invalid, skipped, verified, failed,
busy, and interrupted counters/status. Cancellation remains the primary exception, performs
best-effort safe fail/finish, and records `interrupted` without starting the next game. A
resolve/read-target failure before allocation calls fenced `record_unclaimed_failure`; failure to
record it cannot replace the primary error or mutate another run's live claim/current pointer.

- [ ] **Step 4: Run RED and implement safe stage mapping**

```powershell
uv run pytest tests/unit/application/test_feature_service.py -q
```

Map normalization/calculation/source consistency failures to allowlisted structural codes;
storage/lifecycle failures to bounded persistence codes. Logs contain only the safe scalar fields
from spec §21 and never `str(error)`.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/application/test_feature_service.py tests/unit/application/test_lineup_service.py tests/unit/application/test_possession_service.py -q
uv run ruff check src/nba_live_totals/application tests/unit/application
uv run ruff format --check src/nba_live_totals/application tests/unit/application
uv run mypy src tests/unit/application
git diff --check
git add src/nba_live_totals/application/features/service.py src/nba_live_totals/application/features/__init__.py tests/unit/application/test_feature_service.py
git commit -m "feat: orchestrate feature snapshot builds"
```

---

### Task 15: Add dataset settings, CLI, Make/Compose wiring, and runbook

**Files:**

- Create: `scripts/build_dataset.py`
- Modify: `src/nba_live_totals/settings.py`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `Makefile`
- Modify: `README.md`
- Create: `tests/unit/test_build_dataset_cli.py`
- Modify: `tests/unit/test_settings.py`
- Modify: `tests/unit/test_compose_contract.py`
- Modify: `tests/unit/test_operator_documentation.py`

**Produces:** the complete offline operator workflow and safe configuration surface.

- [ ] **Step 1: Write RED settings/CLI tests**

Pin the five version defaults and lease bounds. Test `--season`, game-with-season, inclusive
`--season-from/--season-to`, `--rebuild`, `--verify`, mutual exclusion, malformed settings/async
URL/schema, and exact summary order:

```text
run/status/discovered/materialized/invalid/skipped_current/verified/failed/busy
```

Match lineup CLI exits: `0` only when invalid=failed=busy=0; `1` for quality-invalid or
operational partial; `2` for arguments/config/schema; `130` for Ctrl+C. Test safe logs against
payload, player-array, raw-event, URL/DSN/header/credential/proxy, SQL, and exception-message
sentinels.

- [ ] **Step 2: Write RED composition/Make/Compose tests**

Assert `run()` validates all four required schema IDs (possessions, lineup segments, game-state
features, and player-pregame features), constructs normalizer/source reader/calculator/
work store/snapshot store/service, and closes clients/engine. Add `.PHONY dataset` with the five
documented invocations and the same selector guards as lineups. Pass version/lease settings into
the API image without changing Dockerfile, four-service topology, host port, non-root user,
health/readiness routes, or any public HTTP route.

- [ ] **Step 3: Write RED runbook tests**

README documents:

```text
backfill -> build_possessions -> build_lineups -> build_dataset
```

Require dual-source freshness, strict causal history, no target box features, no NBA fallback,
default/rebuild/verify, quality-invalid semantics, recovery states, exact pointer/two-table
readback, migration order, safe logs, fixed-point interpretation, deferred scope, and destructive
migration warnings for all four analytical tables.

- [ ] **Step 4: Run RED and implement composition only**

```powershell
uv run pytest tests/unit/test_build_dataset_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py -q
```

`scripts.build_dataset` contains no formulas or feature transformations. It validates settings,
URLs, and both schemas before building adapters.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/test_build_dataset_cli.py tests/unit/test_build_lineups_cli.py tests/unit/test_build_possessions_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py -q
docker compose config --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add scripts/build_dataset.py src/nba_live_totals/settings.py .env.example compose.yaml Makefile README.md tests/unit/test_build_dataset_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py
git commit -m "feat: add dataset build operator workflow"
```

---

### Task 16: Prove the full historical dataset pipeline and final acceptance

**Files:**

- Create: `tests/fixtures/nba/features/feature_schedule_2097-98.json`
- Create: `tests/fixtures/nba/features/feature_box_scores.json`
- Create: `tests/fixtures/nba/features/feature_play_by_plays.json`
- Create: `tests/e2e/test_feature_engineering.py`
- Modify: `README.md`
- Modify: `tests/unit/test_operator_documentation.py`

**Produces:** deterministic ingestion -> possession -> lineup -> dataset acceptance and final
Phase 5 verification evidence.

- [ ] **Step 1: Build RED owned historical fixtures/tests**

Use far-future IDs with several prior games, one equal-time excluded game, one future excluded
game, and a target containing substitutions plus a recoverable invalid lineup interval. Preflight
ownership; journal every PostgreSQL and ClickHouse identity before later work can fail; cleanup
only journaled rows/snapshot UUIDs in dependency order.

Run the complete pipeline and assert state 0 plus exactly one row per possession, exact
end-boundary lineups, 5/10/20 sample counts, known player/live values, empty invalid arrays,
valid recovered rows, and snapshot-wide invalid status.

- [ ] **Step 2: Add RED leakage/idempotence/recovery tests**

Separately mutate the target final box score and the future/equal-time games; target feature rows,
revision, and hashes remain byte-identical. Mutate one prior dependency; current target becomes
stale and rebuild changes the dependency/revision. Default rerun makes zero writes; forced rebuild
uses a new UUID with identical semantic revision/rows/hashes; verify is read-only and exact.

- [ ] **Step 3: Add RED structural/partial/CLI tests**

PBP lineage mismatch, ambiguous boundary join, historical manifest drift, and partial two-table
write retain the old pointer. Run the real CLI subprocess and assert quality-invalid exit `1`,
valid success `0`, bad input `2`, exact counter order, safe stderr/stdout, and no NBA call.
Possession/lineup rows and pointers stay unchanged throughout.

- [ ] **Step 4: Run focused E2E and fix only integration gaps**

```powershell
$apiHostPort = if ($env:API_HOST_PORT) { $env:API_HOST_PORT } else { "8000" }
$env:TEST_API_URL = "http://127.0.0.1:$apiHostPort"
uv run pytest tests/e2e/test_feature_engineering.py tests/e2e/test_possession_reconstruction.py tests/e2e/test_lineup_reconstruction.py -q -m e2e
```

Do not weaken invariants to fit fixtures. Add a focused unit/integration regression before any
cross-layer correction.

- [ ] **Step 5: Run fresh isolated Docker acceptance**

Use a generated Compose project name and ports preflighted as unused. Do not reuse or delete the
user's normal volumes. The concrete values below are examples selected only after the preflight;
set all Compose and host-side test URLs in the same shell:

```powershell
function Get-Phase5FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}
$phase5Project = "nba_phase5_$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$env:COMPOSE_PROJECT_NAME = $phase5Project
$phase5Ports = [System.Collections.Generic.HashSet[int]]::new()
while ($phase5Ports.Count -lt 4) { [void]$phase5Ports.Add((Get-Phase5FreeTcpPort)) }
$selectedPorts = @($phase5Ports)
$env:API_HOST_PORT = [string]$selectedPorts[0]
$env:POSTGRES_HOST_PORT = [string]$selectedPorts[1]
$env:CLICKHOUSE_HOST_PORT = [string]$selectedPorts[2]
$env:REDIS_HOST_PORT = [string]$selectedPorts[3]
$env:TEST_API_URL = "http://127.0.0.1:$env:API_HOST_PORT"
$env:TEST_DATABASE_URL = "postgresql+asyncpg://nba:nba@127.0.0.1:$env:POSTGRES_HOST_PORT/nba"
$env:TEST_CLICKHOUSE_URL = "http://127.0.0.1:$env:CLICKHOUSE_HOST_PORT"
$env:TEST_REDIS_URL = "redis://127.0.0.1:$env:REDIS_HOST_PORT/0"
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.build_dataset --help
docker compose exec -T api id -u
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

Require four healthy services, readiness green, nonzero container UID, migrations idempotent,
full non-live tests including Phase 1-4 regressions, unchanged HTTP routes, and no live NBA
variables/calls. Only after explicit empty checks for all four analytical tables, run:

```powershell
$env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS = "1"
uv run pytest tests/integration/test_clickhouse_migrations.py -v -m integration -rs
```

Require executed destructive tests rather than skips, then run
`docker compose down --volumes --remove-orphans` with the same generated
`COMPOSE_PROJECT_NAME`; verify the resolved project name before removal so only its exact
containers/network/volumes are deleted.

- [ ] **Step 6: Commit final acceptance artifacts and stop before Phase 6**

```powershell
rg -n "TO[D]O|TB[D]|FIXM[E]|placeholde[r]" src tests README.md docs/superpowers/plans/2026-09-24-phase-5-feature-engineering.md
rg -n "Phase[ ]6" src tests README.md
git diff --check
git status --short
git add tests/fixtures/nba/features tests/e2e/test_feature_engineering.py README.md tests/unit/test_operator_documentation.py
git commit -m "test: accept feature engineering end to end"
```

Do not start labels, baselines, models, training, replay, predictions, API, live worker, or synergy.

## Spec-to-task coverage

| Spec area | Implemented by |
|---|---|
| Fixed point, missing values, immutable contracts | Task 1 |
| State grain, exact inclusive boundary join, invalid recovery | Task 2 |
| Team 5/10/20 history, rest/B2B, equal-time exclusion | Task 3 |
| Raw/canonical stat normalization, pace/PPP/event-rate prefixes | Task 4 |
| Player last-20 formulas, lineup means/differences, composed calculator | Task 5 |
| Canonical box/manifests, input/revision/output hashes | Task 6 |
| Application models, protocols, lifecycle values | Task 7 |
| PostgreSQL tables and schema constraints | Task 8 |
| ClickHouse state/player schemas and migration safety | Task 9 |
| Atomic dual-source resolution, manifest selection, claims | Task 10 |
| Full fencing, publish-time races, pointers, run accounting | Task 11 |
| Exact target/history source reads and batching | Task 12 |
| Two-table immutable writes, readback, partial/uncertain recovery | Task 13 |
| Sequential service, resume, verify, cancellation | Task 14 |
| Settings, CLI, Make, Compose, safe logs, operator docs | Task 15 |
| Leakage/idempotence E2E and isolated final acceptance | Task 16 |

## Final review checklist

- [ ] Every task begins with a focused failing test and records RED before GREEN.
- [ ] All five version strings, canonical schema markers, and public signatures match the frozen
  sections of this plan and the approved spec.
- [ ] State 0 and every possession state are contiguous, and every target join returns exactly
  one inclusive lineup segment at the requested boundary.
- [ ] The five-field possession/lineup PBP lineage matches before any ClickHouse source read.
- [ ] Equal-time/current/future games and target final box statistics cannot affect features,
  dependencies, revisions, or hashes.
- [ ] No float, decimal string, imputation, guessed lineup, pair synergy, target, model, or live
  network fallback exists.
- [ ] Invalid intervals persist safely, recovered rows become valid, and snapshot status remains
  invalid if any state was invalid.
- [ ] Publish rechecks both target pointers and the complete dependency manifest under locks.
- [ ] A feature snapshot UUID is never inserted twice; both tables must verify exact before
  publication, and partial/uncertain writes cannot replace the old pointer.
- [ ] Games build sequentially by `(started_at, game_id)`; history reads are batched, state join is
  linear, and live computation is one-pass with bounded deques.
- [ ] Existing Phase 1-4 schemas, hashes, pointers, routes, tests, and manifests remain compatible.
- [ ] Placeholder scan, strict typing, full non-live tests, isolated Docker acceptance, and
  `git diff --check` are clean before claiming completion.
