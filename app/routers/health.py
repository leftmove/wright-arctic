"""Service health endpoint."""

from fastapi import APIRouter, Request

from app.authentication.errors import AuthenticationUnavailable
from app.dependencies import (
    SettingsDep,
    execute,
    require_document_service,
    require_image_service,
)
from app.schemas import HealthResponse

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
    await execute(request, require_document_service(request).open)
    await execute(request, require_image_service(request).open)
    return HealthResponse(service=settings.app_name)
