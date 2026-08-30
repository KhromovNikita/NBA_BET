import logging
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nba_live_totals.api.request_id import RequestIdMiddleware, get_request_id
from nba_live_totals.api.routes.health import router as health_router
from nba_live_totals.lifecycle import create_lifespan
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved)
    app = FastAPI(
        title="NBA Live Totals",
        version="0.1.0",
        lifespan=create_lifespan(resolved),
    )
    app.state.settings = resolved
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, error: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception(
            "unhandled_request_error",
            extra={"error_type": type(error).__name__},
        )
        request_id = get_request_id() or cast(str | None, request.scope["state"].get("request_id"))
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_server_error", "request_id": request_id},
            headers={"X-Request-ID": request_id} if request_id is not None else {},
        )

    return app
