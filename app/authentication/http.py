"""Bearer transport and protection before request bodies are consumed."""

from fastapi import FastAPI, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.accounts import Account
from app.authentication.errors import (
    AuthenticationError,
    AuthenticationRequired,
    AuthenticationUnavailable,
    InvalidCredentials,
)
from app.authentication.service import AuthenticationService
from app.request_paths import route_path
from app.schemas import error_body


async def authenticate_request(
    request: Request, service: AuthenticationService | None
) -> Account:
    """Accept one Authorization header; never query strings, cookies or account IDs."""
    existing = getattr(request.state, "authenticated_account", None)
    if isinstance(existing, Account):
        return existing
    values = Headers(scope=request.scope).getlist("authorization")
    if not values:
        raise AuthenticationRequired()
    if len(values) != 1:
        raise InvalidCredentials()
    scheme, separator, token = values[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise InvalidCredentials()
    if service is None:
        raise AuthenticationUnavailable()
    account = await service.authenticate(token)
    request.state.authenticated_account = account
    return account


class AuthenticationMiddleware:
    """Protect configured route trees before upload buffering and multipart parsing."""

    def __init__(
        self,
        app: ASGIApp,
        service: AuthenticationService | None,
        protected_prefixes: tuple[str, ...],
    ) -> None:
        self._app = app
        self._service = service
        self._prefixes = protected_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._protected(route_path(scope)):
            await self._app(scope, receive, send)
            return
        try:
            await authenticate_request(Request(scope), self._service)
        except AuthenticationError as exc:
            await authentication_error_response(exc)(scope, receive, send)
            return

        async def private_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers.append("Vary", "Authorization")
            await send(message)

        await self._app(scope, receive, private_response)

    def _protected(self, path: str) -> bool:
        return any(
            path == prefix or path.startswith(prefix + "/") for prefix in self._prefixes
        )


def authentication_error_response(exc: AuthenticationError) -> JSONResponse:
    """Use the same safe error contract in middleware and route dependencies."""
    headers = {"Cache-Control": "no-store", "Vary": "Authorization"}
    if exc.challenge is not None:
        headers["WWW-Authenticate"] = exc.challenge
    if exc.status_code == 503:
        headers["Retry-After"] = "30"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message),
        headers=headers,
    )


def install_authentication_error_handler(application: FastAPI) -> None:
    application.add_exception_handler(AuthenticationError, _handle_authentication_error)


def _handle_authentication_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AuthenticationError):
        raise exc
    return authentication_error_response(exc)
