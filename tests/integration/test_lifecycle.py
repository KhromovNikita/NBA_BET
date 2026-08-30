import pytest
from pydantic import AnyUrl, HttpUrl, RedisDsn, SecretStr

from nba_live_totals.application.health import ReadinessState
from nba_live_totals.lifecycle import close_resources, create_resources
from nba_live_totals.settings import Settings

pytestmark = pytest.mark.integration


async def test_resources_wire_real_dependencies_and_close_cleanly() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=AnyUrl("postgresql+asyncpg://nba:nba@localhost:5432/nba"),
        clickhouse_url=HttpUrl("http://localhost:8123"),
        clickhouse_user="nba",
        clickhouse_password=SecretStr("nba"),
        redis_url=RedisDsn("redis://localhost:6379/0"),
    )
    resources = create_resources(settings)

    try:
        result = await resources.health_service.check_readiness()
    finally:
        await close_resources(resources)

    assert result.status is ReadinessState.READY
    assert resources.clickhouse_client.is_closed
