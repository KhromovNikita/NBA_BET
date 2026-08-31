import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nba_live_totals.api.request_id import RequestIdMiddleware, get_request_id
from nba_live_totals.api.routes.health import router as health_router
from nba_live_totals.lifecycle import create_lifespan
from nba_live_totals.observability.logging import configure_logging
from nba_live_totals.settings import Settings


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False
        response_completed = False

        async def track_response(message: Message) -> None:
            nonlocal response_started, response_completed
            await send(message)
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_completed = True

        try:
            await self._app(scope, receive, track_response)
        except Exception as error:
            request_id = get_request_id()
            logging.getLogger(__name__).exception(
                "unhandled_request_error",
                extra={"error_type": type(error).__name__, "request_id": request_id},
            )
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "internal_server_error", "request_id": request_id},
                )
                try:
                    await response(scope, receive, send)
                except Exception:
                    return
            elif not response_completed:
                try:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except Exception:
                    return


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    configure_logging(resolved)
    app = FastAPI(
        title="NBA Live Totals",
        version="0.1.0",
        lifespan=create_lifespan(resolved),
    )
    app.state.settings = resolved
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)

    return app
