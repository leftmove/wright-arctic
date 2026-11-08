"""Typed dependency providers shared by API routers."""

import os
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.accounts import Account
from app.authentication.http import authenticate_request
from app.services.documents.errors import Unavailable
from app.services.documents.service import DocumentService
from app.services.images.errors import StorageUnconfigured
from app.services.images.service import ImageService
from app.settings import Settings
from app.state import AppResources, scope_resources


def request_resources(request: Request) -> AppResources:
    return scope_resources(request.scope) or cast(
        AppResources, request.app.state.resources
    )


@lru_cache
def get_settings() -> Settings:
    """Load validated settings once per process."""
    return Settings(_secrets_dir=os.getenv("WRIGHT_ARCTIC_SECRETS_DIR"))


_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="NativeAccessToken",
    description="Access JWT from the native app's AuthManager.get_valid_access_token(false).",
)


async def get_current_account(
    request: Request,
    _credential: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> Account:
    """Return the verified identity, reusing the middleware's request-local result."""
    resources = request_resources(request)
    return await authenticate_request(request, resources.authentication)


def require_image_service(request: Request) -> ImageService:
    """Return the image service after the route has validated the request.

    FastAPI runs dependencies before it turns parameter errors into a 422, so
    this is called from handlers instead of being declared with Depends.
    """
    resources = request_resources(request)
    if resources.image_service is None:
        raise StorageUnconfigured()
    return resources.image_service


async def request_settings(request: Request) -> Settings:
    """Use this application's settings without starting a dependency worker thread."""
    resources = request_resources(request)
    return resources.settings if resources.settings is not None else get_settings()


async def execute[**P, T](
    request: Request, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    resources = request_resources(request)
    return await resources.executor.run(function, *args, **kwargs)


type SettingsDep = Annotated[Settings, Depends(request_settings)]
type AccountDep = Annotated[Account, Depends(get_current_account)]


def require_document_service(request: Request) -> DocumentService:
    resources = request_resources(request)
    if resources.document_service is None:
        raise Unavailable()
    return resources.document_service
