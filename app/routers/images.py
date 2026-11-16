"""Direct AVIF uploads and authorized redirects to R2."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import UUID7

from app.dependencies import AccountDep, execute, require_image_service
from app.schemas import ImageResponse, ImageUploadResponse, IssueImage
from app.services.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/images", tags=["images"])
avatars = APIRouter(prefix="/avatars", tags=["public avatars"])


@router.post("/uploads", status_code=201)
async def issue_image(
    command: IssueImage, account: AccountDep, request: Request
) -> ImageUploadResponse:
    return await execute(
        request, require_image_service(request).issue, account, command
    )


@router.post("/{image_id}/confirm")
async def confirm_image(
    image_id: UUID7, account: AccountDep, request: Request
) -> ImageResponse:
    return await execute(
        request, require_image_service(request).confirm, account, image_id
    )


@router.get("")
async def list_images(
    account: AccountDep,
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> list[ImageResponse]:
    images = await execute(
        request,
        require_image_service(request).list_images,
        account,
        limit=limit,
        offset=offset,
        cursor=decode_cursor(cursor, offset=offset),
    )
    if len(images) == limit:
        response.headers["X-Next-Cursor"] = encode_cursor(
            images[-1].created_at, images[-1].id
        )
    return images


@router.get("/by-hash/{digest}")
async def by_hash(
    digest: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    account: AccountDep,
    request: Request,
) -> ImageResponse:
    return await execute(
        request, require_image_service(request).by_hash, account, digest
    )


@router.get("/{image_id}")
async def image_metadata(
    image_id: UUID7, account: AccountDep, request: Request
) -> ImageResponse:
    return await execute(
        request, require_image_service(request).metadata, account, image_id
    )


@router.get("/{image_id}/content", response_class=RedirectResponse)
async def image_content(
    image_id: UUID7, account: AccountDep, request: Request
) -> Response:
    download = await execute(
        request, require_image_service(request).content, account, image_id
    )
    return RedirectResponse(
        download.url, headers={"Cache-Control": "private, no-store"}
    )


@router.delete("/{image_id}", status_code=204)
async def delete_image(
    image_id: UUID7, account: AccountDep, request: Request
) -> Response:
    await execute(request, require_image_service(request).delete, account, image_id)
    return Response(status_code=204)


@avatars.get("/{account_id}", response_class=RedirectResponse)
async def public_avatar(account_id: UUID7, request: Request) -> Response:
    download = await execute(request, require_image_service(request).avatar, account_id)
    return RedirectResponse(
        download.url, headers={"Cache-Control": "private, no-store"}
    )
