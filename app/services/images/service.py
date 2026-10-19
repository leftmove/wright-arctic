"""Store general-purpose images for an account."""

import logging
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.schemas import ImageListResponse, ImageResponse, ImageSummary
from app.services.images.errors import (
    ImageEmpty,
    ImageQuotaExceeded,
    ImageServiceError,
    ImageTooLarge,
    StorageInvalid,
    StorageUnavailable,
)
from app.services.images.limits import MAX_LISTED_IMAGES, TEMPORARY_ACCOUNT_IMAGE_CAP
from app.services.images.models import ImageExtension, ImageObject
from app.services.images.processing import ImagePipeline, process_image
from app.services.images.storage import S3ImageStore, build_image_store
from app.settings import Settings

logger = logging.getLogger(__name__)

_URL = TypeAdapter(AnyHttpUrl)


class ImageService:
    """Compress uploads and keep them in object storage."""

    def __init__(
        self, store: S3ImageStore, pipeline: ImagePipeline | None = None
    ) -> None:
        self._store = store
        self._pipeline = ImagePipeline() if pipeline is None else pipeline

    def close(self) -> None:
        """Release the storage client."""
        self._store.close()

    def create(
        self, account_id: UUID, source: BinaryIO, known_size: int | None
    ) -> ImageResponse:
        """Recompress an upload and store it under the account."""
        payload = _read_limited(source, known_size, self._pipeline.max_upload_bytes)
        limit = TEMPORARY_ACCOUNT_IMAGE_CAP
        if self._store.count_images(account_id, maximum=limit + 1) >= limit:
            raise ImageQuotaExceeded(limit)
        processed = process_image(payload, self._pipeline)
        image_id = uuid4()
        self._store.put_image(account_id, image_id, processed)
        self._enforce_cap(account_id, image_id, processed.extension, limit)
        return ImageResponse(
            id=image_id,
            media_type=processed.media_type,
            byte_size=len(processed.data),
            width=processed.width,
            height=processed.height,
            url=_http_url(
                self._store.url_for(account_id, image_id, processed.extension)
            ),
            url_expires_in=self._store.url_expires_in,
            created_at=datetime.now(UTC),
        )

    def list_images(self, account_id: UUID) -> ImageListResponse:
        """List the account's stored images, oldest first."""
        images = [
            _summary(account_id, image, self._store)
            for image in self._store.list_images(account_id, limit=MAX_LISTED_IMAGES)
        ]
        return ImageListResponse(images=images)

    def get_image(self, account_id: UUID, image_id: UUID) -> ImageResponse:
        """Return one stored image, including its pixel dimensions."""
        image = self._store.get_image(account_id, image_id)
        if image.width is None or image.height is None:
            raise StorageInvalid()
        summary = _summary(account_id, image, self._store)
        return ImageResponse(
            id=summary.id,
            media_type=summary.media_type,
            byte_size=summary.byte_size,
            width=image.width,
            height=image.height,
            url=summary.url,
            url_expires_in=summary.url_expires_in,
            created_at=summary.created_at,
        )

    def delete_image(self, account_id: UUID, image_id: UUID) -> None:
        """Delete one stored image belonging to the account."""
        self._store.delete_image(account_id, image_id)

    def _enforce_cap(
        self,
        account_id: UUID,
        image_id: UUID,
        extension: ImageExtension,
        limit: int,
    ) -> None:
        stored = self._store.list_images(account_id, limit=MAX_LISTED_IMAGES)
        if len(stored) <= limit:
            return
        keep = {image.id for image in stored[:limit]}
        if image_id in keep:
            return
        try:
            self._store.discard_image(account_id, image_id, extension)
        except ImageServiceError:
            logger.warning("failed to remove an image stored above the account cap")
            raise
        raise ImageQuotaExceeded(limit)


def build_image_service(settings: Settings) -> ImageService | None:
    """Build the image service when object storage is configured."""
    store = build_image_store(settings)
    if store is None:
        return None
    return ImageService(store)


def _read_limited(source: BinaryIO, known_size: int | None, limit: int) -> bytes:
    if known_size is not None and known_size > limit:
        raise ImageTooLarge()
    source.seek(0)
    payload = source.read(limit + 1)
    if len(payload) > limit:
        raise ImageTooLarge()
    if not payload:
        raise ImageEmpty()
    return payload


def _summary(account_id: UUID, image: ImageObject, store: S3ImageStore) -> ImageSummary:
    return ImageSummary(
        id=image.id,
        media_type=image.media_type,
        byte_size=image.byte_size,
        url=_http_url(store.url_for(account_id, image.id, image.extension)),
        url_expires_in=store.url_expires_in,
        created_at=image.created_at,
    )


def _http_url(value: str) -> AnyHttpUrl:
    try:
        return _URL.validate_python(value)
    except ValidationError:
        logger.warning("object storage produced an unusable image URL")
        raise StorageUnavailable() from None
