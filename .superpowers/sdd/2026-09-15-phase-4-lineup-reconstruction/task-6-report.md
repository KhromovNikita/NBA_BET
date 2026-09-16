# Task 6 report: validate immutable NBA lineup inputs

## Evidence

- `uv run pytest tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py -q` — 8 passed.
- `uv run pytest tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py tests/unit/application/test_lineup_models.py -q` — 36 passed.
- `uv run pytest tests/unit/infrastructure/nba tests/unit/domain/test_lineup_reconstructor.py tests/unit/domain/test_lineups.py -q` — 491 passed.
- `uv run ruff check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba` — passed.
- `uv run ruff format --check src/nba_live_totals/infrastructure/nba tests/unit/infrastructure/nba` — passed.
- `uv run mypy src tests/unit/infrastructure/nba` — passed.
- `git diff --check` — passed.

## Files

- `src/nba_live_totals/infrastructure/nba/lineup_normalizer.py`
- `src/nba_live_totals/infrastructure/nba/__init__.py`
- `tests/unit/infrastructure/nba/lineup_helpers.py`
- `tests/unit/infrastructure/nba/test_lineup_normalizer_inputs.py`
- `tests/fixtures/nba/lineups/box_score_v1.json`
- `tests/fixtures/nba/lineups/play_by_play_v1.json`

## Implementation summary

The normalizer independently validates source/endpoint/resource/game/checksum identities for
both applied documents, validates the raw box envelope without constructing global `Player`
objects, and cross-checks raw roster rows against canonical scalar fields. It derives starter
signals from explicit flags or legacy positions, applies exact played/comment eligibility, and
returns normalized rosters even when the starter count is not five. It reparses PBP through the
public mapper, compares every canonical event field in raw array order, preserves contiguous
raw-position sequence numbers despite duplicate provider action numbers, and emits
`NoLineupChange` envelopes pending Task 7 substitution semantics. Structural failures expose
only the safe allowlisted `LineupNormalizationError` codes.

## Self-review and concerns

- Names are retained only in a private `_RawPlayer` value and are not part of any canonical or
  domain projection.
- This task intentionally does not parse substitution aliases or emit `Substitute` transitions;
  Task 7 owns those semantics.
- The added V1 fixtures mirror the existing small mapper fixtures (one active/DNP home pair and
  one away player); broader five-player golden coverage belongs to the later integration fixtures.
