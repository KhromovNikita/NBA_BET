# Phase 2 Historical NBA Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable CLI pipeline that fetches five seasons of NBA schedules, traditional box scores, and play-by-play, preserves raw provider JSON, and idempotently persists canonical historical data in PostgreSQL.

**Architecture:** Keep the modular-monolith boundaries established in Phase 1. Immutable domain types and application ports remain provider/storage neutral; an `nba_api` anti-corruption adapter fetches raw documents and maps them after raw persistence; one SQLAlchemy Core store owns explicit PostgreSQL transaction boundaries and resource fencing.

**Tech Stack:** Python 3.12–3.13, `nba_api` 1.11.4, asyncio, SQLAlchemy 2 async/Core, asyncpg, PostgreSQL 17, Alembic, Pydantic Settings, pytest, Ruff, strict mypy, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-31-phase-2-historical-ingestion-design.md`

## Global Constraints

- Read the spec before every task; it is authoritative when this plan omits explanatory context.
- Domain/application modules must not import `nba_api`, SQLAlchemy, FastAPI, Redis, or ClickHouse clients.
- Use only `ScheduleLeagueV2`, `BoxScoreTraditionalV3`, and `PlayByPlayV3` upstream contracts.
- Include only game ID prefixes `002` (Regular Season) and `004` (Playoffs); exclude `001` and `003`; reject unknown prefixes.
- Persist raw JSON before provider-specific parsing; never emit raw payloads, DSNs, headers, or proxy values in logs/errors.
- PostgreSQL is the only Phase 2 store. Do not add ClickHouse tables, Redis state, queues, workers, schedulers, or HTTP game routes.
- Default network policy: concurrency `1`, minimum interval `0.75s`, timeout `15s`, `4` attempts, backoff `1s..15s`, lease `600s`.
- A failed game resource must not stop other resources; partial completion returns CLI exit code `1`.
- `--refresh` refetches, normal resume skips succeeded resources and reparses existing raw failed resources before networking.
- Use TDD for every behavior. Run the focused RED test before implementation and preserve its failure output in the task report.
- Use `apply_patch` for source, tests, migrations, fixtures, and docs. Formatting and lockfile generation may use their native commands.
- Every task must pass focused tests, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy src scripts tests` before commit.
- Work directly on `main` only because the user previously declined an isolated worktree; do not rewrite unrelated user changes.

## Planned File Structure

```text
scripts/
    __init__.py                         # makes python -m scripts.backfill_games stable
    backfill_games.py                   # CLI parsing and composition root
src/nba_live_totals/
    domain/
        __init__.py
        identifiers.py                  # SeasonId, GameId and positive-ID validation
        games.py                        # SeasonType, GameStatus, Team, Player, Game
        source.py                       # recursive JSON types and SourceDocument
        box_scores.py                   # TraditionalStats and box-score aggregates
        play_by_play.py                 # PlayByPlayEvent and sequence validation
    application/ingestion/
        __init__.py
        models.py                       # request/result/resource/claim types
        ports.py                        # HistoricalDataSource and store protocols
        service.py                      # resumable orchestration
    infrastructure/nba/
        __init__.py
        errors.py                       # safe provider error taxonomy
        transport.py                    # to_thread, rate limiter, retry, NBAStatsHTTP
        source.py                       # SourceDocument construction and endpoints
        mappers/
            __init__.py
            common.py                   # strict payload access and conversion helpers
            schedule.py
            box_score.py
            play_by_play.py
    infrastructure/postgres/
        __init__.py
        schema.py                       # shared SQLAlchemy Core Table definitions
        ingestion_store.py              # resource state/raw/canonical persistence
alembic/versions/0002_historical_ingestion.py
tests/
    fixtures/nba/*.json
    unit/domain/test_*.py
    unit/application/test_ingestion_*.py
    unit/infrastructure/nba/test_*.py
    integration/test_historical_schema.py
    integration/test_ingestion_store.py
    e2e/test_historical_backfill.py
    live/test_nba_ingestion_canary.py
```

---

### Task 1: Dependency, settings, and quality-gate contract

**Files:**
- Modify: `pyproject.toml:7-52`
- Modify: `src/nba_live_totals/settings.py:1-36`
- Modify: `.env.example:8-12`
- Create: `scripts/__init__.py`
- Modify: `tests/unit/test_settings.py`
- Modify: `uv.lock` (generated by `uv lock`)

**Interfaces:**
- Consumes: Phase 1 frozen `Settings` model.
- Produces: `Settings.nba_api_timeout_seconds: PositiveFloat`, `nba_api_max_concurrency: PositiveInt`, `nba_api_min_interval_seconds: NonNegativeFloat`, `nba_api_max_attempts: PositiveInt`, `nba_api_backoff_base_seconds: PositiveFloat`, `nba_api_backoff_max_seconds: PositiveFloat`, and `nba_api_resource_lease_seconds: PositiveInt`.

- [ ] **Step 1: Write failing settings tests**

Add parameterized assertions to `tests/unit/test_settings.py`:

```python
def test_nba_ingestion_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.nba_api_timeout_seconds == 15.0
    assert settings.nba_api_max_concurrency == 1
    assert settings.nba_api_min_interval_seconds == 0.75
    assert settings.nba_api_max_attempts == 4
    assert settings.nba_api_backoff_base_seconds == 1.0
    assert settings.nba_api_backoff_max_seconds == 15.0
    assert settings.nba_api_resource_lease_seconds == 600


def test_backoff_max_cannot_be_less_than_base() -> None:
    with pytest.raises(ValidationError, match="backoff max"):
        Settings(
            _env_file=None,
            nba_api_backoff_base_seconds=2.0,
            nba_api_backoff_max_seconds=1.0,
        )
```

Extend the existing non-positive test with concurrency, attempts, base/max backoff, and lease. Add a dedicated case proving `nba_api_min_interval_seconds=0` is valid and `-0.1` is rejected.

- [ ] **Step 2: Run the RED test**

Run: `uv run pytest tests/unit/test_settings.py -q`

Expected: FAIL because the six new fields and cross-field validator do not exist.

- [ ] **Step 3: Add dependency and settings implementation**

In `pyproject.toml`, add direct dependencies:

```toml
"nba_api>=1.11.4,<1.12",
"requests>=2.32.3,<3",
```

Add `"live: makes opt-in requests to NBA.com and requires explicit test IDs"` to pytest markers. Change strict mypy files to `files = ["src", "scripts", "tests"]`.

Create an empty `scripts/__init__.py` in the same step so the new mypy target exists
from this commit onward.

In `settings.py`, import `NonNegativeFloat` and `model_validator`, add the fields with the exact defaults above, and add:

```python
@model_validator(mode="after")
def validate_nba_backoff(self) -> "Settings":
    if self.nba_api_backoff_max_seconds < self.nba_api_backoff_base_seconds:
        raise ValueError("NBA API backoff max must be greater than or equal to base")
    return self
```

Mirror all six new settings in `.env.example` with uppercase names and exact defaults.

- [ ] **Step 4: Generate and verify the lockfile**

Run: `uv lock`

Run: `uv sync --frozen`

Expected: both exit `0`; `uv.lock` resolves `nba_api` 1.11.4 and compatible requests/pandas/numpy packages for Python 3.12–3.13.

- [ ] **Step 5: Run task gates**

Run:

```bash
uv run pytest tests/unit/test_settings.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
uv lock --check
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .env.example scripts/__init__.py src/nba_live_totals/settings.py tests/unit/test_settings.py
git commit -m "build: configure historical NBA ingestion"
```

---

### Task 2: Core domain identifiers, games, and raw source documents

**Files:**
- Create: `src/nba_live_totals/domain/__init__.py`
- Create: `src/nba_live_totals/domain/identifiers.py`
- Create: `src/nba_live_totals/domain/games.py`
- Create: `src/nba_live_totals/domain/source.py`
- Create: `tests/unit/domain/__init__.py`
- Create: `tests/unit/domain/test_games.py`
- Create: `tests/unit/domain/test_source.py`

**Interfaces:**
- Produces: `SeasonId`, `GameId`, `SeasonType`, `GameStatus`, `Team`, `Player`, `Game`, `SeasonSchedule`, `JsonValue`, `JsonObject`, `SourceDocument`, `canonical_json_checksum()`.
- Validation: IDs, positive numeric identities, timezone-aware UTC datetimes, distinct teams, final scores, and canonical SHA-256.

- [ ] **Step 1: Write failing identifier and game tests**

Create tests covering these exact contracts:

```python
def test_season_and_game_ids_validate_and_classify() -> None:
    assert SeasonId("2025-26").start_year == 2025
    assert GameId("0022500123").season_type is SeasonType.REGULAR_SEASON
    assert GameId("0042500101").season_type is SeasonType.PLAYOFFS
    assert GameId("0012500001").is_excluded
    assert GameId("0032500001").is_excluded

    with pytest.raises(ValueError, match="season"):
        SeasonId("2025-27")
    with pytest.raises(ValueError, match="game type"):
        GameId("0092500123")


def test_final_game_requires_distinct_teams_scores_and_utc() -> None:
    with pytest.raises(ValueError, match="scores"):
        make_game(status=GameStatus.FINAL, home_score=None)
    with pytest.raises(ValueError, match="distinct"):
        make_game(home_team_id=1, away_team_id=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        make_game(started_at=datetime(2026, 1, 1, 1, 0))
```

`make_game()` in the test returns a fully specified `Game` using game ID `0022500123`, season `2025-26`, two teams, nonnegative final scores, and a UTC datetime.

- [ ] **Step 2: Write failing source-document tests**

```python
def test_source_document_checksum_is_format_independent() -> None:
    left = {"b": [2, 1], "a": {"value": True}}
    right = {"a": {"value": True}, "b": [2, 1]}

    assert canonical_json_checksum(left) == canonical_json_checksum(right)
    assert len(canonical_json_checksum(left)) == 64


def test_source_document_requires_utc_and_matching_checksum() -> None:
    payload: JsonObject = {"game": {"gameId": "0022500123"}}
    with pytest.raises(ValueError, match="checksum"):
        SourceDocument(
            source="nba.com",
            endpoint="playbyplayv3",
            resource_key="play_by_play:0022500123",
            requested_at=datetime.now(UTC),
            request_parameters={"GameID": "0022500123"},
            payload=payload,
            checksum_sha256="0" * 64,
        )
```

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/domain/test_games.py tests/unit/domain/test_source.py -q`

Expected: collection FAIL because `nba_live_totals.domain` does not exist.

- [ ] **Step 4: Implement exact immutable domain contracts**

Use frozen, slotted dataclasses. `SeasonId` and `GameId` expose `value: str`; `GameId.season_type` raises for unknown prefixes and `is_excluded` is true only for `001`/`003`. Define:

```python
class SeasonType(StrEnum):
    REGULAR_SEASON = "regular_season"
    PLAYOFFS = "playoffs"


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
```

`Team` fields: `team_id`, `city`, `name`, `tricode`, `slug`. `Player` fields: `player_id`, `first_name`, `family_name`, `full_name`. `Game` fields exactly match spec section 6.2. `SeasonSchedule` contains `season`, `teams: tuple[Team, ...]`, and `games: tuple[Game, ...]` and rejects duplicate IDs.

In `source.py`, use recursive PEP 695 aliases and canonical serialization:

```python
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def canonical_json_checksum(payload: JsonObject) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 5: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/domain/test_games.py tests/unit/domain/test_source.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/nba_live_totals/domain tests/unit/domain
git commit -m "feat: define historical NBA domain types"
```

---

### Task 3: Box-score and play-by-play domain contracts

**Files:**
- Create: `src/nba_live_totals/domain/box_scores.py`
- Create: `src/nba_live_totals/domain/play_by_play.py`
- Modify: `src/nba_live_totals/domain/__init__.py`
- Create: `tests/unit/domain/test_box_scores.py`
- Create: `tests/unit/domain/test_play_by_play.py`

**Interfaces:**
- Consumes: `GameId`, `Team`, `Player` from Task 2.
- Produces: `TraditionalStats`, `TeamBoxScore`, `PlayerBoxScore`, `BoxScore`, `PlayByPlayEvent`, and `validate_play_by_play()`.

- [ ] **Step 1: Write failing box-score aggregate tests**

Test a valid two-team final box score, then assert rejection of duplicate players, a third team, an unknown player team, and team points that disagree with final scores. Explicitly verify DNP values remain `None`:

```python
def test_dnp_player_does_not_gain_false_zero_statistics() -> None:
    row = PlayerBoxScore(
        game_id=GameId("0022500123"),
        player=Player(1, "Bench", "Player", "Bench Player"),
        team_id=10,
        position=None,
        jersey_number="9",
        starter=False,
        played=False,
        comment="DNP - Coach's Decision",
        statistics=TraditionalStats.empty(),
    )

    assert row.statistics.points is None
    assert row.statistics.seconds_played is None
```

- [ ] **Step 2: Write failing PBP tests**

```python
def test_play_by_play_orders_actions_and_rejects_duplicates() -> None:
    events = (make_event(7), make_event(3), make_event(5))
    assert tuple(event.action_number for event in validate_play_by_play(events)) == (3, 5, 7)

    with pytest.raises(ValueError, match="duplicate action"):
        validate_play_by_play((make_event(3), make_event(3)))


def test_completed_game_cannot_have_empty_play_by_play() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_play_by_play((), completed=True)
```

Also cover a substitution event, overtime period `5`, nullable `person_id`, and clock milliseconds boundaries.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/domain/test_box_scores.py tests/unit/domain/test_play_by_play.py -q`

Expected: import FAIL for both new modules.

- [ ] **Step 4: Implement box-score types**

`TraditionalStats` has these exact nullable fields:

```text
seconds_played
field_goals_made, field_goals_attempted, field_goals_percentage
three_pointers_made, three_pointers_attempted, three_pointers_percentage
free_throws_made, free_throws_attempted, free_throws_percentage
rebounds_offensive, rebounds_defensive, rebounds_total
assists, steals, blocks, turnovers, fouls_personal, points, plus_minus
```

Counts are `int | None`; percentages and plus/minus are `Decimal | None`. Reject negative attempts/made/counting stats, made greater than attempted, and percentages outside `[0, 1]`. `empty()` returns every field as `None`.

`BoxScore` contains `game_id`, `home_team_id`, `away_team_id`, final scores, exactly two `TeamBoxScore` rows, and player rows. Enforce every invariant listed in spec section 6.3 in `__post_init__`.

- [ ] **Step 5: Implement PBP contract**

`PlayByPlayEvent` fields exactly match spec section 6.4. Require positive period/action number, nonnegative clock milliseconds/scores/shot distance, and a ten-digit game ID. `validate_play_by_play(events, completed=True)` requires nonempty events, one game ID, unique action numbers, and returns a sorted tuple.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/domain/test_box_scores.py tests/unit/domain/test_play_by_play.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/domain tests/unit/domain
git commit -m "feat: model box scores and play-by-play"
```

---

### Task 4: Application ingestion contracts and state machine

**Files:**
- Create: `src/nba_live_totals/application/ingestion/__init__.py`
- Create: `src/nba_live_totals/application/ingestion/models.py`
- Create: `src/nba_live_totals/application/ingestion/ports.py`
- Create: `tests/unit/application/__init__.py`
- Create: `tests/unit/application/test_ingestion_models.py`

**Interfaces:**
- Consumes: all Task 2–3 domain types.
- Produces: `ResourceType`, `ResourceKey`, `ClaimAction`, `ResourceClaim`, `StoredSourceDocument`, `ClaimDecision`, `BackfillRequest`, `BackfillStatus`, `BackfillResult`, `IngestionResourceError`, `ClaimLostError`, `HistoricalDataSource`, and `HistoricalIngestionStore` with signatures from spec section 8.

- [ ] **Step 1: Write failing request/range tests**

```python
def test_backfill_request_expands_inclusive_season_range() -> None:
    request = BackfillRequest.for_range(SeasonId("2023-24"), SeasonId("2025-26"))
    assert request.seasons == (
        SeasonId("2023-24"),
        SeasonId("2024-25"),
        SeasonId("2025-26"),
    )


def test_backfill_request_rejects_reverse_range_and_mismatched_game() -> None:
    with pytest.raises(ValueError, match="range"):
        BackfillRequest.for_range(SeasonId("2025-26"), SeasonId("2024-25"))
    with pytest.raises(ValueError, match="season"):
        BackfillRequest(
            seasons=(SeasonId("2024-25"), SeasonId("2025-26")),
            game_id=GameId("0022500123"),
        )
```

The targeted-game rule is one explicit season only. A range plus `game_id` is invalid.

- [ ] **Step 2: Write failing resource/claim tests**

Verify stable keys `schedule:2025-26`, `box_score:0022500123`, and `play_by_play:0022500123`. Assert `FETCH`/`REPARSE` require a claim, `REPARSE` requires a stored document, and `SKIP`/`BUSY` reject both.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/application/test_ingestion_models.py -q`

Expected: import FAIL because the ingestion package does not exist.

- [ ] **Step 4: Implement request/result and claim types**

Use frozen dataclasses and these exact status values:

```python
class BackfillStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class BackfillResult:
    run_id: UUID
    status: BackfillStatus
    discovered: int
    succeeded: int
    skipped: int
    failed: int
```

`BackfillRequest` contains `seasons`, nullable `game_id`, and `refresh`. `ResourceClaim` contains `key`, `run_id`, and positive `version`.

Define a provider-neutral safe error base in `models.py`:

```python
class IngestionResourceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClaimLostError(IngestionResourceError):
    def __init__(self) -> None:
        super().__init__("claim_lost")
```

This lets application orchestration classify adapter errors without importing an
infrastructure package.

- [ ] **Step 5: Add both exact protocols**

Copy the method signatures from spec sections 5.2 and 8 into `ports.py`. Decorate both
protocols with `@runtime_checkable`; protocol methods use `...` bodies and all return
types are explicit. `HistoricalIngestionStore.finish_run()` accepts a `BackfillResult`;
every resource write accepts a `ResourceClaim` fencing token.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/application/test_ingestion_models.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/application/ingestion tests/unit/application
git commit -m "feat: define ingestion application ports"
```

---

### Task 5: NBA transport, retry, rate limiting, and raw source facade

**Files:**
- Create: `src/nba_live_totals/infrastructure/nba/__init__.py`
- Create: `src/nba_live_totals/infrastructure/nba/errors.py`
- Create: `src/nba_live_totals/infrastructure/nba/transport.py`
- Create: `src/nba_live_totals/infrastructure/nba/source.py`
- Create: `tests/unit/infrastructure/nba/__init__.py`
- Create: `tests/unit/infrastructure/nba/test_transport.py`
- Create: `tests/unit/infrastructure/nba/test_source.py`

**Interfaces:**
- Consumes: `Settings` and `SourceDocument`.
- Produces: `NBAApiTransport.get(endpoint, parameters) -> JsonObject`, safe `ProviderError`, and `NBAApiDocumentFetcher.fetch_schedule/fetch_box_score/fetch_play_by_play` for exact endpoint/resource keys. The complete `HistoricalDataSource` facade is produced in Task 6 after real parsers exist.

- [ ] **Step 1: Write failing transport tests with injected sync sender**

Cover these deterministic cases without sleeping or networking:

```python
async def test_transport_retries_transient_error_then_returns_json() -> None:
    sender = StubSender([UpstreamHttpError(429), {"ok": True}])
    sleeps: list[float] = []
    transport = make_transport(sender=sender, sleep=record_into(sleeps), jitter=lambda: 0.0)

    assert await transport.get("scheduleleaguev2", {"Season": "2025-26"}) == {"ok": True}
    assert sender.calls == 2
    assert sleeps == [1.0]


async def test_transport_does_not_retry_schema_or_parent_cancellation() -> None:
    sender = StubSender([ProviderSchemaError("schema_mismatch")])
    with pytest.raises(ProviderSchemaError):
        await make_transport(sender=sender).get("x", {})
    assert sender.calls == 1

    cancelled = StubSender([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await make_transport(sender=cancelled).get("x", {})
```

Also prove semaphore max concurrency, global start spacing via injected monotonic clock, exponential cap at 15 seconds, and sanitised `str(error)` that contains no response body.

- [ ] **Step 2: Write failing source tests**

Assert exact requests:

```python
assert transport.requests == [
    ("scheduleleaguev2", {"LeagueID": "00", "Season": "2025-26"}),
    (
        "boxscoretraditionalv3",
        {
            "GameID": "0022500123",
            "StartPeriod": 0,
            "EndPeriod": 0,
            "StartRange": 0,
            "EndRange": 0,
            "RangeType": 0,
        },
    ),
    (
        "playbyplayv3",
        {"GameID": "0022500123", "StartPeriod": 0, "EndPeriod": 0},
    ),
]
```

Verify endpoint/resource keys, UTC requested time, canonical checksum, and that returned payload object is retained unchanged.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/infrastructure/nba/test_transport.py tests/unit/infrastructure/nba/test_source.py -q`

Expected: import FAIL for the NBA infrastructure package.

- [ ] **Step 4: Implement safe errors and transport**

`ProviderError` subclasses Task 4 `IngestionResourceError` and exposes only `code` and
class name. Subclasses cover timeout, connection, 429, 5xx, empty response, invalid JSON,
and schema mismatch. The synchronous production sender performs:

```python
response = NBAStatsHTTP().send_api_request(
    endpoint=endpoint,
    parameters=parameters,
    timeout=timeout_seconds,
)
if response.status_code == 429:
    raise UpstreamHttpError(429)
if response.status_code >= 500:
    raise UpstreamHttpError(response.status_code)
payload = response.get_dict()
if not isinstance(payload, dict) or not payload:
    raise EmptyProviderResponseError()
return cast(JsonObject, payload)
```

Run the injected synchronous sender through `asyncio.to_thread`, a semaphore, and a lock-protected global next-start time. Retry only errors enumerated in spec section 10. Never catch `BaseException` or `CancelledError`.

- [ ] **Step 5: Implement source-document facade**

`NBAApiDocumentFetcher` builds the exact request parameters from Step 2 and receives an injectable UTC clock. Fetch methods call transport, then construct `SourceDocument` with source `nba.com`. This task ends at the raw-document boundary; Task 6 composes it with the real parsers.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/infrastructure/nba/test_transport.py tests/unit/infrastructure/nba/test_source.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass; no real NBA request is made.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba
git commit -m "feat: add resilient NBA raw transport"
```

---

### Task 6: Strict NBA V3 payload mappers and fixtures

**Files:**
- Create: `src/nba_live_totals/infrastructure/nba/mappers/__init__.py`
- Create: `src/nba_live_totals/infrastructure/nba/mappers/common.py`
- Create: `src/nba_live_totals/infrastructure/nba/mappers/schedule.py`
- Create: `src/nba_live_totals/infrastructure/nba/mappers/box_score.py`
- Create: `src/nba_live_totals/infrastructure/nba/mappers/play_by_play.py`
- Modify: `src/nba_live_totals/infrastructure/nba/source.py`
- Create: `tests/fixtures/nba/schedule_2025-26.json`
- Create: `tests/fixtures/nba/box_score_0022500123.json`
- Create: `tests/fixtures/nba/play_by_play_0022500123.json`
- Create: `tests/unit/infrastructure/nba/test_mappers.py`

**Interfaces:**
- Consumes: Task 2–3 domain types and Task 5 `ProviderSchemaError`.
- Produces: `parse_schedule(SourceDocument) -> SeasonSchedule`, `parse_box_score(SourceDocument) -> BoxScore`, `parse_play_by_play(SourceDocument) -> tuple[PlayByPlayEvent, ...]`, and `NBAApiHistoricalDataSource` implementing the complete Task 4 protocol by composing `NBAApiDocumentFetcher` with these functions.

- [ ] **Step 1: Add minimal real-shape fixtures**

Use provider shapes, not flattened rows:

```json
{"leagueSchedule":{"seasonYear":"2025-26","gameDates":[{"gameDate":"2026-01-01","games":[{"gameId":"0022500123","gameStatus":3,"gameDateTimeUTC":"2026-01-02T01:00:00Z","arenaName":"Ball Arena","arenaCity":"Denver","arenaState":"CO","homeTeam":{"teamId":10,"teamCity":"Denver","teamName":"Nuggets","teamTricode":"DEN","teamSlug":"nuggets","score":120},"awayTeam":{"teamId":20,"teamCity":"Los Angeles","teamName":"Lakers","teamTricode":"LAL","teamSlug":"lakers","score":110}}]}]}}
```

The box-score fixture contains `boxScoreTraditional.gameId`, `homeTeam`, `awayTeam`, each team `statistics`, and one starter plus one DNP player. The PBP fixture contains `game.gameId` and three actions: made shot, substitution, and period end; include all fields listed in spec 6.4 with nullable values represented as JSON null.

- [ ] **Step 2: Write failing mapper tests**

Assert:

- schedule maps `002` and a copied `004` fixture to the correct season types;
- copied `001` and `003` games are excluded;
- copied `009` game raises `ProviderSchemaError` with code `unknown_game_type`;
- status `3` maps to final, scores/UTC/arena and both teams match;
- box score preserves starter/DNP and all Traditional V3 fields;
- PBP parses ISO clocks into milliseconds, orders actions, and preserves substitution;
- missing required nested keys raise safe `schema_mismatch` without embedding payload.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/infrastructure/nba/test_mappers.py -q`

Expected: FAIL because mapper functions still raise schema mismatch.

- [ ] **Step 4: Implement strict common conversion helpers**

`common.py` provides `require_mapping`, `require_list`, `require_str`, `require_int`, `optional_*`, `parse_utc_datetime`, `parse_iso_duration_seconds`, `parse_clock_milliseconds`, and `decimal_or_none`. Every helper raises `ProviderSchemaError("schema_mismatch")` without including the offending value or payload.

Clock parser accepts `PT11M59.00S`, `PT0M00.00S`, and overtime clocks; reject negative or malformed values.

- [ ] **Step 5: Implement schedule mapper**

Traverse `leagueSchedule.gameDates[*].games[*]`. Build `Team` and `Game` values. Map numeric statuses `1/2/3` to scheduled/live/final; map status text containing postponed/cancelled before falling back to unknown. Exclude `001`/`003`, reject unknown prefixes, and return deduplicated teams/games sorted by ID.

- [ ] **Step 6: Implement box-score mapper**

Traverse `boxScoreTraditional.homeTeam` and `awayTeam`. Map team and player statistics using this exact provider-to-domain mapping:

```text
minutes -> seconds_played
fieldGoalsMade/Attempted/Percentage
threePointersMade/Attempted/Percentage
freeThrowsMade/Attempted/Percentage
reboundsOffensive/Defensive/Total
assists, steals, blocks, turnovers, foulsPersonal, points, plusMinusPoints
```

Map player identity/name/position/jersey/starter/comment. Treat `played=false` or a nonempty DNP comment as `TraditionalStats.empty()` unless explicit statistics are present. Build the aggregate and let domain invariants reject mismatches.

- [ ] **Step 7: Implement PBP mapper**

Traverse `game.actions`, require matching game ID, map every field in spec section 6.4, then call `validate_play_by_play(..., completed=True)`. Do not foreign-key validate `personId` against box-score players.

Normalize provider `teamId=0` and `personId=0` to `None`, because neutral actions are
not valid positive team/player identities. Empty score strings become `None`; numeric
score strings become nonnegative integers.

Replace the exported facade in `source.py` with `NBAApiHistoricalDataSource`, retaining
the three fetch methods from `NBAApiDocumentFetcher` and adding synchronous parse methods
that call the three mapper functions. Add an `isinstance(source,
HistoricalDataSource)` runtime-checkable protocol assertion to the source unit test.

- [ ] **Step 8: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/infrastructure/nba -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/nba_live_totals/infrastructure/nba tests/fixtures/nba tests/unit/infrastructure/nba
git commit -m "feat: map NBA historical payloads"
```

---

### Task 7: PostgreSQL Core schema and Alembic migration

**Files:**
- Create: `src/nba_live_totals/infrastructure/postgres/__init__.py`
- Create: `src/nba_live_totals/infrastructure/postgres/schema.py`
- Modify: `src/nba_live_totals/infrastructure/metadata.py`
- Create: `alembic/versions/0002_historical_ingestion.py`
- Create: `tests/unit/test_historical_schema_contract.py`
- Create: `tests/integration/test_historical_schema.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: Phase 1 shared `metadata` and spec section 7.
- Produces: exported SQLAlchemy `Table` objects `seasons`, `teams`, `players`, `games`, `team_box_scores`, `player_box_scores`, `play_by_play_events`, `source_documents`, `ingestion_runs`, `ingestion_resources`; Alembic revision `0002_historical_ingestion` after `0001_baseline`.

- [ ] **Step 1: Write failing static schema-contract test**

Assert exact table names, primary keys, required indexes, foreign keys, `JSONB` payload columns, source-document unique constraint, resource composite primary key, and that PBP `person_id` has no foreign key. Assert no ClickHouse/Redis/possession/prediction table appears.

- [ ] **Step 2: Write failing real migration tests**

After `alembic upgrade head`, inspect PostgreSQL and assert all ten tables plus `alembic_version` exist. Run upgrade twice. Insert invalid same-team game and assert `IntegrityError`; insert a PBP person ID absent from players and assert success inside a rolled-back test transaction.

- [ ] **Step 3: Run RED tests**

Run:

```bash
uv run pytest tests/unit/test_historical_schema_contract.py -q
uv run pytest tests/integration/test_historical_schema.py tests/integration/test_migrations.py -q -m integration
```

Expected: unit import FAIL and integration table assertions FAIL.

- [ ] **Step 4: Define shared SQLAlchemy Core tables**

Use PostgreSQL `JSONB`, UUID, timezone-aware `DateTime`, `Numeric(8, 6)` for percentages and `Numeric(8, 2)` for plus/minus. Use exact canonical columns from spec sections 6–7. Add checks for ID shapes, positive identities, nonnegative scores, distinct teams, positive periods/action numbers, and allowed enum text values.

Traditional statistics use the exact snake-case domain field names from Task 3. Keep `person_id BIGINT NULL` on PBP without a foreign key. `source_documents.payload` and `request_parameters` are non-null JSONB.

Import every table in `metadata.py` so Alembic target metadata is complete without changing Phase 1 checker behavior.

- [ ] **Step 5: Write reversible migration**

Create all tables in dependency order. `downgrade()` drops only Phase 2 tables in reverse order. Use explicit constraint and index names so tests and future migrations are deterministic. Do not fetch data during migration.

- [ ] **Step 6: Run migration and static gates**

Run:

```bash
uv run pytest tests/unit/test_historical_schema_contract.py -q
uv run pytest tests/integration/test_historical_schema.py tests/integration/test_migrations.py -q -m integration
uv run alembic upgrade head
uv run alembic upgrade head
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass against the Docker PostgreSQL URL selected through `TEST_DATABASE_URL`/`DATABASE_URL`.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/infrastructure/postgres src/nba_live_totals/infrastructure/metadata.py alembic/versions/0002_historical_ingestion.py tests/unit/test_historical_schema_contract.py tests/integration/test_historical_schema.py tests/integration/test_migrations.py
git commit -m "feat: add historical ingestion schema"
```

---

### Task 8: Raw documents, runs, resource claims, and fencing

**Files:**
- Create: `src/nba_live_totals/infrastructure/postgres/ingestion_store.py`
- Create: `tests/integration/test_ingestion_store_state.py`

**Interfaces:**
- Consumes: Task 4 store protocol, Task 7 state/raw tables, async `AsyncEngine`.
- Produces: `PostgresHistoricalIngestionStore.start_run`, `finish_run`, `claim_resource`, `latest_document`, `save_document`, and `fail_resource`.

- [ ] **Step 1: Write failing raw versioning test**

Save the same `SourceDocument` twice and assert one database row and the same UUID. Save the same resource with a changed payload/checksum and assert two immutable versions ordered by `stored_at`; `latest_document()` returns the second UUID/document.

- [ ] **Step 2: Write failing claim/fencing tests**

Cover:

```text
missing resource -> FETCH with claim version 1
succeeded + refresh false -> SKIP
succeeded + refresh true -> FETCH with incremented version
failed + raw -> REPARSE with StoredSourceDocument
failed + no raw -> FETCH
unexpired foreign running claim -> BUSY
expired claim -> FETCH with incremented version
old claim save/apply/fail -> ClaimLostError; newer row remains unchanged
```

Use two store instances against the same real PostgreSQL database for the race cases.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/integration/test_ingestion_store_state.py -q -m integration`

Expected: import FAIL because the store does not exist.

- [ ] **Step 4: Implement run and resource-state operations**

Use short `engine.begin()` blocks. Claim uses PostgreSQL `INSERT ... ON CONFLICT` plus row locking/update to atomically set owner, `claimed_until`, `attempt_count`, and increment `claim_version`. Return exact `ClaimDecision` invariants from Task 4.

Every resource mutation adds `WHERE last_run_id=:run_id AND claim_version=:version`. If rowcount is not one, raise the Task 4 safe `ClaimLostError()`.

- [ ] **Step 5: Implement immutable document persistence**

Use `INSERT ... ON CONFLICT DO NOTHING` on `(source, endpoint, resource_key, checksum_sha256)`, then select the existing/new UUID. Verify the document resource key matches the claim key before SQL. Update `latest_document_id` only under the fencing predicate. Never update JSONB of an existing row.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/integration/test_ingestion_store_state.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/infrastructure/postgres/ingestion_store.py tests/integration/test_ingestion_store_state.py
git commit -m "feat: persist resumable ingestion state"
```

---

### Task 9: Atomic canonical schedule, box-score, and PBP persistence

**Files:**
- Modify: `src/nba_live_totals/infrastructure/postgres/ingestion_store.py`
- Create: `tests/integration/test_ingestion_store_canonical.py`

**Interfaces:**
- Consumes: Task 2–3 aggregates, Task 7 canonical tables, Task 8 fencing.
- Produces: `apply_schedule`, `replace_box_score`, and `replace_play_by_play` transaction semantics from spec section 8.

- [ ] **Step 1: Write failing schedule idempotency test**

Claim/save/apply one schedule twice using refresh claims. Assert one season, two teams, one game, and updated team display metadata by stable NBA ID. Assert the resource is succeeded and claim fields are cleared.

- [ ] **Step 2: Write failing snapshot replacement tests**

Apply box score and PBP fixtures. Refresh with changed documents that remove one player/action and change a score/description. Assert old game-scoped rows are absent, new rows exactly equal the new aggregate, and raw versions remain two.

- [ ] **Step 3: Write failing rollback/fencing tests**

Inject an invalid row after scoped delete and assert the transaction rolls back to the previous complete snapshot. Repeat with an expired old claim and assert it cannot replace newer canonical data.

- [ ] **Step 4: Run RED tests**

Run: `uv run pytest tests/integration/test_ingestion_store_canonical.py -q -m integration`

Expected: missing-method assertion FAIL.

- [ ] **Step 5: Implement canonical conversions and transactions**

Create private pure row functions `_season_rows`, `_team_rows`, `_game_rows`, `_team_box_rows`, `_player_rows`, `_player_box_rows`, and `_event_rows`. Explicitly unwrap `SeasonId.value` and `GameId.value`; convert `Decimal` and datetimes without stringifying.

Schedule uses PostgreSQL upserts. Box/PBP methods validate the aggregate before opening SQL, then scoped-delete and bulk-insert inside one transaction. Mark resource succeeded only after all canonical writes and under the fencing predicate.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/integration/test_ingestion_store_state.py tests/integration/test_ingestion_store_canonical.py -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/infrastructure/postgres/ingestion_store.py tests/integration/test_ingestion_store_canonical.py
git commit -m "feat: persist canonical historical NBA data"
```

---

### Task 10: Historical ingestion service happy path and targeting

**Files:**
- Create: `src/nba_live_totals/application/ingestion/service.py`
- Modify: `src/nba_live_totals/application/ingestion/__init__.py`
- Create: `tests/unit/application/test_ingestion_service.py`

**Interfaces:**
- Consumes: Task 4 ports/models and Task 2–3 domain aggregates.
- Produces: `HistoricalIngestionService(source, store, max_concurrency)` and `await backfill(request) -> BackfillResult`.

- [ ] **Step 1: Build reusable fakes and write failing happy-path test**

Use in-memory fake source/store implementing the exact protocols. One season contains one regular final, one playoff final, one excluded preseason, and one scheduled game. Assert service:

```text
starts one run
applies schedule
fetches box/PBP only for the two final accepted games
does not fetch excluded or non-final games
finishes succeeded with discovered=2, succeeded=5, failed=0
```

The five succeeded resources are one schedule plus two resources for each final game.

- [ ] **Step 2: Write failing targeted-game and skip tests**

For a request with one season and game ID, assert only that game enters the queue. When all claims return `SKIP`, assert no fetch/parse/write occurs and skipped counters match. A `BUSY` decision must produce partial status and one failure count.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/application/test_ingestion_service.py -q`

Expected: import FAIL because the service does not exist.

- [ ] **Step 4: Implement one reusable resource pipeline**

Private `_process_resource` accepts key, fetch callable, parse callable, and apply callable. Its exact order is:

```text
claim
SKIP/BUSY terminal handling
REPARSE -> use returned StoredSourceDocument
FETCH -> fetch -> save_document
parse
apply canonical aggregate
return resource outcome
```

Never parse before `save_document` returns. Use a semaphore around per-game tasks but leave transport rate limiting to Task 5.

- [ ] **Step 5: Implement schedule and per-game orchestration**

Process seasons sequentially so schedule failures are isolated by season. Sort final games by `(game_date, game_id.value)`. Per game, process box score and PBP independently; one failure cannot cancel the sibling or other games. Always call `finish_run` with computed result on ordinary completion.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/application/test_ingestion_service.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/application/ingestion tests/unit/application/test_ingestion_service.py
git commit -m "feat: orchestrate historical NBA backfill"
```

---

### Task 11: Partial failures, local reparse, logging, and cancellation

**Files:**
- Modify: `src/nba_live_totals/application/ingestion/service.py`
- Create: `tests/unit/application/test_ingestion_resilience.py`
- Modify: `tests/unit/test_observability.py`

**Interfaces:**
- Consumes: Task 10 service.
- Produces: stable safe error classification, resource-level continuation, `interrupted` finalization, and structured ingestion events.

- [ ] **Step 1: Write failing partial-failure tests**

Inject one box network failure, one PBP schema failure, and a succeeding second game. Assert both failures call `fail_resource` with safe code/type, second game completes, result is partial/code-level failed count `2`, and no exception text/raw payload is logged.

- [ ] **Step 2: Write failing local-reparse test**

Return `REPARSE` with a stored document and assert provider fetch count stays zero, parser and apply run once, and success is counted. With `refresh=True`, return a FETCH claim and prove the source is called despite prior raw data.

- [ ] **Step 3: Write failing cancellation test**

Cancel the service while a fake provider is blocked. Assert `CancelledError` propagates after `finish_run` receives `BackfillStatus.INTERRUPTED`; do not call `fail_resource` for the cancelled item.

- [ ] **Step 4: Run RED tests**

Run: `uv run pytest tests/unit/application/test_ingestion_resilience.py tests/unit/test_observability.py -q`

Expected: failures in continuation/reparse/cancellation/log fields.

- [ ] **Step 5: Implement resilience and safe logging**

Catch only ordinary `Exception` around one resource. Map
`IngestionResourceError.code`, domain validation to the three invalid-data codes, and
unknown persistence exceptions to `persistence_error`. The application service imports
no infrastructure error type. Log these fields through stdlib `extra`:

```python
{
    "event": "ingestion_resource_finished",
    "run_id": str(run_id),
    "season": season.value,
    "game_id": game_id.value if game_id else None,
    "resource_type": key.resource_type.value,
    "attempt": claim.version,
    "status": outcome.value,
    "duration_ms": duration_ms,
    "error_code": error_code,
    "error_type": error_type,
}
```

Extend the JSON formatter allowlist for these exact keys. Omit `None` values. Do not add exception message, payload, parameters, headers, or DSN.

Use `except asyncio.CancelledError` only at run scope to finalize interrupted and re-raise.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/application tests/unit/test_observability.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/nba_live_totals/application/ingestion/service.py src/nba_live_totals/observability/logging.py tests/unit/application/test_ingestion_resilience.py tests/unit/test_observability.py
git commit -m "feat: make historical backfill resumable"
```

---

### Task 12: Backfill CLI and composition root

**Files:**
- Modify: `scripts/__init__.py`
- Create: `scripts/backfill_games.py`
- Create: `tests/unit/test_backfill_cli.py`

**Interfaces:**
- Consumes: `Settings`, `NBAApiHistoricalDataSource`, `PostgresHistoricalIngestionStore`, and `HistoricalIngestionService`.
- Produces: `parse_args(argv) -> BackfillRequest`, `async run(request, settings) -> BackfillResult`, `RunCommand = Callable[[BackfillRequest, Settings], Awaitable[BackfillResult]]`, and `main(argv: Sequence[str] | None = None, *, run_command: RunCommand = run) -> int`.

- [ ] **Step 1: Write failing argument tests**

Cover:

```text
--season 2025-26
--season-from 2021-22 --season-to 2025-26
--season 2025-26 --game-id 0022500123
--refresh
missing selector -> argparse exit 2
only one range bound -> argparse exit 2
range plus --game-id -> argparse exit 2
invalid/reverse season -> argparse exit 2
```

- [ ] **Step 2: Write failing exit and cleanup tests**

Inject `run_command` returning succeeded/partial and assert `main()` returns `0/1`.
Inject invalid settings and assert `2`. Raise `KeyboardInterrupt` from `asyncio.run` and
assert `130`. For `run()`, spy on engine disposal for success, ordinary exception, and
cancellation.

- [ ] **Step 3: Run RED tests**

Run: `uv run pytest tests/unit/test_backfill_cli.py -q`

Expected: import FAIL because `scripts.backfill_games` does not exist.

- [ ] **Step 4: Implement parser and deterministic exit mapping**

Use an argparse mutually exclusive group for `--season` versus `--season-from`; require `--season-to` exactly with `--season-from`. Parse all identifiers through domain constructors and convert `ValueError` to `parser.error()`.

Print exactly one summary line:

```text
run=<uuid> status=<status> discovered=<n> succeeded=<n> skipped=<n> failed=<n>
```

End the module with `if __name__ == "__main__": raise SystemExit(main())` so both host
and container invocations return the documented status to the shell.

- [ ] **Step 5: Implement async composition and cleanup**

Create the engine with `pool_pre_ping=True`, transport from settings, NBA source, PostgreSQL store, and service. Configure logging first. In `finally`, dispose the engine. `main()` calls `asyncio.run(run(...))`, maps result status, catches `ValidationError`/argparse through code `2`, and catches `KeyboardInterrupt` as `130`.

- [ ] **Step 6: Run focused and static gates**

Run:

```bash
uv run pytest tests/unit/test_backfill_cli.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts tests/unit/test_backfill_cli.py
git commit -m "feat: add historical backfill CLI"
```

---

### Task 13: Real-store E2E and opt-in live canary

**Files:**
- Create: `tests/e2e/test_historical_backfill.py`
- Create: `tests/live/__init__.py`
- Create: `tests/live/test_nba_ingestion_canary.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete Phase 2 pipeline.
- Produces: fixture-driven two-run proof and a one-game NBA.com live acceptance test controlled by `TEST_NBA_SEASON` and `TEST_NBA_GAME_ID`.

- [ ] **Step 1: Write fixture-driven E2E test**

Run migrations, construct real store plus fixture source, and backfill one regular and one playoff game. Query exact row counts. Run the same request again and assert unchanged canonical/raw counts, zero provider fetches on the second run, succeeded first result, and succeeded/skipped second result.

- [ ] **Step 2: Write partial/resume E2E test**

The fixture source fails PBP for one game on first run. Assert first result partial and other resources persisted. Enable that response, rerun, assert only the failed resource is fetched, final result succeeds, and no duplicates exist.

- [ ] **Step 3: Write opt-in live canary**

Use:

```python
SEASON = os.getenv("TEST_NBA_SEASON")
GAME_ID = os.getenv("TEST_NBA_GAME_ID")
pytestmark = [pytest.mark.live, pytest.mark.integration]

if not SEASON or not GAME_ID:
    pytest.skip(
        "set TEST_NBA_SEASON and TEST_NBA_GAME_ID for the opt-in live canary",
        allow_module_level=True,
    )
```

Backfill the selected game through the real source/store and assert one game, two team box rows, at least two player rows, nonempty PBP, three raw endpoint documents, and succeeded resource states. Rerun without refresh and assert no new raw/canonical rows.

- [ ] **Step 4: Run E2E RED/GREEN without live networking**

Run:

```bash
uv run pytest tests/e2e/test_historical_backfill.py -q -m e2e
uv run pytest -q -m "not live"
```

Expected: E2E and all non-live tests pass. Live test is deselected or module-skipped.

- [ ] **Step 5: Run explicit live canary**

Set a known completed game and matching season in the current shell, then run:

```bash
uv run pytest tests/live/test_nba_ingestion_canary.py -q -m live
```

Expected: PASS. If NBA.com blocks the environment, capture the exact safe error code and prove the same payload with official recorded fixtures; do not claim the live gate passed until a real request succeeds.

- [ ] **Step 6: Document live test safety**

Add a short README section explaining that live tests are opt-in, contact NBA.com, require explicit IDs, respect rate limits, and are excluded from ordinary CI. Do not document any bypass for TLS, proxy security, or remote-test database loopback guard.

- [ ] **Step 7: Run static gates and commit**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
git diff --check
```

Then:

```bash
git add tests/e2e/test_historical_backfill.py tests/live README.md
git commit -m "test: verify historical ingestion end to end"
```

---

### Task 14: Container/runbook integration and final acceptance

**Files:**
- Modify: `docker/api.Dockerfile:12-18`
- Modify: `Makefile`
- Modify: `README.md`
- Modify: `tests/unit/test_operator_documentation.py`
- Modify: `tests/unit/test_compose_contract.py`

**Interfaces:**
- Consumes: complete CLI and existing configurable loopback Compose stack.
- Produces: container-accessible `scripts.backfill_games`, documented one-game/one-season/five-season commands, resume/refresh/interrupt semantics, and final runtime evidence.

- [ ] **Step 1: Write failing container/documentation contract tests**

Assert Dockerfile contains `COPY scripts ./scripts` before `USER app`. Assert README contains these commands and concepts:

```text
python -m scripts.backfill_games --season 2025-26
--season-from 2021-22
--season-to 2025-26
--game-id 0022500123
--refresh
resume
exit code 1
Regular Season
Playoffs
```

Assert no games/predictions API route is documented. Assert Makefile exposes `backfill` and requires `SEASON` or `SEASON_FROM`/`SEASON_TO` without hard-coding calendar-dependent seasons.

- [ ] **Step 2: Run RED tests**

Run: `uv run pytest tests/unit/test_operator_documentation.py tests/unit/test_compose_contract.py -q`

Expected: FAIL because Dockerfile, Makefile, and README lack Phase 2 CLI contracts.

- [ ] **Step 3: Integrate scripts into image and Makefile**

Copy `scripts` before final `uv sync`. Add a Make target that constructs exactly one of:

```make
uv run python -m scripts.backfill_games --season "$(SEASON)"
uv run python -m scripts.backfill_games --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)"
```

The target must fail with an explanatory message if neither selector is supplied. It may append `--refresh` only when `REFRESH=1`.

- [ ] **Step 4: Complete operator runbook**

Document host and container migration, one game, one season, the explicit five-season range `2021-22..2025-26`, expected multi-hour duration, rate limiting, structured summary, resume, refresh, safe Ctrl+C, exit codes, raw JSON versioning, PostgreSQL tables, and opt-in live canary. State that the date range is an example current at spec time; operators explicitly choose future ranges.

- [ ] **Step 5: Run full local quality gates**

Run:

```bash
uv lock --check
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts tests
uv run pytest -q -m "not live"
git diff --check
```

Expected: all pass; all Phase 1 tests remain green.

- [ ] **Step 6: Run Docker runtime acceptance**

With loopback port overrides if defaults are occupied:

```bash
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.backfill_games --help
docker compose exec -T api id
```

Expected: Compose config valid; all four existing services healthy; migration succeeds; CLI help exits `0`; container UID is non-root. Run fixture E2E against the same PostgreSQL and one explicit live canary before claiming Phase 2 complete.

- [ ] **Step 7: Verify database and API boundaries**

Assert canonical row counts after two fixture runs are stable, raw duplicate checksum is deduplicated, and `/health` plus `/health/ready` still return their Phase 1 contracts. Assert there are no new public game routes.

- [ ] **Step 8: Commit final integration**

```bash
git add docker/api.Dockerfile Makefile README.md tests/unit/test_operator_documentation.py tests/unit/test_compose_contract.py
git commit -m "docs: operate Phase 2 historical ingestion"
```

- [ ] **Step 9: Request final code and runtime review**

Provide reviewers the spec, this plan, complete Phase 2 commit range, full test output, Docker service status, migration output, idempotency row counts, partial/resume evidence, live-canary output, and confirmation that no secrets/raw payloads were logged. Resolve every finding through a new RED/GREEN fix commit and repeat the full final gates.

## Spec Coverage Map

| Spec requirement | Implemented and verified by |
|---|---|
| Scope, provider independence, no Phase 3+ work | Tasks 2–6; Task 14 boundary tests |
| `nba_api` version and operational settings | Task 1 |
| Immutable domain and raw-document contracts | Tasks 2–3 |
| Provider/store application ports and fencing types | Task 4 |
| Raw-before-parse, exact endpoints, retry/rate limiting | Tasks 5–6 |
| Regular/Playoffs mapping and excluded/unknown games | Task 6 |
| Canonical/raw/run/resource PostgreSQL schema | Task 7 |
| Raw dedup/versioning, lease and stale-worker fencing | Task 8 |
| Idempotent canonical upsert and atomic replacement | Task 9 |
| Schedule/game orchestration, targeting and counters | Task 10 |
| Partial failure, reparse, cancellation and safe logs | Task 11 |
| CLI selectors, resume/refresh and exit codes | Task 12 |
| Fixture E2E, partial/resume and real live canary | Task 13 |
| Docker image, operator runbook and complete gates | Task 14 |
