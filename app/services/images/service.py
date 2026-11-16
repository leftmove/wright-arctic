"""Direct AVIF uploads, catalog quotas, and signed downloads."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import UUID7, AwareDatetime

from app.schemas import ImageResponse, ImageUploadResponse, IssueImage, Permission
from app.services.catalog.reader import CatalogReader
from app.services.database import AsyncDatabase as Database
from app.services.errors import ServiceError
from app.services.identity.actor import Actor
from app.services.limits import MAX_IMAGE_BYTES
from app.services.models import Image, Record
from app.services.operations import operation
from app.services.pagination import PageCursor
from app.services.storage.objects import (
    AsyncImageObjects as ImageObjects,
)
from app.services.storage.objects import (
    StorageUnavailable,
)
from app.services.storage.signing import S3Signer


class ImageUpload(Record):
    id: UUID7
    account: UUID7
    key: str
    size: int
    hash: str
    state: Literal["issued", "confirmed", "pending", "deleted"]
    issued_at: AwareDatetime
    expires_at: AwareDatetime


def image_response(image: Image) -> ImageResponse:
    return ImageResponse(
        id=image.id,
        account=image.account,
        size=image.size,
        hash=image.hash,
        created_at=image.created_at,
        url=f"/images/{image.id}/content",
    )


def is_avif(prefix: bytes) -> bool:
    """Inspect only the ISO BMFF file-type box; never decode or transcode pixels."""
    if len(prefix) < 16 or prefix[4:8] != b"ftyp":
        return False
    length = int.from_bytes(prefix[:4], "big")
    if length < 16 or length > len(prefix) or length % 4:
        return False
    brands = [
        prefix[8:12],
        *[prefix[index : index + 4] for index in range(16, length, 4)],
    ]
    return b"avif" in brands or b"avis" in brands


class ImageService:
    def __init__(
        self,
        database: Database,
        objects: ImageObjects,
        signer: S3Signer,
        *,
        presign_seconds: int = 300,
        reader: CatalogReader | None = None,
        daily_quota: int = 100,
        total_quota: int = 300,
    ) -> None:
        self._database = database
        self._objects = objects
        self._signer = signer
        self._seconds = presign_seconds
        self._reader = reader or CatalogReader(database)
        self._daily = daily_quota
        self._total = total_quota

    def _slot(self, upload: ImageUpload, now: datetime) -> ImageUploadResponse:
        seconds = min(self._seconds, int((upload.expires_at - now).total_seconds()))
        permission = (
            self._signer.permission(
                upload.key,
                seconds,
                method="PUT",
                size=upload.size,
                sha256=upload.hash,
                media_type="image/avif",
                now=now,
            )
            if upload.state == "issued" and seconds > 0
            else None
        )
        return ImageUploadResponse(
            id=upload.id, key=upload.key, upload=permission, image=None
        )

    async def issue(self, account: Actor, command: IssueImage) -> ImageUploadResponse:
        state = await operation(
            self._database,
            "issue_image",
            account,
            (
                command.id,
                command.size,
                command.hash,
                self._seconds,
                self._daily,
                self._total,
            ),
            _Issued,
        )
        if state.image is not None:
            image = state.image
            return ImageUploadResponse(
                id=image.id, key=image.key, upload=None, image=image_response(image)
            )
        assert state.upload is not None and state.now is not None
        return self._slot(state.upload, state.now)

    async def confirm(self, account: Actor, image_id: UUID) -> ImageResponse:
        state = await operation(
            self._database, "image_preflight", account, (image_id,), _Confirmation
        )
        if state.image is not None:
            return image_response(state.image)
        upload = state.upload
        assert upload is not None
        metadata = await self._objects.head(upload.key)
        valid = (
            metadata.size == upload.size
            and 0 < metadata.size <= MAX_IMAGE_BYTES
            and metadata.media_type == "image/avif"
            and metadata.metadata.get("sha256") == upload.hash
            and is_avif(await self._objects.prefix(upload.key, 512))
        )
        state = await operation(
            self._database,
            "confirm_verified_image",
            account,
            (image_id, upload.key, upload.hash, upload.size, upload.issued_at, valid),
            _Confirmation,
        )
        if state.image is not None:
            return image_response(state.image)
        await self.discard(upload.id, upload.key)
        raise ServiceError(
            "invalid_avif",
            "Upload an AVIF matching the authorized size and SHA-256.",
            415,
        )

    async def discard(self, image_id: UUID, key: str) -> bool:
        try:
            (await self._objects.delete(key))
        except StorageUnavailable:
            return False
        await self._database.write(
            "update coordination.uploads set state='deleted' where id=%s and state='pending'",
            (image_id,),
        )
        return True

    async def list_images(
        self,
        account: Actor,
        *,
        limit: int,
        offset: int,
        cursor: PageCursor | None = None,
    ) -> list[ImageResponse]:
        return [
            image_response(image)
            for image in await self._reader.images(
                account, limit=limit, offset=offset, cursor=cursor
            )
        ]

    async def by_hash(self, account: Actor, digest: str) -> ImageResponse:
        return image_response(await self._reader.image_by_hash(account, digest))

    async def metadata(self, account: Actor, image_id: UUID) -> ImageResponse:
        return image_response(await self._reader.image(account, image_id))

    async def content(self, account: Actor, image_id: UUID) -> Permission:
        image = await self._reader.image(account, image_id)
        return self._signer.permission(image.key, self._seconds, method="GET")

    async def avatar(self, account_id: UUID) -> Permission:
        image = await self._reader.avatar(account_id)
        return self._signer.permission(image.key, self._seconds, method="GET")

    async def delete(self, account: Actor, image_id: UUID) -> None:
        image = await operation(
            self._database, "delete_image", account, (image_id,), Image
        )
        await self.discard(image.id, image.key)


class _Confirmation(Record):
    image: Image | None = None
    upload: ImageUpload | None = None
    invalid: bool = False


class _Issued(_Confirmation):
    now: AwareDatetime | None = None
