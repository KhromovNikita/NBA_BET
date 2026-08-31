import pytest
from pydantic import AnyUrl, HttpUrl, RedisDsn, SecretStr

from nba_live_totals.application.health import ReadinessState
from nba_live_totals.lifecycle import close_resources, create_resources
from nba_live_totals.settings import Settings
from tests import runtime_config

pytestmark = pytest.mark.integration


async def test_resources_wire_real_dependencies_and_close_cleanly() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=AnyUrl(runtime_config.postgres_url()),
        clickhouse_url=HttpUrl(runtime_config.clickhouse_url()),
        clickhouse_user="nba",
        clickhouse_password=SecretStr("nba"),
        redis_url=RedisDsn(runtime_config.redis_url()),
    )
    resources = await create_resources(settings)

    try:
        result = await resources.health_service.check_readiness()
    finally:
        await close_resources(resources)

    assert result.status is ReadinessState.READY
    assert resources.clickhouse_client.is_closed
