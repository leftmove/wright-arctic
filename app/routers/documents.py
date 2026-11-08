"""Documents API. Authentication runs before body buffering; handlers stay thin."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.dependencies import AccountDep, require_document_service
from app.schemas import (
    AppendDocuments,
    CreateDocument,
    DocumentChange,
    DocumentCommand,
    DocumentContent,
    DocumentGrant,
    DocumentReceipt,
    DocumentSummary,
    GrantDocument,
    RenameDocument,
    RestoreDocument,
    ShareDocument,
    UploadDocument,
    error_body,
)
from app.services.documents.errors import DocumentError, InvalidNative, TooLarge
from app.services.documents.native import MAX_STATE, MAX_UPDATE

router = APIRouter(prefix="/documents", tags=["documents"])


def _metadata[T: BaseModel](model: type[T], raw: str) -> T:
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise DocumentError(
            "invalid_document_metadata", "Document metadata is invalid.", 422
        ) from None


def _binary(file: UploadFile, maximum: int) -> bytes:
    if file.size is not None and file.size > maximum:
        raise TooLarge()
    data = file.file.read(maximum + 1)
    if len(data) > maximum:
        raise TooLarge()
    if not data:
        raise InvalidNative()
    return data


@router.post("", status_code=201)
def create_document(
    command: CreateDocument, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).create(account, command)


@router.get("")
def list_documents(
    account: AccountDep,
    request: Request,
    trashed: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[DocumentSummary]:
    return require_document_service(request).list_documents(
        account, trashed=trashed, limit=limit, offset=offset
    )


@router.get("/resolve/{namespace}/{slug}")
def resolve_document(
    namespace: str, slug: str, account: AccountDep, request: Request
) -> DocumentSummary:
    return require_document_service(request).resolve(account, namespace, slug)


@router.put("/{document_id}")
def upload_document(
    document_id: UUID,
    account: AccountDep,
    request: Request,
    metadata: Annotated[str, Form(max_length=8192)],
    state: Annotated[UploadFile, File()],
) -> DocumentReceipt:
    command = _metadata(UploadDocument, metadata)
    return require_document_service(request).upload(
        account, document_id, command, _binary(state, MAX_STATE)
    )


@router.get("/{document_id}")
def document_metadata(
    document_id: UUID, account: AccountDep, request: Request
) -> DocumentSummary:
    return require_document_service(request).metadata(account, document_id)


@router.get("/{document_id}/state", response_class=Response)
def document_state(
    document_id: UUID,
    account: AccountDep,
    request: Request,
    through_seq: Annotated[int | None, Query(ge=0)] = None,
) -> Response:
    prefix, prepared = require_document_service(request).state(
        account, document_id, through_seq
    )
    return Response(
        prepared.state,
        media_type="application/octet-stream",
        headers={
            "X-Document-Id": str(document_id),
            "X-Document-Head": str(prefix.through_seq),
            "X-Document-Format": str(prefix.document.format_version),
            "X-Document-Schema": prefix.document.schema_name,
            "X-Document-Schema-Version": str(prefix.document.schema_version),
        },
    )


@router.get("/{document_id}/content")
def document_content(
    document_id: UUID, account: AccountDep, request: Request
) -> DocumentContent:
    return require_document_service(request).content(account, document_id)


@router.post("/{document_id}/changes")
def append_changes(
    document_id: UUID,
    account: AccountDep,
    request: Request,
    metadata: Annotated[str, Form(max_length=16384)],
    updates: Annotated[list[UploadFile], File()],
) -> list[DocumentReceipt]:
    command = _metadata(AppendDocuments, metadata)
    return require_document_service(request).append(
        account,
        document_id,
        command.operations,
        [_binary(file, MAX_UPDATE) for file in updates],
    )


@router.get("/{document_id}/changes")
def document_history(
    document_id: UUID,
    account: AccountDep,
    request: Request,
    after: Annotated[int, Query(ge=-1)] = -1,
    limit: Annotated[int, Query(ge=1, le=512)] = 128,
) -> list[DocumentChange]:
    return require_document_service(request).history(account, document_id, after, limit)


@router.get("/{document_id}/changes/{sequence}", response_class=Response)
def change_bytes(
    document_id: UUID, sequence: int, account: AccountDep, request: Request
) -> Response:
    payload = require_document_service(request).change(account, document_id, sequence)
    return Response(
        payload,
        status_code=200 if payload is not None else 204,
        media_type="application/octet-stream",
    )


@router.patch("/{document_id}/title")
def rename_document(
    document_id: UUID, command: RenameDocument, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).rename(account, document_id, command)


@router.post("/{document_id}/share")
def share_document(
    document_id: UUID, command: ShareDocument, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).share(account, document_id, command)


@router.get("/{document_id}/access")
def document_grants(
    document_id: UUID, account: AccountDep, request: Request
) -> list[DocumentGrant]:
    return require_document_service(request).grants(account, document_id)


@router.put("/{document_id}/access/{user_id}")
def grant_document(
    document_id: UUID,
    user_id: UUID,
    command: GrantDocument,
    account: AccountDep,
    request: Request,
) -> DocumentReceipt:
    return require_document_service(request).grant(
        account, document_id, command.operation_id, user_id, command.role
    )


@router.post("/{document_id}/trash")
def trash_document(
    document_id: UUID, command: DocumentCommand, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).lifecycle(
        account, document_id, command.operation_id, True
    )


@router.post("/{document_id}/restore")
def restore_document(
    document_id: UUID, command: DocumentCommand, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).lifecycle(
        account, document_id, command.operation_id, False
    )


@router.post("/{document_id}/restore-content")
def restore_content(
    document_id: UUID, command: RestoreDocument, account: AccountDep, request: Request
) -> DocumentReceipt:
    return require_document_service(request).restore(
        account, document_id, command.operation_id, command.through_seq
    )


@router.post("/{document_id}/snapshots")
def checkpoint_document(
    document_id: UUID,
    account: AccountDep,
    request: Request,
    through_seq: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, int]:
    return {
        "through_seq": require_document_service(request).snapshot(
            account, document_id, through_seq
        )
    }


def install_document_error_handler(application: FastAPI) -> None:
    application.add_exception_handler(DocumentError, _error)


def _error(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, DocumentError):
        raise error
    return JSONResponse(
        error_body(error.code, error.message),
        status_code=error.status,
        headers={"Cache-Control": "private, no-store", "Vary": "Authorization"},
    )
