"""Service health endpoint."""

from fastapi import APIRouter, Request

from app.dependencies import SettingsDep, execute, request_resources
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    """Report whether the API process is serving requests."""
    return HealthResponse(service=settings.app_name)


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request, settings: SettingsDep) -> HealthResponse:
    """Report whether catalog and object storage are ready to serve."""
    await execute(request, request_resources(request).confirm_ready)
    return HealthResponse(service=settings.app_name)
