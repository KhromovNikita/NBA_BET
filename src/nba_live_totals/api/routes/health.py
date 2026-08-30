from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from nba_live_totals.api.dependencies import get_health_service
from nba_live_totals.application.health import (
    HealthService,
    ReadinessState,
    ReadinessStatus,
)

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=ReadinessStatus)
async def readiness(
    service: Annotated[HealthService, Depends(get_health_service)],
) -> JSONResponse:
    result = await service.check_readiness()
    status_code = 200 if result.status is ReadinessState.READY else 503
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))
