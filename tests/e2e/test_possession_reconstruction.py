"""End-to-end acceptance from stored NBA V3 PBP to the PG current pointer."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from nba_live_totals.application.ingestion import ResourceKey
from nba_live_totals.application.possessions import BuildMode, BuildPossessionsRequest
from nba_live_totals.domain import (
    AnalyticalPossessionParser,
    NormalizedGameActions,
    Possession,
    PossessionParseContext,
    PossessionParseResult,
)
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from tests.integration.test_possession_snapshot_protocol import (
    AWAY_TEAM_ID,
    GAME_ID,
    HOME_TEAM_ID,
    RESOURCE_KEYS,
    SEASON,
    VERSIONS,
    Phase3Harness,
    RecordingClickHouse,
    ingest_fixture,
)

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
pytest_plugins = ("tests.integration.test_possession_snapshot_protocol",)

ORIGINAL_OUTPUT_SHA256 = "118586664378d6a9c83ab665bb42b26f8848862d168a37390d963b699313c991"
CHANGED_OUTPUT_SHA256 = "9780a2c8100aa4c697567f2fefbd2606ebd166a38b029357194c7bcbe51ef4dc"


class RecordingParser:
    """Delegate to the production parser and expose its exact result to acceptance."""

    def __init__(self) -> None:
        self.parser = AnalyticalPossessionParser()
        self.results: list[PossessionParseResult] = []

    def parse(
        self,
        context: PossessionParseContext,
        normalized: NormalizedGameActions,
    ) -> PossessionParseResult:
        result = self.parser.parse(context, normalized)
        self.results.append(result)
        return result


def exact_projection(rows: tuple[Possession, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            row.possession_number,
            row.period,
            row.offense_team_id,
            row.defense_team_id,
            (row.start_boundary.sequence_number, row.start_boundary.side.value),
            (row.end_boundary.sequence_number, row.end_boundary.side.value),
            row.start_seconds_remaining_period_ms,
            row.end_seconds_remaining_period_ms,
            (row.home_score_before, row.away_score_before),
            (row.home_score, row.away_score),
            row.end_reason.value,
            row.source_event_count,
            (row.home_possessions, row.away_possessions),
        )
        for row in rows
    )


async def test_fixture_pipeline_is_deterministic_versioned_and_pointer_authoritative(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    assert harness.source.fetches == [
        ResourceKey.schedule(SEASON),
        ResourceKey.box_score(GAME_ID),
        ResourceKey.play_by_play(GAME_ID),
    ]

    first_store = harness.work_store()
    first_transport = RecordingClickHouse(harness.client)
    parser = RecordingParser()
    first = await harness.service(first_store, first_transport, parser).build(
        BuildPossessionsRequest((SEASON,), game_id=GAME_ID)
    )
    assert (first.discovered, first.materialized, first.failed, first.busy) == (1, 1, 0, 0)
    assert first_transport.insert_calls == 1
    assert first_transport.query_calls == 1
    assert len(parser.results) == 1
    parsed = parser.results[0]
    assert parsed.output_sha256 == ORIGINAL_OUTPUT_SHA256
    assert [
        (issue.severity.value, issue.code, issue.sequence_number, issue.related_sequence_number)
        for issue in parsed.issues
    ] == [
        ("warning", "offense_inferred", 2, None),
        ("warning", "offense_inferred", 3, None),
        ("warning", "offense_inferred", 8, None),
        ("warning", "offense_inferred", 11, None),
        ("warning", "offense_inferred", 14, None),
    ]
    current_one = await first_store.resolve_current(GAME_ID, VERSIONS)
    assert current_one is not None
    rows_one = await PossessionSnapshotStore(harness.client).read_snapshot(current_one)
    assert current_one.output_sha256 == ORIGINAL_OUTPUT_SHA256
    assert exact_projection(rows_one) == (
        (
            1,
            1,
            HOME_TEAM_ID,
            AWAY_TEAM_ID,
            (2, "before"),
            (2, "after"),
            700000,
            700000,
            (0, 0),
            (2, 0),
            "made_field_goal",
            1,
            (1, 0),
        ),
        (
            2,
            1,
            AWAY_TEAM_ID,
            HOME_TEAM_ID,
            (3, "before"),
            (3, "after"),
            670000,
            670000,
            (2, 0),
            (2, 0),
            "turnover",
            1,
            (1, 1),
        ),
        (
            3,
            1,
            HOME_TEAM_ID,
            AWAY_TEAM_ID,
            (3, "after"),
            (5, "after"),
            670000,
            642000,
            (2, 0),
            (2, 0),
            "defensive_rebound",
            2,
            (2, 1),
        ),
        (
            4,
            1,
            AWAY_TEAM_ID,
            HOME_TEAM_ID,
            (5, "after"),
            (6, "after"),
            642000,
            0,
            (2, 0),
            (2, 0),
            "period_end",
            1,
            (2, 2),
        ),
        (
            5,
            2,
            AWAY_TEAM_ID,
            HOME_TEAM_ID,
            (8, "before"),
            (8, "after"),
            690000,
            690000,
            (2, 0),
            (2, 3),
            "made_field_goal",
            1,
            (2, 3),
        ),
        (
            6,
            3,
            HOME_TEAM_ID,
            AWAY_TEAM_ID,
            (11, "before"),
            (11, "after"),
            660000,
            660000,
            (2, 3),
            (4, 3),
            "made_field_goal",
            1,
            (3, 3),
        ),
        (
            7,
            4,
            AWAY_TEAM_ID,
            HOME_TEAM_ID,
            (14, "before"),
            (14, "after"),
            5000,
            5000,
            (4, 3),
            (4, 5),
            "made_field_goal",
            1,
            (3, 4),
        ),
    )

    skipped_store = harness.work_store()
    skipped_transport = RecordingClickHouse(harness.client)
    skipped = await harness.service(skipped_store, skipped_transport).build(
        BuildPossessionsRequest((SEASON,), game_id=GAME_ID)
    )
    assert (skipped.skipped_current, skipped.materialized) == (1, 0)
    assert (skipped_transport.insert_calls, skipped_transport.query_calls) == (0, 0)
    assert await harness.clickhouse_count() == 7

    rebuilt_store = harness.work_store()
    rebuilt = await harness.service(rebuilt_store).build(
        BuildPossessionsRequest((SEASON,), game_id=GAME_ID, rebuild=True)
    )
    assert rebuilt.materialized == 1
    current_two = await rebuilt_store.resolve_current(GAME_ID, VERSIONS)
    assert current_two is not None
    assert current_two.attempt.snapshot_id != current_one.attempt.snapshot_id
    assert current_two.attempt.generation == current_one.attempt.generation + 1
    assert current_two.attempt.parse_revision_id == current_one.attempt.parse_revision_id
    assert current_two.output_sha256 == current_one.output_sha256
    assert await harness.clickhouse_count() == 14

    verify_store = harness.work_store()
    verify_transport = RecordingClickHouse(harness.client)
    before_verify_resource = await harness.resource()
    before_verify_attempts = await harness.attempts()
    verified = await harness.service(verify_store, verify_transport).build(
        BuildPossessionsRequest((SEASON,), game_id=GAME_ID, mode=BuildMode.VERIFY)
    )
    assert (verified.verified, verified.materialized, verified.failed) == (1, 0, 0)
    assert (verify_transport.insert_calls, verify_transport.query_calls) == (0, 1)
    assert await harness.resource() == before_verify_resource
    assert await harness.attempts() == before_verify_attempts
    assert await harness.clickhouse_count() == 14


async def test_changed_valid_and_invalid_pbp_never_publish_the_wrong_candidate(
    harness: Phase3Harness,
) -> None:
    await ingest_fixture(harness)
    initial_store = harness.work_store()
    assert (
        await harness.service(initial_store).build(BuildPossessionsRequest((SEASON,)))
    ).materialized == 1
    original = await initial_store.resolve_current(GAME_ID, VERSIONS)
    assert original is not None

    changed_payload = deepcopy(harness.source.payloads[RESOURCE_KEYS[2]])
    changed_actions = cast(
        list[dict[str, object]], cast(dict[str, object], changed_payload["game"])["actions"]
    )
    changed_actions[13]["clock"] = "PT0M04.00S"
    harness.source.payloads[RESOURCE_KEYS[2]] = changed_payload
    await ingest_fixture(harness, refresh=True)

    changed_store = harness.work_store()
    changed = await harness.service(changed_store).build(BuildPossessionsRequest((SEASON,)))
    assert changed.materialized == 1
    current = await changed_store.resolve_current(GAME_ID, VERSIONS)
    assert current is not None
    assert current.attempt.snapshot_id != original.attempt.snapshot_id
    assert current.output_sha256 == CHANGED_OUTPUT_SHA256
    changed_rows = await PossessionSnapshotStore(harness.client).read_snapshot(current)
    assert changed_rows[-1].start_seconds_remaining_period_ms == 4000
    assert changed_rows[-1].seconds_remaining_game_ms == 4000

    invalid_payload = deepcopy(changed_payload)
    invalid_actions = cast(
        list[dict[str, object]], cast(dict[str, object], invalid_payload["game"])["actions"]
    )
    invalid_actions[13]["actionType"] = "Unsupported Scoring Action"
    harness.source.payloads[RESOURCE_KEYS[2]] = invalid_payload
    await ingest_fixture(harness, refresh=True)

    invalid_store = harness.work_store()
    invalid = await harness.service(invalid_store).build(BuildPossessionsRequest((SEASON,)))
    resource = await harness.resource()
    assert (invalid.materialized, invalid.failed) == (0, 1)
    assert resource["published_snapshot_id"] == current.attempt.snapshot_id
    assert resource["publication_state"] == "invalid"
    assert [row["state"] for row in await harness.attempts()] == [
        "published",
        "published",
        "poisoned",
    ]
    assert await invalid_store.resolve_current(GAME_ID, VERSIONS) is None
    assert await PossessionSnapshotStore(harness.client).read_snapshot(current) == changed_rows
