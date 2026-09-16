"""Live PostgreSQL checks for the Phase 4 control-plane tables."""
# ruff: noqa: E501

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from nba_live_totals.infrastructure.postgres.schema import (
    lineup_rebuild_resources,
    lineup_snapshot_attempts,
)
from tests import runtime_config

pytestmark = pytest.mark.integration

TABLES = {"lineup_rebuild_runs", "lineup_snapshot_attempts", "lineup_rebuild_resources"}


@pytest.fixture(scope="module", autouse=True)
def upgrade_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"DATABASE_URL": runtime_config.postgres_url()},
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(runtime_config.postgres_url())
    async with engine.connect() as database_connection:
        transaction = await database_connection.begin()
        try:
            yield database_connection
        finally:
            await transaction.rollback()
    await engine.dispose()


async def _seed_lineup_inputs(connection: AsyncConnection) -> tuple[str, UUID, UUID, UUID]:
    game_id, box_document_id, pbp_document_id, run_id = "0022509999", uuid4(), uuid4(), uuid4()
    await connection.execute(
        text(
            "INSERT INTO seasons (season, season_type, season_start_year) VALUES ('2025-26', 'regular_season', 2025) ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO teams (team_id, city, name, tricode, slug) VALUES (920001, 'Home', 'Home', 'HOM', 'home'), (920002, 'Away', 'Away', 'AWY', 'away') ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO games (game_id, season, season_type, game_date, started_at, home_team_id, away_team_id, home_score, away_score, status) VALUES (:game_id, '2025-26', 'regular_season', '2026-01-02', '2026-01-02T00:00:00+00:00', 920001, 920002, 1, 0, 'final')"
        ),
        {"game_id": game_id},
    )
    for document_id, endpoint in (
        (box_document_id, "box_score"),
        (pbp_document_id, "play_by_play"),
    ):
        await connection.execute(
            text(
                "INSERT INTO source_documents (document_id, source, endpoint, resource_key, request_parameters, payload, checksum_sha256, requested_at) VALUES (:id, 'nba', :endpoint, :key, '{}'::jsonb, '{}'::jsonb, :sha, '2026-01-02T01:00:00+00:00')"
            ),
            {
                "id": document_id,
                "endpoint": endpoint,
                "key": f"{endpoint}:{game_id}",
                "sha": "a" * 64,
            },
        )
    await connection.execute(
        text(
            "INSERT INTO lineup_rebuild_runs (run_id, selectors, mode, forced_rebuild, status) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running')"
        ),
        {"run_id": run_id},
    )
    return game_id, box_document_id, pbp_document_id, run_id


async def _insert_attempt(
    connection: AsyncConnection,
    game_id: str,
    box_document_id: UUID,
    pbp_document_id: UUID,
    run_id: UUID,
    *,
    snapshot_id: UUID | None = None,
    generation: int = 1,
    state: str = "allocated",
    issue_codes: str = "NULL",
    result_status: str = "NULL",
) -> UUID:
    snapshot_id = snapshot_id or uuid4()
    await connection.execute(
        text(
            f"""INSERT INTO lineup_snapshot_attempts (snapshot_id, game_id, generation, lineup_revision_id, input_token_sha256, input_box_score_document_id, input_box_score_sha256, input_pbp_document_id, input_pbp_sha256, canonical_player_box_sha256, canonical_event_sha256, game_context_sha256, normalizer_version, reconstructor_version, rules_version, output_schema_version, expected_row_count, expected_output_sha256, expected_issue_codes, result_status, state, created_by_run_id, created_by_claim_version, insert_started_at, verified_at, published_at) VALUES (:snapshot_id, :game_id, :generation, :sha, :sha, :box_id, :sha, :pbp_id, :sha, :sha, :sha, :sha, 'nba-v1', 'lineup-v1', 'nba-v1', 'lineups-v1', NULL, NULL, {issue_codes}, {result_status}, :state, :run_id, 1, NULL, NULL, NULL)"""
        ),
        {
            "snapshot_id": snapshot_id,
            "game_id": game_id,
            "generation": generation,
            "sha": "a" * 64,
            "box_id": box_document_id,
            "pbp_id": pbp_document_id,
            "state": state,
            "run_id": run_id,
        },
    )
    return snapshot_id


async def test_lineup_control_plane_has_all_tables_and_deferred_pointer_fks() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            foreign_keys = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys("lineup_rebuild_resources")
            )
    finally:
        await engine.dispose()
    assert TABLES <= set(names)
    pointers = {fk["name"]: fk for fk in foreign_keys}
    assert pointers["fk_lineup_rebuild_resources_active_snapshot"]["options"] == {
        "initially": "DEFERRED",
        "deferrable": True,
    }
    assert pointers["fk_lineup_rebuild_resources_published_snapshot"]["options"] == {
        "initially": "DEFERRED",
        "deferrable": True,
    }


async def test_lineup_database_rejects_bad_counters_generations_json_and_pointers(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_runs (run_id, selectors, mode, forced_rebuild, status, invalid) VALUES (:run_id, '{}'::jsonb, 'materialize', false, 'running', -1)"
                ),
                {"run_id": uuid4()},
            )
    await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, publication_state) VALUES (:game_id, 'stale')"
                ),
                {"game_id": game_id},
            )
    attempt_id = await _insert_attempt(connection, game_id, box_id, pbp_id, run_id, generation=2)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_snapshot_attempts SET state = 'writing', expected_row_count = 1, expected_output_sha256 = :sha, expected_issue_codes = '[{}]'::jsonb, result_status = 'invalid', insert_started_at = CURRENT_TIMESTAMP WHERE snapshot_id = :snapshot_id"
                ),
                {"sha": "a" * 64, "snapshot_id": attempt_id},
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_snapshot_attempts SET state = 'writing', expected_row_count = 1, "
                    "expected_output_sha256 = :sha, expected_issue_codes = "
                    '\'[{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"},{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"}]\'::jsonb, '
                    "result_status = 'invalid', insert_started_at = CURRENT_TIMESTAMP "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"sha": "a" * 64, "snapshot_id": attempt_id},
            )


async def test_lineup_deferred_pointer_can_reference_attempt_created_later(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    snapshot_id = uuid4()
    async with connection.begin_nested():
        await connection.execute(
            text(
                "INSERT INTO lineup_rebuild_resources (game_id, claim_version, active_run_id, active_claim_version, active_snapshot_id, active_lineup_revision_id, active_input_token_sha256, claimed_until) VALUES (:game_id, 1, :run_id, 1, :snapshot_id, :sha, :sha, CURRENT_TIMESTAMP)"
            ),
            {"game_id": game_id, "run_id": run_id, "snapshot_id": snapshot_id, "sha": "a" * 64},
        )
        await _insert_attempt(connection, game_id, box_id, pbp_id, run_id, snapshot_id=snapshot_id)
        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


async def test_lineup_database_rejects_complete_control_plane_mutation_matrix(
    connection: AsyncConnection,
) -> None:
    game_id, box_id, pbp_id, run_id = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, game_id, box_id, pbp_id, run_id)
    invalid_issue = (
        '[{"team_id":920001,"reason":"bad_lineup","sequence_number":1,"boundary_side":"before"}]'
    )
    rejected_updates: tuple[tuple[str, dict[str, object]], ...] = (
        ("lineup_revision_id = 'bad'", {}),
        ("input_pbp_document_id = :missing_document", {"missing_document": uuid4()}),
        ("safe_error_code = 'safe_error'", {}),
        ("state = 'verified'", {}),
        (
            "state = 'writing', expected_row_count = 1, expected_output_sha256 = :sha, expected_issue_codes = CAST(:issues AS jsonb), result_status = 'valid', insert_started_at = CURRENT_TIMESTAMP",
            {"sha": "a" * 64, "issues": invalid_issue},
        ),
    )
    for assignment, parameters in rejected_updates:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        f"UPDATE lineup_snapshot_attempts SET {assignment} WHERE snapshot_id = :snapshot_id"
                    ),
                    parameters | {"snapshot_id": attempt_id},
                )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, active_run_id) VALUES (:game_id, :run_id)"
                ),
                {"game_id": game_id, "run_id": run_id},
            )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO lineup_rebuild_resources (game_id, claim_version, active_run_id, active_claim_version, active_snapshot_id, active_lineup_revision_id, active_input_token_sha256, claimed_until) VALUES (:game_id, 2, :run_id, 1, :snapshot_id, :sha, :sha, CURRENT_TIMESTAMP)"
                ),
                {"game_id": game_id, "run_id": run_id, "snapshot_id": attempt_id, "sha": "a" * 64},
            )
    await connection.execute(
        text("INSERT INTO lineup_rebuild_resources (game_id) VALUES (:game_id)"),
        {"game_id": game_id},
    )
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "UPDATE lineup_rebuild_resources SET last_issue_codes = '[{}]'::jsonb WHERE game_id = :game_id"
                ),
                {"game_id": game_id},
            )


ISSUE = {"team_id": 920001, "reason": "bad_lineup", "sequence_number": 1, "boundary_side": "before"}
NOW = datetime(2026, 1, 2, tzinfo=UTC)
EXPECTED = {
    "expected_row_count": 1,
    "expected_output_sha256": "a" * 64,
    "expected_issue_codes": [],
    "result_status": "valid",
}
ATTEMPT_STATES: dict[str, dict[str, object]] = {
    "allocated": {},
    "writing": EXPECTED | {"insert_started_at": NOW},
    "verified": EXPECTED | {"insert_started_at": NOW, "verified_at": NOW},
    "published": EXPECTED | {"insert_started_at": NOW, "verified_at": NOW, "published_at": NOW},
    "poisoned": {"safe_error_code": "bad_lineup", "safe_error_type": "validation_error"},
    "superseded": {},
}


async def _reject_attempt_change(
    connection: AsyncConnection, attempt_id: UUID, changes: Mapping[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError) as caught:
        async with connection.begin_nested():
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.snapshot_id == attempt_id)
                .values(**changes)
            )
    # A neighboring lifecycle check must not make a malformed regex/JSON case pass.
    assert (
        getattr(getattr(caught.value.orig, "__cause__", None), "constraint_name", None)
        == f"ck_lineup_snapshot_attempts_{constraint}"
    )


@pytest.mark.parametrize(
    "column",
    ["normalizer_version", "reconstructor_version", "rules_version", "output_schema_version"],
)
@pytest.mark.parametrize("value", ["", "Uppercase", "has space", "a" * 129])
async def test_attempt_version_format_is_independent_of_lifecycle(
    connection: AsyncConnection, column: str, value: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    await _reject_attempt_change(connection, attempt_id, {column: value}, f"{column}_format")


@pytest.mark.parametrize("column", ["safe_error_code", "safe_error_type"])
@pytest.mark.parametrize("value", ["", "Unsafe", "bad-value", "a" * 65])
async def test_attempt_safe_error_regex_on_valid_poisoned_row(
    connection: AsyncConnection, column: str, value: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    await connection.execute(
        update(lineup_snapshot_attempts).values(state="poisoned", **ATTEMPT_STATES["poisoned"])
    )
    await _reject_attempt_change(connection, attempt_id, {column: value}, f"{column}_format")


@pytest.mark.parametrize("state", list(ATTEMPT_STATES))
@pytest.mark.parametrize("timestamp", ["insert_started_at", "verified_at", "published_at"])
async def test_attempt_lifecycle_timestamp_contract(
    connection: AsyncConnection, state: str, timestamp: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    baseline = ATTEMPT_STATES[state]
    await connection.execute(update(lineup_snapshot_attempts).values(state=state, **baseline))
    changed = {timestamp: None if timestamp in baseline else NOW}
    if state in {"poisoned", "superseded"} and timestamp != "published_at":
        # Terminal failures retain the point of failure, including timestamps.
        await connection.execute(update(lineup_snapshot_attempts).values(**changed))
    else:
        await _reject_attempt_change(connection, attempt_id, changed, "state_fields")


@pytest.mark.parametrize("state", ["poisoned", "superseded"])
@pytest.mark.parametrize("with_metadata", [False, True])
async def test_terminal_attempt_accepts_both_complete_metadata_shapes(
    connection: AsyncConnection, state: str, with_metadata: bool
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    baseline = ATTEMPT_STATES[state] | (
        EXPECTED | {"insert_started_at": NOW} if with_metadata else {}
    )
    await connection.execute(update(lineup_snapshot_attempts).values(state=state, **baseline))
    await _reject_attempt_change(
        connection,
        attempt_id,
        {"expected_output_sha256": None if with_metadata else "a" * 64},
        "expected_metadata",
    )


@pytest.mark.parametrize("status,issues", [("valid", [ISSUE]), ("invalid", [])])
async def test_result_issue_cardinality_on_complete_writing_metadata(
    connection: AsyncConnection, status: str, issues: list[dict[str, object]]
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    await connection.execute(
        update(lineup_snapshot_attempts).values(state="writing", **ATTEMPT_STATES["writing"])
    )
    await _reject_attempt_change(
        connection,
        attempt_id,
        {"result_status": status, "expected_issue_codes": issues},
        "expected_metadata",
    )


BAD_ISSUES = [
    [ISSUE, ISSUE],
    [ISSUE | {"extra": "unsafe"}],
    [{key: value for key, value in ISSUE.items() if key != "reason"}],
    [ISSUE | {"team_id": True}],
    [ISSUE | {"team_id": 0}],
    [ISSUE | {"team_id": 9_223_372_036_854_775_808}],
    [ISSUE | {"sequence_number": 0}],
    [ISSUE | {"sequence_number": 9_223_372_036_854_775_808}],
    [ISSUE | {"sequence_number": 1.5}],
    [ISSUE | {"reason": "bad value"}],
    [ISSUE | {"boundary_side": "during"}],
    [ISSUE | {"sequence_number": sequence} for sequence in range(1, 34)],
]


@pytest.mark.parametrize("state", list(ATTEMPT_STATES))
async def test_attempt_error_presence_matches_state(
    connection: AsyncConnection, state: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    await connection.execute(
        update(lineup_snapshot_attempts).values(state=state, **ATTEMPT_STATES[state])
    )
    await _reject_attempt_change(
        connection,
        attempt_id,
        {"safe_error_code": None if state == "poisoned" else "safe_code"},
        "state_error_fields",
    )


@pytest.mark.parametrize("state,issues", [("current", [ISSUE]), ("invalid", [])])
async def test_publication_issue_state_with_complete_pointer(
    connection: AsyncConnection, state: str, issues: list[dict[str, object]]
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    await connection.execute(
        lineup_rebuild_resources.insert().values(
            game_id=inputs[0],
            publication_state=state,
            published_snapshot_id=attempt_id,
            published_lineup_revision_id="a" * 64,
            published_input_token_sha256="a" * 64,
            published_at=NOW,
            last_issue_codes=[] if state == "current" else [ISSUE],
        )
    )
    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with pytest.raises(IntegrityError) as caught:
        async with connection.begin_nested():
            await connection.execute(
                update(lineup_rebuild_resources).values(last_issue_codes=issues)
            )
    assert (
        getattr(getattr(caught.value.orig, "__cause__", None), "constraint_name", None)
        == "ck_lineup_rebuild_resources_publication_issue_codes_state"
    )


async def test_issue_projection_accepts_32_distinct_items(connection: AsyncConnection) -> None:
    inputs = await _seed_lineup_inputs(connection)
    await _insert_attempt(connection, *inputs)
    issues = [ISSUE | {"sequence_number": sequence} for sequence in range(1, 33)]
    await connection.execute(
        update(lineup_snapshot_attempts).values(
            state="writing",
            **(
                ATTEMPT_STATES["writing"]
                | {"result_status": "invalid", "expected_issue_codes": issues}
            ),
        )
    )
    await connection.execute(
        lineup_rebuild_resources.insert().values(game_id=inputs[0], last_issue_codes=issues)
    )


@pytest.mark.parametrize("issues", BAD_ISSUES)
@pytest.mark.parametrize("target", ["expected_issue_codes", "last_issue_codes"])
async def test_both_issue_projections_reject_malformed_and_duplicate_items(
    connection: AsyncConnection, target: str, issues: list[dict[str, object]]
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    if target == "expected_issue_codes":
        await connection.execute(
            update(lineup_snapshot_attempts).values(
                state="writing",
                **(
                    ATTEMPT_STATES["writing"]
                    | {"result_status": "invalid", "expected_issue_codes": [ISSUE]}
                ),
            )
        )
        await _reject_attempt_change(
            connection, attempt_id, {target: issues}, "expected_issue_codes_contract"
        )
    else:
        await connection.execute(lineup_rebuild_resources.insert().values(game_id=inputs[0]))
        with pytest.raises(IntegrityError) as caught:
            async with connection.begin_nested():
                await connection.execute(
                    update(lineup_rebuild_resources).values(last_issue_codes=issues)
                )
        assert (
            getattr(getattr(caught.value.orig, "__cause__", None), "constraint_name", None)
            == "ck_lineup_rebuild_resources_last_issue_codes_contract"
        )


@pytest.mark.parametrize("state", ["absent", "current", "stale", "invalid"])
async def test_publication_state_requires_exact_pointer_presence(
    connection: AsyncConnection, state: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    attempt_id = await _insert_attempt(connection, *inputs)
    pointer = {
        "published_snapshot_id": attempt_id,
        "published_lineup_revision_id": "a" * 64,
        "published_input_token_sha256": "a" * 64,
        "published_at": NOW,
    }
    await connection.execute(
        lineup_rebuild_resources.insert().values(
            game_id=inputs[0],
            publication_state=state,
            last_issue_codes=[ISSUE] if state == "invalid" else [],
            **({} if state == "absent" else pointer),
        )
    )
    await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    with pytest.raises(IntegrityError) as caught:
        async with connection.begin_nested():
            await connection.execute(
                update(lineup_rebuild_resources).values(
                    **(pointer if state == "absent" else dict.fromkeys(pointer))
                )
            )
    constraint_name = getattr(
        getattr(caught.value.orig, "__cause__", None), "constraint_name", None
    )
    if state == "absent":
        # A complete pointer satisfies the tuple check, isolating the state check.
        assert constraint_name == "ck_lineup_rebuild_resources_publication_pointer_state"
    else:
        # Clearing a required pointer violates both checks; PostgreSQL may report either.
        assert constraint_name in {
            "ck_lineup_rebuild_resources_publication_pointer_state",
            "ck_lineup_rebuild_resources_published_pointer_tuple",
        }


@pytest.mark.parametrize("column", ["last_error_code", "last_error_type"])
async def test_resource_safe_error_regex_with_complete_error_tuple(
    connection: AsyncConnection, column: str
) -> None:
    inputs = await _seed_lineup_inputs(connection)
    await connection.execute(
        lineup_rebuild_resources.insert().values(
            game_id=inputs[0], last_error_code="bad_lineup", last_error_type="validation_error"
        )
    )
    with pytest.raises(IntegrityError) as caught:
        async with connection.begin_nested():
            await connection.execute(
                update(lineup_rebuild_resources).values(**{column: "bad-value"})
            )
    assert (
        getattr(getattr(caught.value.orig, "__cause__", None), "constraint_name", None)
        == f"ck_lineup_rebuild_resources_{column}_format"
    )
