"""Account-owned document spaces."""

from fastapi import APIRouter, Request

from app.dependencies import AccountDep, execute, require_document_service
from app.schemas import CreateSpace, SpaceResponse

router = APIRouter(prefix="/spaces", tags=["metadata"])


@router.get("")
async def spaces(account: AccountDep, request: Request) -> list[SpaceResponse]:
    return await execute(request, require_document_service(request).spaces, account)


@router.post("", status_code=201)
async def create_space(
    command: CreateSpace, account: AccountDep, request: Request
) -> SpaceResponse:
    return await execute(
        request, require_document_service(request).create_space, account, command.id
    )
