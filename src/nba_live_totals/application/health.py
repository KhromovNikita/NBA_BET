import asyncio
import logging
from enum import StrEnum
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class DependencyState(StrEnum):
    UP = "up"
    DOWN = "down"


class ReadinessState(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class DependencyStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DependencyState
    latency_ms: float


class ReadinessStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ReadinessState
    checks: dict[str, DependencyStatus]


class DependencyChecker(Protocol):
    name: str

    async def check(self) -> None:
        raise NotImplementedError


class HealthService:
    def __init__(
        self,
        checkers: tuple[DependencyChecker, ...],
        timeout_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._checkers = checkers
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

    async def check_readiness(self) -> ReadinessStatus:
        statuses = await asyncio.gather(*(self._run(checker) for checker in self._checkers))
        checks = {
            checker.name: status for checker, status in zip(self._checkers, statuses, strict=True)
        }
        overall = (
            ReadinessState.READY
            if all(item.status is DependencyState.UP for item in statuses)
            else ReadinessState.NOT_READY
        )
        return ReadinessStatus(status=overall, checks=checks)

    async def _run(self, checker: DependencyChecker) -> DependencyStatus:
        started = perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await checker.check()
        except TimeoutError:
            status = DependencyState.DOWN
            self._log_failure(checker.name, "TimeoutError", started)
        except Exception as error:
            status = DependencyState.DOWN
            self._log_failure(checker.name, type(error).__name__, started)
        else:
            status = DependencyState.UP
        latency_ms = round((perf_counter() - started) * 1000, 1)
        return DependencyStatus(status=status, latency_ms=latency_ms)

    def _log_failure(self, dependency: str, error_type: str, started: float) -> None:
        self._logger.warning(
            "dependency_check_failed",
            extra={
                "dependency": dependency,
                "error_type": error_type,
                "latency_ms": round((perf_counter() - started) * 1000, 1),
            },
        )
