import re
from contextvars import ContextVar, Token
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def normalize_request_id(value: str | None) -> str:
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def get_request_id() -> str | None:
    return _request_id.get()


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope["headers"]}
        raw_request_id = headers.get(b"x-request-id")
        request_id = normalize_request_id(_decode_request_id(raw_request_id))
        token: Token[str | None] = _request_id.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("ascii"))
                )
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)


def _decode_request_id(value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None
