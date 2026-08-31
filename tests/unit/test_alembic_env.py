import runpy
from pathlib import Path
from types import TracebackType

import pytest
from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

from alembic import context

PROJECT_ROOT = Path(__file__).parents[2]


class FakeAlembicConfig:
    config_file_name = None
    config_ini_section = "alembic"

    def get_section(self, name: str, default: dict[str, str]) -> dict[str, str]:
        return default


class FailingConnectionContext:
    async def __aenter__(self) -> None:
        raise RuntimeError("connection failed")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeAsyncEngine:
    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> FailingConnectionContext:
        return FailingConnectionContext()

    async def dispose(self) -> None:
        self.disposed = True


def test_online_migration_disposes_engine_when_connection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeAsyncEngine()
    monkeypatch.setattr(context, "config", FakeAlembicConfig(), raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(
        sqlalchemy_asyncio, "async_engine_from_config", lambda *args, **kwargs: engine
    )

    with pytest.raises(RuntimeError, match="connection failed"):
        runpy.run_path(str(PROJECT_ROOT / "alembic" / "env.py"))

    assert engine.disposed
