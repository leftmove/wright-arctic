"""Reject oversized image uploads before the multipart parser stores them."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.schemas import error_body
from app.services.images.errors import ImageTooLarge


class ImageUploadLimitMiddleware:
    """Stop POST /images once the body exceeds the upload budget.

    Starlette's multipart part limit does not apply to file fields. The body
    is read here, up to the budget, and replayed to the application. A limit
    raised from inside the parser becomes a generic body-parse error.
    Content-Length is only an early reject; a missing or understated length
    cannot skip the counter.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not _is_image_create(scope)
        ):
            await self._app(scope, receive, send)
            return
        lengths = _content_lengths(scope)
        if any(length > self._max_body_bytes for length in lengths):
            await _too_large()(scope, receive, send)
            return
        try:
            payload = await _read_body(receive, self._max_body_bytes)
        except ImageTooLarge:
            await _too_large()(scope, receive, send)
            return
        except _ClientDisconnected:
            return

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self._app(scope, replay, send)


class _ClientDisconnected(Exception):
    pass


def _is_image_create(scope: Scope) -> bool:
    path = scope.get("path")
    return isinstance(path, str) and path.rstrip("/") == "/images"


def _content_lengths(scope: Scope) -> list[int]:
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return []
    lengths: list[int] = []
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        name, value = item
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            continue
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed >= 0:
            lengths.append(parsed)
    return lengths


async def _read_body(receive: Receive, max_body_bytes: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientDisconnected()
        if message["type"] != "http.request":
            continue
        body = message.get("body", b"")
        if not isinstance(body, bytes) or received + len(body) > max_body_bytes:
            raise ImageTooLarge()
        received += len(body)
        if body:
            chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _too_large() -> JSONResponse:
    error = ImageTooLarge()
    return JSONResponse(
        status_code=error.status_code, content=error_body(error.code, error.message)
    )
