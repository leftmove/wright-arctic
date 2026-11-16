"""Identity and avatar pointer operations."""

from fastapi import APIRouter, Request

from app.dependencies import AccountDep, execute, require_identity_service
from app.schemas import AccountResponse, AvatarSelection

router = APIRouter(prefix="/account", tags=["identity"])


@router.get("")
async def current_account(account: AccountDep, request: Request) -> AccountResponse:
    return await execute(request, require_identity_service(request).current, account)


@router.put("/avatar")
async def select_avatar(
    command: AvatarSelection, account: AccountDep, request: Request
) -> AccountResponse:
    return await execute(
        request,
        require_identity_service(request).select_avatar,
        account,
        command.image_id,
    )
