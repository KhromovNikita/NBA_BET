import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests import runtime_config

pytestmark = pytest.mark.integration


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


async def test_games_reject_the_same_home_and_away_team() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(
                    """
                    INSERT INTO seasons (season, season_type, season_start_year)
                    VALUES ('2025-26', 'regular_season', 2025)
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO teams (team_id, city, name, tricode, slug)
                    VALUES (1, 'Boston', 'Celtics', 'BOS', 'celtics')
                    """
                )
            )

            with pytest.raises(IntegrityError, match="ck_games_distinct_teams"):
                await connection.execute(
                    text(
                        """
                        INSERT INTO games (
                            game_id, season, season_type, game_date, started_at,
                            home_team_id, away_team_id, status
                        ) VALUES (
                            '0022500123', '2025-26', 'regular_season', '2025-10-22',
                            '2025-10-22T00:00:00+00:00', 1, 1, 'scheduled'
                        )
                        """
                    )
                )
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_play_by_play_accepts_a_person_absent_from_players() -> None:
    engine = create_async_engine(runtime_config.postgres_url())
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(
                    """
                    INSERT INTO seasons (season, season_type, season_start_year)
                    VALUES ('2025-26', 'regular_season', 2025)
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO teams (team_id, city, name, tricode, slug)
                    VALUES
                        (1, 'Boston', 'Celtics', 'BOS', 'celtics'),
                        (2, 'New York', 'Knicks', 'NYK', 'knicks')
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO games (
                        game_id, season, season_type, game_date, started_at,
                        home_team_id, away_team_id, home_score, away_score, status
                    ) VALUES (
                        '0022500123', '2025-26', 'regular_season', '2025-10-22',
                        '2025-10-22T00:00:00+00:00', 1, 2, 110, 105, 'final'
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO play_by_play_events (
                        game_id, sequence_number, action_number, period, clock,
                        seconds_remaining_period_ms,
                        team_id, person_id, action_type, sub_type, description,
                        is_field_goal, points_total, video_available
                    ) VALUES (
                        '0022500123', 1, 1, 1, 'PT11M59.00S', 719000,
                        1, 999999, 'jumpball', '', 'Official tosses the ball',
                        false, 0, false
                    )
                    """
                )
            )

            stored_person_id = await connection.scalar(
                text(
                    """
                    SELECT person_id FROM play_by_play_events
                    WHERE game_id = '0022500123' AND sequence_number = 1
                    """
                )
            )
            assert stored_person_id == 999999
        finally:
            await transaction.rollback()
    await engine.dispose()
