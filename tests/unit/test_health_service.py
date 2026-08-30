import asyncio

from nba_live_totals.application.health import (
    DependencyState,
    HealthService,
    ReadinessState,
)


class SuccessfulChecker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self) -> None:
        await asyncio.sleep(0)


class FailingChecker:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self._message = message

    async def check(self) -> None:
        raise RuntimeError(self._message)


async def test_readiness_is_ready_when_all_dependencies_are_up() -> None:
    service = HealthService(
        checkers=(SuccessfulChecker("postgres"), SuccessfulChecker("redis")),
        timeout_seconds=0.1,
    )

    result = await service.check_readiness()

    assert result.status is ReadinessState.READY
    assert list(result.checks) == ["postgres", "redis"]
    assert result.checks["postgres"].status is DependencyState.UP
    assert result.checks["redis"].status is DependencyState.UP


async def test_readiness_collects_all_results_without_exposing_errors() -> None:
    service = HealthService(
        checkers=(
            FailingChecker("postgres", "postgresql://user:secret@host/db"),
            SuccessfulChecker("redis"),
        ),
        timeout_seconds=0.1,
    )

    result = await service.check_readiness()
    payload = result.model_dump(mode="json")

    assert result.status is ReadinessState.NOT_READY
    assert result.checks["postgres"].status is DependencyState.DOWN
    assert result.checks["redis"].status is DependencyState.UP
    assert "secret" not in str(payload)
    assert "error" not in payload["checks"]["postgres"]


class BlockingChecker:
    def __init__(self, name: str, started: set[str], release: asyncio.Event) -> None:
        self.name = name
        self._started = started
        self._release = release

    async def check(self) -> None:
        self._started.add(self.name)
        if len(self._started) == 3:
            self._release.set()
        await self._release.wait()


class NeverCompletesChecker:
    name = "clickhouse"

    async def check(self) -> None:
        await asyncio.Event().wait()


async def test_checks_start_concurrently() -> None:
    started: set[str] = set()
    release = asyncio.Event()
    service = HealthService(
        checkers=tuple(
            BlockingChecker(name, started, release) for name in ("postgres", "clickhouse", "redis")
        ),
        timeout_seconds=0.2,
    )

    result = await service.check_readiness()

    assert started == {"postgres", "clickhouse", "redis"}
    assert result.status is ReadinessState.READY


async def test_timeout_marks_only_that_dependency_down() -> None:
    service = HealthService(
        checkers=(NeverCompletesChecker(), SuccessfulChecker("redis")),
        timeout_seconds=0.01,
    )

    result = await service.check_readiness()

    assert result.status is ReadinessState.NOT_READY
    assert result.checks["clickhouse"].status is DependencyState.DOWN
    assert result.checks["redis"].status is DependencyState.UP
