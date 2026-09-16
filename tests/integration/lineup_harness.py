"""Owned dual-input rows and publication fixtures for real PostgreSQL tests."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from nba_live_totals.application.lineups import LineupBuildClaim
from nba_live_totals.domain import JsonObject, canonical_json_checksum
from nba_live_totals.infrastructure.postgres.schema import (
    ingestion_resources,
    lineup_rebuild_resources,
    lineup_rebuild_runs,
    lineup_snapshot_attempts,
    player_box_scores,
    players,
    source_documents,
)
from tests.integration.possession_harness import BASE_TIME, PossessionHarness


@dataclass
class LineupHarness:
    engine: AsyncEngine
    base: PossessionHarness = field(init=False)
    run_ids: list[UUID] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.base = PossessionHarness(self.engine, source=f"lineup-task10-{uuid4()}")

    @property
    def source(self) -> str:
        return self.base.source

    async def seed_game(
        self,
        *,
        game_id: str = "0022500111",
        status: str = "final",
        season_type: str = "regular_season",
        season: str = "2025-26",
    ) -> tuple[UUID, UUID]:
        pbp_id = await self.base.seed_game(
            game_id=game_id, status=status, season_type=season_type, season=season
        )
        box_id = uuid4()
        payload: JsonObject = {"game_id": game_id, "raw": "box-v1"}
        async with self.engine.begin() as connection:
            await connection.execute(
                source_documents.insert().values(
                    document_id=box_id,
                    source=self.source,
                    endpoint="boxscoretraditionalv3",
                    resource_key=f"box_score:{game_id}",
                    request_parameters={"game_id": game_id},
                    payload=payload,
                    checksum_sha256=canonical_json_checksum(payload),
                    requested_at=BASE_TIME,
                )
            )
            await connection.execute(
                ingestion_resources.insert().values(
                    source=self.source,
                    resource_type="box_score",
                    resource_key=f"box_score:{game_id}",
                    status="succeeded",
                    applied_document_id=box_id,
                )
            )
            # Reverse insertion order exercises deterministic player ordering.
            for player_id, team_id in ((930002, 910002), (930001, 910001)):
                await connection.execute(
                    insert(players)
                    .values(
                        player_id=player_id,
                        first_name="Raw",
                        family_name="Player",
                        full_name="Mutable global name",
                    )
                    .on_conflict_do_nothing()
                )
                await connection.execute(
                    player_box_scores.insert().values(
                        game_id=game_id,
                        player_id=player_id,
                        team_id=team_id,
                        position="G",
                        jersey_number="1",
                        starter=True,
                        played=True,
                        comment=None,
                    )
                )
        return box_id, pbp_id

    async def publish(self, claim: LineupBuildClaim, *, invalid: bool = False) -> None:
        """Seed a coherent published state without implementing Task 11 operations."""
        issues = (
            [
                {
                    "team_id": 910001,
                    "reason": "bad_lineup",
                    "sequence_number": 1,
                    "boundary_side": "before",
                }
            ]
            if invalid
            else []
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                update(lineup_snapshot_attempts)
                .where(lineup_snapshot_attempts.c.snapshot_id == claim.attempt.snapshot_id)
                .values(
                    state="published",
                    expected_row_count=1,
                    expected_output_sha256="a" * 64,
                    expected_issue_codes=issues,
                    result_status="invalid" if invalid else "valid",
                    insert_started_at=BASE_TIME,
                    verified_at=BASE_TIME,
                    published_at=BASE_TIME,
                )
            )
            await connection.execute(
                update(lineup_rebuild_resources)
                .where(lineup_rebuild_resources.c.game_id == claim.attempt.game_id.value)
                .values(
                    publication_state="invalid" if invalid else "current",
                    last_issue_codes=issues,
                    published_snapshot_id=claim.attempt.snapshot_id,
                    published_lineup_revision_id=claim.attempt.lineup_revision_id,
                    published_input_token_sha256=claim.attempt.input_token.sha256,
                    published_at=BASE_TIME,
                    active_run_id=None,
                    active_claim_version=None,
                    active_snapshot_id=None,
                    active_lineup_revision_id=None,
                    active_input_token_sha256=None,
                    claimed_until=None,
                )
            )

    async def cleanup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(ingestion_resources).where(
                    ingestion_resources.c.source == self.source + "-wrong"
                )
            )
            for table in (lineup_rebuild_resources, lineup_snapshot_attempts, player_box_scores):
                await connection.execute(
                    delete(table).where(table.c.game_id.in_(self.base.game_ids))
                )
            await connection.execute(
                delete(lineup_rebuild_runs).where(lineup_rebuild_runs.c.run_id.in_(self.run_ids))
            )
        await self.base.cleanup()
