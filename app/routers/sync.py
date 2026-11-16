"""Whole-checkpoint capabilities and batch pointer publication."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import UUID7

from app.dependencies import AccountDep, execute, require_sync_service
from app.schemas import (
    BatchResponse,
    ImageReferences,
    Pointer,
    PublishBatch,
    PublishResponse,
    RetryUpload,
    StartBatch,
    UploadSlot,
)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/documents/{document_id}")
async def open_document(
    document_id: UUID7,
    account: AccountDep,
    request: Request,
    revision: Annotated[UUID7 | None, Query()] = None,
) -> Pointer:
    return await execute(
        request, require_sync_service(request).open, account, document_id, revision
    )


@router.get("/documents/{document_id}/checkpoints")
async def retained_checkpoints(
    document_id: UUID7, account: AccountDep, request: Request
) -> list[Pointer]:
    return await execute(
        request, require_sync_service(request).checkpoints, account, document_id
    )


@router.post(
    "/batches/{batch_id}/documents/{document_id}/{revision}/images", status_code=204
)
async def stage_images(
    batch_id: UUID7,
    document_id: UUID7,
    revision: UUID7,
    command: ImageReferences,
    account: AccountDep,
    request: Request,
) -> Response:
    await execute(
        request,
        require_sync_service(request).stage_images,
        account,
        batch_id,
        document_id,
        revision,
        command.images,
    )
    return Response(status_code=204)


@router.post("/batches")
async def start_batch(
    command: StartBatch, account: AccountDep, request: Request
) -> BatchResponse:
    return await execute(request, require_sync_service(request).start, account, command)


@router.post("/batches/{batch_id}/documents/{document_id}")
async def retry_upload(
    batch_id: UUID7,
    document_id: UUID7,
    command: RetryUpload,
    account: AccountDep,
    request: Request,
) -> UploadSlot:
    return await execute(
        request,
        require_sync_service(request).retry,
        account,
        batch_id,
        document_id,
        command,
    )


@router.post("/batches/{batch_id}/publish")
async def publish_batch(
    batch_id: UUID7, command: PublishBatch, account: AccountDep, request: Request
) -> PublishResponse:
    return await execute(
        request, require_sync_service(request).publish, account, batch_id, command
    )
