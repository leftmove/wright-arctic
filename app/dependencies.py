"""Typed API dependencies; provider identities never substitute for account IDs."""

import os
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.authentication.http import authenticate_request
from app.services.database import Database
from app.services.documents.service import DocumentService
from app.services.errors import ServiceError, Unavailable
from app.services.identity.service import IdentityService
from app.services.images.service import ImageService
from app.services.models import Account
from app.services.sync.service import SyncService
from app.settings import Settings
from app.state import AppResources, scope_resources


def request_resources(request: Request) -> AppResources:
    return scope_resources(request.scope) or cast(
        AppResources, request.app.state.resources
    )


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=os.getenv("WRIGHT_ARCTIC_SECRETS_DIR"))


_bearer = HTTPBearer(auto_error=False, scheme_name="NativeAccessToken")


async def get_current_account(
    request: Request,
    _credential: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> Account:
    existing = getattr(request.state, "resolved_account", None)
    if isinstance(existing, Account):
        return existing
    resources = request_resources(request)
    identity = await authenticate_request(request, resources.authentication)
    service = require_identity_service(request)
    account = await resources.executor.run(service.resolve, identity)
    request.state.resolved_account = account
    return account


async def request_settings(request: Request) -> Settings:
    return request_resources(request).settings


async def execute[**P, T](
    request: Request, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    return await request_resources(request).executor.run(function, *args, **kwargs)


def require_database(request: Request) -> Database:
    value = request_resources(request).database
    if value is None:
        raise Unavailable()
    return value


def require_identity_service(request: Request) -> IdentityService:
    value = request_resources(request).identity
    if value is None:
        raise Unavailable()
    return value


def require_document_service(request: Request) -> DocumentService:
    value = request_resources(request).documents
    if value is None:
        raise Unavailable()
    return value


def require_image_service(request: Request) -> ImageService:
    value = request_resources(request).images
    if value is None:
        raise ServiceError(
            "storage_unconfigured", "Image storage is not configured.", 503
        )
    return value


def require_sync_service(request: Request) -> SyncService:
    value = request_resources(request).sync
    if value is None:
        raise ServiceError(
            "storage_unconfigured", "Checkpoint storage is not configured.", 503
        )
    return value


type AccountDep = Annotated[Account, Depends(get_current_account)]
type SettingsDep = Annotated[Settings, Depends(request_settings)]
