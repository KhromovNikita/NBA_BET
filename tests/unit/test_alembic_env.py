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
    def __init__(self, dispose_error: Exception | None = None) -> None:
        self.disposed = False
        self.dispose_error = dispose_error

    def connect(self) -> FailingConnectionContext:
        return FailingConnectionContext()

    async def dispose(self) -> None:
        self.disposed = True
        if self.dispose_error is not None:
            raise self.dispose_error


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


def test_online_migration_preserves_connection_error_when_dispose_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeAsyncEngine(dispose_error=RuntimeError("dispose failed"))
    monkeypatch.setattr(context, "config", FakeAlembicConfig(), raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(
        sqlalchemy_asyncio, "async_engine_from_config", lambda *args, **kwargs: engine
    )

    with pytest.raises(RuntimeError, match="connection failed"):
        runpy.run_path(str(PROJECT_ROOT / "alembic" / "env.py"))

    assert engine.disposed
