"""Document metadata, permissions, and trash lifecycle."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import UUID7

from app.dependencies import AccountDep, execute, require_document_service
from app.schemas import (
    CreateDocument,
    DocumentGrant,
    DocumentSummary,
    GrantDocument,
    RenameDocument,
)

router = APIRouter(prefix="/documents", tags=["metadata"])


@router.post("", status_code=201)
async def create_document(
    command: CreateDocument, account: AccountDep, request: Request
) -> DocumentSummary:
    return await execute(
        request, require_document_service(request).create, account, command
    )


@router.get("")
async def list_documents(
    account: AccountDep,
    request: Request,
    trashed: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[DocumentSummary]:
    return await execute(
        request,
        require_document_service(request).list_documents,
        account,
        trashed=trashed,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}")
async def document_metadata(
    document_id: UUID7, account: AccountDep, request: Request
) -> DocumentSummary:
    return await execute(
        request, require_document_service(request).metadata, account, document_id
    )


@router.patch("/{document_id}/title")
async def rename_document(
    document_id: UUID7, command: RenameDocument, account: AccountDep, request: Request
) -> DocumentSummary:
    return await execute(
        request,
        require_document_service(request).rename,
        account,
        document_id,
        command.title,
    )


@router.get("/{document_id}/grants")
async def document_grants(
    document_id: UUID7, account: AccountDep, request: Request
) -> list[DocumentGrant]:
    return await execute(
        request, require_document_service(request).grants, account, document_id
    )


@router.put("/{document_id}/grants/{account_id}", status_code=204)
async def grant_document(
    document_id: UUID7,
    account_id: UUID7,
    command: GrantDocument,
    account: AccountDep,
    request: Request,
) -> Response:
    await execute(
        request,
        require_document_service(request).grant,
        account,
        document_id,
        account_id,
        command.role,
    )
    return Response(status_code=204)


@router.delete("/{document_id}/grants/{account_id}", status_code=204)
async def revoke_document(
    document_id: UUID7, account_id: UUID7, account: AccountDep, request: Request
) -> Response:
    await execute(
        request,
        require_document_service(request).grant,
        account,
        document_id,
        account_id,
        None,
    )
    return Response(status_code=204)


@router.post("/{document_id}/trash")
async def trash_document(
    document_id: UUID7, account: AccountDep, request: Request
) -> DocumentSummary:
    return await execute(
        request,
        require_document_service(request).lifecycle,
        account,
        document_id,
        trashed=True,
    )


@router.post("/{document_id}/restore")
async def restore_document(
    document_id: UUID7, account: AccountDep, request: Request
) -> DocumentSummary:
    return await execute(
        request,
        require_document_service(request).lifecycle,
        account,
        document_id,
        trashed=False,
    )
