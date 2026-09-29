"""Owned Phase 5 target and claim fixtures for real PostgreSQL tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.application.features import BuildDatasetRequest, FeatureBuildClaim
from nba_live_totals.application.lineups import BuildLineupsRequest
from nba_live_totals.application.possessions import BuildPossessionsRequest
from nba_live_totals.domain import GameId, PossessionVersions, SeasonId
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.infrastructure.postgres.lineup_store import PostgresLineupWorkStore
from nba_live_totals.infrastructure.postgres.possession_store import PostgresPossessionWorkStore
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
    games,
    player_box_scores,
    possession_rebuild_resources,
    possession_snapshot_attempts,
    team_box_scores,
)
from tests.integration.lineup_harness import LineupHarness
from tests.integration.possession_harness import BASE_TIME

GAME_ID = GameId("0022500111")
REQUEST = BuildDatasetRequest(seasons=(SeasonId("2025-26"),))
POSSESSION_VERSIONS = PossessionVersions(
    "nba-pbp-v2", "analytical-v1", "nba-2025-v1", "possessions-v1"
)
LINEUP_VERSIONS = LineupVersions("nba-v1", "lineup-v1", "nba-v1", "lineups-v1")
FEATURE_VERSIONS = FeatureVersions()


@dataclass
class FeatureHarness:
    engine: AsyncEngine
    lineups: LineupHarness = field(init=False)
    feature_run_ids: list[UUID] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lineups = LineupHarness(self.engine)

    @property
    def source(self) -> str:
        return self.lineups.source

    async def seed_published_target(
        self, *, game_id: str = GAME_ID.value, invalid_lineup: bool = False
    ) -> GameId:
        await self.lineups.seed_game(game_id=game_id)
        target = GameId(game_id)

        await self._publish_possession(target)

        lineup_store = PostgresLineupWorkStore(
            self.engine, source=self.source, utc_now=lambda: BASE_TIME
        )
        lineup_run = await lineup_store.start_run(
            BuildLineupsRequest(seasons=(SeasonId("2025-26"),))
        )
        self.lineups.run_ids.append(lineup_run)
        lineup = await lineup_store.claim_game(lineup_run, target, LINEUP_VERSIONS, rebuild=False)
        assert lineup.claim is not None
        await self.lineups.publish(lineup.claim, invalid=invalid_lineup)
        return target

    async def seed_history(self, *, game_id: str, started_at: datetime) -> GameId:
        await self.lineups.seed_game(game_id=game_id)
        target = GameId(game_id)
        stat_values = {
            "seconds_played": 14_400,
            "field_goals_made": 40,
            "field_goals_attempted": 80,
            "three_pointers_made": 12,
            "three_pointers_attempted": 30,
            "free_throws_made": 18,
            "free_throws_attempted": 22,
            "rebounds_offensive": 10,
            "rebounds_defensive": 30,
            "rebounds_total": 40,
            "assists": 24,
            "turnovers": 11,
            "points": 110,
        }
        async with self.engine.begin() as connection:
            await connection.execute(
                update(games).where(games.c.game_id == game_id).values(started_at=started_at)
            )
            for team_id in (910001, 910002):
                await connection.execute(
                    insert(team_box_scores).values(game_id=game_id, team_id=team_id, **stat_values)
                )
            await connection.execute(
                update(player_box_scores)
                .where(player_box_scores.c.game_id == game_id)
                .values(**(stat_values | {"seconds_played": 1_800}))
            )
        await self._publish_possession(target)
        return target

    async def _publish_possession(self, target: GameId) -> None:
        possession_store = PostgresPossessionWorkStore(
            self.engine, source=self.source, utc_now=lambda: BASE_TIME
        )
        possession_run = await possession_store.start_run(
            BuildPossessionsRequest(seasons=(SeasonId("2025-26"),))
        )
        self.lineups.base.run_ids.append(possession_run)
        possession = await possession_store.claim_game(
            possession_run, target, POSSESSION_VERSIONS, rebuild=False
        )
        assert possession.claim is not None
        possession_claim = possession.claim
        async with self.engine.begin() as connection:
            await connection.execute(
                update(possession_snapshot_attempts)
                .where(
                    possession_snapshot_attempts.c.snapshot_id
                    == possession_claim.attempt.snapshot_id
                )
                .values(
                    state="published",
                    expected_row_count=1,
                    expected_output_sha256="c" * 64,
                    insert_started_at=BASE_TIME,
                    verified_at=BASE_TIME,
                    published_at=BASE_TIME,
                )
            )
            await connection.execute(
                update(possession_rebuild_resources)
                .where(possession_rebuild_resources.c.game_id == target.value)
                .values(
                    publication_state="current",
                    active_run_id=None,
                    active_claim_version=None,
                    active_snapshot_id=None,
                    active_parse_revision_id=None,
                    active_input_token_sha256=None,
                    claimed_until=None,
                    published_snapshot_id=possession_claim.attempt.snapshot_id,
                    published_parse_revision_id=possession_claim.attempt.parse_revision_id,
                    published_input_token_sha256=possession_claim.attempt.input_token.sha256,
                    published_at=BASE_TIME,
                )
            )

    async def publish_feature(self, claim: FeatureBuildClaim, *, invalid: bool = False) -> None:
        issues = ["bad_feature"] if invalid else []
        async with self.engine.begin() as connection:
            await connection.execute(
                update(feature_snapshot_attempts)
                .where(feature_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state="published",
                    expected_state_row_count=1,
                    expected_state_output_sha256="d" * 64,
                    expected_player_row_count=0,
                    expected_player_output_sha256="e" * 64,
                    expected_feature_output_sha256="f" * 64,
                    expected_issue_codes=issues,
                    result_status="invalid" if invalid else "valid",
                    insert_started_at=BASE_TIME,
                    verified_at=BASE_TIME,
                    published_at=BASE_TIME,
                )
            )
            await connection.execute(
                update(feature_build_resources)
                .where(feature_build_resources.c.game_id == claim.attempt.game_id.value)
                .values(
                    publication_state="invalid" if invalid else "current",
                    last_issue_codes=issues,
                    active_run_id=None,
                    active_claim_version=None,
                    active_snapshot_id=None,
                    active_feature_revision_id=None,
                    active_feature_input_token_sha256=None,
                    claimed_until=None,
                    published_snapshot_id=claim.attempt.snapshot_id,
                    published_feature_revision_id=claim.attempt.feature_revision_id,
                    published_feature_input_token_sha256=claim.attempt.input_token.sha256,
                    published_at=BASE_TIME,
                )
            )

    async def cleanup(self) -> None:
        game_ids = tuple(self.lineups.base.game_ids)
        async with self.engine.begin() as connection:
            if game_ids:
                attempt_ids = tuple(
                    await connection.scalars(
                        feature_snapshot_attempts.select()
                        .with_only_columns(feature_snapshot_attempts.c.snapshot_id)
                        .where(feature_snapshot_attempts.c.game_id.in_(game_ids))
                    )
                )
                if attempt_ids:
                    await connection.execute(
                        delete(feature_snapshot_dependencies).where(
                            feature_snapshot_dependencies.c.snapshot_id.in_(attempt_ids)
                        )
                    )
                await connection.execute(
                    delete(feature_build_resources).where(
                        feature_build_resources.c.game_id.in_(game_ids)
                    )
                )
                await connection.execute(
                    delete(feature_snapshot_attempts).where(
                        feature_snapshot_attempts.c.game_id.in_(game_ids)
                    )
                )
                await connection.execute(
                    delete(team_box_scores).where(team_box_scores.c.game_id.in_(game_ids))
                )
            if self.feature_run_ids:
                await connection.execute(
                    delete(feature_build_runs).where(
                        feature_build_runs.c.run_id.in_(self.feature_run_ids)
                    )
                )
        await self.lineups.cleanup()

    @staticmethod
    def now(*, elapsed: int = 0) -> Callable[[], datetime]:
        return lambda: BASE_TIME + timedelta(seconds=elapsed)


__all__ = [
    "FEATURE_VERSIONS",
    "GAME_ID",
    "LINEUP_VERSIONS",
    "POSSESSION_VERSIONS",
    "REQUEST",
    "FeatureHarness",
]
