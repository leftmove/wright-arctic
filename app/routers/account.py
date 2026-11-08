"""The caller owns both image selection and delivery; no delegated subjects."""

from fastapi import APIRouter, Request

from app.dependencies import AccountDep, require_image_service
from app.schemas import ImageResponse, PortraitSelection

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/portrait", response_model=ImageResponse | None)
def portrait(account: AccountDep, request: Request) -> ImageResponse | None:
    return require_image_service(request).portrait(account)


@router.put("/portrait", response_model=ImageResponse | None)
def select_portrait(
    selection: PortraitSelection, account: AccountDep, request: Request
) -> ImageResponse | None:
    return require_image_service(request).select_portrait(
        account, selection.image_id, if_absent=selection.if_absent
    )
