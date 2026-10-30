"""Typed dependency providers shared by API routers."""

import os
from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.accounts import Account
from app.authentication.http import authenticate_request
from app.services.images.errors import StorageUnconfigured
from app.services.images.service import ImageService
from app.settings import Settings
from app.state import AppResources


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
    resources = cast(AppResources, request.app.state.resources)
    return await authenticate_request(request, resources.authentication)


def require_image_service(request: Request) -> ImageService:
    """Return the image service after the route has validated the request.

    FastAPI runs dependencies before it turns parameter errors into a 422, so
    this is called from handlers instead of being declared with Depends.
    """
    resources = cast(AppResources, request.app.state.resources)
    if resources.image_service is None:
        raise StorageUnconfigured()
    return resources.image_service


type SettingsDep = Annotated[Settings, Depends(get_settings)]
type AccountDep = Annotated[Account, Depends(get_current_account)]
