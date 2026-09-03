# Phase 3 Possession Parser Implementation Plan

> **For agentic workers:** implement this plan task-by-task in subagent-driven mode. Every
> behavior starts with a focused failing test; do not combine commits or skip RED evidence.

**Goal:** Deterministically reconstruct complete historical NBA possessions from the locally
stored applied PlayByPlayV3 snapshot, publish immutable verified rows in ClickHouse, and keep
claims, fencing, attempt history and the authoritative current pointer in PostgreSQL.

**Architecture:** An NBA-specific versioned normalizer converts raw plus canonical Phase 2 PBP
into a closed provider-neutral signal stream. A pure domain state machine converts those
signals into immutable possession rows. PostgreSQL owns input freshness, leases, attempt state
and publication; ClickHouse stores immutable analytical snapshots selected only through the
PostgreSQL pointer. There is no distributed transaction.

**Tech stack:** Python 3.12–3.13, frozen dataclasses/StrEnum, asyncio, SQLAlchemy Core/asyncpg,
PostgreSQL 17, Alembic, ClickHouse 26.3 HTTP interface, httpx, Pydantic Settings, pytest, Ruff,
strict mypy and Docker Compose.

**Authoritative spec:**
`docs/superpowers/specs/2026-09-02-phase-3-possession-parser-design.md`

## Global constraints

- Read the authoritative spec before every task. The spec wins when this plan omits detail.
- Work directly on `main`; preserve the user's unrelated changes and never rewrite history.
- Phase 3 reads only the already applied local Phase 2 PBP document. It makes no NBA request.
- Domain/application code must not import `nba_api`, SQLAlchemy, httpx, FastAPI, Redis or a
  ClickHouse client.
- Provider strings and anchored description patterns exist only in
  `infrastructure/nba/possession_normalizer.py`.
- Ordering and relationships use only `sequence_number`; duplicate provider action numbers
  and clock ties are retained.
- Only complete final Regular Season/Playoffs games are eligible. Partial/live state remains
  Phase 11.
- Unknown control/scoring semantics fail closed; no guessed ownership or fabricated row.
- ClickHouse never decides which snapshot is current. No normal-path mutation, delete,
  `argMax(snapshot_id)` or `OPTIMIZE FINAL`.
- After an INSERT request is sent, never INSERT that `snapshot_id` again. Diagnose by exact
  readback; a new claim always gets a new UUID/generation.
- Never log descriptions, raw payloads, SQL, HTTP bodies/headers, credentials, proxies, DSNs
  or exception messages.
- Do not add lineup, features, pace, PPP, model, prediction/API or live-worker behavior.
- Use `apply_patch` for manual file edits. Generated format/lock/schema artifacts may use their
  native generators.
- Before each task commit run the focused tests, `ruff check`, `ruff format --check`, strict
  mypy and `git diff --check`.
- Do not push without explicit user permission.

## Planned file structure

```text
clickhouse/
    migrations/
        0001_possessions.sql
        0001_possessions.schema.json
scripts/
    migrate_clickhouse.py
    build_possessions.py
src/nba_live_totals/
    domain/
        canonical.py
        possessions.py
        possession_hashing.py
        possession_parser.py
    application/possessions/
        __init__.py
        models.py
        ports.py
        service.py
    infrastructure/nba/
        possession_normalizer.py
    infrastructure/postgres/
        possession_store.py
    infrastructure/clickhouse/
        __init__.py
        checker.py
        client.py
        migrations.py
        possessions.py
alembic/versions/
    0003_possession_reconstruction.py
```

---

### Task 1: Immutable possession-domain contracts

**Files:**

- Create: `src/nba_live_totals/domain/possessions.py`
- Modify: `src/nba_live_totals/domain/__init__.py`
- Create: `tests/unit/domain/test_possessions.py`

**Produces:** `GameContext`, `PossessionVersions`, `PossessionParseContext`,
`EventBoundary`, all closed signal dataclasses/unions, `NormalizedAction`,
`NormalizedGameActions`, `Possession`, `PossessionIssue` and `PossessionParseResult`.

- [ ] **Step 1: Write RED validation tests**

Cover frozen/slotted behavior; tuple snapshots; final-game context; distinct positive teams;
period `1..65535`; exact-int validation that rejects bool; nonnegative clocks/scores; boundary
ordering; safe issue/version/SHA identifiers; resume-disposition/team coupling; nonempty
signals; distinct offense/defense; valid-result/error incompatibility; invalid-result required
error; and absence of provider descriptions from repr.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/domain/test_possessions.py -q
```

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement the exact immutable contracts**

Use frozen slotted dataclasses and `StrEnum`. Keep the signal vocabulary closed:
`PeriodStart`, `PeriodEnd`, `FieldGoalAttempt`, `Rebound`, `Turnover`, `StealAnnotation`,
`Foul`, `FreeThrowTripStart/Attempt/End`, `JumpBall`, `Violation`, `Goaltending`,
`ControlAward`, `Administrative` and `Unsupported`. Signal fields use domain names such as
`turnover_kind`; `signal_type` is reserved for serialization.

`GameContext.from_game()` copies only the hash/output projection—never arena/display metadata
or `started_at`. Do not introduce provider values in this module.

- [ ] **Step 4: Run task gates**

```powershell
uv run pytest tests/unit/domain/test_possessions.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

- [ ] **Step 5: Commit**

```powershell
git add src/nba_live_totals/domain tests/unit/domain/test_possessions.py
git commit -m "feat: define possession domain contracts"
```

---

### Task 2: Strict canonical identity hashing

**Files:**

- Create: `src/nba_live_totals/domain/canonical.py`
- Create: `src/nba_live_totals/domain/possession_hashing.py`
- Modify: `src/nba_live_totals/domain/__init__.py`
- Create: `tests/unit/domain/test_possession_hashing.py`
- Modify: `tests/unit/domain/test_source.py`

**Produces:** strict `nba-live-totals-cjson-v1` bytes/SHA functions and the exact event,
game-context, normalized-actions, parse-revision, input-token and output projections.

- [ ] **Step 1: Write RED encoder and golden-vector tests**

Assert the Unicode and GameContext digests from spec section 6; no BOM/newline; object-key
order independence; array-order dependence; rejection of float/UUID/date/datetime/enum
objects; exact distinction among `1`, `true` and `"1"`; explicit nullable keys; exact 23
canonical PBP fields; `signal_type` discriminator; and exact inclusion/exclusion of UUID,
version, transport and lineage fields in each projection.

Prove the existing Phase 2 `canonical_json_checksum()` still accepts finite provider floats
and is not silently tightened.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/domain/test_possession_hashing.py tests/unit/domain/test_source.py -q
```

- [ ] **Step 3: Implement explicit typed projections**

The generic strict encoder accepts only dict/list/tuple/string/exact-int/exact-bool/null.
Projection functions convert dates, UUIDs and enums first. Every root contains its schema tag;
nullable keys are never omitted; hashes use SHA-256 of canonical UTF-8 JSON, never string
concatenation. Keep raw Phase 2 document hashing separate.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/domain/test_possession_hashing.py tests/unit/domain/test_source.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/domain tests/unit/domain
git commit -m "feat: add canonical possession hashing"
```

---

### Task 3: Application models, ports and Phase 3 settings

**Files:**

- Create: `src/nba_live_totals/application/possessions/__init__.py`
- Create: `src/nba_live_totals/application/possessions/models.py`
- Create: `src/nba_live_totals/application/possessions/ports.py`
- Create: `tests/unit/application/test_possession_models.py`
- Modify: `src/nba_live_totals/settings.py`
- Modify: `.env.example`
- Modify: `tests/unit/test_settings.py`

**Produces:** `GamePbpSnapshot`, `PossessionInputToken`, build request/result/status/mode,
claim/attempt/current-reference/verification contracts, `PossessionNormalizer`,
`PossessionParser`, `PossessionWorkStore` and `PossessionSnapshotStore` protocols.

- [ ] **Step 1: Write RED request, claim, port and settings tests**

Cover single/range selectors, targeted-game restriction, verify/rebuild exclusion, all claim
actions (`BUILD`, `RESUME`, `SKIP_CURRENT`, `BUSY`), positive generation/claim identity,
immutable PBP snapshot consistency and runtime protocol conformance.

Add settings with exact defaults/bounds:

```text
POSSESSION_RESOURCE_LEASE_SECONDS=300        # 1..86400
POSSESSION_BUILD_MAX_CONCURRENCY=4           # 1..32
CLICKHOUSE_OPERATION_TIMEOUT_SECONDS=30      # positive finite
POSSESSION_NORMALIZER_VERSION=nba-pbp-v1
POSSESSION_PARSER_VERSION=analytical-v1
POSSESSION_RULES_VERSION=nba-2025-v1
POSSESSION_OUTPUT_SCHEMA_VERSION=possessions-v1
```

Version identifiers match the spec safe regex without implicit trimming.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/application/test_possession_models.py tests/unit/test_settings.py -q
```

- [ ] **Step 3: Implement exact operation-level ports**

The work port must expose `start_run`, `select_games`, `claim_game`, `mark_writing`,
`mark_verified`, `poison_attempt`, `publish_snapshot`, `fail_game`, `resolve_current` and
`finish_run`. The snapshot port exposes one-block `write_snapshot`, exact
`verify_snapshot` and exact-ID `read_snapshot`. Document UUID/input token stay outside the
pure parser.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/application/test_possession_models.py tests/unit/test_settings.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/application/possessions src/nba_live_totals/settings.py .env.example tests/unit/application/test_possession_models.py tests/unit/test_settings.py
git commit -m "feat: define possession application contracts"
```

---

### Task 4: NBA V3 envelope cross-check and basic normalization

**Files:**

- Create: `src/nba_live_totals/infrastructure/nba/possession_normalizer.py`
- Modify: `src/nba_live_totals/infrastructure/nba/__init__.py`
- Create: `tests/unit/infrastructure/nba/possession_helpers.py`
- Create: `tests/unit/infrastructure/nba/test_possession_normalizer.py`
- Create: `tests/fixtures/nba/possessions/pbp_v3_normalizer_v1.json`
- Create: `tests/fixtures/nba/possessions/pbp_v3_normalizer_v1.expected.json`

- [ ] **Step 1: Write RED raw/canonical and basic-action tests**

Test applied document identity, endpoint/resource/game and `meta.version=1`; raw/canonical
cardinality and positional matching; every Phase 2 canonical field; preserved duplicate/null
provider identities; exact period, made/missed FG, compact `2pt/3pt`, timeout, substitution,
replay and blank block forms; `shotValue`; cumulative team-score checkpoints; scoring-only
combined `pointsTotal`; unknown control versus safe administrative forms; safe issues; exact
golden normalized JSON and SHA.

Do not reuse the existing three-action PBP mapper fixture as a possession golden corpus.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_possession_normalizer.py -q
```

- [ ] **Step 3: Implement the versioned basic normalizer**

Use exact `strip().casefold()` lookup tables and anchored fullmatch only. One raw action always
produces one envelope; sorting/deduplication is forbidden. Structured fields beat description.
Multi-signal order is scoring effect, control annotation, administrative annotation.

- [ ] **Step 4: Run task gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_possession_normalizer.py tests/unit/infrastructure/nba/test_mappers.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba tests/fixtures/nba/possessions
git commit -m "feat: normalize NBA possession actions"
```

---

### Task 5: NBA control-event normalization

**Files:**

- Modify: `src/nba_live_totals/infrastructure/nba/possession_normalizer.py`
- Create: `tests/unit/infrastructure/nba/test_possession_normalizer_control.py`
- Modify: `tests/fixtures/nba/possessions/*`

- [ ] **Step 1: Write RED tests for every Appendix A.2 branch**

Cover player/team rebound identity, provider-side-as-annotation, FT placeholder, unknown team
award, exact turnover/foul/violation allowlists, linked/orphan/ambiguous steal, anchored jump
ball without name-to-team inference, offensive foul linkage, retained kicked-ball/delay,
defensive/offensive goaltending, lane/jump dispositions and replay corrections. Include
positive and negative parameterized forms; no raw text may escape signals/issues.

- [ ] **Step 2: Implement table-driven relationship resolution**

Relationships use sequence and bounded same-clock clusters. Never infer a team from a name or
rebound description. Jump winner stays unknown without structured evidence. Every
administrative action remains represented.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_possession_normalizer.py tests/unit/infrastructure/nba/test_possession_normalizer_control.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/nba/possession_normalizer.py tests/unit/infrastructure/nba tests/fixtures/nba/possessions
git commit -m "feat: normalize NBA control transitions"
```

---

### Task 6: Free-throw normalization and linkage

**Files:**

- Modify: `src/nba_live_totals/infrastructure/nba/possession_normalizer.py`
- Create: `tests/unit/infrastructure/nba/test_possession_normalizer_free_throws.py`
- Modify: `tests/fixtures/nba/possessions/*`

- [ ] **Step 1: Write RED tests for Appendix A.3–A.4**

Cover every ordinary/special subtype; anchored made/miss forms; player cumulative points not
being event points; deterministic trip IDs; start/attempt/end ordering; ordinal continuity;
ordinary final make/miss; structural and-one; retained special trips; nested technical only;
lane retry/forfeit/double violation; FT placeholder rebound; orphan/incomplete trip; and exact
golden normalized SHA.

- [ ] **Step 2: Implement the exact FT table and normalizer-local trip stack**

Do not expose provider strings or infer parser ownership. Synthetic start/end signals share
the source sequence boundary. Unknown/malformed trip data produces bounded error issues.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/nba/test_possession_normalizer*.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/nba/possession_normalizer.py tests/unit/infrastructure/nba tests/fixtures/nba/possessions
git commit -m "feat: normalize NBA free throw procedures"
```

---

### Task 7: Pure state machine for ordinary possessions

**Files:**

- Create: `src/nba_live_totals/domain/possession_parser.py`
- Modify: `src/nba_live_totals/domain/__init__.py`
- Create: `tests/unit/domain/possession_helpers.py`
- Create: `tests/unit/domain/test_possession_parser.py`

- [ ] **Step 1: Write RED ownership/boundary tests**

Test opening control; miss/offensive rebound/make as one possession; defensive rebound;
turnover plus steal exactly once; inferred versus ambiguous owner; made-FG entitlement hint;
same-boundary end/new control without double consumption; pending jump resolved at
`BEFORE(action)` followed by the action; duplicate action numbers/clock ties; contiguous game
and offense counters; and exact source boundary/count semantics.

- [ ] **Step 2: Implement the six-state pure parser core**

Use private mutable frames for `WAITING_FOR_CONTROL`, `CONTROLLED`, `AWAITING_REBOUND`,
`FT_PROCEDURE`, `JUMP_PENDING`, `COMPLETE`; return only frozen results. Exhaustively dispatch
on the closed signal union and consume every envelope exactly once.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/domain/test_possession_parser.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/domain/possession_parser.py src/nba_live_totals/domain/__init__.py tests/unit/domain
git commit -m "feat: reconstruct basic NBA possessions"
```

---

### Task 8: FT stack, jump balls, periods and overtime

**Files:**

- Modify: `src/nba_live_totals/domain/possession_parser.py`
- Create: `tests/unit/domain/test_possession_parser_procedures.py`

- [ ] **Step 1: Write RED procedure matrix**

Cover ordinary/and-one/special FT; final miss plus either rebound; full technical suspended
frame during and between possessions; pending baseline delta; retained awards; lane procedures;
nested technical; conflicting control; jump same/opposite/unknown/re-jump; team rebound at
period end; exact period-boundary synthesis; missing final marker; buzzer actions; period
`5..65535`; and unresolved complete-game state.

- [ ] **Step 2: Implement procedures without hidden provider logic**

`RESUME_INTERRUPTED` restores the complete suspended frame. An entitlement is not a started
possession. Period synthesis is allowed only under the spec's exact preconditions; no final
period synthesis or phantom possession.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/domain/test_possession_parser.py tests/unit/domain/test_possession_parser_procedures.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/domain/possession_parser.py tests/unit/domain/test_possession_parser_procedures.py
git commit -m "feat: handle possession procedures and periods"
```

---

### Task 9: Score/time reconciliation and final invariants

**Files:**

- Modify: `src/nba_live_totals/domain/possession_parser.py`
- Modify: `src/nba_live_totals/domain/possession_hashing.py`
- Create: `tests/unit/domain/test_possession_reconciliation.py`
- Create: `tests/unit/domain/test_possession_regressions.py`

- [ ] **Step 1: Write RED reconciliation/regression tests**

Cover explained deltas only; FG/FT/goaltending score; regression; missing checkpoint warning;
combined scoring `pointsTotal`; technical defense points; between-possession baseline;
terminal unattributed baseline; final score; offense/defense points; score-diff sign; cumulative
team counters; regulation/OT remaining time; clock-increase warning; complete issue taxonomy;
all spec invariants; deterministic revision/rows/output; version/input invalidation; and
snapshot/run/claim UUID exclusion.

- [ ] **Step 2: Implement final audit and hash**

Update score only from signals, then reconcile checkpoints. Finalize rows only at explicit
boundaries. Compute output SHA only for a valid complete result using the exact typed row
projection. Invalid diagnostics never become publishable output.

- [ ] **Step 3: Run the full domain/normalizer lane and commit**

```powershell
uv run pytest tests/unit/domain tests/unit/infrastructure/nba/test_possession_normalizer*.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/domain tests/unit/domain
git commit -m "feat: reconcile deterministic possession outputs"
```

---

### Task 10: PostgreSQL Phase 3 control schema

**Files:**

- Create: `alembic/versions/0003_possession_reconstruction.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/schema.py`
- Modify: `tests/unit/test_historical_schema_contract.py`
- Create: `tests/unit/test_possession_schema_contract.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_possession_schema.py`

**Creates:** `possession_rebuild_runs`, `possession_snapshot_attempts`,
`possession_rebuild_resources` and `clickhouse_schema_migrations`.

- [ ] **Step 1: Write RED metadata and real-schema tests**

Assert exact columns, defaults, enum/check values, run counters, signed BIGINT bounds,
lowercase hex/version/error constraints, all-null/all-non-null active tuple, publication-state
rules, attempt state-dependent fields, bounded JSON issues, unique game/generation and
game/snapshot keys, real run/game/document FKs and deferrable composite active/published
snapshot FKs. Keep the ten Phase 2 tables protected as a subset rather than an exact total.

- [ ] **Step 2: Run RED**

```powershell
uv run pytest tests/unit/test_historical_schema_contract.py tests/unit/test_possession_schema_contract.py -q
uv run pytest tests/integration/test_migrations.py tests/integration/test_possession_schema.py -q -m integration
```

- [ ] **Step 3: Implement migration and shared Core metadata**

Create tables in dependency order: runs, attempts, resources, ClickHouse ledger. The circular
resource/attempt references are `DEFERRABLE INITIALLY DEFERRED`. Initial resource state is
absent, claim/counters zero, next generation one. Downgrade removes only these four tables in
reverse order.

- [ ] **Step 4: Verify migration idempotency and gates**

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://nba:nba@localhost:5432/nba"
uv run alembic heads
uv run alembic upgrade head
uv run alembic upgrade head
uv run pytest tests/unit/test_historical_schema_contract.py tests/unit/test_possession_schema_contract.py -q
uv run pytest tests/integration/test_migrations.py tests/integration/test_possession_schema.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

- [ ] **Step 5: Commit**

```powershell
git add alembic/versions/0003_possession_reconstruction.py src/nba_live_totals/infrastructure/postgres/schema.py tests/unit/test_historical_schema_contract.py tests/unit/test_possession_schema_contract.py tests/integration/test_migrations.py tests/integration/test_possession_schema.py
git commit -m "feat: add possession control schema"
```

---

### Task 11: PostgreSQL game selection, immutable input and claims

**Files:**

- Create: `src/nba_live_totals/infrastructure/postgres/possession_store.py`
- Modify: `src/nba_live_totals/infrastructure/postgres/__init__.py`
- Create: `tests/integration/possession_harness.py`
- Create: `tests/integration/test_possession_work_store_claims.py`

- [ ] **Step 1: Write RED selection/claim tests**

Cover final eligible Regular/Playoffs selection and deterministic ordering; missing/failed or
unapplied PBP; immutable raw/canonical loading; contiguous sequence; exact hashes/token;
current skip; version invalidation; forced rebuild; same-owner resume; foreign live lease BUSY;
exact-expiry reclaim; concurrent missing resource row; signed BIGINT exhaustion; new UUID and
generation on every new/reclaimed claim.

- [ ] **Step 2: Implement the literal lock order**

Every claim transaction locks:

```text
Phase 2 PBP ingestion_resources FOR UPDATE
→ games FOR SHARE
→ possession_rebuild_resources FOR UPDATE
→ possession_snapshot_attempts
→ run row only if needed
```

Under the resource lock, checked-increment claim version, next generation and attempt count;
supersede an expired attempt before allocating UUIDv4. Same-run live resume increments
nothing. Read raw and canonical PBP under the Phase 2 lock; never use `games.updated_at` as a
fence.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/integration/test_possession_work_store_claims.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/postgres tests/integration/possession_harness.py tests/integration/test_possession_work_store_claims.py
git commit -m "feat: claim possession reconstruction work"
```

---

### Task 12: PostgreSQL attempt transitions, publication and current reads

**Files:**

- Modify: `src/nba_live_totals/infrastructure/postgres/possession_store.py`
- Create: `tests/integration/test_possession_work_store_fencing.py`
- Create: `tests/integration/test_possession_current_resolution.py`

- [ ] **Step 1: Write RED transition/fencing tests**

Cover `allocated→writing→verified→published`; poison/supersede; expected count/hash before
I/O; stale mutation rejection for every method; PBP and game-context race; failed candidate
preserving fresh old publication; changed input making it effectively stale; exact current
token resolution; read-only verify; interrupted run; and uncertain `writing` preservation.
Add concurrent lock-observation tests proving that **both** pre-ClickHouse and
post-ClickHouse `resolve_current()` phases acquire the complete standard lock order and cannot
observe a mixed PBP/game/resource snapshot.

- [ ] **Step 2: Implement fenced methods and two-phase read token**

Every mutation requires active run, claim version, snapshot ID and compatible attempt state.
Publication reacquires the standard locks, recomputes the complete input token, checks exact
verification metadata, updates attempt and pointer atomically, clears the lease and marks
cached state current. Each `resolve_current()` call uses the same ordered PBP-resource
`FOR UPDATE` → game `FOR SHARE` → possession-resource `FOR UPDATE` → attempt-row locks as
claim/publication. It never trusts cached current without token comparison. The application
still performs this complete transaction twice around the ClickHouse read.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/integration/test_possession_work_store_fencing.py tests/integration/test_possession_current_resolution.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/postgres/possession_store.py tests/integration/test_possession_work_store_fencing.py tests/integration/test_possession_current_resolution.py
git commit -m "feat: fence possession snapshot publication"
```

---

### Task 13: ClickHouse package, DDL and migration runner

**Files:**

- Replace: `src/nba_live_totals/infrastructure/clickhouse.py`
- Create: `src/nba_live_totals/infrastructure/clickhouse/__init__.py`
- Create: `src/nba_live_totals/infrastructure/clickhouse/checker.py`
- Create: `src/nba_live_totals/infrastructure/clickhouse/client.py`
- Create: `src/nba_live_totals/infrastructure/clickhouse/migrations.py`
- Create: `clickhouse/migrations/0001_possessions.sql`
- Create: `clickhouse/migrations/0001_possessions.schema.json`
- Create: `scripts/migrate_clickhouse.py`
- Create: `tests/unit/infrastructure/clickhouse/__init__.py`
- Create: `tests/unit/infrastructure/clickhouse/test_client.py`
- Create: `tests/unit/infrastructure/clickhouse/test_migrations.py`
- Create: `tests/unit/test_migrate_clickhouse_cli.py`
- Create: `tests/integration/test_clickhouse_migrations.py`
- Modify: `docker/api.Dockerfile`
- Modify: `tests/unit/test_compose_contract.py`

- [ ] **Step 1: Write RED package/client tests**

Preserve `from nba_live_totals.infrastructure.clickhouse import ClickHouseChecker` so lifespan
does not change. Test authenticated parameterized command/query/one-block JSONEachRow,
operation timeout independent from readiness, `trust_env=False`, safe error codes and no
response body/SQL/credential leakage.

- [ ] **Step 2: Write RED DDL/migration tests**

Assert exact `nba.possessions_v1` types/enums, `ReplacingMergeTree(insert_version)`, partition
and order key; exact migration bytes checksum; PostgreSQL session advisory lock; applying then
applied ledger; idempotent replay; crash after DDL; actual-schema recheck even for applied
ledger; checksum/fingerprint/missing-table drift; forward-only policy; and absence of startup
migration behavior.

- [ ] **Step 3: Run RED**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse tests/unit/test_migrate_clickhouse_cli.py tests/unit/test_compose_contract.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py -q -m integration
```

- [ ] **Step 4: Implement package conversion and migration contract**

Use no new third-party ClickHouse dependency. DDL statements have an explicit delimiter and
are independently idempotent. Fingerprint exact ordered `system.tables/system.columns` typed
projection against the committed schema JSON generated for pinned
`clickhouse:26.3.25.2-jammy`. The API image copies `clickhouse/` before `USER app`; FastAPI
startup remains unchanged.

- [ ] **Step 5: Runtime migration and task gates**

```powershell
docker compose config --quiet
docker compose up -d --wait postgres clickhouse
$env:DATABASE_URL = "postgresql+asyncpg://nba:nba@localhost:5432/nba"
$env:CLICKHOUSE_URL = "http://localhost:8123"
$env:CLICKHOUSE_USER = "nba"
$env:CLICKHOUSE_PASSWORD = "nba"
uv run alembic upgrade head
uv run python -m scripts.migrate_clickhouse
uv run python -m scripts.migrate_clickhouse
uv run pytest tests/unit/infrastructure/clickhouse tests/unit/test_migrate_clickhouse_cli.py -q
uv run pytest tests/integration/test_clickhouse_migrations.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

- [ ] **Step 6: Commit**

```powershell
git add src/nba_live_totals/infrastructure/clickhouse.py src/nba_live_totals/infrastructure/clickhouse clickhouse scripts/migrate_clickhouse.py docker/api.Dockerfile tests/unit/infrastructure/clickhouse tests/unit/test_migrate_clickhouse_cli.py tests/unit/test_compose_contract.py tests/integration/test_clickhouse_migrations.py
git commit -m "feat: migrate ClickHouse possession schema"
```

---

### Task 14: Immutable ClickHouse snapshot adapter

**Files:**

- Create: `src/nba_live_totals/infrastructure/clickhouse/possessions.py`
- Modify: `src/nba_live_totals/infrastructure/clickhouse/__init__.py`
- Create: `tests/unit/infrastructure/clickhouse/test_possession_snapshot_store.py`
- Create: `tests/integration/test_possession_snapshot_store.py`

- [ ] **Step 1: Write RED serialization/readback tests**

Cover a single ordered JSONEachRow request; `insert_version == generation`; shared
identity/version/partition; exact filters on season year, game and snapshot ID; `FINAL ORDER
BY possession_number`; typed reconstruction before hashing; exact/zero/fewer/extra/changed
rows; contiguity and boundaries; duplicate equal/lower/higher versions; old/orphan exact-ID
read; and no dependence on merges or `OPTIMIZE`.

- [ ] **Step 2: Implement exact-ID immutable persistence**

The adapter never selects latest/current, never mutates rows and never retries a sent snapshot
ID. Query parameters—not interpolated identifiers—carry filters. Verification returns bounded
safe status without raw row/response content.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/infrastructure/clickhouse/test_possession_snapshot_store.py -q
uv run pytest tests/integration/test_possession_snapshot_store.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/infrastructure/clickhouse tests/unit/infrastructure/clickhouse tests/integration/test_possession_snapshot_store.py
git commit -m "feat: persist immutable possession snapshots"
```

---

### Task 15: Possession build orchestration and resilience

**Files:**

- Create: `src/nba_live_totals/application/possessions/service.py`
- Modify: `src/nba_live_totals/application/possessions/__init__.py`
- Create: `tests/unit/application/possession_fakes.py`
- Create: `tests/unit/application/test_possession_service.py`
- Create: `tests/unit/application/test_possession_resilience.py`
- Modify: `src/nba_live_totals/observability/logging.py`
- Modify: `tests/unit/test_observability.py`

- [ ] **Step 1: Write RED happy-path/mode tests**

Test deterministic selection, bounded concurrency, default build, current skip, forced rebuild
with new attempt but same content identity, verify success/failure, BUSY accounting, invalid
parse without ClickHouse, exact publication order and result status/counters.

- [ ] **Step 2: Write RED crash/cancellation/safe-log tests**

Test expected metadata before write; response loss after INSERT followed only by readback;
exact readback publish; partial/different poison; unavailable readback leaves `writing`; stale
input/fence; per-game failure continuation; cancellation stopping new claims and best-effort
interrupted finalization; cleanup failure not masking primary error; and absence of unsafe data
from logs.

- [ ] **Step 3: Implement orchestration in this exact order**

```text
start run → select → bounded claim
→ normalize → parse
→ fail invalid OR mark writing
→ one INSERT request → exact readback
→ mark verified → fenced publish
→ finish run
```

Verify mode performs two PostgreSQL current resolutions around one ClickHouse exact read and
writes only its audit run/counters. It does not normalize, parse, write ClickHouse, publish or
mutate freshness.

- [ ] **Step 4: Extend safe observability**

Allow only the spec Phase 3 fields and one terminal per-game event. Omit nulls. Never pass raw
description/signal/payload, SQL/HTTP details, credentials or exception strings.

- [ ] **Step 5: Run gates and commit**

```powershell
uv run pytest tests/unit/application/test_possession_models.py tests/unit/application/test_possession_service.py tests/unit/application/test_possession_resilience.py tests/unit/test_observability.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add src/nba_live_totals/application/possessions src/nba_live_totals/observability/logging.py tests/unit/application tests/unit/test_observability.py
git commit -m "feat: orchestrate possession reconstruction"
```

---

### Task 16: Build/rebuild/verify CLI

**Files:**

- Create: `scripts/build_possessions.py`
- Create: `tests/unit/test_build_possessions_cli.py`

- [ ] **Step 1: Write RED selector/exit/composition tests**

Cover single season, complete range, game only with single season, rebuild/verify mutual
exclusion, missing selectors, absence of `--refresh`, all exit codes, exact stdout summary,
Ctrl+C, invalid settings/schema, exact version/concurrency/timeout wiring, dedicated
analytical ClickHouse client, deterministic cleanup order and primary-error preservation.

- [ ] **Step 2: Implement CLI contract**

Summary:

```text
run=<uuid> status=<status> discovered=N materialized=N skipped_current=N verified=N failed=N busy=N
```

Return `0` only when failed/busy are zero, `1` for partial/failed/busy/stale verify, `2` for
arguments/settings/schema, `130` for interrupt. Configure logging first. Create a PostgreSQL
engine and a dedicated `httpx.AsyncClient` with operation timeout and `trust_env=False`; close
ClickHouse then PostgreSQL in `finally`. Do not reuse the lifespan readiness client.

- [ ] **Step 3: Run gates and commit**

```powershell
uv run pytest tests/unit/test_build_possessions_cli.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
git add scripts/build_possessions.py tests/unit/test_build_possessions_cli.py
git commit -m "feat: add possession build CLI"
```

---

### Task 17: Cross-store E2E, live canary and operator acceptance

**Files:**

- Create: `tests/fixtures/nba/possessions/possession_schedule_2098-99.json`
- Create: `tests/fixtures/nba/possessions/possession_box_score_0029899001.json`
- Create: `tests/fixtures/nba/possessions/possession_play_by_play_0029899001.json`
- Create: `tests/integration/test_possession_snapshot_protocol.py`
- Create: `tests/e2e/test_possession_reconstruction.py`
- Create: `tests/live/test_nba_possession_canary.py`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `tests/unit/test_compose_contract.py`
- Modify: `tests/unit/test_operator_documentation.py`

- [ ] **Step 1: Build realistic isolated fixture E2E**

Use far-future test identities and never the old mapper fixture. Prove Phase 2 stored PBP →
normalize → parse → ClickHouse write/readback → PostgreSQL publish, with exact count, teams,
boundaries, reasons, scores and output hash. Prove unchanged skip, forced rebuild identity,
verify no-write behavior, changed/invalid PBP, response loss, poison/rotation, orphan
invisibility and two-phase current-read race.

- [ ] **Step 2: Add real cross-store crash protocol tests**

Cover crash before write, after verification and before publish; stale worker rows; PBP/game
context mid-build; unavailable ClickHouse; exact resume; reclaim superseding rather than
adopting an old attempt; and old valid publication preservation.

- [ ] **Step 3: Add opt-in live canary**

Require explicit `TEST_NBA_SEASON` and `TEST_NBA_GAME_ID` plus empty isolated PostgreSQL and
ClickHouse. Ingest one completed game through Phase 2, build and verify Phase 3, rerun with
zero additional NBA requests/ClickHouse writes, read only through the PG pointer, then clean
both stores. Unsupported provider forms fail closed and require a reviewed golden/version
bump—never weaken the canary.

- [ ] **Step 4: Complete Docker/Makefile/README contracts**

Expose Phase 3 settings, but add no service. Add `migrate-clickhouse` and
`build-possessions` targets with selector/mode validation. Document migration order, all CLI
modes, no Phase 3 networking, authoritative PG pointer, immutable/orphan/poisoned/writing
semantics, exit codes, Ctrl+C, live safety and Phase 4+ exclusions. Health/API contracts stay
unchanged and image UID stays non-root.

- [ ] **Step 5: Run focused acceptance and commit**

```powershell
uv run pytest tests/integration/test_possession_snapshot_protocol.py -q -m integration
uv run pytest tests/e2e/test_possession_reconstruction.py -q -m e2e
uv run pytest tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py -q
docker compose config --quiet
git diff --check
git add tests/fixtures/nba/possessions tests/integration/test_possession_snapshot_protocol.py tests/e2e/test_possession_reconstruction.py tests/live/test_nba_possession_canary.py compose.yaml .env.example Makefile README.md tests/unit/test_compose_contract.py tests/unit/test_operator_documentation.py
git commit -m "test: verify Phase 3 possession reconstruction"
```

---

## Final acceptance gate

- [ ] Apply both schema systems in their required order and repeat them idempotently:

```powershell
docker compose up -d --wait postgres clickhouse redis
$env:DATABASE_URL = "postgresql+asyncpg://nba:nba@localhost:5432/nba"
$env:CLICKHOUSE_URL = "http://localhost:8123"
$env:CLICKHOUSE_USER = "nba"
$env:CLICKHOUSE_PASSWORD = "nba"
uv run alembic upgrade head
uv run alembic upgrade head
uv run python -m scripts.migrate_clickhouse
uv run python -m scripts.migrate_clickhouse
```

- [ ] Run all automated gates without live networking:

```powershell
uv lock --check
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
uv run pytest tests/unit tests/api -v
uv run pytest tests/integration -v -m integration
uv run pytest tests/e2e -v -m e2e
uv run pytest -q -m "not live"
git diff --check
```

- [ ] Run container acceptance:

```powershell
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.build_possessions --help
docker compose exec -T api id -u
```

Expected: all four services healthy; migration and help exit zero; UID is not `0`; `/health`
and `/health/ready` retain their Phase 1 contracts; no public game/prediction route appears.

- [ ] Run one explicit live canary before claiming Phase 3 runtime-complete:

```powershell
$env:TEST_NBA_SEASON = "2022-23"
$env:TEST_NBA_GAME_ID = "0022200001"
uv run pytest tests/live/test_nba_possession_canary.py -q -m live
```

If the environment cannot reach NBA.com, record the safe error and do not claim this gate
passed. Fixture evidence is not a substitute for the live gate.

- [ ] Request final code/runtime review with the spec, this plan, commit range, full gate
outputs, Docker health, both migration outputs, idempotency counts, crash/fencing evidence,
live-canary output and confirmation that no unsafe data was logged. Resolve every finding in a
new RED/GREEN fix commit and repeat the final gate.

## Dependency order

```text
Task 1 domain types
  → Task 2 hashes
  → Task 3 application contracts/settings
  → Tasks 4–6 NBA normalizer
  → Tasks 7–9 pure parser/reconciliation
  → Task 10 PostgreSQL schema
  → Tasks 11–12 PostgreSQL lifecycle
  → Tasks 13–14 ClickHouse migration/snapshots
  → Task 15 application orchestration
  → Task 16 CLI
  → Task 17 cross-store/runtime acceptance
```

## Spec coverage map

| Spec area | Plan tasks |
|---|---|
| Provider-neutral immutable contracts | 1 |
| Exact canonical identity and byte-level hashing | 2, 9, 14 |
| Application snapshots, claims, ports and settings | 3 |
| NBA V3 normative normalization matrix | 4–6 |
| Pure ownership/FT/jump/period state machine | 7–8 |
| Score/time reconciliation, issues and invariants | 9 |
| PostgreSQL schema, locks, leases and fencing | 10–12 |
| ClickHouse schema migration and fingerprint | 13 |
| Immutable writes, `FINAL` readback and poisoned recovery | 14–15, 17 |
| Build/rebuild/verify CLI and safe operations | 15–16 |
| Fixture, crash, live, Docker and documentation acceptance | 17 |
| No Phase 4+ or API/live-worker leakage | Global constraints, 17 |
