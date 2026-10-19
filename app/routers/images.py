"""Image upload and retrieval."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, FastAPI, File, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.dependencies import AccountDep, require_image_service
from app.schemas import ErrorResponse, ImageListResponse, ImageResponse, error_body
from app.services.images.errors import ImageServiceError
from app.services.images.limits import MAX_STORED_BYTES, MAX_UPLOAD_BYTES

_UPLOAD_MIB = MAX_UPLOAD_BYTES // (1024 * 1024)
_STORED_MIB = MAX_STORED_BYTES // (1024 * 1024)

router = APIRouter(
    prefix="/images",
    tags=["images"],
    responses={
        status.HTTP_502_BAD_GATEWAY: {
            "model": ErrorResponse,
            "description": "Object storage request failed.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Object storage is not configured.",
        },
    },
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ImageResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "The file is empty, unreadable, or cannot be decoded.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "The account has reached the image limit.",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorResponse,
            "description": f"The upload is larger than {_UPLOAD_MIB} MiB.",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "model": ErrorResponse,
            "description": "The file is not a supported still image.",
        },
    },
)
def create_image(
    account: AccountDep,
    request: Request,
    response: Response,
    file: Annotated[
        UploadFile,
        File(
            description=(
                "JPEG, PNG, WebP, AVIF, or static GIF. "
                f"Maximum upload size is {_UPLOAD_MIB} MiB. "
                f"The stored object is at most {_STORED_MIB} MiB."
            )
        ),
    ],
) -> ImageResponse:
    """Store a general-purpose image.

    The upload is limited to 10 MiB. JPEG, PNG, WebP, AVIF, and static GIF are
    stripped of metadata and recompressed to AVIF or lossless WebP. The stored
    object is at most 1 MiB. Each account can store 10 images.
    """
    images = require_image_service(request)
    image = images.create(account.id, file.file, file.size)
    response.headers["Location"] = f"/images/{image.id}"
    return image


@router.get("", response_model=ImageListResponse)
def list_images(account: AccountDep, request: Request) -> ImageListResponse:
    """List images stored for the account, oldest first."""
    return require_image_service(request).list_images(account.id)


@router.get(
    "/{image_id}",
    response_model=ImageResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "The image is not stored for this account.",
        },
    },
)
def read_image(image_id: UUID, account: AccountDep, request: Request) -> ImageResponse:
    """Return one stored image and a URL that fetches its bytes."""
    return require_image_service(request).get_image(account.id, image_id)


@router.delete(
    "/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "The image is not stored for this account.",
        },
    },
)
def delete_image(image_id: UUID, account: AccountDep, request: Request) -> Response:
    """Delete one stored image belonging to the account."""
    require_image_service(request).delete_image(account.id, image_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install_image_error_handler(application: FastAPI) -> None:
    """Render image failures as a stable code and message."""
    application.add_exception_handler(ImageServiceError, _handle_image_error)


def _handle_image_error(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ImageServiceError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code, content=error_body(exc.code, exc.message)
    )
