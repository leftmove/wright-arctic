"""Bound control-plane JSON bodies before parsing, including chunked requests."""

import asyncio
from contextlib import suppress

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import Operation, current_operation, report
from app.services.limits import MAX_CONTROL_BODY_BYTES


class OperationMiddleware:
    """Admission has no waiting queue; timeout cancels the entire ASGI operation."""

    def __init__(self, app: ASGIApp, *, deadline: float, limit: int) -> None:
        self._app, self._deadline, self._limit = app, deadline, limit
        self._active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self._app(scope, receive, send)
            return
        if self._active >= self._limit:
            await JSONResponse(
                {"code": "overloaded", "message": "Retry this operation."},
                status_code=503,
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        self._active += 1
        operation = Operation()
        token = current_operation.set(operation)
        started = False
        status = 500

        async def sending(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
            await send(message)

        try:
            async with asyncio.timeout(self._deadline):
                await self._app(scope, receive, sending)
        except TimeoutError:
            status = 504
            if not started:
                await JSONResponse(
                    {
                        "code": "deadline_exceeded",
                        "message": "Retry using the same operation identifiers.",
                    },
                    status_code=504,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            else:
                # Terminate an incomplete image stream rather than emitting a
                # second response. StreamingResponse has already closed R2.
                raise
        finally:
            self._active -= 1
            route = getattr(scope.get("route"), "path", "unmatched")
            report(operation, route=route, status=status)
            current_operation.reset(token)


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
