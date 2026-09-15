# Phase 4 Lineup Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Every behavior starts
> with a focused failing test; preserve RED/GREEN evidence and use a fresh worker per task.

**Goal:** Deterministically reconstruct complete-game NBA five-player lineups from the
already applied local box-score and PlayByPlayV3 snapshots, publish immutable verified lineup
segments in ClickHouse, and maintain dual-input claims, fencing, attempt history, quality
diagnostics, and the authoritative current pointer in PostgreSQL.

**Architecture:** A versioned NBA infrastructure normalizer cross-checks immutable raw source
documents against canonical Phase 2 rows and emits exactly one provider-neutral lineup event
per canonical PBP event. A pure causal domain state machine converts that stream into minimal
valid or publishable-invalid segments. PostgreSQL owns atomic input identity, leases, attempts,
and publication; ClickHouse stores immutable snapshots addressed only through the PostgreSQL
pointer. Existing Phase 3 possession rows and hashes remain unchanged.

**Tech stack:** Python 3.12-3.13, frozen slotted dataclasses and `StrEnum`, asyncio,
SQLAlchemy Core/asyncpg, PostgreSQL 17, Alembic, ClickHouse 26.3 HTTP interface, httpx,
Pydantic Settings, pytest, Ruff, strict mypy, and Docker Compose.

**Spec:**
`docs/superpowers/specs/2026-09-15-phase-4-lineup-reconstruction-design.md`

## Global constraints

- Read the authoritative spec before each task. The spec wins if this plan omits detail.
- Create an isolated git worktree before implementation; do not implement Phase 4 in the
  current `main` checkout without the user's explicit consent.
- Phase 4 reads only locally applied Phase 2 box-score and PBP documents. It makes no NBA
  request in normal builds.
- Domain/application code must not import `nba_api`, SQLAlchemy, httpx, FastAPI, Redis, or a
  ClickHouse client. Provider strings and description parsing stay in
  `infrastructure/nba/lineup_normalizer.py`.
- Reuse `canonical_event_checksum`, `canonical_pbp_events_projection`,
  `game_context_checksum`, and `game_context_projection` byte-for-byte. Keep the existing
  `possession-game-context-v1` marker; do not rename or duplicate those helpers.
- Read player names only from the immutable applied raw box-score document. Never join or
  depend on the mutable global `players` table for normalization or hashing.
- Use only `sequence_number` for ordering. Carry state across period/overtime boundaries,
  apply same-clock substitutions sequentially, and never look ahead.
- Structural raw/canonical disagreement fails the attempt and preserves the old pointer.
  A domain-invalid lineup is still written, verified, and published with resource state
  `invalid`, run status `partial`, and CLI exit code `1`.
- ClickHouse never decides current identity. Never select by `max`, mutate normal-path rows,
  delete old snapshots, or send a second INSERT request for the same snapshot UUID.
- Lock dual inputs in lexical ingestion-resource order (`box_score`, then `play_by_play`),
  then the game, lineup resource, and attempt. Recheck the full input token before terminal
  mutation or publication.
- Never log raw payloads, descriptions, player names, SQL, URLs, DSNs, headers, credentials,
  proxies, or exception messages. Persist only allowlisted safe codes/types and bounded issue
  projections.
- Do not add the Phase 5 possession-lineup join, feature tables, pace/PPP, models,
  predictions, API routes, live incremental state, or workers.
- Use `apply_patch` for manual edits. Native formatters/generators may update generated
  artifacts. Preserve unrelated user changes and do not push without permission.
- At every task checkpoint run focused tests, relevant Phase 3 regressions, Ruff, strict
  mypy, and `git diff --check` before committing.

## Frozen public names and versions

Use these names consistently; do not invent aliases in adapters:

```text
domain.lineups:
  LineupVersions, LineupPlayerBox, TeamRoster, LineupStatus, LineupIssue,
  Substitute, InvalidTeamTransition, FullLineupReset, NoLineupChange,
  LineupTransition, NormalizedLineupEvent, NormalizedGameLineups,
  TeamLineupState, LineupSegment, LineupReconstructionContext,
  LineupReconstructionResult, find_lineup_segment

domain.analytical_lineage:
  PbpLineage, require_matching_pbp_lineage

application.lineups:
  GameLineupInputSnapshot, LineupInputToken, LineupBuildMode,
  LineupBuildStatus, LineupAttemptState, LineupClaimAction,
  LineupVerificationStatus, LineupPublicationState, BuildLineupsRequest,
  BuildLineupsResult, LineupSnapshotAttempt, LineupBuildClaim,
  CurrentLineupSnapshotReference, LineupClaimDecision,
  CurrentLineupSnapshotResolution, LineupSnapshotVerification,
  LineupNormalizer, LineupReconstructor, LineupWorkStore, LineupSnapshotStore,
  LineupBuildService
```

Freeze these defaults before creating golden hashes:

```text
LINEUP_NORMALIZER_VERSION=nba-pbp-v3-lineup-v1
LINEUP_RECONSTRUCTOR_VERSION=causal-lineup-v1
LINEUP_RULES_VERSION=nba-5x5-v1
LINEUP_OUTPUT_SCHEMA_VERSION=lineup-segments-v1
LINEUP_BUILD_MAX_CONCURRENCY=4
LINEUP_CLAIM_LEASE_SECONDS=300
```

## Planned file structure

```text
src/nba_live_totals/
  domain/
    analytical_lineage.py
    lineups.py
    lineup_hashing.py
    lineup_reconstructor.py
  application/lineups/
    __init__.py
    errors.py
    models.py
    ports.py
    service.py
  infrastructure/nba/lineup_normalizer.py
  infrastructure/postgres/lineup_store.py
  infrastructure/clickhouse/lineups.py
alembic/versions/0004_lineup_reconstruction.py
clickhouse/migrations/
  0002_lineup_segments.sql
  0002_lineup_segments.schema.json
scripts/build_lineups.py
```

---

### Task 1: Make ClickHouse schema validation table-aware

**Files:**

- Modify: `src/nba_live_totals/infrastructure/clickhouse/migrations.py`
- Modify: `scripts/build_possessions.py`
- Modify: `tests/unit/infrastructure/clickhouse/test_migrations.py`
- Modify: `tests/unit/test_build_possessions_cli.py`
- Modify: `tests/integration/test_clickhouse_migrations.py`

**Produces:** stable schema-ID selection without changing the pinned `0001` projection.

- [ ] **Step 1: Write RED multi-manifest tests**

Add synthetic two-table manifests and an additive second revision of one table. Assert ordered
loading, a stable one-to-one mapping between schema ID and `(database, table)`, repeated
revisions only when both mapping sides remain unchanged, rejection of mismatched SQL/manifest
sets, and bounded safe schema/database/table names. Assert `fetch_schema_projection` uses the
passed schema ID rather than the hard-coded possession marker.

- [ ] **Step 2: Write RED selected-schema regression tests**

Assert `validate_schema(..., schema_id=POSSESSIONS_SCHEMA_ID)` selects the latest manifest for
that schema ID and validates only its ledger row/projection; a different table's known pending
migration must not block `build-possessions`. Assert missing, unapplied, checksum-drifted,
fingerprint-drifted, and catalog-unavailable selected heads fail closed. Preserve the existing
same-table additive-upgrade test and assert migration replay validates each known table's
latest head, not global `migrations[-1]` and not obsolete projections.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_migrations.py tests/unit/test_build_possessions_cli.py -q
```

Expected: new tests fail because schema selection is global/latest and the projection schema
ID is hard-coded.

- [ ] **Step 4: Implement the table-aware interface**

Add:

```python
POSSESSIONS_SCHEMA_ID = "clickhouse-possession-schema-v1"

@dataclass(frozen=True, slots=True)
class ClickHouseMigration:
    migration_id: str
    schema_id: str
    database: str
    table: str
    statements: tuple[str, ...]
    checksum_sha256: str
    expected_schema: dict[str, Any]
    expected_schema_fingerprint_sha256: str

async def fetch_schema_projection(
    client: SchemaQueryClient, *, schema_id: str, database: str, table: str
) -> dict[str, Any]: ...

async def validate_schema(
    engine: AsyncEngine,
    client: SchemaQueryClient,
    *,
    schema_id: str,
    migrations_dir: Path | None = None,
) -> None: ...
```

The migration runner applies pending revisions in order, leaves crash-recoverable ledger
states, and validates the latest head projection for every schema ID before completing the
run. Multiple revisions may share a schema ID/table, but one schema ID must never map to two
tables and one table must never map to two schema IDs. Change `build_possessions.run()` to
pass `POSSESSIONS_SCHEMA_ID` explicitly. Do not change `0001_possessions.schema.json` bytes or
fingerprint.

- [ ] **Step 5: Run regression gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_migrations.py tests/unit/test_build_possessions_cli.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py -q -m integration
uv run pytest tests/unit tests/e2e/test_possession_reconstruction.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/clickhouse/migrations.py scripts/build_possessions.py tests/unit/infrastructure/clickhouse/test_migrations.py tests/unit/test_build_possessions_cli.py tests/integration/test_clickhouse_migrations.py
git commit -m "refactor: select ClickHouse schemas by stable ID"
```

---

### Task 2: Define immutable lineup-domain contracts

**Files:**

- Create: `src/nba_live_totals/domain/lineups.py`
- Create: `tests/unit/domain/lineup_helpers.py`
- Create: `tests/unit/domain/test_lineups.py`

**Produces:** the provider-neutral lineup vocabulary and validated immutable values.

- [ ] **Step 1: Write RED constructor/invariant tests**

Cover exact-int validation rejecting bool; positive IDs; nullable player-box strings;
safe/version strings; frozen/slotted behavior; tuple/frozenset snapshots; invalid starter
cardinality retained for publishable domain invalidity; reset duplicates retained in a tuple;
one-based contiguous normalized events; segment endpoint metadata; ordered/non-overlapping
boundaries; contiguous segment numbers; valid five-player sets; invalid empty sets with reason
and `invalid_since`; and result status derived from every segment.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/domain/test_lineups.py -q
```

Expected: collection fails because `domain.lineups` does not exist.

- [ ] **Step 3: Implement exact contracts**

Use frozen slotted dataclasses. `TeamRoster` contains `team_id`, `roster_player_ids`,
`eligible_player_ids`, and `starter_player_ids`. `FullLineupReset.player_ids` remains a tuple
until transition validation. `NormalizedLineupEvent` wraps every canonical event, including
`NoLineupChange`. `LineupSegment` stores boundary, period, and millisecond clock at both ends.
`LineupIssue` contains exactly `(team_id, reason, sequence_number, boundary_side)`.
`LineupReconstructionResult.issues` is the first 32 unique deterministic invalid-state
transitions and is not a second semantic source.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_lineups.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/lineups.py tests/unit/domain/lineup_helpers.py tests/unit/domain/test_lineups.py
git commit -m "feat: define lineup domain contracts"
```

---

### Task 3: Add canonical lineup hashes and the shared PBP-lineage contract

**Files:**

- Create: `src/nba_live_totals/domain/lineup_hashing.py`
- Create: `src/nba_live_totals/domain/analytical_lineage.py`
- Modify: `src/nba_live_totals/application/possessions/models.py`
- Create: `tests/unit/domain/test_lineup_hashing.py`
- Create: `tests/unit/domain/test_analytical_lineage.py`
- Modify: `tests/unit/application/test_possession_models.py`

**Produces:** exact Phase 4 projections and a future Phase 5 fail-closed lineage predicate.

- [ ] **Step 1: Write RED projection and golden-vector tests**

Pin literal canonical bytes and independently calculated SHA-256 values for
`canonical-lineup-player-box-v1`, `lineup-revision-v1`, `lineup-input-token-v1`,
`normalized-lineups-v1`, and `lineup-output-v1`. Assert player rows sort by
`(team_id, player_id)`, contain exactly the seven spec fields, preserve null versus empty
string, serialize lineup IDs ascending, and exclude UUID/generation/timestamps/issues from
the output hash. Assert every content hash/version changes the revision while document UUID-
only changes affect only the input token.

- [ ] **Step 2: Write RED compatibility tests**

Add `PbpLineage` with exactly `game_id`, `input_pbp_document_id`, `input_pbp_sha256`,
`canonical_event_sha256`, and `game_context_sha256`. Add an additive `.pbp_lineage` property to
`PossessionInputToken`. Test exact equality and rejection when any one field differs, even when
the requested boundary exists. Do not change any Phase 3 hash/projection.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/domain/test_lineup_hashing.py tests/unit/domain/test_analytical_lineage.py tests/unit/application/test_possession_models.py -q
```

- [ ] **Step 4: Implement the public hashing API**

Implement `canonical_player_boxes_projection/checksum`,
`lineup_revision_projection/id`, `lineup_input_token_projection/checksum`,
`normalized_lineups_projection/checksum`, and `lineup_output_projection/checksum`. Reuse the
Phase 3 event and game-context helpers directly and retain their literal schema markers.
Implement `require_matching_pbp_lineage(left, right)` as an equality guard only; do not add the
possession-to-lineup dataset join.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_lineup_hashing.py tests/unit/domain/test_analytical_lineage.py tests/unit/domain/test_possession_hashing.py tests/unit/application/test_possession_models.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
git diff --check
git add src/nba_live_totals/domain/lineup_hashing.py src/nba_live_totals/domain/analytical_lineage.py src/nba_live_totals/application/possessions/models.py tests/unit/domain/test_lineup_hashing.py tests/unit/domain/test_analytical_lineage.py tests/unit/application/test_possession_models.py
git commit -m "feat: hash lineup snapshots and shared PBP lineage"
```

---

### Task 4: Implement the causal lineup state machine and boundary lookup

**Files:**

- Create: `src/nba_live_totals/domain/lineup_reconstructor.py`
- Modify: `src/nba_live_totals/domain/lineups.py`
- Modify: `tests/unit/domain/lineup_helpers.py`
- Create: `tests/unit/domain/test_lineup_reconstructor.py`
- Create: `tests/unit/domain/test_lineup_boundaries.py`

**Produces:** `DeterministicLineupReconstructor` and inclusive unique-segment lookup.

- [ ] **Step 1: Write RED transition tests**

Test `A -> F`, two same-clock substitutions preserving the intermediate state, period/overtime
carry, no-substitution single segment, missing outgoing player, incoming already on court,
same-player substitution, other-team player, ineligible player, unknown team, independent home
and away invalidity, absorbing invalid state, valid reset recovery, duplicate/ineligible reset
rejection, invalid initial starters, and recovery that does not erase an earlier invalid result.

- [ ] **Step 2: Write RED causality and coverage tests**

Assert old state through `BEFORE(substitution)`, new state from `AFTER(substitution)`, first
start `BEFORE(first)`, final end `AFTER(last)`, every event-side boundary covered exactly once,
future contradictions leave prior segments byte-identical, and `find_lineup_segment` returns
exactly one inclusive match or fails on zero/multiple matches.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/domain/test_lineup_reconstructor.py tests/unit/domain/test_lineup_boundaries.py -q
```

- [ ] **Step 4: Implement a single forward pass**

Initialize both teams independently before the first event. Preserve the first invalid reason
and boundary until an authoritative valid five-player reset. Ignore normal substitutions for
an already invalid side while continuing the other side. Close a segment only when at least
one complete team state changes: previous end is `BEFORE(n)`, next start is `AFTER(n)`.
Derive result validity from all emitted segments and build the deterministic bounded issues.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_lineup_reconstructor.py tests/unit/domain/test_lineup_boundaries.py tests/unit/domain/test_lineups.py -q
uv run ruff check src/nba_live_totals/domain tests/unit/domain
uv run ruff format --check src/nba_live_totals/domain tests/unit/domain
uv run mypy src tests/unit/domain
git diff --check
git add src/nba_live_totals/domain/lineup_reconstructor.py src/nba_live_totals/domain/lineups.py tests/unit/domain/lineup_helpers.py tests/unit/domain/test_lineup_reconstructor.py tests/unit/domain/test_lineup_boundaries.py
git commit -m "feat: reconstruct causal lineup segments"
```

---

### Task 5: Freeze lineup application models and ports

**Files:**

- Create: `src/nba_live_totals/application/lineups/__init__.py`
- Create: `src/nba_live_totals/application/lineups/errors.py`
- Create: `src/nba_live_totals/application/lineups/models.py`
- Create: `src/nba_live_totals/application/lineups/ports.py`
- Create: `tests/unit/application/lineup_fakes.py`
- Create: `tests/unit/application/test_lineup_models.py`

**Produces:** dependency-injected orchestration boundaries and strict persisted attempt values.

- [ ] **Step 1: Write RED snapshot/token tests**

`GameLineupInputSnapshot` contains `GameContext`, sorted `LineupPlayerBox` rows, canonical PBP
events, independent applied box/PBP document UUIDs and checksums, three canonical hashes, and
both `SourceDocument` objects. Test resource/game/checksum identity and an exact
`LineupInputToken.from_snapshot()` with `.pbp_lineage`. Leave raw/canonical cardinality and
semantic disagreement to the normalizer after attempt allocation.

- [ ] **Step 2: Write RED lifecycle/result tests**

Cover all frozen enums/models listed above. `ALLOCATED` requires all expected metadata null;
`WRITING|VERIFIED|PUBLISHED` require positive row count, output hash, non-null issue tuple, and
result status. `POISONED|SUPERSEDED` accept either the complete null group or complete populated
group, preserving whether failure occurred before or after writing started. Valid populated
metadata requires no issues; invalid requires 1..32 unique canonical issues. Test both pre-write
and post-write terminal states. `BuildLineupsResult` operational counters sum to discovered;
`invalid` is orthogonal, bounded by successful outcomes, and forces `PARTIAL`.

- [ ] **Step 3: Write RED protocol tests and run them**

Use runtime-checkable fakes for `LineupNormalizer`, `LineupReconstructor`, `LineupWorkStore`,
and `LineupSnapshotStore` with the exact method signatures frozen below.

Freeze these protocol signatures:

```python
class LineupNormalizer(Protocol):
    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups: ...

class LineupReconstructor(Protocol):
    def reconstruct(
        self,
        context: LineupReconstructionContext,
        normalized: NormalizedGameLineups,
    ) -> LineupReconstructionResult: ...

class LineupWorkStore(Protocol):
    async def start_run(self, request: BuildLineupsRequest) -> UUID: ...
    async def select_games(self, request: BuildLineupsRequest) -> tuple[GameId, ...]: ...
    async def claim_game(
        self,
        run_id: UUID,
        game_id: GameId,
        versions: LineupVersions,
        *,
        rebuild: bool,
    ) -> LineupClaimDecision: ...
    async def mark_writing(
        self,
        claim: LineupBuildClaim,
        *,
        expected_row_count: int,
        expected_output_sha256: str,
        expected_issue_codes: tuple[LineupIssue, ...],
        result_status: LineupStatus,
    ) -> LineupSnapshotAttempt: ...
    async def mark_verified(
        self,
        claim: LineupBuildClaim,
        verification: LineupSnapshotVerification,
    ) -> LineupSnapshotAttempt: ...
    async def poison_attempt(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None: ...
    async def publish_snapshot(
        self, claim: LineupBuildClaim
    ) -> CurrentLineupSnapshotReference: ...
    async def fail_game(
        self,
        claim: LineupBuildClaim,
        *,
        error_code: str,
        error_type: str,
    ) -> None: ...
    async def resolve_current(
        self, game_id: GameId, versions: LineupVersions
    ) -> CurrentLineupSnapshotResolution: ...
    async def finish_run(
        self, run_id: UUID, result: BuildLineupsResult
    ) -> None: ...

class LineupSnapshotStore(Protocol):
    async def write_snapshot(
        self,
        claim: LineupBuildClaim,
        result: LineupReconstructionResult,
    ) -> None: ...
    async def verify_snapshot(
        self, attempt: LineupSnapshotAttempt
    ) -> LineupSnapshotVerification: ...
    async def read_snapshot(
        self, reference: CurrentLineupSnapshotReference
    ) -> tuple[LineupSegment, ...]: ...
```

```powershell
uv run pytest tests/unit/application/test_lineup_models.py -q
```

- [ ] **Step 4: Implement contracts and safe structural errors**

`LineupNormalizationError(code)` accepts only allowlisted safe codes and never embeds raw
content. `LineupSnapshotVerification(EXACT)` carries count/hash/result status/issues; absent
and unavailable carry no semantic metadata. Current resolution carries a reference only for
`current|invalid`, with matching attempt status.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/application/test_lineup_models.py -q
uv run ruff check src/nba_live_totals/application tests/unit/application
uv run ruff format --check src/nba_live_totals/application tests/unit/application
uv run mypy src tests/unit/application
git diff --check
git add src/nba_live_totals/application/lineups/__init__.py src/nba_live_totals/application/lineups/errors.py src/nba_live_totals/application/lineups/models.py src/nba_live_totals/application/lineups/ports.py tests/unit/application/lineup_fakes.py tests/unit/application/test_lineup_models.py
git commit -m "feat: add lineup application contracts"
```

---

### Task 6: Validate the immutable NBA box/PBP input envelope

**Files:**

- Create: `src/nba_live_totals/infrastructure/nba/lineup_normalizer.py`
- Create: `tests/unit/infrastructure/nba/lineup_helpers.py`
- Create: `tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py`
- Create: `tests/fixtures/nba/lineups/box_score_v1.json`
- Create: `tests/fixtures/nba/lineups/play_by_play_v1.json`

**Produces:** strict structural validation and starter/roster normalization.

- [ ] **Step 1: Write RED source-envelope tests**

Assert both document source/endpoint/resource/game/checksum identities; independent document
IDs/hashes; raw/canonical PBP cardinality and every canonical field; raw position mapped to
contiguous `sequence_number` despite duplicate provider action numbers; duplicate players;
wrong team/game; empty completed PBP; and player projection without a global-player join.

- [ ] **Step 2: Write RED starter/eligibility tests**

Cover explicit starter flags, legacy nonempty position, explicit/legacy conflict, played/DNP
eligibility, raw-to-canonical position/comment normalization, and fewer/more than five starters
returning normalized roster input that the domain marks `invalid_initial_lineup` rather than a
structural exception.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py -q
```

- [ ] **Step 4: Implement bounded structural validation**

Use public `parse_play_by_play` to derive the comparable canonical event stream, supplemented
by explicit raw positional checks. Read names only from the immutable raw box document and
keep them in private infrastructure values. Cross-check scalar canonical player fields without
constructing the global `Player` model. Convert all malformed/empty/cross-representation
failures to safe `LineupNormalizationError` codes.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py tests/unit/application/test_lineup_models.py -q
uv run ruff check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba
uv run ruff format --check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba
uv run mypy src tests/unit/infrastructure/nba
git diff --check
git add src/nba_live_totals/infrastructure/nba/lineup_normalizer.py tests/unit/infrastructure/nba/lineup_helpers.py tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py tests/fixtures/nba/lineups/box_score_v1.json tests/fixtures/nba/lineups/play_by_play_v1.json
git commit -m "feat: validate immutable lineup inputs"
```

---

### Task 7: Normalize NBA substitutions with exact finite aliases

**Files:**

- Modify: `src/nba_live_totals/infrastructure/nba/lineup_normalizer.py`
- Modify: `tests/unit/infrastructure/nba/lineup_helpers.py`
- Create: `tests/unit/infrastructure/nba/test_lineup_normalizer_substitutions.py`
- Create: `tests/unit/infrastructure/nba/test_lineup_normalizer_aliases.py`
- Create: `tests/unit/infrastructure/nba/test_lineup_normalizer_golden.py`
- Create: `tests/fixtures/nba/lineups/normalized_v1.expected.json`

**Produces:** `NBAPlayByPlayV3LineupNormalizer.normalize()` and an exact golden event stream.

- [ ] **Step 1: Write RED classification tests**

Recognize only trimmed/casefolded `Substitution` with subtype empty or `out`; reject `in` even
though Phase 3 accepts it. Require fullmatch of the entire normalized `SUB: incoming FOR
outgoing` description. Bound captured labels to 256 Unicode characters and reject controls
before whitespace normalization.

- [ ] **Step 2: Write RED alias tests**

Pin NFKC, casefold, whitespace, apostrophe, dash, punctuation, suffix, full-name, family-name,
and initial aliases. Never fuzz or reorder tokens. Verify raw `personId`, raw `playerName`, and
description outgoing capture all identify the same same-team roster player. Resolve incoming
only through the finite raw same-team roster alias map; distinguish ambiguous, unknown, and
ineligible incoming players.

- [ ] **Step 3: Write RED stream/golden tests**

Assert one envelope per canonical event, administrative events become `NoLineupChange`,
normalization preserves sequence order, NBA V1 never emits `FullLineupReset`, failures emit
safe `InvalidTeamTransition` without raw text, future events never change the normalized
prefix, and the exact projection/hash matches the committed golden fixture.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_lineup_normalizer_substitutions.py tests/unit/infrastructure/nba/test_lineup_normalizer_aliases.py tests/unit/infrastructure/nba/test_lineup_normalizer_golden.py -q
```

Implement private bounded label/alias helpers and the public synchronous normalizer. Feed the
golden result through `DeterministicLineupReconstructor` to prove the pure end-to-end boundary.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba tests/unit/domain/test_lineup_reconstructor.py -q
uv run ruff check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba
uv run ruff format --check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba
uv run mypy src tests/unit/infrastructure/nba
git diff --check
git add src/nba_live_totals/infrastructure/nba/lineup_normalizer.py tests/unit/infrastructure/nba/lineup_helpers.py tests/unit/infrastructure/nba/test_lineup_normalizer_substitutions.py tests/unit/infrastructure/nba/test_lineup_normalizer_aliases.py tests/unit/infrastructure/nba/test_lineup_normalizer_golden.py tests/fixtures/nba/lineups/normalized_v1.expected.json
git commit -m "feat: normalize NBA lineup substitutions"
```

---

### Task 8: Orchestrate materialize, resume, verify, and cancellation

**Files:**

- Create: `src/nba_live_totals/application/lineups/service.py`
- Modify: `src/nba_live_totals/application/lineups/__init__.py`
- Modify: `tests/unit/application/lineup_fakes.py`
- Create: `tests/unit/application/test_lineup_service.py`
- Create: `tests/unit/application/test_lineup_resilience.py`

**Produces:** `LineupBuildService.build()` with no infrastructure imports.

- [ ] **Step 1: Write RED happy-path and invalid-path tests**

Assert exact lifecycle order: claim, normalize, reconstruct, mark writing, one write, exact
readback, mark verified, publish. A publishable invalid result increments both `materialized`
and `invalid`, produces `PARTIAL`, and is not counted as storage failure. Structural
normalizer failure calls `fail_game`, makes no ClickHouse call, and preserves the pointer.

- [ ] **Step 2: Write RED recovery/verify tests**

Cover skip-current valid/invalid; busy; writing resume performs readback only; verified resume
publishes directly from persisted expected metadata; response-loss exact readback; absent or
different poisoning; unavailable leaves writing; stale fence isolation; verify resolution
before and after ClickHouse read; stale/missing/different/unavailable verify; and verify never
claims or mutates a pointer/attempt.

- [ ] **Step 3: Write RED resilience tests**

Port the proven Phase 3 bounded worker/cancellation pattern. Test maximum concurrency,
no new claims after cancellation, local task gather/cancel, best-effort interrupted run finish,
cleanup failure not masking the primary safe error, and log capture containing no raw text,
names, exception messages, DSNs, or credentials.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/application/test_lineup_service.py tests/unit/application/test_lineup_resilience.py -q
```

Use injected ports only. Compare exact verification UUID/count/hash/status/issues. Operational
outcomes sum to discovered; quality-invalid remains orthogonal.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/application -q
uv run ruff check src/nba_live_totals/application tests/unit/application
uv run ruff format --check src/nba_live_totals/application tests/unit/application
uv run mypy src tests/unit/application
git diff --check
git add src/nba_live_totals/application/lineups/service.py src/nba_live_totals/application/lineups/__init__.py tests/unit/application/lineup_fakes.py tests/unit/application/test_lineup_service.py tests/unit/application/test_lineup_resilience.py
git commit -m "feat: orchestrate lineup snapshot builds"
```

---

### Task 9: Add the PostgreSQL Phase 4 control-plane schema

**Files:**

- Create: `alembic/versions/0004_lineup_reconstruction.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/schema.py`
- Create: `tests/unit/test_lineup_schema_contract.py`
- Modify: `tests/unit/test_possession_schema_contract.py`
- Create: `tests/integration/test_lineup_schema.py`
- Modify: `tests/integration/test_migrations.py`

**Produces:** `lineup_rebuild_runs`, `lineup_snapshot_attempts`, and
`lineup_rebuild_resources` with database-enforced invariants.

- [ ] **Step 1: Write RED exact schema tests**

Pin the spec's column order/types/defaults/checks/FKs/indexes. Attempts include complete
dual-input lineage, four versions, expected row/hash/issues/status, lifecycle timestamps, and
safe errors. Resources include active and published tuples, `publication_state`, monotonically
bounded counters, bounded `last_issue_codes`, and deferred composite active/published FKs.

- [ ] **Step 2: Write RED migration tests**

Assert head upgrade creates all three tables; downgrade to `0003_possession_reconstruction`
drops only those tables in resource/attempt/run order; re-upgrade is exact. Relax only the old
Phase 3 total-table-count assertion to a subset assertion; keep every exact Phase 3 assertion.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/test_lineup_schema_contract.py tests/unit/test_possession_schema_contract.py -q
uv run pytest tests/integration/test_migrations.py tests/integration/test_lineup_schema.py -q -m integration
```

- [ ] **Step 4: Implement migration and metadata**

Use revision `0004_lineup_reconstruction`, down revision
`0003_possession_reconstruction`. Enforce all-null/all-present expected metadata, lifecycle
state/timestamp coupling, valid-empty versus invalid-nonempty issues, exact issue JSON keys,
safe regexes, signed BIGINT bounds, pointer presence by publication state, and active fence
tuple integrity. `POISONED|SUPERSEDED` permit all-null or all-present expected metadata so both
pre-write structural failures and post-write failures are representable.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/test_lineup_schema_contract.py tests/unit/test_possession_schema_contract.py -q
uv run pytest tests/integration/test_migrations.py tests/integration/test_lineup_schema.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
git diff --check
git add alembic/versions/0004_lineup_reconstruction.py src/nba_live_totals/infrastructure/postgres/schema.py tests/unit/test_lineup_schema_contract.py tests/unit/test_possession_schema_contract.py tests/integration/test_lineup_schema.py tests/integration/test_migrations.py
git commit -m "feat: add lineup PostgreSQL control schema"
```

---

### Task 10: Implement atomic dual-input selection and lineup claims

**Files:**

- Create: `src/nba_live_totals/infrastructure/postgres/lineup_store.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/__init__.py`
- Create: `tests/integration/lineup_harness.py`
- Create: `tests/integration/test_lineup_work_store_claims.py`

**Produces:** selector, consistent input snapshot, revision/token calculation, and fenced claims.

- [ ] **Step 1: Write RED selection/snapshot tests**

Select only final regular-season/playoff games whose exact `box_score:<game>` and
`play_by_play:<game>` ingestion resources both succeeded with `applied_document_id`. Build the
snapshot from immutable source documents, scalar `player_box_scores` without a `players` join,
ordered PBP rows, reused hashes, and the exact game context.

- [ ] **Step 2: Write RED locking/freshness tests**

Observe explicit lock order: box resource, PBP resource, game, lineup resource, attempt/run as
needed. Prove a Phase 2-style writer holding either resource cannot update canonical rows
during the snapshot transaction. Vary box/PBP UUID/checksum, player projection, event
projection, game projection, and each version independently.

- [ ] **Step 3: Write RED claim tests**

Cover BUILD, same-run RESUME, other-run BUSY, SKIP_CURRENT for valid/invalid, forced rebuild,
expired/stale supersession, simultaneous first claim, and claim/generation exhaustion with no
partial mutation. Allocation stores both input lineages and increments attempt count, claim
version, and next generation atomically.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/integration/test_lineup_work_store_claims.py -q -m integration
```

Constructor:

```python
PostgresLineupWorkStore(
    engine: AsyncEngine,
    *,
    source: str = "nba.com",
    lease_seconds: int = 300,
    utc_now: Callable[[], datetime] = _system_utc_now,
)
```

Use explicit sorted resource locks, durable `applied_document_id` only, deterministic hashes,
and the full fence tuple.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/integration/test_lineup_work_store_claims.py tests/integration/test_possession_work_store_claims.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/postgres tests/integration
uv run ruff format --check src/nba_live_totals/infrastructure/postgres tests/integration
uv run mypy src tests/integration
git diff --check
git add src/nba_live_totals/infrastructure/postgres/lineup_store.py src/nba_live_totals/infrastructure/postgres/__init__.py tests/integration/lineup_harness.py tests/integration/test_lineup_work_store_claims.py
git commit -m "feat: add dual-input lineup claims"
```

---

### Task 11: Fence lineup attempt transitions and current publication

**Files:**

- Modify: `src/nba_live_totals/infrastructure/postgres/lineup_store.py`
- Create: `tests/integration/test_lineup_work_store_fencing.py`
- Create: `tests/integration/test_lineup_current_resolution.py`

**Produces:** writing/verified/published/poisoned/superseded transitions and read-only current
resolution.

- [ ] **Step 1: Write RED lifecycle/fence tests**

Test `allocated -> writing -> verified -> published`; exact count/hash/status/issues matching;
expired/stolen run/claim/snapshot/revision/token fences; poison/fail behavior; and invalid
publication copying `expected_issue_codes` into `last_issue_codes`.

- [ ] **Step 2: Write RED stale-input tests**

Before publish, poison, or fail, change each box/PBP document field or canonical projection in
isolation. Assert the attempt becomes superseded, active tuple clears, old pointer survives,
and publication state recomputes to absent/current/invalid/stale. Do the same for previous
valid and invalid pointers.

- [ ] **Step 3: Write RED current-resolution tests**

Resolve absent/stale/current/invalid without mutation. Require exact pointer-attempt
metadata, current input token/revision/versions, result status, and bounded issues. Corrupted
pointer metadata fails closed.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/integration/test_lineup_work_store_fencing.py tests/integration/test_lineup_current_resolution.py -q -m integration
```

Every terminal method repeats the complete dual-input lock/read/hash sequence. Exact publish
atomically marks the attempt, updates the pointer and publication state from persisted status,
copies issues, clears active/error fields, and releases the claim.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/integration/test_lineup_work_store_fencing.py tests/integration/test_lineup_current_resolution.py tests/integration/test_possession_work_store_fencing.py -q -m integration
uv run ruff check src/nba_live_totals/infrastructure/postgres tests/integration
uv run ruff format --check src/nba_live_totals/infrastructure/postgres tests/integration
uv run mypy src tests/integration
git diff --check
git add src/nba_live_totals/infrastructure/postgres/lineup_store.py tests/integration/test_lineup_work_store_fencing.py tests/integration/test_lineup_current_resolution.py
git commit -m "feat: fence lineup snapshot publication"
```

---

### Task 12: Add the versioned lineup-segments ClickHouse schema

**Files:**

- Create: `clickhouse/migrations/0002_lineup_segments.sql`
- Create: `clickhouse/migrations/0002_lineup_segments.schema.json`
- Modify: `src/nba_live_totals/infrastructure/clickhouse/migrations.py`
- Create: `tests/unit/infrastructure/clickhouse/test_lineup_schema.py`
- Modify: `tests/unit/infrastructure/clickhouse/test_migrations.py`
- Modify: `tests/integration/test_clickhouse_migrations.py`

**Produces:** `nba.lineup_segments_v1` and schema ID
`clickhouse-lineup-segments-schema-v1`.

- [ ] **Step 1: Write RED DDL/golden-contract tests**

Pin all 43 columns and exact types from spec section 17, `ReplacingMergeTree(insert_version)`,
partition by `season_start_year`, and order by `(game_id, snapshot_id, segment_number)`. Assert
the manifest schema/database/table and exact system projection for the pinned ClickHouse image.

- [ ] **Step 2: Write RED additive-migration regressions**

Guard that both analytical tables are empty before destructive migration tests. Assert both
ledger rows and projections after apply/replay; dropping either table fails validation; adding
`0002` preserves a sentinel possession snapshot and keeps the `0001` projection byte-identical.
Cleanup only exact owned snapshot UUIDs.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_lineup_schema.py tests/unit/infrastructure/clickhouse/test_migrations.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py -q -m integration
```

- [ ] **Step 4: Add DDL, manifest, and schema constant**

Add `LINEUP_SEGMENTS_SCHEMA_ID`. Generate/check the golden projection against
`clickhouse:26.3.25.2-jammy`; do not hand-wave types or engine normalization. Update destructive
helpers to drop/restore both exact tables and both known ledger rows only in isolated tests.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse tests/unit/test_build_possessions_cli.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py tests/integration/test_possession_schema.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
git diff --check
git add clickhouse/migrations/0002_lineup_segments.sql clickhouse/migrations/0002_lineup_segments.schema.json src/nba_live_totals/infrastructure/clickhouse/migrations.py tests/unit/infrastructure/clickhouse/test_lineup_schema.py tests/unit/infrastructure/clickhouse/test_migrations.py tests/integration/test_clickhouse_migrations.py
git commit -m "feat: add lineup segments ClickHouse schema"
```

---

### Task 13: Persist and verify immutable lineup snapshots

**Files:**

- Create: `src/nba_live_totals/infrastructure/clickhouse/lineups.py`
- Modify: `src/nba_live_totals/infrastructure/clickhouse/__init__.py`
- Create: `tests/unit/infrastructure/clickhouse/test_lineup_snapshot_store.py`
- Create: `tests/integration/test_lineup_snapshot_store.py`

**Produces:** the concrete `ClickHouseLineupSnapshotStore` adapter implementing the
`LineupSnapshotStore` protocol with one-send and exact-readback guarantees.

- [ ] **Step 1: Write RED serialization tests**

Assert one ordered JSONEachRow block, sorted player arrays, all lineage/version/game/boundary
columns, valid empty reason/zero/none sentinel, invalid empty player array with safe reason and
positive boundary, and `is_valid` consistency. Both valid and invalid results are writable.

- [ ] **Step 2: Write RED one-send/readback tests**

Reject a second send of one UUID after successful or uncertain transport. Classify EXACT,
ABSENT, DIFFERENT, and UNAVAILABLE from `FINAL` readback by exact season/game/snapshot.
Reject missing/extra/mixed metadata, duplicates, gaps/overlap, unsorted/wrong-cardinality
arrays, wrong sentinels/status/issues/is_valid, booleans as integers, and recomputed hash drift.

- [ ] **Step 3: Write RED integration protocol tests**

Cover equal/lower `insert_version` replacement remaining exact, higher corrupted replacement
becoming different, old published and orphan IDs separately readable, and cleanup limited to
owned lineup UUIDs without touching possession rows.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_lineup_snapshot_store.py -q
uv run pytest tests/integration/test_lineup_snapshot_store.py -q -m integration
```

Maintain process-local `_sent_snapshot_ids`, adding before transport. Only WRITING attempts
may write. `read_snapshot` validates and reconstructs the complete immutable domain result
internally, then returns its segment tuple; `verify_snapshot` compares all expected lineage,
semantic rows, result status, issues, cardinality, and output hash.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse tests/integration/test_lineup_snapshot_store.py tests/integration/test_possession_snapshot_store.py -q
uv run ruff check src/nba_live_totals/infrastructure/clickhouse tests
uv run ruff format --check src/nba_live_totals/infrastructure/clickhouse tests
uv run mypy src tests
git diff --check
git add src/nba_live_totals/infrastructure/clickhouse/lineups.py src/nba_live_totals/infrastructure/clickhouse/__init__.py tests/unit/infrastructure/clickhouse/test_lineup_snapshot_store.py tests/integration/test_lineup_snapshot_store.py
git commit -m "feat: persist immutable lineup snapshots"
```

---

### Task 14: Add lineup settings, CLI, Make target, Compose wiring, and runbook

**Files:**

- Create: `scripts/build_lineups.py`
- Modify: `src/nba_live_totals/settings.py`
- Modify: `.env.example`
- Modify: `compose.yaml`
- Modify: `Makefile`
- Modify: `README.md`
- Create: `tests/unit/test_build_lineups_cli.py`
- Modify: `tests/unit/test_settings.py`
- Modify: `tests/unit/test_compose_contract.py`
- Modify: `tests/unit/test_operator_documentation.py`

**Produces:** the complete offline operator workflow.

- [ ] **Step 1: Write RED settings/CLI tests**

Pin the six defaults and bounds. Test season, game-with-season, inclusive season range,
`--rebuild`, `--verify`, mutual exclusion, malformed settings/async URL/schema, and exact
summary order: run/status/discovered/materialized/invalid/skipped_current/verified/failed/busy.
Exit `0` only with invalid=failed=busy=0; exit `1` for quality invalid or operational partial;
exit `2` for input/config/schema; exit `130` for Ctrl+C.

- [ ] **Step 2: Write RED composition/Make/Compose tests**

Assert `run()` validates `LINEUP_SEGMENTS_SCHEMA_ID` explicitly, constructs the four adapters
and service, and closes resources. Add the five documented Make invocation forms with the
same selector/mode guards as possessions. Pass settings through the API container without
changing its host port, health endpoints, non-root user, or four-service topology.

- [ ] **Step 3: Write RED runbook tests**

Require dual-input freshness, no NBA network, default/rebuild/verify behavior, invalid quality
semantics, recovery states, exact pointer/readback, migration ordering, safe logs, deferred
scope, and destructive migration warning covering both analytical tables.

- [ ] **Step 4: Run RED and implement**

```powershell
uv run pytest tests/unit/test_build_lineups_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py -q
```

Compose `NBAPlayByPlayV3LineupNormalizer`, `DeterministicLineupReconstructor`,
`PostgresLineupWorkStore`, `ClickHouseLineupSnapshotStore`, and `LineupBuildService` only after settings,
URL, and schema validation. Add `build-lineups` to `.PHONY`; do not change the Dockerfile.

- [ ] **Step 5: Run task gates and commit**

```powershell
uv run pytest tests/unit/test_build_lineups_cli.py tests/unit/test_build_possessions_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py -q
docker compose config --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add scripts/build_lineups.py src/nba_live_totals/settings.py .env.example compose.yaml Makefile README.md tests/unit/test_build_lineups_cli.py tests/unit/test_settings.py tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py
git commit -m "feat: add lineup build operator workflow"
```

---

### Task 15: Prove the full offline lineup pipeline end to end

**Files:**

- Create: `tests/fixtures/nba/lineups/lineup_schedule_2098-99.json`
- Create: `tests/fixtures/nba/lineups/lineup_box_score_0029899002.json`
- Create: `tests/fixtures/nba/lineups/lineup_play_by_play_0029899002.json`
- Create: `tests/fixtures/nba/lineups/lineup_play_by_play_invalid_0029899002.json`
- Create: `tests/e2e/test_lineup_reconstruction.py`

**Produces:** deterministic Phase 2 -> Phase 4 -> PostgreSQL pointer -> ClickHouse acceptance.

- [ ] **Step 1: Build RED valid fixtures/tests**

Use home starters `101..105`, bench `106,107`; away starters `201..205`, bench `206,207`.
Sequence 3 substitutes home `101 -> 106`; sequence 4 at the same clock substitutes away
`201 -> 206`. Assert the exact intervals: segment 1 `BEFORE(1)..BEFORE(3)`, segment 2
`AFTER(3)..BEFORE(4)`, and segment 3 `AFTER(4)..AFTER(last)`, with the corresponding home/away
sets. Also assert both applied input lineages, current pointer, ClickHouse rows, and output hash.

- [ ] **Step 2: Add RED idempotence/recovery tests**

First run inserts once and verifies. Default rerun allocates/inserts nothing. Forced rebuild
uses a new UUID/generation but identical semantic output hash. Verify reads without mutation.
Raw/canonical mismatch and structural normalizer failure retain the previous pointer.

- [ ] **Step 3: Add RED invalid/CLI tests**

Use a well-formed home substitution whose outgoing roster player is not on court. Assert home
invalid from `AFTER(3)` with `outgoing_not_on_court`, away valid, published resource state
invalid, exact issue projection, `invalid=1`, no storage failure, and a real CLI subprocess
exit `1`. Assert possession rows are unchanged throughout.

- [ ] **Step 4: Run RED, fix only integration gaps, then verify**

```powershell
$env:TEST_API_URL = "http://localhost:18000"
uv run pytest tests/e2e/test_lineup_reconstruction.py -q -m e2e
uv run pytest tests/e2e/test_possession_reconstruction.py -q -m e2e
```

Do not weaken domain/normalizer invariants to accommodate fixtures. If a cross-layer contract
is wrong, add a focused unit/integration regression before correcting it.

- [ ] **Step 5: Run task gates and commit**

```powershell
$env:TEST_API_URL = "http://localhost:18000"
uv run pytest tests/e2e/test_lineup_reconstruction.py tests/e2e/test_possession_reconstruction.py -q -m e2e
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add tests/fixtures/nba/lineups/lineup_schedule_2098-99.json tests/fixtures/nba/lineups/lineup_box_score_0029899002.json tests/fixtures/nba/lineups/lineup_play_by_play_0029899002.json tests/fixtures/nba/lineups/lineup_play_by_play_invalid_0029899002.json tests/e2e/test_lineup_reconstruction.py
git commit -m "test: accept lineup reconstruction end to end"
```

---

### Task 16: Add opt-in live canary and final Docker acceptance

**Files:**

- Create: `tests/live/test_nba_lineup_canary.py`
- Modify: `README.md`
- Modify: `tests/unit/test_operator_documentation.py`

**Produces:** isolated provider-drift detection and final Phase 4 evidence.

- [ ] **Step 1: Write the skipped-by-default live contract**

Require `TEST_NBA_SEASON`, `TEST_NBA_GAME_ID`, `TEST_DATABASE_URL`,
`TEST_CLICKHOUSE_URL`, and `TEST_LIVE_ISOLATED_STORES=1` at collection. Reuse loopback-only
runtime guards. Make exactly bounded schedule, box-score, and PBP calls for one pinned final
game; assert five starters per team, at least one normalized substitution, valid five-player
sides, deterministic rebuild output, verify success, and offline default rerun.

- [ ] **Step 2: Implement owned-identity cleanup**

Record every created run/game/team/player/document/snapshot ID before later work can fail.
Delete ClickHouse data only by owned snapshot UUID, clean PostgreSQL in dependency order, and
prove both stores return to their initially empty state. Ordinary collection performs no NBA
network call.

- [ ] **Step 3: Run non-live full quality gates**

```powershell
$env:TEST_API_URL = "http://localhost:18000"
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

- [ ] **Step 4: Run Docker acceptance twice where idempotence matters**

```powershell
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.build_possessions --help
docker compose exec -T api /app/.venv/bin/python -m scripts.build_lineups --help
docker compose exec -T api id -u
$env:TEST_API_URL = "http://localhost:18000"
uv run pytest tests/e2e -v -m e2e
```

Require four healthy services, nonzero container UID, unchanged `/health` and `/health/ready`,
no new public API route, both migrations succeeding twice, and possession regressions green.
In a confirmed isolated empty analytical store, additionally run the destructive migration
suite and require executed tests rather than skips:

```powershell
$env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS = "1"
uv run pytest tests/integration/test_clickhouse_migrations.py -v -m integration -rs
```

Run the live test only when the user supplies the isolated opt-in variables.

- [ ] **Step 5: Review scope, commit, and stop before Phase 5**

```powershell
rg -n "TODO|TBD|FIXME|placeholder|Phase 5" docs/superpowers/plans/2026-09-15-phase-4-lineup-reconstruction.md src tests README.md
git diff --check
git status --short
git add tests/live/test_nba_lineup_canary.py README.md tests/unit/test_operator_documentation.py
git commit -m "test: add isolated lineup live canary"
```

Do not start possession-lineup joins, features, models, predictions, live workers, or API work.

## Spec-to-task coverage

| Spec area | Implemented by |
|---|---|
| Causal boundaries, invalid/reset semantics, lookup | Tasks 2-4 |
| Exact immutable box/PBP normalizer and aliases | Tasks 5-7 |
| Revision, token, output hashes, lineage equality | Task 3 |
| Application lifecycle, recovery, verify, cancellation | Tasks 5 and 8 |
| PostgreSQL schema, dual locks, fencing, pointer | Tasks 9-11 |
| Multi-table migration safety and lineup schema | Tasks 1 and 12 |
| Immutable ClickHouse write/readback | Task 13 |
| Settings, CLI, Make, Compose, operator docs | Task 14 |
| Offline E2E, quality-invalid behavior | Task 15 |
| Live drift canary and final acceptance | Task 16 |

## Final review checklist

- [ ] Every task shows a failing focused test before implementation.
- [ ] All names and version strings match the frozen section above.
- [ ] Phase 3 canonical event/context projections, possession schema, and behavior are unchanged.
- [ ] `0001_possessions.schema.json` is byte-identical and build-possessions validates only its
  stable schema ID after `0002` exists.
- [ ] No implementation reads names from global `players` or performs NBA network I/O during
  a build.
- [ ] Invalid snapshots are publishable, diagnosable, and visibly partial; structural failures
  never replace the pointer.
- [ ] No snapshot UUID can be inserted twice and no current snapshot is inferred from
  ClickHouse.
- [ ] All destructive tests are isolated, guard both analytical stores, and clean exact owned
  identities only.
- [ ] Placeholder scan, strict typing, full tests, Docker acceptance, and `git diff --check`
  are clean before claiming completion.
