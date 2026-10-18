"""API entrypoint and discovery links."""

from fastapi import APIRouter

from app.dependencies import SettingsDep
from app.schemas import RootResponse

router = APIRouter(tags=["root"])


@router.get("/", response_model=RootResponse)
def root(settings: SettingsDep) -> RootResponse:
    """Identify the service and expose links to documentation and health."""
    return RootResponse(service=settings.app_name)
