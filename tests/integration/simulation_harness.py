"""Owned real Phase 2→8 acceptance; all provider payloads are local fixtures."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildService
from nba_live_totals.application.ingestion import BackfillRequest, HistoricalIngestionService
from nba_live_totals.application.lineups import BuildLineupsRequest, LineupBuildService
from nba_live_totals.application.lineups.models import GameLineupInputSnapshot
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    ModelSourceReference,
)
from nba_live_totals.application.possessions import BuildPossessionsRequest, PossessionBuildService
from nba_live_totals.application.simulation.models import ValidatedSimulationExport
from nba_live_totals.domain import AnalyticalPossessionParser, GameId, JsonObject, SeasonId
from nba_live_totals.domain.features import DeterministicFeatureCalculator
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import FullLineupReset, NormalizedGameLineups
from nba_live_totals.domain.source import JsonValue
from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.clickhouse.features import (
    ClickHouseFeatureSnapshotStore,
    ClickHouseFeatureSourceReader,
)
from nba_live_totals.infrastructure.clickhouse.lineups import ClickHouseLineupSnapshotStore
from nba_live_totals.infrastructure.clickhouse.possessions import PossessionSnapshotStore
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.simulation_export import CanonicalSimulationReader
from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.nba import (
    NBAPlayByPlayV3FeatureEventNormalizer,
    NBAPlayByPlayV3LineupNormalizer,
    NBAPlayByPlayV3PossessionNormalizer,
)
from nba_live_totals.infrastructure.postgres.feature_store import PostgresFeatureWorkStore
from nba_live_totals.infrastructure.postgres.ingestion_store import PostgresHistoricalIngestionStore
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.model_source import PublishedModelFeatureSource
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from scripts.build_training_dataset import parse_args
from tests import runtime_config
from tests.integration.baseline_harness import FV, LV, PV, OwnershipJournal
from tests.integration.test_possession_snapshot_protocol import FixtureSource

SEASON = SeasonId("2095-96")
GAMES = tuple(GameId(f"002959510{i}") for i in range(1, 6))
DATES = ("2095-10-01", "2095-10-02", "2095-10-31", "2095-11-05", "2095-12-05")
FIXTURES = Path(__file__).parents[1] / "fixtures/nba"


def retarget(value: JsonValue, game_id: str, date: str) -> JsonValue:
    ids = {91318001: 91395001, 91318002: 91395002}
    ids.update({n: 91395000 + n for n in (*range(101, 108), *range(201, 208))})
    ids.update({91394000 + n: 91395000 + n for n in (1, 2, *range(101, 108), *range(201, 208))})
    if isinstance(value, dict):
        return {key: retarget(item, game_id, date) for key, item in value.items()}
    if isinstance(value, list):
        return [retarget(item, game_id, date) for item in value]
    if type(value) is int:
        return ids.get(value, value)
    if type(value) is str:
        if value in {"0029899002", "0029494101"}:
            return game_id
        if value == "2098-99":
            return SEASON.value
        if value.startswith("2099-"):
            return date + value[10:]
        if value.isdigit() and int(value) in ids:
            return str(ids[int(value)])
    return value


def load(name: str) -> JsonObject:
    return cast(JsonObject, json.loads((FIXTURES / name).read_text(encoding="utf-8")))


class ResetNormalizer(NBAPlayByPlayV3LineupNormalizer):
    """Provider-neutral reset tagged with a separate fixture-only version."""

    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        normalized = super().normalize(snapshot)
        if normalized.game_id != GAMES[-1]:
            return normalized
        events = list(normalized.events)
        index = next(i for i, item in enumerate(events) if item.period == 3)
        events[index] = replace(
            events[index],
            transition=FullLineupReset(
                normalized.home_roster.team_id,
                tuple(sorted(normalized.home_roster.starter_player_ids)),
            ),
        )
        return replace(normalized, events=tuple(events))


@dataclass(frozen=True, slots=True)
class SimulationArtifacts:
    dataset_path: Path
    bundle_path: Path
    all_states_path: Path
    selected_state_path: Path
    all_states: ValidatedSimulationExport
    selected_state: ValidatedSimulationExport


class SimulationAcceptance:
    def __init__(
        self, engine: AsyncEngine, client: ClickHouseClient, *, recovery: bool = False
    ) -> None:
        self.engine, self.client = engine, client
        self.owned = OwnershipJournal()
        self.lv = replace(LV, normalizer_version="fixture-simulation-reset-v1") if recovery else LV
        self.normalizer = ResetNormalizer() if recovery else NBAPlayByPlayV3LineupNormalizer()
        self.work = PostgresFeatureWorkStore(
            self.engine, possession_versions=PV, lineup_versions=self.lv
        )
        self.source = PublishedModelFeatureSource(
            self.work,
            ClickHouseFeatureSourceReader(self.client),
            ClickHouseFeatureSnapshotStore(self.client),
            versions=FV,
            normalizer=NBAPlayByPlayV3PossessionNormalizer(),
        )
        self.fixture = FixtureSource()
        self.fixture.payloads = {}
        dates: list[JsonValue] = []
        for index, (gid, date) in enumerate(zip(GAMES, DATES, strict=True)):
            schedule = cast(
                JsonObject, retarget(load("lineups/lineup_schedule_2098-99.json"), gid.value, date)
            )
            box = cast(
                JsonObject,
                retarget(load("lineups/lineup_box_score_0029899002.json"), gid.value, date),
            )
            pbp = cast(
                JsonObject,
                retarget(
                    load(
                        "modeling/two-overtimes.json" if index == 1 else "modeling/regulation.json"
                    ),
                    gid.value,
                    date,
                ),
            )
            game = cast(dict[str, JsonValue], pbp["game"])
            actions = cast(list[dict[str, JsonValue]], game["actions"])
            final = actions[-1]
            league = cast(dict[str, JsonValue], schedule["leagueSchedule"])
            game_dates = cast(list[dict[str, JsonValue]], league["gameDates"])
            scheduled = cast(list[dict[str, JsonValue]], game_dates[0]["games"])[0]
            traditional = cast(dict[str, JsonValue], box["boxScoreTraditional"])
            for side, score in (("homeTeam", "scoreHome"), ("awayTeam", "scoreAway")):
                cast(dict[str, JsonValue], scheduled[side])["score"] = int(str(final[score]))
                team = cast(dict[str, JsonValue], traditional[side])
                cast(dict[str, JsonValue], team["statistics"])["points"] = int(str(final[score]))
            if recovery and index == 4:
                substitution = deepcopy(actions[4])
                substitution.update(
                    actionType="substitution",
                    subType="Out",
                    teamId=91395001,
                    personId=91395107,
                    description="SUB: Player106 FOR Player107",
                    playerName="Home Player107",
                    playerNameI="H. Player107",
                )
                actions.insert(5, substitution)
                for sequence, action in enumerate(actions, 1):
                    action["actionNumber"], action["actionId"] = sequence, sequence * 10
            dates.extend(game_dates)
            self.fixture.payloads[f"box_score:{gid.value}"] = box
            self.fixture.payloads[f"play_by_play:{gid.value}"] = pbp
        cast(dict[str, JsonValue], schedule["leagueSchedule"])["gameDates"] = dates
        self.fixture.payloads[f"schedule:{SEASON.value}"] = schedule

    async def seed_pipeline(self) -> tuple[ModelSourceReference, ...]:
        ingestion = await HistoricalIngestionService(
            self.fixture, PostgresHistoricalIngestionStore(self.engine), max_concurrency=1
        ).backfill(BackfillRequest((SEASON,)))
        assert ingestion.failed == 0
        for gid in GAMES:
            possessions = await PossessionBuildService(
                normalizer=NBAPlayByPlayV3PossessionNormalizer(),
                parser=AnalyticalPossessionParser(),
                work_store=PostgresPossessionWorkStore(self.engine),
                snapshot_store=PossessionSnapshotStore(self.client),
                versions=PV,
                max_concurrency=1,
            ).build(BuildPossessionsRequest((SEASON,), game_id=gid))
            assert possessions.materialized == 1 and possessions.failed == 0, possessions
            lineups = await LineupBuildService(
                normalizer=self.normalizer,
                reconstructor=DeterministicLineupReconstructor(),
                work_store=PostgresLineupWorkStore(self.engine),
                snapshot_store=ClickHouseLineupSnapshotStore(self.client),
                versions=self.lv,
                max_concurrency=1,
            ).build(BuildLineupsRequest((SEASON,), game_id=gid))
            assert lineups.materialized == 1 and lineups.failed == 0, lineups
            features = await FeatureBuildService(
                normalizer=NBAPlayByPlayV3FeatureEventNormalizer(),
                source_reader=ClickHouseFeatureSourceReader(self.client),
                calculator=DeterministicFeatureCalculator(),
                work_store=self.work,
                snapshot_store=ClickHouseFeatureSnapshotStore(self.client),
                versions=FV,
            ).build(BuildDatasetRequest((SEASON,), game_id=gid))
            assert features.materialized == 1 and features.failed == 0, features
        return tuple([await self.source.resolve(gid) for gid in GAMES])

    async def storage_projection(self) -> tuple[dict[str, list[dict[str, Any]]], list[object]]:
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

    async def cleanup(self) -> None:
        await self.owned.cleanup(self.engine, self.client)

    def dataset_args(self, path: Path) -> tuple[str, ...]:
        return (
            "--season",
            SEASON.value,
            "--validation-from",
            "2095-11-01",
            "--test-from",
            "2095-12-01",
            "--output",
            str(path),
        )

    def dataset_request(self, path: Path) -> BuildTrainingDatasetRequest:
        return parse_args(self.dataset_args(path))

    async def run_cli(self, module: str, *arguments: str) -> tuple[int, str, str]:
        environment = os.environ | {
            "DATABASE_URL": runtime_config.postgres_url(),
            "CLICKHOUSE_URL": runtime_config.clickhouse_url(),
            "CLICKHOUSE_USER": "nba",
            "CLICKHOUSE_PASSWORD": "nba",
            "LINEUP_NORMALIZER_VERSION": self.lv.normalizer_version,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            f"scripts.{module}",
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 120)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                await process.communicate()
            await process.wait()
        assert process.returncode is not None
        return process.returncode, out.decode(), err.decode()

    async def run_clis(self, directory: Path) -> SimulationArtifacts:
        dataset, bundle, all_states, selected = (
            directory / name
            for name in ("dataset.jsonl", "models.nbm", "totals.jsonl", "selected.jsonl")
        )
        for module, args in (
            ("build_training_dataset", self.dataset_args(dataset)),
            (
                "train_models",
                (
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(bundle),
                    "--iterations",
                    "12",
                    "--early-stopping-rounds",
                    "3",
                ),
            ),
        ):
            code, out, err = await self.run_cli(module, *args)
            assert code == 0, (module, code, out, err)
        baseline = await self.storage_projection()
        calls = tuple(self.fixture.fetches)
        base = (
            "--season",
            SEASON.value,
            "--model",
            str(bundle),
            "--simulations",
            "128",
            "--batch-size",
            "64",
        )
        query = ("--line", "210", "--line", "210.5")
        code, out, err = await self.run_cli(
            "simulate_totals", *base, *query, "--output", str(all_states)
        )
        assert code == 1 and "artifact_sha256=" in out, (code, out, err)
        reader = CanonicalSimulationReader()
        checked = reader.validate(all_states)
        chosen = next(row for row in reader.iter_outcomes(checked) if row.frame.is_available)
        code, out, err = await self.run_cli(
            "simulate_totals",
            *base,
            *query,
            "--game-id",
            chosen.frame.game_id.value,
            "--state-number",
            str(chosen.frame.state_number),
            "--output",
            str(selected),
        )
        assert code in (0, 1), (code, out, err)
        checked_selected = reader.validate(selected)
        assert next(reader.iter_outcomes(checked_selected)) == chosen
        repeated = directory / "totals-again.jsonl"
        code, out, err = await self.run_cli(
            "simulate_totals", *base, *query, "--output", str(repeated)
        )
        assert code == 1 and repeated.read_bytes() == all_states.read_bytes(), (code, out, err)
        changed_query = directory / "query.jsonl"
        code, out, err = await self.run_cli(
            "simulate_totals",
            *base,
            "--line",
            "0.5",
            "--quantile",
            "0.5",
            "--output",
            str(changed_query),
        )
        assert code == 1, (code, out, err)
        changed = reader.validate(changed_query)
        assert tuple(reader.iter_outcomes(changed)) == tuple(reader.iter_outcomes(checked))
        assert changed.manifest.run_semantic_revision != checked.manifest.run_semantic_revision
        assert tuple(self.fixture.fetches) == calls and await self.storage_projection() == baseline
        evidence = {
            "season": SEASON.value,
            "fixture_lineup_version": self.lv.normalizer_version,
            "games": checked.manifest.counts.games,
            "states": checked.manifest.counts.states,
            "complete": checked.manifest.counts.complete,
            "incomplete": checked.manifest.counts.incomplete,
            "unavailable": checked.manifest.counts.unavailable,
            "unresolved": checked.manifest.counts.unresolved,
            "artifact_sha256": checked.artifact_sha256,
            "run_semantic_revision": checked.manifest.run_semantic_revision,
            "selected_artifact_sha256": checked_selected.artifact_sha256,
            "model_artifact_sha256": VerifiedModelBundleStore().load(bundle).artifact_sha256,
            "read_only_storage_equal": True,
        }
        (directory / "acceptance-evidence.json").write_text(
            json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return SimulationArtifacts(dataset, bundle, all_states, selected, checked, checked_selected)


@asynccontextmanager
async def simulation_acceptance(*, recovery: bool = False) -> AsyncIterator[SimulationAcceptance]:
    engine = create_async_engine(runtime_config.postgres_url())
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30
    )
    harness = SimulationAcceptance(engine, client, recovery=recovery)
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
