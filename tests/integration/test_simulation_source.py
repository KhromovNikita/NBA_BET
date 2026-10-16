"""Real source integrity failures must never publish a partial replacement."""

from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest
from sqlalchemy import event, update
from sqlalchemy.dialects.postgresql import insert

from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.simulation.models import SimulateTotalsResult
from nba_live_totals.application.simulation.service import SimulateTotalsService
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.simulation_export import AtomicSimulationExporter
from nba_live_totals.infrastructure.postgres.schema import team_box_scores, teams
from nba_live_totals.infrastructure.simulation.executor import OwnedSimulationExecutor
from nba_live_totals.infrastructure.simulation.numpy_engine import NumpySimulationEngine
from scripts.simulate_totals import parse_args
from tests.integration.baseline_harness import OwnershipJournal
from tests.integration.simulation_harness import GAMES, SEASON, simulation_acceptance

pytestmark = pytest.mark.integration


async def test_simulation_pointer_change_after_checked_prepare_preserves_old(
    tmp_path: Path,
) -> None:
    pytest.importorskip("catboost", reason="optional ML extra required")
    async with simulation_acceptance() as harness:
        await harness.seed_pipeline()
        artifacts = await harness.run_clis(tmp_path)
        path = tmp_path / "previous.jsonl"
        path.write_bytes(b"previous artifact")
        prepared = False

        class ObservedExporter(AtomicSimulationExporter):
            async def prepare(
                self, sources: tuple[ModelSourceReference, ...]
            ) -> SimulateTotalsResult:
                nonlocal prepared
                result = await super().prepare(sources)
                prepared = True
                return result

        original = harness.source.recheck

        async def raced(source: ModelSourceReference) -> None:
            if prepared:
                async with harness.engine.begin() as connection:
                    await connection.execute(
                        update(team_box_scores)
                        .where(team_box_scores.c.game_id == GAMES[0].value)
                        .values(points=99)
                    )
            await original(source)

        async def future_forbidden(source: ModelSourceReference) -> NoReturn:
            raise AssertionError("simulation must never request training targets")

        request = parse_args(
            [
                "--season",
                SEASON.value,
                "--model",
                str(artifacts.bundle_path),
                "--output",
                str(path),
                "--simulations",
                "8",
                "--batch-size",
                "4",
            ],
            environ={},
        )
        with (
            patch.object(harness.source, "recheck", raced),
            patch.object(harness.source, "read_training", future_forbidden),
        ):
            with pytest.raises(ModelingError):
                await SimulateTotalsService(
                    harness.source,
                    VerifiedModelBundleStore(),
                    OwnedSimulationExecutor(NumpySimulationEngine()),
                    ObservedExporter(),
                ).simulate(request)
        assert prepared and path.read_bytes() == b"previous artifact"


async def test_published_features_include_known_ot_without_future_targets() -> None:
    async with simulation_acceptance() as harness:
        sources = await harness.seed_pipeline()
        before = await harness.storage_projection()
        calls = tuple(harness.fixture.fetches)
        for source in sources:
            features = await harness.source.read_features(source)
            assert len(features.states) == source.reference.state_row_count
            await harness.source.recheck(source)
        overtime = await harness.source.read_features(sources[1])
        assert {s.period for s in overtime.states} >= {4, 5, 6}
        assert any(s.period == 5 and s.seconds_remaining_game_ms > 0 for s in overtime.states)
        assert (
            tuple(harness.fixture.fetches) == calls and await harness.storage_projection() == before
        )


async def test_ownership_journal_refuses_existing_identity_without_claiming_or_deleting_it() -> (
    None
):
    async with simulation_acceptance() as harness:
        await harness.seed_pipeline()
        before = await harness.storage_projection()
        other = OwnershipJournal()
        event.listen(harness.engine.sync_engine, "before_cursor_execute", other.before_insert)
        try:
            with pytest.raises(AssertionError, match="unowned fixture collision"):
                async with harness.engine.begin() as connection:
                    await connection.execute(
                        insert(teams)
                        .values(
                            team_id=91395001,
                            city="collision",
                            name="collision",
                            tricode="COL",
                            slug="collision",
                        )
                        .on_conflict_do_nothing()
                    )
        finally:
            event.remove(harness.engine.sync_engine, "before_cursor_execute", other.before_insert)
        assert not any(other.rows.values())
        await other.cleanup(harness.engine, harness.client)
        assert await harness.storage_projection() == before
