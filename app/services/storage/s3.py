"""S3/R2 transport for native ASGI hosts. No put/list/multipart API is exposed."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from app.services.limits import MAX_IMAGE_BYTES
from app.services.storage.objects import (
    ImageBody,
    ObjectMetadata,
    ObjectMissing,
    StorageUnavailable,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class S3Objects:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def close(self) -> None:
        self._client.close()

    def _invoke[T](self, call: Callable[[], T]) -> T:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return call()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise ObjectMissing() from None
            raise StorageUnavailable() from None
        except BotoCoreError, OSError:
            raise StorageUnavailable() from None

    def head(self, key: str) -> ObjectMetadata:
        value = self._invoke(
            lambda: self._client.head_object(Bucket=self._bucket, Key=key)
        )
        return ObjectMetadata(
            value["ContentLength"],
            value.get("ContentType", ""),
            value.get("Metadata", {}),
        )

    def delete(self, key: str) -> None:
        self._invoke(lambda: self._client.delete_object(Bucket=self._bucket, Key=key))

    def prefix(self, key: str, maximum: int) -> bytes:
        value = self._invoke(
            lambda: self._client.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes=0-{maximum - 1}"
            )
        )
        body = value["Body"]
        try:
            return self._invoke(lambda: body.read(maximum))
        finally:
            body.close()

    def stream(self, key: str) -> ImageBody:
        value = self._invoke(
            lambda: self._client.get_object(Bucket=self._bucket, Key=key)
        )
        body = value["Body"]
        size = value["ContentLength"]
        if not 0 < size <= MAX_IMAGE_BYTES or value.get("ContentType") != "image/avif":
            body.close()
            raise StorageUnavailable()

        def chunks() -> Iterator[bytes]:
            remaining = size
            while remaining:
                chunk = self._invoke(lambda: body.read(65536))
                if not chunk or len(chunk) > remaining:
                    raise StorageUnavailable()
                remaining -= len(chunk)
                yield chunk

        return ImageBody(size, chunks(), body.close)
