"""Service health endpoint."""

from contextlib import suppress

from fastapi import APIRouter, Request

from app.authentication.errors import AuthenticationUnavailable
from app.dependencies import (
    SettingsDep,
    execute,
    request_resources,
    require_database,
)
from app.schemas import HealthResponse
from app.services.errors import ServiceError
from app.services.storage.objects import ObjectMissing

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    """Report whether the API process is serving requests."""
    return HealthResponse(service=settings.app_name)


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request, settings: SettingsDep) -> HealthResponse:
    """Check runtime database privileges/schema and access to the image bucket."""
    if settings.auth_issuer is None:
        raise AuthenticationUnavailable()
    await execute(request, require_database(request).open)
    storage = request_resources(request).storage
    if storage is None:
        raise ServiceError(
            "storage_unconfigured", "Object storage is not configured.", 503
        )
    for objects, key in (
        (storage.checkpoints, "documents/.readiness"),
        (storage.images, "images/.readiness"),
    ):
        with suppress(ObjectMissing):
            await execute(request, objects.head, key)
    return HealthResponse(service=settings.app_name)
