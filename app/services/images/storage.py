"""Store processed images with the S3 API.

AWS S3 and Cloudflare R2 both use this client. The endpoint URL selects the
service. Checksums are calculated only when an operation requires them,
because botocore's default full-object CRC32 is not accepted by R2.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import quote
from uuid import UUID

from pydantic import SecretStr

from app.accounts import Account
from app.services.images.content import SOURCE_SHA256_PATTERN
from app.services.images.errors import (
    ImageNotFound,
    StorageInvalid,
    StorageUnavailable,
    StoredImageTooLarge,
)
from app.services.images.limits import MAX_STORED_BYTES
from app.services.images.models import (
    ImageExtension,
    ImageMediaType,
    ImageObject,
    ProcessedImage,
)
from app.services.images.objects import ObjectPage, ObjectStorage, StoredObject
from app.settings import Settings

if TYPE_CHECKING:
    from botocore.exceptions import ClientError
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import (
        ListObjectsV2OutputTypeDef,
        PutObjectRequestTypeDef,
    )

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 3
_READ_TIMEOUT_SECONDS = 20
_RETRY_ATTEMPTS = 3
_LIST_PAGE_SIZE = 1000
_MAX_LIST_PAGES = 20
_OBJECT_KEY = re.compile(
    r"^(?P<account>[0-9a-f]{64})"
    r"/images/"
    r"(?P<image>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.(?P<ext>jxl|avif|webp)$"
)
_MEDIA_TYPES: dict[ImageExtension, ImageMediaType] = {
    "jxl": "image/jxl",
    "avif": "image/avif",
    "webp": "image/webp",
}
_EXTENSIONS: tuple[ImageExtension, ...] = ("jxl", "avif", "webp")
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_T = TypeVar("_T")


class ImageStore:
    """Images for one bucket, addressed by account and image id."""

    def __init__(
        self, objects: ObjectStorage, public_base_url: str | None, presign_seconds: int
    ) -> None:
        self._objects = objects
        self._public_base_url = public_base_url
        self._presign_seconds = presign_seconds

    @property
    def url_expires_in(self) -> int | None:
        """Seconds a generated URL remains valid, or None for a public URL."""
        if self._public_base_url is not None:
            return None
        return self._presign_seconds

    def close(self) -> None:
        self._objects.close()

    def open(self) -> None:
        """Check bucket access without reading account objects."""
        with suppress(ImageNotFound):
            self._objects.head(".readiness")

    def portrait_id(self, account: Account) -> UUID | None:
        try:
            value = self._objects.read(f"{account.storage_key}/portrait", 36)
        except ImageNotFound:
            return None
        if len(value) != 36:
            raise StorageInvalid()
        try:
            return UUID(value.decode("ascii"))
        except ValueError, UnicodeError:
            raise StorageInvalid() from None

    def select_portrait(
        self, account: Account, image_id: UUID, *, if_absent: bool = False
    ) -> None:
        self.get_image(account, image_id)
        self._objects.put(
            f"{account.storage_key}/portrait",
            str(image_id).encode("ascii"),
            media_type="text/plain",
            cache_control="private, no-store",
            metadata={},
            if_absent=if_absent,
        )

    def put_image(
        self,
        account: Account,
        image_id: UUID,
        image: ProcessedImage,
        *,
        if_absent: bool = False,
        variant: bool = False,
        created_at: datetime | None = None,
        source_sha256: str | None = None,
    ) -> bool:
        if len(image.data) > MAX_STORED_BYTES:
            raise StoredImageTooLarge()
        metadata = {
            "width": str(image.width),
            "height": str(image.height),
            "channels": str(image.channels),
        }
        if created_at is not None:
            metadata["created_at"] = created_at.isoformat()
        if source_sha256 is not None:
            metadata["source_sha256"] = source_sha256
        return self._objects.put(
            object_key(account, image_id, image.extension, variant=variant),
            image.data,
            media_type=image.media_type,
            cache_control="public, max-age=31536000, immutable"
            if self._public_base_url
            else "private, max-age=86400",
            metadata=metadata,
            if_absent=if_absent,
        )

    def discard_image(
        self, account: Account, image_id: UUID, extension: ImageExtension
    ) -> None:
        self._objects.delete(object_key(account, image_id, extension))

    def delete_image(self, account: Account, image_id: UUID) -> None:
        self.get_image(account, image_id)
        for extension in ("avif", "webp"):
            self._objects.delete(object_key(account, image_id, extension, variant=True))
        for extension in _EXTENSIONS:
            self.discard_image(account, image_id, extension)

    def get_image(self, account: Account, image_id: UUID) -> ImageObject:
        """Read metadata for one stored image."""
        for extension in _EXTENSIONS:
            try:
                return self.get_representation(account, image_id, extension)
            except ImageNotFound:
                continue
        raise ImageNotFound()

    def get_representation(
        self,
        account: Account,
        image_id: UUID,
        extension: ImageExtension,
        *,
        variant: bool = False,
    ) -> ImageObject:
        stored = self._objects.head(
            object_key(account, image_id, extension, variant=variant)
        )
        return replace(_object_from_head(image_id, extension, stored), variant=variant)

    def read_image(self, account: Account, image: ImageObject) -> bytes:
        if image.byte_size > MAX_STORED_BYTES:
            raise StorageInvalid()
        value = self._objects.read(
            object_key(account, image.id, image.extension, variant=image.variant),
            MAX_STORED_BYTES,
        )
        if len(value) != image.byte_size:
            raise StorageInvalid()
        return value

    def list_images(self, account: Account, *, limit: int) -> list[ImageObject]:
        """Return up to `limit` images for an account, oldest first.

        Listing order is lexicographic, so the prefix is read and then sorted
        by creation time. The result is the oldest objects, not the first keys.
        """
        if limit < 1:
            return []
        found: dict[UUID, ImageObject] = {}
        for image in self._iter_images(account):
            previous = found.get(image.id)
            if previous is None:
                found[image.id] = image
            else:
                canonical = image if image.extension == "jxl" else previous
                found[image.id] = replace(
                    canonical, created_at=min(previous.created_at, image.created_at)
                )
        ordered = list(found.values())
        ordered.sort(key=_listing_order)
        return ordered[:limit]

    def count_images(self, account: Account, *, maximum: int) -> int:
        """Count stored images, stopping once `maximum` is reached."""
        if maximum < 1:
            return 0
        counted: set[UUID] = set()
        for _image in self._iter_images(account):
            counted.add(_image.id)
            if len(counted) >= maximum:
                break
        return len(counted)

    def _iter_images(self, account: Account) -> Iterator[ImageObject]:
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            page = self._objects.list(account_prefix(account), _LIST_PAGE_SIZE, cursor)
            for item in page.objects:
                image = _object_from_listing(account, item)
                if image is not None:
                    yield image
            if page.cursor is None:
                return
            if page.cursor == cursor:
                raise StorageInvalid()
            cursor = page.cursor
        raise StorageUnavailable()

    def url_for(
        self,
        account: Account,
        image_id: UUID,
        extension: ImageExtension,
        *,
        variant: bool = False,
    ) -> str:
        key = object_key(account, image_id, extension, variant=variant)
        if self._public_base_url is not None:
            return f"{self._public_base_url}/{quote(key, safe='/')}"
        return self._objects.signed_url(key, self._presign_seconds)


class S3ObjectStorage:
    """The portable S3 transport; the Worker uses a binding instead."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def close(self) -> None:
        self._client.close()

    def head(self, key: str) -> StoredObject:
        result = self._invoke(
            lambda: self._client.head_object(Bucket=self._bucket, Key=key)
        )
        return StoredObject(
            key, result["ContentLength"], result["LastModified"], result["Metadata"]
        )

    def read(self, key: str, maximum: int) -> bytes:
        def read() -> bytes:
            result = self._client.get_object(Bucket=self._bucket, Key=key)
            with result["Body"] as body:
                value = body.read(maximum + 1)
            if len(value) > maximum:
                raise StorageInvalid()
            return value

        return self._invoke(read)

    def put(
        self,
        key: str,
        payload: bytes,
        *,
        media_type: str,
        cache_control: str,
        metadata: dict[str, str],
        if_absent: bool = False,
    ) -> bool:
        from botocore.exceptions import ClientError

        def put() -> bool:
            parameters: PutObjectRequestTypeDef = {
                "Bucket": self._bucket,
                "Key": key,
                "Body": payload,
                "ContentType": media_type,
                "CacheControl": cache_control,
                "ContentDisposition": "inline",
                "Metadata": metadata,
            }
            if if_absent:
                parameters["IfNoneMatch"] = "*"
            try:
                self._client.put_object(**parameters)
            except ClientError as error:
                if if_absent and _http_status(error) in {409, 412}:
                    return False
                raise
            return True

        return self._invoke(put)

    def delete(self, key: str) -> None:
        self._invoke(lambda: self._client.delete_object(Bucket=self._bucket, Key=key))

    def list(self, prefix: str, limit: int, cursor: str | None) -> ObjectPage:
        def listing() -> ListObjectsV2OutputTypeDef:
            if cursor is None:
                return self._client.list_objects_v2(
                    Bucket=self._bucket, Prefix=prefix, MaxKeys=limit
                )
            return self._client.list_objects_v2(
                Bucket=self._bucket,
                Prefix=prefix,
                MaxKeys=limit,
                ContinuationToken=cursor,
            )

        result = self._invoke(listing)
        objects = [
            StoredObject(item["Key"], item["Size"], item["LastModified"])
            for item in result.get("Contents", [])
            if "Key" in item and "Size" in item and "LastModified" in item
        ]
        next_cursor = (
            result.get("NextContinuationToken") if result.get("IsTruncated") else None
        )
        if result.get("IsTruncated") and not next_cursor:
            raise StorageInvalid()
        return ObjectPage(objects, next_cursor)

    def signed_url(self, key: str, seconds: int) -> str:
        return self._invoke(
            lambda: self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=seconds,
            )
        )

    def _invoke(self, operation: Callable[[], _T]) -> _T:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return operation()
        except ClientError as error:
            if _missing(error):
                raise ImageNotFound() from None
            _log_client_error(error)
            raise StorageUnavailable() from None
        except BotoCoreError as error:
            logger.warning("object storage request failed (%s)", type(error).__name__)
            raise StorageUnavailable() from None


class S3ImageStore(ImageStore):
    def __init__(
        self,
        client: S3Client,
        bucket: str,
        public_base_url: str | None,
        presign_seconds: int,
    ) -> None:
        super().__init__(
            S3ObjectStorage(client, bucket), public_base_url, presign_seconds
        )


def build_image_store(settings: Settings) -> S3ImageStore | None:
    """Build a store when storage settings are complete, otherwise None."""
    endpoint = settings.storage_endpoint_url
    region = settings.storage_region
    bucket = settings.storage_bucket
    access_key = _secret_text(settings.storage_access_key_id)
    secret_key = _secret_text(settings.storage_secret_access_key)
    if (
        endpoint is None
        or region is None
        or bucket is None
        or access_key is None
        or secret_key is None
    ):
        return None
    import boto3
    from botocore.config import Config

    public = settings.storage_public_base_url
    session = boto3.session.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    client = session.client(
        "s3",
        endpoint_url=str(endpoint).rstrip("/"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.storage_addressing_style},
            retries={"max_attempts": _RETRY_ATTEMPTS, "mode": "standard"},
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_READ_TIMEOUT_SECONDS,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    public_base = None if public is None else str(public).rstrip("/")
    return S3ImageStore(
        client=client,
        bucket=bucket,
        public_base_url=public_base,
        presign_seconds=settings.storage_presign_seconds,
    )


def object_key(
    account: Account,
    image_id: UUID,
    extension: ImageExtension,
    *,
    variant: bool = False,
) -> str:
    """Return the bucket key for one image."""
    suffix = (
        f"{image_id}/variants/v1.{extension}" if variant else f"{image_id}.{extension}"
    )
    return f"{account_prefix(account)}{suffix}"


def account_prefix(account: Account) -> str:
    """Return the key prefix that contains one account's images."""
    return f"{account.storage_key}/images/"


def _secret_text(value: SecretStr | None) -> str | None:
    if value is None:
        return None
    text = value.get_secret_value()
    if text == "":
        return None
    return text


def _object_from_head(
    image_id: UUID,
    extension: ImageExtension,
    response: StoredObject,
) -> ImageObject:
    byte_size = response.size
    modified = response.modified
    metadata = response.metadata
    if byte_size < 1:
        raise StorageInvalid()
    created_at = _aware(modified)
    if "created_at" in metadata:
        try:
            created_at = _aware(datetime.fromisoformat(metadata["created_at"]))
        except ValueError:
            raise StorageInvalid() from None
    source_sha256 = metadata.get("source_sha256")
    if (
        source_sha256 is not None
        and re.fullmatch(SOURCE_SHA256_PATTERN, source_sha256) is None
    ):
        raise StorageInvalid()
    return ImageObject(
        id=image_id,
        media_type=_MEDIA_TYPES[extension],
        extension=extension,
        byte_size=byte_size,
        created_at=created_at,
        width=_dimension(metadata, "width"),
        height=_dimension(metadata, "height"),
        channels=_dimension(metadata, "channels") if extension == "jxl" else None,
        source_sha256=source_sha256,
    )


def _object_from_listing(account: Account, item: StoredObject) -> ImageObject | None:
    key = item.key
    size = item.size
    modified = item.modified
    if key is None or size is None or size < 1 or modified is None:
        return None
    parsed = _parse_object_key(key, account)
    if parsed is None:
        return None
    image_id, extension = parsed
    return ImageObject(
        id=image_id,
        media_type=_MEDIA_TYPES[extension],
        extension=extension,
        byte_size=size,
        created_at=_aware(modified),
    )


def _parse_object_key(key: str, account: Account) -> tuple[UUID, ImageExtension] | None:
    match = _OBJECT_KEY.fullmatch(key)
    if match is None:
        return None
    try:
        owner = match.group("account")
        image_id = UUID(match.group("image"))
    except ValueError:
        return None
    if owner != account.storage_key:
        return None
    extension = match.group("ext")
    if extension == "jxl":
        return image_id, "jxl"
    if extension == "avif":
        return image_id, "avif"
    if extension == "webp":
        return image_id, "webp"
    return None


def _dimension(metadata: Mapping[str, str], name: str) -> int:
    raw = metadata.get(name)
    if raw is None or not raw.isdigit():
        raise StorageInvalid()
    value = int(raw)
    # Stored metadata also describes images written before today's decode caps.
    if value < 1 or value > 65535:
        raise StorageInvalid()
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _listing_order(image: ImageObject) -> tuple[datetime, str]:
    return (image.created_at, str(image.id))


def _missing(exc: ClientError) -> bool:
    return _http_status(exc) == 404 or _error_code(exc) in _MISSING_CODES


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error")
    if error is None:
        return ""
    return error.get("Code", "")


def _http_status(exc: ClientError) -> int | None:
    metadata = exc.response.get("ResponseMetadata")
    if metadata is None:
        return None
    return metadata.get("HTTPStatusCode")


def _log_client_error(exc: ClientError) -> None:
    code = _error_code(exc) or "unknown"
    logger.warning("object storage request failed (%s)", code)
