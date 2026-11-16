"""Direct AVIF uploads, catalog quotas, and authorized byte delivery."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import UUID7, AwareDatetime

from app.schemas import ImageResponse, ImageUploadResponse, IssueImage
from app.services.database import AsyncDatabase as Database
from app.services.database import AsyncSession as Session
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.identifiers import image_key
from app.services.identity.actor import Actor, resolve_actor
from app.services.limits import MAX_IMAGE_BYTES
from app.services.models import Account, Image, Record
from app.services.operations import operation
from app.services.pagination import PageCursor
from app.services.storage.objects import (
    AsyncImageBody as ImageBody,
)
from app.services.storage.objects import (
    AsyncImageObjects as ImageObjects,
)
from app.services.storage.objects import (
    ObjectMissing,
    StorageUnavailable,
)
from app.services.storage.signing import S3Signer
from app.services.time import database_time


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
        daily_quota: int = 100,
        total_quota: int = 300,
    ) -> None:
        self._database = database
        self._objects = objects
        self._signer = signer
        self._seconds = presign_seconds
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
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (
                await session.execute(
                    "select id from public.accounts where id=%s for update",
                    (account.id,),
                )
            )
            now = await database_time(session)
            existing = await session.execute(
                "select * from public.images where id=%s and account=%s and deleted_at is null",
                (command.id, account.id),
            )
            if existing:
                image = Image.model_validate(existing[0])
                if image.hash != command.hash or image.size != command.size:
                    raise Conflict("image_id_reused")
                return ImageUploadResponse(
                    id=image.id, key=image.key, upload=None, image=image_response(image)
                )
            if await session.execute(
                "select id from public.images where id=%s", (command.id,)
            ):
                raise Conflict("image_id_reused")
            reservations = await session.execute(
                "select * from coordination.uploads where id=%s", (command.id,)
            )
            if reservations:
                upload = ImageUpload.model_validate(reservations[0])
                if upload.account != account.id:
                    raise NotFound()
                if (
                    upload.hash != command.hash
                    or upload.size != command.size
                    or upload.state != "issued"
                ):
                    raise Conflict("image_id_reused")
                return self._slot(upload, now)
            duplicates = await session.execute(
                "select * from public.images where account=%s and hash=%s and deleted_at is null order by id limit 1",
                (account.id, command.hash),
            )
            if duplicates:
                image = Image.model_validate(duplicates[0])
                return ImageUploadResponse(
                    id=image.id, key=image.key, upload=None, image=image_response(image)
                )
            counts = (
                await session.execute(
                    """select
                    (select count(*) from coordination.uploads where account=%s and issued_at >= %s) as daily,
                    ((select count(*) from public.images where account=%s and deleted_at is null) +
                     (select count(*) from coordination.uploads where account=%s and state='issued')) as total""",
                    (
                        account.id,
                        now.astimezone(UTC).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ),
                        account.id,
                        account.id,
                    ),
                )
            )[0]
            if (
                int(str(counts["daily"])) >= self._daily
                or int(str(counts["total"])) >= self._total
            ):
                raise ServiceError(
                    "image_quota_exceeded",
                    "The daily or stored image quota has been reached.",
                    429,
                )
            rows = await session.execute(
                """insert into coordination.uploads(id,account,key,size,hash,issued_at,expires_at)
                    values(%s,%s,%s,%s,%s,%s,%s) returning *""",
                (
                    command.id,
                    account.id,
                    image_key(account.id, command.id),
                    command.size,
                    command.hash,
                    now,
                    now + timedelta(seconds=self._seconds),
                ),
            )
            return self._slot(ImageUpload.model_validate(rows[0]), now)

    async def confirm(self, account: Actor, image_id: UUID) -> ImageResponse:
        async with self._database.read() as session:
            account = await resolve_actor(session, account)
            state = await operation(
                session, "image_preflight", (account.id, image_id), _Confirmation
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
        async with self._database.transaction() as session:
            state = await operation(
                session,
                "confirm_verified_image",
                (
                    account.id,
                    image_id,
                    upload.key,
                    upload.hash,
                    upload.size,
                    upload.issued_at,
                    valid,
                ),
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
        async with self._database.transaction() as session:
            (
                await session.execute(
                    "update coordination.uploads set state='deleted' where id=%s and state='pending'",
                    (image_id,),
                )
            )
        return True

    async def list_images(
        self, account: Actor, *, limit: int, offset: int, cursor: PageCursor | None = None
    ) -> list[ImageResponse]:
        async with self._database.read() as session:
            account = await resolve_actor(session, account)
            boundary = " and (created_at<%s or (created_at=%s and id>%s))" if cursor else ""
            parameters: list[object] = [account.id]
            if cursor:
                parameters.extend([cursor.timestamp, cursor.timestamp, cursor.id])
            parameters.extend([limit, offset])
            return [
                image_response(Image.model_validate(row))
                for row in (
                    await session.execute(
                        "select * from public.images where account=%s and deleted_at is null"+boundary+" order by created_at desc,id limit %s offset %s",
                        parameters,
                    )
                )
            ]

    async def by_hash(self, account: Actor, digest: str) -> ImageResponse:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            rows = await session.execute(
                "select * from public.images where account=%s and hash=%s and deleted_at is null order by id limit 1",
                (account.id, digest),
            )
            if not rows:
                raise NotFound()
            return image_response(Image.model_validate(rows[0]))

    async def _readable(
        self, session: Session, account: Account, image_id: UUID
    ) -> Image:
        rows = await session.execute(
            """select i.* from public.images i where i.id=%s and i.deleted_at is null
                and (i.account=%s or exists(select 1 from documents.images r
                    join documents.grants g on g.document_id=r.document_id
                    join documents.documents d on d.id=r.document_id
                    where r.image_id=i.id and g.account_id=%s and d.trashed_at is null))""",
            (image_id, account.id, account.id),
        )
        if not rows:
            raise NotFound()
        return Image.model_validate(rows[0])

    async def metadata(self, account: Actor, image_id: UUID) -> ImageResponse:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            return image_response(await self._readable(session, account, image_id))

    async def content(self, account: Actor, image_id: UUID) -> tuple[Image, ImageBody]:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            image = await self._readable(session, account, image_id)
        return image, (await self._stream(image))

    async def avatar(self, account_id: UUID) -> tuple[Image, ImageBody]:
        async with self._database.transaction() as session:
            rows = await session.execute(
                """select i.* from public.accounts a join public.images i on i.id=a.avatar_id
                where a.id=%s and i.deleted_at is null""",
                (account_id,),
            )
            if not rows:
                raise NotFound()
            image = Image.model_validate(rows[0])
        return image, (await self._stream(image))

    async def _stream(self, image: Image) -> ImageBody:
        try:
            body = await self._objects.stream(image.key)
        except ObjectMissing:
            raise NotFound() from None
        if body.size != image.size:
            (await body.close())
            raise StorageUnavailable()
        return body

    async def delete(self, account: Actor, image_id: UUID) -> None:
        async with self._database.transaction() as session:
            account = await resolve_actor(session, account)
            (
                await session.execute(
                    "select id from public.accounts where id=%s for update",
                    (account.id,),
                )
            )
            rows = await session.execute(
                "select * from public.images where id=%s and account=%s for update",
                (image_id, account.id),
            )
            if not rows:
                raise NotFound()
            image = Image.model_validate(rows[0])
            if await session.execute(
                """select image_id from documents.images where image_id=%s
                union all select avatar_id from public.accounts where avatar_id=%s limit 1""",
                (image_id, image_id),
            ):
                raise ServiceError(
                    "image_referenced",
                    "Images referenced by document history or a profile cannot be deleted.",
                )
            (
                await session.execute(
                    "update public.images set deleted_at=coalesce(deleted_at,clock_timestamp()) where id=%s",
                    (image_id,),
                )
            )
            (
                await session.execute(
                    """insert into coordination.uploads(id,account,key,size,hash,state,issued_at,expires_at)
                values(%s,%s,%s,%s,%s,'pending',%s,clock_timestamp())
                on conflict(id) do update set state='pending'""",
                    (
                        image.id,
                        image.account,
                        image.key,
                        image.size,
                        image.hash,
                        image.created_at,
                    ),
                )
            )
        (await self.discard(image.id, image.key))


class _Confirmation(Record):
    image: Image | None = None
    upload: ImageUpload | None = None
    invalid: bool = False
