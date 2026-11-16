"""Cloudflare adapters; clients and Hyperdrive sockets belong to each invocation."""

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from functools import lru_cache
from http.client import HTTPMessage
from typing import Protocol, cast
from urllib.parse import unquote, urlsplit

from pydantic_settings import SettingsConfigDict
from starlette.requests import Request as ASGIRequest
from starlette.types import Receive, Scope, Send
from workers import Request, Response, WorkerEntrypoint, asgi

from app.authentication import AuthenticationError, build_authentication_service
from app.authentication.http import authenticate_request, authentication_error_response
from app.authentication.jwks import SigningKeys
from app.authentication.models import SigningAlgorithm
from app.execution import InlineExecutor
from app.main import create_app
from app.request_paths import protected_path
from app.services.async_database import RequestPostgresDatabase, Wait
from app.services.limits import MAX_CONTROL_BODY_BYTES
from app.services.storage.factory import Storage, signer
from app.settings import Settings
from app.state import AppResources, legacy_resources
from app.worker_crypto import load_web_key
from app.worker_storage import Buffer, R2Bucket, R2Objects


class HyperdriveBinding(Protocol):
    connectionString: str


class WorkerEnvironment(Protocol):
    HYPERDRIVE: HyperdriveBinding
    DOCUMENTS: R2Bucket
    IMAGES: R2Bucket


class BindingSettings(Settings):
    model_config = SettingsConfigDict(env_file=None)


_settings: Settings | None = None


def binding_settings(environment: object) -> Settings:
    global _settings
    if _settings is not None:
        return _settings
    values: dict[str, str] = {}
    for name in Settings.model_fields:
        if name in {
            "documents_setup_url",
            "documents_database_url",
            "documents_listener_url",
        }:
            continue
        value: object = getattr(environment, "WRIGHT_ARCTIC_" + name.upper(), None)
        if isinstance(value, str):
            values[name] = value
    # Scalar bindings belong to this immutable Worker version. Request-owned
    # sockets and R2 bindings stay outside the settings cache.
    _settings = BindingSettings.model_validate(values)
    return _settings


@lru_cache(maxsize=1)
def signing_keys(url: str, algorithm: SigningAlgorithm) -> SigningKeys:
    return SigningKeys(None, url, algorithm, loader=load_web_key)


def storage_bindings(
    environment: WorkerEnvironment, settings: Settings
) -> Storage | None:
    documents = signer(settings, "documents")
    images = signer(settings, "images")
    if documents is None or images is None:
        return None
    return Storage(
        R2Objects(environment.DOCUMENTS),
        documents,
        R2Objects(environment.IMAGES),
        images,
    )


def resources_for(
    environment: WorkerEnvironment,
    settings: Settings,
    *,
    authenticate: bool,
    persistence: bool = True,
) -> AppResources:
    database = (
        RequestPostgresDatabase(
            environment.HYPERDRIVE.connectionString,
            cast(Wait, importlib.import_module("pyodide.ffi").run_sync),
        )
        if persistence
        else None
    )
    return legacy_resources(
        settings,
        InlineExecutor(),
        database=database,
        storage=storage_bindings(environment, settings) if persistence else None,
        authentication=build_authentication_service(
            settings,
            keys=signing_keys(str(settings.auth_jwks_url), settings.auth_algorithm),
        )
        if authenticate
        else None,
    )


class ResponseFactory(Protocol):
    def __call__(
        self,
        body: object = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response: ...


class RequestFactory(Protocol):
    def __call__(self, request: Request, *, body: bytes) -> Request: ...


_response = cast(ResponseFactory, Response)
_request = cast(RequestFactory, Request)
# Route/model construction is captured in Cloudflare's deployment snapshot.
_shell_settings = BindingSettings()
_application = create_app(
    legacy_resources(
        _shell_settings,
        InlineExecutor(),
        database=None,
        storage=None,
        authentication=build_authentication_service(_shell_settings),
    )
)
_application.middleware_stack = _application.build_middleware_stack()
# JSPI synchronous sockets are serialized as Cloudflare recommends for Python.
_requests = asyncio.Lock()


class Default(WorkerEntrypoint):
    async def fetch(self, request: Request) -> Response:
        async with _requests:
            environment = cast(WorkerEnvironment, self.env)
            settings = binding_settings(environment)
            path = unquote(urlsplit(request.url).path)
            protected = protected_path(path)
            headers = cast(HTTPMessage, request.headers)
            resources = resources_for(
                environment,
                settings,
                authenticate=protected and bool(headers.get("Authorization")),
                persistence=(protected and bool(headers.get("Authorization")))
                or path == "/ready"
                or path.startswith("/avatars/"),
            )
            state: dict[str, object] = {"resources": resources}

            async def application(scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] == "lifespan":
                    return
                scope["state"] = state
                await _application(scope, receive, send)

            try:
                # The SDK buffers the incoming body before ASGI. Authenticate
                # and enforce the control-plane limit before handing it over.
                if protected:
                    scope: Scope = {
                        "type": "http",
                        "method": request.method,
                        "path": path,
                        "headers": [
                            (key.lower().encode(), value.encode())
                            for key, value in headers.items()
                        ],
                        "state": state,
                    }
                    try:
                        await authenticate_request(
                            ASGIRequest(scope), resources.authentication
                        )
                    except AuthenticationError as error:
                        response = authentication_error_response(error)
                        return _response(
                            bytes(response.body),
                            status=response.status_code,
                            headers=dict(response.headers),
                        )
                if request.body:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in cast(AsyncIterator[Buffer], request.body):
                        data = chunk.to_bytes()
                        size += len(data)
                        if size > MAX_CONTROL_BODY_BYTES:
                            return _response(
                                json.dumps(
                                    {
                                        "code": "request_too_large",
                                        "message": "Control requests are limited to 128000 bytes.",
                                    }
                                ),
                                status=413,
                                headers={
                                    "content-type": "application/json",
                                    "cache-control": "no-store",
                                },
                            )
                        chunks.append(data)
                    request = _request(request, body=b"".join(chunks))
                return Response(await asgi.fetch(application, request, self.env))
            finally:
                await resources.close()

    async def scheduled(
        self, controller: object, environment: object, context: object
    ) -> None:
        async with _requests:
            bindings = cast(WorkerEnvironment, self.env)
            resources = resources_for(
                bindings, binding_settings(bindings), authenticate=False
            )
            try:
                if resources.cleanup is None:
                    raise RuntimeError("Cleanup requires both private R2 buckets")
                result = await resources.cleanup.run()
                if result.failures:
                    raise RuntimeError(
                        f"Cleanup left {result.failures} object deletes pending"
                    )
            finally:
                await resources.close()
