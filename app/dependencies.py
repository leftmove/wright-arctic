"""Typed dependency providers shared by API routers."""

import os
from functools import lru_cache
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Header, Request

from app.accounts import Account
from app.services.images.errors import StorageUnconfigured
from app.services.images.service import ImageService
from app.settings import Settings
from app.state import AppResources


@lru_cache
def get_settings() -> Settings:
    """Load validated settings once per process."""
    return Settings(_secrets_dir=os.getenv("WRIGHT_ARCTIC_SECRETS_DIR"))


def get_current_account(
    account_id: Annotated[
        UUID,
        Header(
            alias="X-Account-Id",
            convert_underscores=False,
            description=(
                "Temporary account id until authentication is enabled. "
                "This header is not an authorization check."
            ),
        ),
    ],
) -> Account:
    """Return the stub account named by the request header."""
    return Account(id=account_id)


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
