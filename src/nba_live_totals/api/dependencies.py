from fastapi import Request

from nba_live_totals.application.health import HealthService
from nba_live_totals.lifecycle import AppResources


def get_health_service(request: Request) -> HealthService:
    resources: AppResources = request.app.state.resources
    return resources.health_service
