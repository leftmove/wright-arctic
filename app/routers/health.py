"""Service health endpoint."""

from fastapi import APIRouter

from app.dependencies import SettingsDep
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(settings: SettingsDep) -> HealthResponse:
    """Report whether the API process is serving requests."""
    return HealthResponse(service=settings.app_name)
