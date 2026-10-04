"""Offline Phase 5 acceptance for calculator grain and two-table persistence."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.application.features import (
    FeaturePublicationState,
    FeatureVerificationStatus,
)
from nba_live_totals.application.lineups import BuildLineupsRequest
from nba_live_totals.application.possessions import (
    BuildPossessionsRequest,
    PossessionBuildService,
)
from nba_live_totals.domain import JsonObject
from nba_live_totals.domain.features import (
    DeterministicFeatureCalculator,
    FeatureCalculationInput,
    FeatureResultStatus,
    FeatureStatDelta,
    FeatureVersions,
    NormalizedFeatureEvents,
)
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    ClickHouseFeatureSourceReader,
)
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.nba import NBAPlayByPlayV3PossessionNormalizer
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
    games,
    possession_rebuild_resources,
    possession_rebuild_runs,
    possession_snapshot_attempts,
)
from tests import runtime_config
from tests.e2e.test_lineup_reconstruction import (
    GAME_ID as PIPELINE_GAME_ID,
)
from tests.e2e.test_lineup_reconstruction import (
    KEYS as PIPELINE_KEYS,
)
from tests.e2e.test_lineup_reconstruction import (
    SEASON as PIPELINE_SEASON,
)
from tests.e2e.test_lineup_reconstruction import (
    VERSIONS as LINEUP_VERSIONS,
)
from tests.e2e.test_lineup_reconstruction import (
    LineupAcceptance,
    acceptance_context,
)
from tests.e2e.test_possession_reconstruction import RecordingParser
from tests.integration.test_possession_snapshot_protocol import (
    VERSIONS as POSSESSION_VERSIONS,
)
from tests.integration.test_possession_snapshot_protocol import (
    TrackingWorkStore,
)
from tests.unit.domain.feature_helpers import GAME_ID, make_game, make_possession
from tests.unit.domain.lineup_helpers import make_invalid_state, make_segment, make_state
from tests.unit.infrastructure.clickhouse.test_feature_snapshot_store import (
    PLAYER_TABLE,
    STATE_TABLE,
    snapshot_fixture,
)

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "nba" / "features"
PIPELINE_FEATURE_VERSIONS = FeatureVersions(
    "possession-lineup-inclusive-v1",
    "feature-formulas-v1",
    "nba-feature-events-v1",
    "game-state-features-v1",
    "player-pregame-features-v1",
)


def load_fixture(name: str) -> JsonObject:
    return cast(JsonObject, json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")))


def after(sequence: int) -> EventBoundary:
    return EventBoundary(sequence, BoundarySide.AFTER)


def _install_scoring_pipeline_pbp(harness: LineupAcceptance) -> None:
    source_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "nba"
        / "possessions"
        / "possession_play_by_play_0029899001.json"
    )
    serialized = source_path.read_text(encoding="utf-8")
    for old, new in (
        ("0029899001", PIPELINE_GAME_ID.value),
        ("91317001", "91318001"),
        ("91317002", "91318002"),
        ("91317101", "101"),
        ("91317201", "201"),
    ):
        serialized = serialized.replace(old, new)
    payload = cast(JsonObject, json.loads(serialized))
    game = cast(dict[str, object], payload["game"])
    actions = cast(list[dict[str, object]], game["actions"])
    original_lineup = cast(dict[str, object], deepcopy(harness.source.payloads[PIPELINE_KEYS[2]]))
    lineup_actions = cast(
        list[dict[str, object]], cast(dict[str, object], original_lineup["game"])["actions"]
    )
    home_sub = deepcopy(lineup_actions[2])
    away_sub = deepcopy(lineup_actions[3])
    home_sub["clock"] = "PT11M30.00S"
    away_sub["clock"] = "PT11M00.00S"
    home_sub["subType"] = "Out"
    away_sub["subType"] = "Out"
    for substitution in (home_sub, away_sub):
        substitution["scoreHome"] = "2"
        substitution["scoreAway"] = "0"
    combined = [actions[0], actions[1], home_sub, actions[2], away_sub, *actions[3:]]
    for sequence, action in enumerate(combined, start=1):
        action["actionNumber"] = sequence
        action["actionId"] = sequence * 10
    game["actions"] = combined
    harness.source.payloads[PIPELINE_KEYS[2]] = payload

    schedule = cast(dict[str, object], deepcopy(harness.source.payloads[PIPELINE_KEYS[0]]))
    league = cast(dict[str, object], schedule["leagueSchedule"])
    dates = cast(list[dict[str, object]], league["gameDates"])
    games = cast(list[dict[str, object]], dates[0]["games"])
    cast(dict[str, object], games[0]["homeTeam"])["score"] = 4
    cast(dict[str, object], games[0]["awayTeam"])["score"] = 5
    harness.source.payloads[PIPELINE_KEYS[0]] = cast(JsonObject, schedule)

    box = cast(dict[str, object], deepcopy(harness.source.payloads[PIPELINE_KEYS[1]]))
    traditional = cast(dict[str, object], box["boxScoreTraditional"])
    cast(dict[str, object], cast(dict[str, object], traditional["homeTeam"])["statistics"])[
        "points"
    ] = 4
    cast(dict[str, object], cast(dict[str, object], traditional["awayTeam"])["statistics"])[
        "points"
    ] = 5
    harness.source.payloads[PIPELINE_KEYS[1]] = cast(JsonObject, box)


def _invalidate_home_substitution(harness: LineupAcceptance) -> None:
    payload = cast(dict[str, object], deepcopy(harness.source.payloads[PIPELINE_KEYS[2]]))
    actions = cast(list[dict[str, object]], cast(dict[str, object], payload["game"])["actions"])
    substitution = next(
        action
        for action in actions
        if str(action["actionType"]).casefold() == "substitution" and action["teamId"] == 91_318_001
    )
    substitution["personId"] = 107
    substitution["playerName"] = "Home Player107"
    substitution["playerNameI"] = "H. Player107"
    substitution["description"] = "SUB: Player106 FOR Player107"
    harness.source.payloads[PIPELINE_KEYS[2]] = cast(JsonObject, payload)


def test_owned_manifest_pins_causal_exclusions_and_no_network_fallback() -> None:
    schedule = load_fixture("feature_schedule_2097-98.json")
    boxes = load_fixture("feature_box_scores.json")
    pbp = load_fixture("feature_play_by_plays.json")
    rows = cast(list[dict[str, str]], schedule["ordered_games"])
    target = next(row for row in rows if row["role"] == "target")
    included = [row["game_id"] for row in rows if row["started_at"] < target["started_at"]]

    assert included == ["0029797001"]
    assert boxes["history_games"] == included
    assert boxes["target_box_features_allowed"] is False
    assert boxes["fixed_point_scale"] == 1_000_000
    assert pbp["network_fallback_allowed"] is False


def test_calculator_retains_invalid_interval_and_recovers_later_state() -> None:
    initial_home = frozenset({101, 102, 103, 104, 105})
    recovered_home = frozenset({102, 103, 104, 105, 106})
    away = frozenset({201, 202, 203, 204, 205})
    segments = (
        make_segment(end_boundary=EventBoundary(2, BoundarySide.BEFORE)),
        make_segment(
            segment_number=2,
            start_boundary=after(2),
            end_boundary=EventBoundary(4, BoundarySide.BEFORE),
            home_state=make_invalid_state(reason="missing_home_sub", boundary=after(2)),
        ),
        make_segment(
            segment_number=3,
            start_boundary=after(4),
            end_boundary=after(5),
            home_state=make_state(recovered_home),
            away_state=make_state(away),
        ),
    )
    possessions = (
        make_possession(),
        make_possession(
            possession_number=2,
            offense_team_id=20,
            defense_team_id=10,
            offense_possession_number=1,
            start_boundary=EventBoundary(4, BoundarySide.BEFORE),
            end_boundary=after(5),
            start_seconds_remaining_period_ms=700_000,
            end_seconds_remaining_period_ms=680_000,
            seconds_remaining_game_ms=2_840_000,
            home_score_before=2,
            away_score_before=0,
            home_score=2,
            away_score=3,
            score_diff=-1,
            offense_points=3,
            home_possessions=1,
            away_possessions=1,
            source_event_count=2,
        ),
    )
    events = NormalizedFeatureEvents(
        GAME_ID, tuple(FeatureStatDelta(i, after(i)) for i in range(1, 6))
    )
    dataset = DeterministicFeatureCalculator().calculate(
        FeatureCalculationInput(make_game(), possessions, segments, (), (), ()), events
    )

    assert tuple(row.state_number for row in dataset.states) == (0, 1, 2)
    assert dataset.states[0].home_player_ids == tuple(sorted(initial_home))
    assert dataset.states[1].home_lineup_status is LineupStatus.INVALID
    assert dataset.states[1].home_player_ids == ()
    assert dataset.states[2].home_player_ids == tuple(sorted(recovered_home))
    assert dataset.states[2].is_valid
    assert dataset.status is FeatureResultStatus.INVALID
    assert dataset.issues == ("missing_home_sub",)


async def _run_dataset_cli(*arguments: str) -> tuple[int, str, str]:
    environment = os.environ | {
        "DATABASE_URL": runtime_config.postgres_url(),
        "CLICKHOUSE_URL": runtime_config.clickhouse_url(),
        "CLICKHOUSE_USER": "nba",
        "CLICKHOUSE_PASSWORD": "nba",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "scripts.build_dataset",
        *arguments,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        await process.wait()
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


async def _feature_run_ids(engine: AsyncEngine) -> set[UUID]:
    selectors = {"seasons": [PIPELINE_SEASON.value], "game_id": PIPELINE_GAME_ID.value}
    async with engine.connect() as connection:
        return set(
            await connection.scalars(
                select(feature_build_runs.c.run_id).where(
                    feature_build_runs.c.selectors == selectors
                )
            )
        )


async def _assert_pipeline_feature_state_absent(
    harness: LineupAcceptance,
) -> None:
    async with harness.engine.connect() as connection:
        for table in (
            possession_rebuild_resources,
            possession_snapshot_attempts,
            feature_build_resources,
            feature_snapshot_attempts,
        ):
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.game_id == PIPELINE_GAME_ID.value)
                )
                == 0
            ), f"refusing fixture collision in {table.name}"
    assert await _feature_run_ids(harness.engine) == set()
    for table_name in (
        "nba.possessions_v1",
        "nba.game_state_features_v1",
        "nba.player_pregame_features_v1",
    ):
        rows = await harness.client.query(
            f"SELECT count() AS count FROM {table_name} WHERE game_id = {{game_id:String}}",
            parameters={"game_id": PIPELINE_GAME_ID.value},
        )
        assert rows == [{"count": 0}], f"refusing fixture collision in {table_name}"


async def _cleanup_pipeline_feature_state(
    harness: LineupAcceptance,
    *,
    possession_run_ids: list[UUID],
    feature_run_ids: set[UUID],
) -> None:
    async with harness.engine.connect() as connection:
        possession_ids = tuple(
            await connection.scalars(
                select(possession_snapshot_attempts.c.snapshot_id).where(
                    possession_snapshot_attempts.c.game_id == PIPELINE_GAME_ID.value
                )
            )
        )
        feature_ids = tuple(
            await connection.scalars(
                select(feature_snapshot_attempts.c.snapshot_id).where(
                    feature_snapshot_attempts.c.game_id == PIPELINE_GAME_ID.value
                )
            )
        )
    for table, snapshot_ids in (
        ("nba.game_state_features_v1", feature_ids),
        ("nba.player_pregame_features_v1", feature_ids),
        ("nba.possessions_v1", possession_ids),
    ):
        for snapshot_id in snapshot_ids:
            await harness.client.execute(
                f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{snapshot_id}' "
                "SETTINGS mutations_sync = 2"
            )
    async with harness.engine.begin() as connection:
        if feature_ids:
            await connection.execute(
                delete(feature_snapshot_dependencies).where(
                    feature_snapshot_dependencies.c.snapshot_id.in_(feature_ids)
                )
            )
        await connection.execute(
            delete(feature_build_resources).where(
                feature_build_resources.c.game_id == PIPELINE_GAME_ID.value
            )
        )
        await connection.execute(
            delete(feature_snapshot_attempts).where(
                feature_snapshot_attempts.c.game_id == PIPELINE_GAME_ID.value
            )
        )
        if feature_run_ids:
            await connection.execute(
                delete(feature_build_runs).where(feature_build_runs.c.run_id.in_(feature_run_ids))
            )
        await connection.execute(
            delete(possession_rebuild_resources).where(
                possession_rebuild_resources.c.game_id == PIPELINE_GAME_ID.value
            )
        )
        await connection.execute(
            delete(possession_snapshot_attempts).where(
                possession_snapshot_attempts.c.game_id == PIPELINE_GAME_ID.value
            )
        )
        if possession_run_ids:
            await connection.execute(
                delete(possession_rebuild_runs).where(
                    possession_rebuild_runs.c.run_id.in_(possession_run_ids)
                )
            )


async def test_real_cli_runs_stored_ingestion_possession_lineup_feature_pipeline() -> None:
    possession_run_ids: list[UUID] = []
    feature_run_ids: set[UUID] = set()
    async with acceptance_context() as harness:
        await _assert_pipeline_feature_state_absent(harness)
        try:
            _install_scoring_pipeline_pbp(harness)
            await harness.ingest()
            possession_store = TrackingWorkStore(harness.engine, possession_run_ids)
            parser = RecordingParser()
            possession_service = PossessionBuildService(
                normalizer=NBAPlayByPlayV3PossessionNormalizer(),
                parser=parser,
                work_store=possession_store,
                snapshot_store=PossessionSnapshotStore(harness.client),
                versions=POSSESSION_VERSIONS,
                max_concurrency=1,
            )
            possession_result = await possession_service.build(
                BuildPossessionsRequest((PIPELINE_SEASON,), game_id=PIPELINE_GAME_ID)
            )
            async with harness.engine.connect() as connection:
                possession_resource = (
                    (
                        await connection.execute(
                            select(possession_rebuild_resources).where(
                                possession_rebuild_resources.c.game_id == PIPELINE_GAME_ID.value
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                stored_score = (
                    await connection.execute(
                        select(games.c.home_score, games.c.away_score).where(
                            games.c.game_id == PIPELINE_GAME_ID.value
                        )
                    )
                ).one()
            assert (possession_result.materialized, possession_result.failed) == (
                1,
                0,
            ), (
                possession_resource["publication_state"],
                possession_resource["last_error_code"],
                possession_resource["last_error_type"],
                [
                    (issue.severity.value, issue.code, issue.sequence_number)
                    for result in parser.results
                    for issue in result.issues
                ],
                tuple(stored_score),
                (
                    parser.results[-1].possessions[-1].home_score,
                    parser.results[-1].possessions[-1].away_score,
                )
                if parser.results and parser.results[-1].possessions
                else None,
            )
            lineup_result = await harness.service.build(
                BuildLineupsRequest((PIPELINE_SEASON,), game_id=PIPELINE_GAME_ID)
            )
            assert (lineup_result.materialized, lineup_result.failed) == (1, 0)

            before_runs = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli(
                "--season", PIPELINE_SEASON.value, "--game-id", PIPELINE_GAME_ID.value
            )
            feature_run_ids |= await _feature_run_ids(harness.engine) - before_runs
            assert code == 0, (output, stderr)
            assert "materialized=1 invalid=0 skipped_current=0 verified=0 failed=0 busy=0" in output

            store = PostgresFeatureWorkStore(
                harness.engine,
                possession_versions=POSSESSION_VERSIONS,
                lineup_versions=LINEUP_VERSIONS,
            )
            target = await store.resolve_target(PIPELINE_GAME_ID)
            target_rows = await ClickHouseFeatureSourceReader(harness.client).read_target(target)
            current = await store.resolve_current(
                target, target_rows.target_player_ids, PIPELINE_FEATURE_VERSIONS
            )
            assert current.status is FeaturePublicationState.CURRENT
            assert current.reference is not None
            reference = current.reference
            dataset = await ClickHouseFeatureSnapshotStore(harness.client).read_snapshot(reference)
            assert len(dataset.states) == len(target_rows.possessions) + 1
            assert tuple(row.state_number for row in dataset.states) == tuple(
                range(len(dataset.states))
            )
            assert all(row.home_player_ids and row.away_player_ids for row in dataset.states)

            state_rows_before = await harness.client.query(
                "SELECT count() AS count FROM nba.game_state_features_v1 "
                "WHERE snapshot_id = {snapshot:UUID}",
                parameters={"snapshot": str(reference.attempt.snapshot_id)},
            )
            attempts_before = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli(
                "--season", PIPELINE_SEASON.value, "--game-id", PIPELINE_GAME_ID.value
            )
            feature_run_ids |= await _feature_run_ids(harness.engine) - attempts_before
            assert code == 0, (output, stderr)
            assert "materialized=0 invalid=0 skipped_current=1 verified=0 failed=0 busy=0" in output
            assert (
                await harness.client.query(
                    "SELECT count() AS count FROM nba.game_state_features_v1 "
                    "WHERE snapshot_id = {snapshot:UUID}",
                    parameters={"snapshot": str(reference.attempt.snapshot_id)},
                )
                == state_rows_before
            )

            source_snapshot_ids = (
                target.possession_reference.attempt.snapshot_id,
                target.lineup_reference.attempt.snapshot_id,
            )
            before_rebuild = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli(
                "--season",
                PIPELINE_SEASON.value,
                "--game-id",
                PIPELINE_GAME_ID.value,
                "--rebuild",
            )
            feature_run_ids |= await _feature_run_ids(harness.engine) - before_rebuild
            assert code == 0, (output, stderr)
            assert "materialized=1 invalid=0 skipped_current=0 verified=0 failed=0 busy=0" in output
            rebuilt_target = await store.resolve_target(PIPELINE_GAME_ID)
            rebuilt_rows = await ClickHouseFeatureSourceReader(harness.client).read_target(
                rebuilt_target
            )
            rebuilt = await store.resolve_current(
                rebuilt_target,
                rebuilt_rows.target_player_ids,
                PIPELINE_FEATURE_VERSIONS,
            )
            assert rebuilt.status is FeaturePublicationState.CURRENT
            assert rebuilt.reference is not None
            rebuilt_reference = rebuilt.reference
            assert rebuilt_reference.attempt.snapshot_id != reference.attempt.snapshot_id
            assert (
                rebuilt_reference.attempt.feature_revision_id
                == reference.attempt.feature_revision_id
            )
            assert (
                rebuilt_reference.state_output_sha256,
                rebuilt_reference.player_output_sha256,
                rebuilt_reference.feature_output_sha256,
            ) == (
                reference.state_output_sha256,
                reference.player_output_sha256,
                reference.feature_output_sha256,
            )
            assert (
                rebuilt_target.possession_reference.attempt.snapshot_id,
                rebuilt_target.lineup_reference.attempt.snapshot_id,
            ) == source_snapshot_ids
            rebuilt_dataset = await ClickHouseFeatureSnapshotStore(harness.client).read_snapshot(
                rebuilt_reference
            )
            assert rebuilt_dataset == dataset
            reference, dataset = rebuilt_reference, rebuilt_dataset

            before_verify = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli(
                "--season",
                PIPELINE_SEASON.value,
                "--game-id",
                PIPELINE_GAME_ID.value,
                "--verify",
            )
            feature_run_ids |= await _feature_run_ids(harness.engine) - before_verify
            assert code == 0, (output, stderr)
            assert "materialized=0 invalid=0 skipped_current=0 verified=1 failed=0 busy=0" in output
            assert (
                await ClickHouseFeatureSnapshotStore(harness.client).read_snapshot(reference)
                == dataset
            )

            before_bad_input = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli("--season", "invalid-season")
            assert code == 2, (output, stderr)
            assert await _feature_run_ids(harness.engine) == before_bad_input
            unsafe = (
                runtime_config.postgres_url(),
                runtime_config.clickhouse_url(),
                "nba:nba",
            )
            assert all(secret not in output + stderr for secret in unsafe)

            _invalidate_home_substitution(harness)
            await harness.ingest()
            changed_possessions = await possession_service.build(
                BuildPossessionsRequest((PIPELINE_SEASON,), game_id=PIPELINE_GAME_ID)
            )
            assert (changed_possessions.materialized, changed_possessions.failed) == (1, 0)
            changed_lineups = await harness.service.build(
                BuildLineupsRequest((PIPELINE_SEASON,), game_id=PIPELINE_GAME_ID)
            )
            assert (
                changed_lineups.materialized,
                changed_lineups.invalid,
                changed_lineups.failed,
            ) == (1, 1, 0)
            invalid_target_before = await store.resolve_target(PIPELINE_GAME_ID)
            invalid_source_ids = (
                invalid_target_before.possession_reference.attempt.snapshot_id,
                invalid_target_before.lineup_reference.attempt.snapshot_id,
            )
            before_invalid = await _feature_run_ids(harness.engine)
            code, output, stderr = await _run_dataset_cli(
                "--season", PIPELINE_SEASON.value, "--game-id", PIPELINE_GAME_ID.value
            )
            feature_run_ids |= await _feature_run_ids(harness.engine) - before_invalid
            assert code == 1, (output, stderr)
            assert "materialized=1 invalid=1 skipped_current=0 verified=0 failed=0 busy=0" in output
            invalid_target = await store.resolve_target(PIPELINE_GAME_ID)
            assert (
                invalid_target.possession_reference.attempt.snapshot_id,
                invalid_target.lineup_reference.attempt.snapshot_id,
            ) == invalid_source_ids
            invalid_rows = await ClickHouseFeatureSourceReader(harness.client).read_target(
                invalid_target
            )
            invalid_current = await store.resolve_current(
                invalid_target,
                invalid_rows.target_player_ids,
                PIPELINE_FEATURE_VERSIONS,
            )
            assert invalid_current.status is FeaturePublicationState.INVALID
            assert invalid_current.reference is not None
            invalid_dataset = await ClickHouseFeatureSnapshotStore(harness.client).read_snapshot(
                invalid_current.reference
            )
            assert invalid_dataset.status is FeatureResultStatus.INVALID
            assert "outgoing_not_on_court" in invalid_dataset.issues
            assert any(not row.home_player_ids for row in invalid_dataset.states)
        finally:
            feature_run_ids |= await _feature_run_ids(harness.engine)
            await _cleanup_pipeline_feature_state(
                harness,
                possession_run_ids=possession_run_ids,
                feature_run_ids=feature_run_ids,
            )
        await _assert_pipeline_feature_state_absent(harness)


@pytest.fixture
async def clickhouse() -> AsyncIterator[ClickHouseClient]:
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(),
        user="nba",
        password="nba",
        timeout_seconds=30,
    )
    claim, _ = snapshot_fixture()
    try:
        for table in (STATE_TABLE, PLAYER_TABLE):
            await client.execute(
                f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{claim.attempt.snapshot_id}' "
                "SETTINGS mutations_sync = 2"
            )
        yield client
    finally:
        for table in (STATE_TABLE, PLAYER_TABLE):
            await client.execute(
                f"ALTER TABLE {table} DELETE WHERE snapshot_id = '{claim.attempt.snapshot_id}' "
                "SETTINGS mutations_sync = 2"
            )
        await client.aclose()


async def test_exact_two_table_round_trip_is_idempotent_and_single_send(
    clickhouse: ClickHouseClient,
) -> None:
    claim, dataset = snapshot_fixture()
    store = ClickHouseFeatureSnapshotStore(clickhouse)

    await store.write_snapshot(claim, dataset)
    first = await store.verify_snapshot(claim.attempt)
    second = await store.verify_snapshot(claim.attempt)

    assert first == second
    assert first.status is FeatureVerificationStatus.EXACT
    assert first.state_row_count == len(dataset.states)
    assert first.player_row_count == len(dataset.players)
