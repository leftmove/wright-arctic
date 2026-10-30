"""Store processed images with the S3 API.

AWS S3 and Cloudflare R2 both use this client. The endpoint URL selects the
service. Checksums are calculated only when an operation requires them,
because botocore's default full-object CRC32 is not accepted by R2.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import quote
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr

from app.accounts import Account
from app.services.images.errors import (
    ImageNotFound,
    StorageInvalid,
    StorageUnavailable,
    StoredImageTooLarge,
)
from app.services.images.limits import MAX_DECODE_EDGE, MAX_STORED_BYTES
from app.services.images.models import (
    ImageExtension,
    ImageMediaType,
    ImageObject,
    ProcessedImage,
)
from app.settings import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import (
        HeadObjectOutputTypeDef,
        ListObjectsV2OutputTypeDef,
        ObjectTypeDef,
        PutObjectRequestTypeDef,
    )

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 3
_READ_TIMEOUT_SECONDS = 20
_RETRY_ATTEMPTS = 3
_LIST_PAGE_SIZE = 1000
_MAX_LIST_PAGES = 20
_OBJECT_KEY = re.compile(
    r"^accounts/"
    r"(?P<account>[0-9a-f]{64})"
    r"/images/"
    r"(?P<image>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.(?P<ext>avif|webp)$"
)
_MEDIA_TYPES: dict[ImageExtension, ImageMediaType] = {
    "avif": "image/avif",
    "webp": "image/webp",
}
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_T = TypeVar("_T")


class S3ImageStore:
    """Images for one bucket, addressed by account and image id."""

    def __init__(
        self,
        client: S3Client,
        bucket: str,
        public_base_url: str | None,
        presign_seconds: int,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._public_base_url = public_base_url
        self._presign_seconds = presign_seconds

    @property
    def url_expires_in(self) -> int | None:
        """Seconds a generated URL remains valid, or None for a public URL."""
        if self._public_base_url is not None:
            return None
        return self._presign_seconds

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def put_image(
        self,
        account: Account,
        image_id: UUID,
        image: ProcessedImage,
        *,
        if_absent: bool = False,
    ) -> bool:
        """Write one recompressed image."""
        if len(image.data) > MAX_STORED_BYTES:
            raise StoredImageTooLarge()
        key = object_key(account, image_id, image.extension)
        cache_control = (
            "public, max-age=31536000, immutable"
            if self._public_base_url is not None
            else "private, max-age=86400"
        )

        def _put() -> bool:
            request: PutObjectRequestTypeDef = {
                "Bucket": self._bucket,
                "Key": key,
                "Body": image.data,
                "ContentType": image.media_type,
                "CacheControl": cache_control,
                "ContentDisposition": "inline",
                "Metadata": {"width": str(image.width), "height": str(image.height)},
            }
            if if_absent:
                request["IfNoneMatch"] = "*"
            try:
                self._client.put_object(**request)
            except ClientError as exc:
                if if_absent and _http_status(exc) == 412:
                    return False
                raise
            return True

        return self._invoke(_put)

    def discard_image(
        self, account: Account, image_id: UUID, extension: ImageExtension
    ) -> None:
        """Delete an exact key written by this process."""
        key = object_key(account, image_id, extension)

        def _delete() -> None:
            self._client.delete_object(Bucket=self._bucket, Key=key)

        self._invoke(_delete)

    def delete_image(self, account: Account, image_id: UUID) -> None:
        """Delete one stored image, or raise when it is not in the account."""
        stored = self.get_image(account, image_id)
        self.discard_image(account, image_id, stored.extension)

    def get_image(self, account: Account, image_id: UUID) -> ImageObject:
        """Read metadata for one stored image."""
        for extension in ("avif", "webp"):
            key = object_key(account, image_id, extension)

            def _head(object_key: str = key) -> HeadObjectOutputTypeDef:
                return self._client.head_object(Bucket=self._bucket, Key=object_key)

            try:
                response = self._invoke(_head, missing_ok=True)
            except ImageNotFound:
                continue
            return _object_from_head(image_id, extension, response)
        raise ImageNotFound()

    def list_images(self, account: Account, *, limit: int) -> list[ImageObject]:
        """Return up to `limit` images for an account, oldest first.

        Listing order is lexicographic, so the prefix is read and then sorted
        by creation time. The result is the oldest objects, not the first keys.
        """
        if limit < 1:
            return []
        found = list(self._iter_images(account))
        found.sort(key=_listing_order)
        return found[:limit]

    def count_images(self, account: Account, *, maximum: int) -> int:
        """Count stored images, stopping once `maximum` is reached."""
        if maximum < 1:
            return 0
        counted = 0
        for _image in self._iter_images(account):
            counted += 1
            if counted >= maximum:
                break
        return counted

    def _iter_images(self, account: Account) -> Iterator[ImageObject]:
        token: str | None = None
        prefix = account_prefix(account)
        for _ in range(_MAX_LIST_PAGES):
            page = self._list_page(prefix, _LIST_PAGE_SIZE, token)
            contents = page.get("Contents")
            if contents is not None:
                for item in contents:
                    image = _object_from_listing(account, item)
                    if image is not None:
                        yield image
            if not page.get("IsTruncated", False):
                return
            next_token = page.get("NextContinuationToken")
            if (
                not isinstance(next_token, str)
                or next_token == ""
                or next_token == token
            ):
                return
            token = next_token

    def url_for(
        self, account: Account, image_id: UUID, extension: ImageExtension
    ) -> str:
        """Return the URL a client uses to fetch the object."""
        key = object_key(account, image_id, extension)
        if self._public_base_url is not None:
            return f"{self._public_base_url}/{quote(key, safe='/')}"

        def _presign() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._presign_seconds,
            )

        return self._invoke(_presign)

    def _list_page(
        self, prefix: str, max_keys: int, token: str | None
    ) -> ListObjectsV2OutputTypeDef:
        if token is None:

            def _list_first() -> ListObjectsV2OutputTypeDef:
                return self._client.list_objects_v2(
                    Bucket=self._bucket,
                    Prefix=prefix,
                    MaxKeys=max_keys,
                )

            return self._invoke(_list_first)

        def _list_next(continuation: str = token) -> ListObjectsV2OutputTypeDef:
            return self._client.list_objects_v2(
                Bucket=self._bucket,
                Prefix=prefix,
                MaxKeys=max_keys,
                ContinuationToken=continuation,
            )

        return self._invoke(_list_next)

    def _invoke(self, func: Callable[[], _T], *, missing_ok: bool = False) -> _T:
        try:
            return func()
        except ClientError as exc:
            if missing_ok and _missing(exc):
                raise ImageNotFound() from None
            _log_client_error(exc)
            raise StorageUnavailable() from None
        except BotoCoreError as exc:
            logger.warning("object storage request failed (%s)", type(exc).__name__)
            raise StorageUnavailable() from None


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


def object_key(account: Account, image_id: UUID, extension: ImageExtension) -> str:
    """Return the bucket key for one image."""
    return f"{account_prefix(account)}{image_id}.{extension}"


def account_prefix(account: Account) -> str:
    """Return the key prefix that contains one account's images."""
    return f"accounts/{account.storage_key}/images/"


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
    response: HeadObjectOutputTypeDef,
) -> ImageObject:
    try:
        byte_size = response["ContentLength"]
        modified = response["LastModified"]
        metadata = response["Metadata"]
    except KeyError:
        raise StorageInvalid() from None
    if byte_size < 1:
        raise StorageInvalid()
    return ImageObject(
        id=image_id,
        media_type=_MEDIA_TYPES[extension],
        extension=extension,
        byte_size=byte_size,
        created_at=_aware(modified),
        width=_dimension(metadata, "width"),
        height=_dimension(metadata, "height"),
    )


def _object_from_listing(account: Account, item: ObjectTypeDef) -> ImageObject | None:
    key = item.get("Key")
    size = item.get("Size")
    modified = item.get("LastModified")
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
    if extension == "avif":
        return image_id, "avif"
    if extension == "webp":
        return image_id, "webp"
    return None


def _dimension(metadata: dict[str, str], name: str) -> int:
    raw = metadata.get(name)
    if raw is None or not raw.isdigit():
        raise StorageInvalid()
    value = int(raw)
    if value < 1 or value > MAX_DECODE_EDGE:
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
