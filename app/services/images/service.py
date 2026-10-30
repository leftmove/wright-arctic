"""Store general-purpose images for an account."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.accounts import Account
from app.schemas import ImageListResponse, ImageResponse, ImageSummary
from app.services.images.errors import (
    ImageEmpty,
    ImageNotFound,
    ImageQuotaExceeded,
    ImageServiceError,
    ImageTooLarge,
    StorageInvalid,
    StorageUnavailable,
)
from app.services.images.limits import MAX_LISTED_IMAGES, TEMPORARY_ACCOUNT_IMAGE_CAP
from app.services.images.models import (
    DeliveryFormat,
    ImageExtension,
    ImageObject,
    ProcessedImage,
)
from app.services.images.processing import (
    ImagePipeline,
    delivery_variant,
    process_image,
)
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
        self._pipeline = replace(pipeline or ImagePipeline(), output_format="jxl")

    def close(self) -> None:
        """Release the storage client."""
        self._store.close()

    def create(
        self,
        account: Account,
        source: BinaryIO,
        known_size: int | None,
        *,
        image_id: UUID | None = None,
        portrait: bool = False,
        format: DeliveryFormat | None = None,
    ) -> ImageResponse:
        """Recompress an upload and store it under the account."""
        # Caller-selected IDs make migration retries idempotent. IDs remain
        # scoped to the verified account and existing objects are immutable.
        if image_id is not None:
            try:
                return self.get_image(
                    account, image_id, format=format or ("webp" if portrait else "jxl")
                )
            except ImageNotFound:
                pass
        payload = _read_limited(source, known_size, self._pipeline.max_upload_bytes)
        limit = TEMPORARY_ACCOUNT_IMAGE_CAP
        if self._store.count_images(account, maximum=limit + 1) >= limit:
            raise ImageQuotaExceeded(limit)
        pipeline = (
            replace(self._pipeline, max_output_edge=512) if portrait else self._pipeline
        )
        processed = process_image(payload, pipeline)
        immutable = image_id is not None
        image_id = image_id or uuid4()
        if not self._store.put_image(account, image_id, processed, if_absent=immutable):
            return self.get_image(
                account, image_id, format=format or ("webp" if portrait else "jxl")
            )
        self._enforce_cap(account, image_id, processed.extension, limit)
        canonical = ImageObject(
            id=image_id,
            media_type=processed.media_type,
            extension=processed.extension,
            channels=processed.channels,
            byte_size=len(processed.data),
            width=processed.width,
            height=processed.height,
            created_at=datetime.now(UTC),
        )
        delivered = self._delivery(
            account,
            canonical,
            format or ("webp" if portrait else "jxl"),
            processed.data,
        )
        return self._response(account, delivered)

    def list_images(self, account: Account) -> ImageListResponse:
        """List the account's stored images, oldest first."""
        images = [
            _summary(account, image, self._store)
            for image in self._store.list_images(account, limit=MAX_LISTED_IMAGES)
        ]
        return ImageListResponse(images=images)

    def get_image(
        self, account: Account, image_id: UUID, *, format: DeliveryFormat = "webp"
    ) -> ImageResponse:
        """Return one stored image, including its pixel dimensions."""
        image = self._store.get_image(account, image_id)
        if image.extension != "jxl":
            # Preserve IDs and existing immutable URLs. Legacy objects remain
            # compatibility copies; all new variants derive from this source.
            payload = self._store.read_image(account, image)
            canonical = process_image(payload, replace(self._pipeline, lossless=True))
            self._store.put_image(
                account,
                image_id,
                canonical,
                if_absent=True,
                created_at=image.created_at,
            )
            image = self._store.get_representation(account, image_id, "jxl")
        return self._response(account, self._delivery(account, image, format))

    def _delivery(
        self,
        account: Account,
        canonical: ImageObject,
        format: DeliveryFormat,
        payload: bytes | None = None,
    ) -> ImageObject:
        if format == "jxl":
            return canonical
        try:
            return replace(
                self._store.get_representation(
                    account, canonical.id, format, variant=True
                ),
                created_at=canonical.created_at,
            )
        except ImageNotFound:
            pass
        variant: ProcessedImage = delivery_variant(
            payload
            if payload is not None
            else self._store.read_image(account, canonical),
            canonical,
            format,
        )
        if not self._store.put_image(
            account,
            canonical.id,
            variant,
            if_absent=True,
            variant=True,
            created_at=canonical.created_at,
        ):
            return self._store.get_representation(
                account, canonical.id, format, variant=True
            )
        return ImageObject(
            id=canonical.id,
            media_type=variant.media_type,
            extension=variant.extension,
            byte_size=len(variant.data),
            width=variant.width,
            height=variant.height,
            created_at=canonical.created_at,
            variant=True,
        )

    def _response(self, account: Account, image: ImageObject) -> ImageResponse:
        if image.width is None or image.height is None:
            raise StorageInvalid()
        summary = _summary(account, image, self._store)
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

    def delete_image(self, account: Account, image_id: UUID) -> None:
        """Delete one stored image belonging to the account."""
        self._store.delete_image(account, image_id)

    def _enforce_cap(
        self,
        account: Account,
        image_id: UUID,
        extension: ImageExtension,
        limit: int,
    ) -> None:
        stored = self._store.list_images(account, limit=MAX_LISTED_IMAGES)
        if len(stored) <= limit:
            return
        keep = {image.id for image in stored[:limit]}
        if image_id in keep:
            return
        try:
            self._store.discard_image(account, image_id, extension)
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


def _summary(account: Account, image: ImageObject, store: S3ImageStore) -> ImageSummary:
    return ImageSummary(
        id=image.id,
        media_type=image.media_type,
        byte_size=image.byte_size,
        url=_http_url(
            store.url_for(account, image.id, image.extension, variant=image.variant)
        ),
        url_expires_in=store.url_expires_in,
        created_at=image.created_at,
    )


def _http_url(value: str) -> AnyHttpUrl:
    try:
        return _URL.validate_python(value)
    except ValidationError:
        logger.warning("object storage produced an unusable image URL")
        raise StorageUnavailable() from None
