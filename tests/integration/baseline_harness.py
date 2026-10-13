"""Owned two-game Phase 2-6 acceptance with exclusively local NBA fixtures."""

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, and_, delete, event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql.dml import Insert

from nba_live_totals.application.baselines.models import BaselineSourceReference
from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildService
from nba_live_totals.application.ingestion import BackfillRequest, HistoricalIngestionService
from nba_live_totals.application.lineups import BuildLineupsRequest, LineupBuildService
from nba_live_totals.application.lineups.models import GameLineupInputSnapshot
from nba_live_totals.application.possessions import BuildPossessionsRequest, PossessionBuildService
from nba_live_totals.domain import AnalyticalPossessionParser, GameId, JsonObject, SeasonId
from nba_live_totals.domain.features import DeterministicFeatureCalculator, FeatureVersions
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import FullLineupReset, LineupVersions, NormalizedGameLineups
from nba_live_totals.domain.possessions import PossessionVersions
from nba_live_totals.domain.source import JsonValue
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    ClickHouseFeatureSourceReader,
)
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.nba import (
    NBAPlayByPlayV3FeatureEventNormalizer,
    NBAPlayByPlayV3LineupNormalizer,
    NBAPlayByPlayV3PossessionNormalizer,
)
from nba_live_totals.infrastructure.postgres import schema
from nba_live_totals.infrastructure.postgres.baseline_source import PublishedFeatureBaselineSource
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.ingestion_store import PostgresHistoricalIngestionStore
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from tests import runtime_config
from tests.e2e.test_feature_engineering import (
    _install_scoring_pipeline_pbp,
    _invalidate_home_substitution,
)
from tests.e2e.test_lineup_reconstruction import KEYS, LineupAcceptance
from tests.integration.test_possession_snapshot_protocol import FixtureSource

SEASON = SeasonId("2096-97")
PRIOR, TARGET = GameId("0029696101"), GameId("0029696102")
PV = PossessionVersions("nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1")
LV = LineupVersions("nba-pbp-v3-lineup-v1", "causal-lineup-v1", "nba-5x5-v1", "lineup-segments-v1")
FV = FeatureVersions(join_version="possession-lineup-inclusive-v1")


def retarget(value: JsonValue, game_id: str, date: str) -> JsonValue:
    ids = {91318001: 91396001, 91318002: 91396002}
    ids.update({n: 91396000 + n for n in (*range(101, 108), *range(201, 208))})
    if isinstance(value, dict):
        return {key: retarget(item, game_id, date) for key, item in value.items()}
    if isinstance(value, list):
        return [retarget(item, game_id, date) for item in value]
    if type(value) is int:
        return ids.get(value, value)
    if type(value) is str:
        if value == "0029899002":
            return game_id
        if value == "2098-99":
            return SEASON.value
        if value.startswith("2099-"):
            return date + value[10:]
        if value.isdigit() and int(value) in ids:
            return str(ids[int(value)])
    return value


class OwnershipJournal:
    def __init__(self) -> None:
        self.rows: dict[Table, set[tuple[object, ...]]] = {}

    def before_insert(
        self,
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        compiled = context.compiled
        if compiled is None or not isinstance(compiled.statement, Insert):
            return
        table = compiled.statement.table
        assert isinstance(table, Table)
        columns = tuple(table.primary_key.columns)
        owned = self.rows.setdefault(table, set())
        for values in context.compiled_parameters:
            suffixes = {
                key[len(columns[0].name) :]
                for key in values
                if key == columns[0].name
                or re.fullmatch(re.escape(columns[0].name) + r"_m\d+", key)
            }
            assert suffixes, "insert primary key must be explicit"
            for suffix in sorted(suffixes):
                identity = tuple(values[column.name + suffix] for column in columns)
                if identity in owned:
                    continue
                predicate = and_(
                    *(column == value for column, value in zip(columns, identity, strict=True))
                )
                assert (
                    connection.scalar(select(func.count()).select_from(table).where(predicate)) == 0
                ), "unowned fixture collision"
                owned.add(identity)

    async def cleanup(self, engine: AsyncEngine, client: ClickHouseClient) -> None:
        for table, name in (
            (schema.possession_snapshot_attempts, "possessions_v1"),
            (schema.lineup_snapshot_attempts, "lineup_segments_v1"),
            (schema.feature_snapshot_attempts, "game_state_features_v1"),
            (schema.feature_snapshot_attempts, "player_pregame_features_v1"),
        ):
            for identity in self.rows.get(table, ()):
                await client.execute(
                    f"ALTER TABLE nba.{name} DELETE WHERE snapshot_id = '{UUID(str(identity[0]))}' "
                    "SETTINGS mutations_sync = 2"
                )
        async with engine.begin() as connection:
            for table in reversed(metadata.sorted_tables):
                for identity in self.rows.get(table, ()):
                    await connection.execute(
                        delete(table).where(
                            and_(
                                *(
                                    column == value
                                    for column, value in zip(
                                        table.primary_key.columns, identity, strict=True
                                    )
                                )
                            )
                        )
                    )


class FixtureResetNormalizer(NBAPlayByPlayV3LineupNormalizer):
    """Explicit provider-neutral fixture reset, distinct from NBA normalizer V1."""

    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        normalized = super().normalize(snapshot)
        if normalized.game_id != TARGET:
            return normalized
        events = list(normalized.events)
        index = next(i for i, e in enumerate(events) if e.period == 3)
        events[index] = replace(
            events[index],
            transition=FullLineupReset(
                normalized.home_roster.team_id,
                tuple(sorted(normalized.home_roster.starter_player_ids)),
            ),
        )
        return replace(normalized, events=tuple(events))


class BaselineAcceptance:
    def __init__(
        self, engine: AsyncEngine, client: ClickHouseClient, *, recovery: bool = False
    ) -> None:
        self.engine, self.client = engine, client
        self.owned = OwnershipJournal()
        base = LineupAcceptance(engine, client)
        _install_scoring_pipeline_pbp(base)
        self.lv = replace(LV, normalizer_version="fixture-lineup-reset-v1") if recovery else LV
        self.normalizer = (
            FixtureResetNormalizer() if recovery else NBAPlayByPlayV3LineupNormalizer()
        )
        self.fixture = FixtureSource()
        self.fixture.payloads = {}
        dates: list[dict[str, JsonValue]] = []
        for gid, date in ((PRIOR, "2096-10-01"), (TARGET, "2096-10-03")):
            if gid == TARGET and recovery:
                _invalidate_home_substitution(base)
            schedule, box, pbp = [
                cast(JsonObject, retarget(deepcopy(base.source.payloads[key]), gid.value, date))
                for key in KEYS
            ]
            league = cast(dict[str, JsonValue], schedule["leagueSchedule"])
            dates.extend(cast(list[dict[str, JsonValue]], league["gameDates"]))
            self.fixture.payloads[f"box_score:{gid.value}"] = box
            self.fixture.payloads[f"play_by_play:{gid.value}"] = pbp
        schedule = cast(
            JsonObject, retarget(deepcopy(base.source.payloads[KEYS[0]]), PRIOR.value, "2096-10-01")
        )
        cast(dict[str, JsonValue], schedule["leagueSchedule"])["gameDates"] = cast(JsonValue, dates)
        self.fixture.payloads[f"schedule:{SEASON.value}"] = schedule
        self.work = PostgresFeatureWorkStore(
            engine, possession_versions=PV, lineup_versions=self.lv
        )
        self.source = PublishedFeatureBaselineSource(
            self.work,
            ClickHouseFeatureSourceReader(client),
            ClickHouseFeatureSnapshotStore(client),
            versions=FV,
        )

    async def seed_pipeline(self) -> tuple[BaselineSourceReference, ...]:
        ingested = await HistoricalIngestionService(
            self.fixture, PostgresHistoricalIngestionStore(self.engine), max_concurrency=1
        ).backfill(BackfillRequest((SEASON,)))
        assert ingested.failed == 0
        for gid in (PRIOR, TARGET):
            possessions = await PossessionBuildService(
                normalizer=NBAPlayByPlayV3PossessionNormalizer(),
                parser=AnalyticalPossessionParser(),
                work_store=PostgresPossessionWorkStore(self.engine),
                snapshot_store=PossessionSnapshotStore(self.client),
                versions=PV,
                max_concurrency=1,
            ).build(BuildPossessionsRequest((SEASON,), game_id=gid))
            assert possessions.materialized == 1 and possessions.failed == 0
            lineups = await LineupBuildService(
                normalizer=self.normalizer,
                reconstructor=DeterministicLineupReconstructor(),
                work_store=PostgresLineupWorkStore(self.engine),
                snapshot_store=ClickHouseLineupSnapshotStore(self.client),
                versions=self.lv,
                max_concurrency=1,
            ).build(BuildLineupsRequest((SEASON,), game_id=gid))
            assert lineups.materialized == 1 and lineups.failed == 0
            features = await FeatureBuildService(
                normalizer=NBAPlayByPlayV3FeatureEventNormalizer(),
                source_reader=ClickHouseFeatureSourceReader(self.client),
                calculator=DeterministicFeatureCalculator(),
                work_store=self.work,
                snapshot_store=ClickHouseFeatureSnapshotStore(self.client),
                versions=FV,
            ).build(BuildDatasetRequest((SEASON,), game_id=gid))
            assert features.materialized == 1 and features.failed == 0
        return tuple([await self.source.resolve(gid) for gid in (PRIOR, TARGET)])

    async def projection(self) -> tuple[dict[str, list[dict[str, Any]]], list[object]]:
        async with self.engine.connect() as connection:
            pg = {
                table.name: [
                    dict(row)
                    for row in (
                        await connection.execute(select(table).order_by(*table.primary_key.columns))
                    ).mappings()
                ]
                for table in metadata.sorted_tables
            }
        ch: list[object] = []
        for name in (
            "possessions_v1",
            "lineup_segments_v1",
            "game_state_features_v1",
            "player_pregame_features_v1",
        ):
            ch.append(await self.client.query(f"SELECT count() AS count FROM nba.{name}"))
        return pg, ch

    async def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.build_baselines",
            *arguments,
            env=os.environ
            | {
                "DATABASE_URL": runtime_config.postgres_url(),
                "CLICKHOUSE_URL": runtime_config.clickhouse_url(),
                "CLICKHOUSE_USER": "nba",
                "CLICKHOUSE_PASSWORD": "nba",
                "LINEUP_NORMALIZER_VERSION": self.lv.normalizer_version,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 90)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            await process.wait()
        assert process.returncode is not None
        return process.returncode, out.decode(), err.decode()

    async def cleanup(self) -> None:
        await self.owned.cleanup(self.engine, self.client)


@asynccontextmanager
async def baseline_acceptance(*, recovery: bool = False) -> AsyncIterator[BaselineAcceptance]:
    engine = create_async_engine(runtime_config.postgres_url())
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30
    )
    harness = BaselineAcceptance(engine, client, recovery=recovery)
    event.listen(engine.sync_engine, "before_cursor_execute", harness.owned.before_insert)
    try:
        yield harness
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", harness.owned.before_insert)
        try:
            await harness.cleanup()
        finally:
            await client.aclose()
            await engine.dispose()
