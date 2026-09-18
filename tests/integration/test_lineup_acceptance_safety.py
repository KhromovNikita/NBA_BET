"""Exercise acceptance teardown with real children and owned database sentinels."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

from nba_live_totals.infrastructure.clickhouse.client import ClickHouseClient
from nba_live_totals.infrastructure.postgres.schema import lineup_rebuild_runs, seasons
from tests import runtime_config
from tests.e2e.test_lineup_reconstruction import (
    GAME_ID,
    SEASON,
    LineupAcceptance,
    acceptance_context,
)

pytestmark = pytest.mark.integration

CHILD = """
import asyncio
import os
import sys
from uuid import UUID
from sqlalchemy.ext.asyncio import create_async_engine
from nba_live_totals.infrastructure.postgres.schema import lineup_rebuild_runs

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as connection:
        await connection.execute(lineup_rebuild_runs.insert().values(
            run_id=UUID(sys.argv[1]),
            selectors={'seasons': ['2098-99'], 'game_id': '0029899002'},
            mode='materialize', forced_rebuild=True, status='running',
        ))
    await engine.dispose()
    print('ready', flush=True)
    if sys.argv[2] == 'timeout':
        await asyncio.sleep(3600)
    sys.exit(3)

asyncio.run(main())
"""


@pytest.fixture
async def safety_harness() -> AsyncIterator[LineupAcceptance]:
    engine = create_async_engine(runtime_config.postgres_url())
    client = ClickHouseClient(
        base_url=runtime_config.clickhouse_url(), user="nba", password="nba", timeout_seconds=30
    )
    harness = LineupAcceptance(engine, client)
    try:
        await harness.assert_unowned_ids_absent()
        try:
            yield harness
        finally:
            await harness.cleanup()
    finally:
        await client.aclose()
        await engine.dispose()


async def insert_run(harness: LineupAcceptance, run_id: UUID, game_id: str) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(
            lineup_rebuild_runs.insert().values(
                run_id=run_id,
                selectors={"seasons": [SEASON.value], "game_id": game_id},
                mode="materialize",
                forced_rebuild=False,
                status="running",
            )
        )


async def read_run(harness: LineupAcceptance, run_id: UUID) -> dict[str, object] | None:
    async with harness.engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(lineup_rebuild_runs).where(lineup_rebuild_runs.c.run_id == run_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


@pytest.mark.parametrize("failure", ["timeout", "early_exit"])
async def test_cli_failure_reaps_child_and_tracks_run_without_summary(
    safety_harness: LineupAcceptance,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    h = safety_harness
    owned_id, unrelated_id = uuid4(), uuid4()
    await insert_run(h, unrelated_id, "0029899003")
    unrelated = await read_run(h, unrelated_id)
    children: list[asyncio.subprocess.Process] = []
    launch = asyncio.create_subprocess_exec

    async def launch_ready(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        child = await launch(*args, **kwargs)
        children.append(child)
        assert child.stdout is not None
        # Begin the tested timeout only after the child has committed its run.
        assert (await asyncio.wait_for(child.stdout.readline(), timeout=15)).strip() == b"ready"
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch_ready)
    try:
        if failure == "timeout":
            with pytest.raises(TimeoutError):
                await h.run_cli("-c", CHILD, str(owned_id), failure, timeout_seconds=0.1)
        else:
            code, output, _ = await h.run_cli("-c", CHILD, str(owned_id), failure)
            assert code == 3 and "run=" not in output
        assert children[0].returncode is not None, "child still running before database teardown"
        assert owned_id in h.run_ids, "started run was lost without a CLI summary"
        assert unrelated_id not in h.run_ids
        await h.cleanup()
        assert await read_run(h, owned_id) is None
        assert await read_run(h, unrelated_id) == unrelated
    finally:
        # Also make the RED run safe when the implementation has not reaped its child.
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()
        async with h.engine.begin() as connection:
            await connection.execute(
                delete(lineup_rebuild_runs).where(
                    lineup_rebuild_runs.c.run_id.in_((owned_id, unrelated_id))
                )
            )


async def test_cli_refuses_preexisting_untracked_run_without_adopting_it(
    safety_harness: LineupAcceptance,
) -> None:
    h = safety_harness
    unowned_id = uuid4()
    await insert_run(h, unowned_id, GAME_ID.value)
    baseline = await read_run(h, unowned_id)
    try:
        with pytest.raises(AssertionError, match="unowned lineup run"):
            await h.run_cli("-c", "pass")
        assert unowned_id not in h.run_ids
        assert await read_run(h, unowned_id) == baseline
    finally:
        async with h.engine.begin() as connection:
            await connection.execute(
                delete(lineup_rebuild_runs).where(lineup_rebuild_runs.c.run_id == unowned_id)
            )


async def test_acceptance_setup_refuses_playoffs_season_and_preserves_it(
    safety_harness: LineupAcceptance,
) -> None:
    h = safety_harness
    predicate = (seasons.c.season == SEASON.value) & (seasons.c.season_type == "playoffs")
    async with h.engine.begin() as connection:
        await connection.execute(
            seasons.insert().values(
                season=SEASON.value,
                season_type="playoffs",
                season_start_year=2098,
            )
        )
        baseline = dict(
            (await connection.execute(select(seasons).where(predicate))).mappings().one()
        )
    try:
        try:
            with pytest.raises(AssertionError, match="fixture collision in seasons"):
                async with acceptance_context():
                    pytest.fail("acceptance setup adopted a pre-existing season")
        finally:
            async with h.engine.connect() as connection:
                rows = (await connection.execute(select(seasons).where(predicate))).mappings().all()
                assert [dict(row) for row in rows] == [baseline]
    finally:
        async with h.engine.begin() as connection:
            await connection.execute(delete(seasons).where(predicate))
