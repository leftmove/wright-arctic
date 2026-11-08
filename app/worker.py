"""Worker bindings and request lifetimes at the boundary of the FastAPI app."""

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
from app.services.documents.async_database import RequestPostgresDatabase, Wait
from app.services.documents.service import DocumentService
from app.services.images.processing import initialize_processing
from app.services.images.service import ImageService
from app.services.images.signing import S3UrlSigner
from app.services.images.storage import ImageStore
from app.settings import Settings
from app.state import AppResources
from app.worker_crypto import load_web_key
from app.worker_storage import Buffer, R2Bucket, R2ObjectStorage


class HyperdriveBinding(Protocol):
    connectionString: str


class WorkerEnvironment(Protocol):
    HYPERDRIVE: HyperdriveBinding
    IMAGES: R2Bucket


class BindingSettings(Settings):
    """Worker settings come from bindings, never a bundled dotenv file."""

    model_config = SettingsConfigDict(env_file=None)


def binding_settings(environment: object) -> Settings:
    values: list[tuple[str, str]] = []
    for name in Settings.model_fields:
        if name in {"documents_setup_url", "documents_database_url"}:
            continue
        value: object = getattr(environment, "WRIGHT_ARCTIC_" + name.upper(), None)
        if isinstance(value, str):
            values.append((name, value))
    return _binding_settings(tuple(values))


@lru_cache(maxsize=1)
def _binding_settings(values: tuple[tuple[str, str], ...]) -> Settings:
    """Reuse immutable configuration while bindings are unchanged.

    Request-owned clients stay outside this cache. Keying by binding values
    also observes secret/config changes without depending on isolate lifetime.
    """
    return BindingSettings.model_validate(dict(values))


# Route/model construction is captured in Cloudflare's deployment-time snapshot.
# Live clients and Hyperdrive connections are created only inside fetch().
_application = create_app(BindingSettings())
_application.middleware_stack = _application.build_middleware_stack()
_executor = InlineExecutor()
# Cloudflare recommends serializing synchronous socket operations in Python.
_requests = asyncio.Lock()
# Capture codec setup in the deployment snapshot, outside request CPU budgets.
initialize_processing()


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


@lru_cache(maxsize=1)
def signing_keys(url: str, algorithm: SigningAlgorithm) -> SigningKeys:
    """Cache only public key material; HTTP clients remain owned by requests."""
    return SigningKeys(None, url, algorithm, loader=load_web_key)


def image_service(
    environment: WorkerEnvironment, settings: Settings
) -> ImageService | None:
    if (
        settings.storage_endpoint_url is None
        or settings.storage_bucket is None
        or settings.storage_region is None
        or settings.storage_access_key_id is None
        or settings.storage_secret_access_key is None
    ):
        return None
    signer = S3UrlSigner(
        str(settings.storage_endpoint_url),
        settings.storage_bucket,
        settings.storage_region,
        settings.storage_access_key_id.get_secret_value(),
        settings.storage_secret_access_key.get_secret_value(),
    )
    public = settings.storage_public_base_url
    return ImageService(
        ImageStore(
            R2ObjectStorage(environment.IMAGES, signer),
            str(public).rstrip("/") if public else None,
            settings.storage_presign_seconds,
        )
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request: Request) -> Response:
        async with _requests:
            environment = cast(WorkerEnvironment, self.env)
            settings = binding_settings(environment)
            path = unquote(urlsplit(request.url).path)
            protected = any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in ("/documents", "/images", "/account")
            )
            headers = cast(HTTPMessage, request.headers) if protected else None
            documents = (
                path == "/ready"
                or path == "/documents"
                or path.startswith("/documents/")
            )
            images = path == "/ready" or any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in ("/images", "/account")
            )
            resources = AppResources(
                authentication=build_authentication_service(
                    settings,
                    keys=signing_keys(
                        str(settings.auth_jwks_url), settings.auth_algorithm
                    ),
                )
                if headers is not None and headers.get("Authorization")
                else None,
                image_service=image_service(environment, settings) if images else None,
                document_service=DocumentService(
                    RequestPostgresDatabase(
                        environment.HYPERDRIVE.connectionString,
                        cast(Wait, importlib.import_module("pyodide.ffi").run_sync),
                    )
                )
                if documents
                else None,
                settings=settings,
                executor=_executor,
            )

            state: dict[str, object] = {"resources": resources}

            async def application(scope: Scope, receive: Receive, send: Send) -> None:
                # The Worker owns resource startup/cleanup. The ASGI factory's
                # process lifespan is for native hosts, not individual requests.
                if scope["type"] == "lifespan":
                    return
                scope["state"] = state
                await _application(scope, receive, send)

            try:
                # The SDK buffers request bodies before entering ASGI. Authenticate
                # first and bound that buffer as well as the application's parser.
                if protected:
                    assert headers is not None
                    scope: Scope = {
                        "type": "http",
                        "method": request.method,
                        "path": path,
                        "headers": [
                            (k.lower().encode(), v.encode()) for k, v in headers.items()
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
                        value = chunk.to_bytes()
                        size += len(value)
                        if size > 2 * 1024 * 1024:
                            return _response(
                                json.dumps(
                                    {
                                        "error": {
                                            "code": "request_too_large",
                                            "message": "Request exceeds its size limit.",
                                        }
                                    }
                                ),
                                status=413,
                                headers={
                                    "content-type": "application/json",
                                    "cache-control": "no-store",
                                },
                            )
                        chunks.append(value)
                    request = _request(request, body=b"".join(chunks))
                return Response(await asgi.fetch(application, request, self.env))
            finally:
                await resources.close()
