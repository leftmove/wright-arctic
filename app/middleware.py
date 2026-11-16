"""Bound control-plane JSON bodies before parsing, including chunked requests."""

from contextlib import suppress

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.limits import MAX_CONTROL_BODY_BYTES


class ControlBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self._app(scope, receive, send)
            return
        chunks: list[bytes] = []
        received = 0
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                with suppress(ValueError):
                    received = max(received, int(value))
        if received <= MAX_CONTROL_BODY_BYTES:
            received = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > MAX_CONTROL_BODY_BYTES:
                    break
                chunks.append(chunk)
                if not message.get("more_body", False):
                    payload = b"".join(chunks)
                    delivered = False

                    async def replay(body: bytes = payload) -> Message:
                        nonlocal delivered
                        if delivered:
                            return await receive()
                        delivered = True
                        return {
                            "type": "http.request",
                            "body": body,
                            "more_body": False,
                        }

                    await self._app(scope, replay, send)
                    return
        await JSONResponse(
            {
                "code": "request_too_large",
                "message": "Control requests are limited to 128000 bytes.",
            },
            status_code=413,
        )(scope, receive, send)
