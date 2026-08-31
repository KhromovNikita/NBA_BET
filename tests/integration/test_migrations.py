import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_alembic_upgrade_head_is_idempotent() -> None:
    environment = os.environ | {
        "DATABASE_URL": "postgresql+asyncpg://nba:nba@localhost:5432/nba",
    }

    first = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    second = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
