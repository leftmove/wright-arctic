"""R2 binding transport. Image policy and object keys stay provider independent."""

import importlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from app.services.images.errors import ImageNotFound, StorageInvalid, StorageUnavailable
from app.services.images.objects import ObjectPage, StoredObject
from app.services.images.signing import S3UrlSigner

logger = logging.getLogger(__name__)


class Buffer(Protocol):
    def to_bytes(self) -> bytes: ...


class JSDate(Protocol):
    def getTime(self) -> float: ...


class R2Object(Protocol):
    key: str
    size: int
    uploaded: datetime | JSDate
    customMetadata: Mapping[str, str] | None


class R2Body(R2Object, Protocol):
    def arrayBuffer(self) -> Awaitable[memoryview]: ...


class R2Listing(Protocol):
    objects: Sequence[R2Object]
    truncated: bool
    cursor: str


class R2Bucket(Protocol):
    def head(self, key: str) -> Awaitable[R2Object | None]: ...
    def get(self, key: str, *, range: dict[str, int]) -> Awaitable[R2Body | None]: ...
    def put(
        self,
        key: str,
        payload: bytes,
        *,
        httpMetadata: dict[str, str],
        customMetadata: dict[str, str],
        onlyIf: dict[str, str],
    ) -> Awaitable[R2Object | None]: ...
    def delete(self, key: str) -> Awaitable[None]: ...
    def list(
        self, *, prefix: str, limit: int, cursor: str = ...
    ) -> Awaitable[R2Listing]: ...


class RunSync(Protocol):
    def __call__[T](self, value: Awaitable[T]) -> T: ...


class R2ObjectStorage:
    def __init__(self, bucket: R2Bucket, signer: S3UrlSigner) -> None:
        self._bucket = bucket
        self._signer = signer
        # JSPI cooperatively suspends the synchronous domain service during I/O.
        self._run_sync = cast(RunSync, importlib.import_module("pyodide.ffi").run_sync)

    def close(self) -> None:
        pass

    def _invoke[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            return self._run_sync(operation())
        except Exception as error:
            logger.warning("object storage request failed (%s)", type(error).__name__)
            raise StorageUnavailable() from None

    def head(self, key: str) -> StoredObject:
        result = self._invoke(lambda: self._bucket.head(key))
        if result is None:
            raise ImageNotFound()
        return _stored(result)

    def read(self, key: str, maximum: int) -> bytes:
        result = self._invoke(
            lambda: self._bucket.get(key, range={"offset": 0, "length": maximum + 1})
        )
        if result is None:
            raise ImageNotFound()
        if result.size > maximum:
            raise StorageInvalid()
        value = bytes(self._invoke(result.arrayBuffer))
        if len(value) > maximum or len(value) != result.size:
            raise StorageInvalid()
        return value

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
        result = self._invoke(
            lambda: self._bucket.put(
                key,
                payload,
                httpMetadata={
                    "contentType": media_type,
                    "cacheControl": cache_control,
                    "contentDisposition": "inline",
                },
                customMetadata=metadata,
                onlyIf={"etagDoesNotMatch": "*"} if if_absent else {},
            )
        )
        return result is not None

    def delete(self, key: str) -> None:
        self._invoke(lambda: self._bucket.delete(key))

    def list(self, prefix: str, limit: int, cursor: str | None) -> ObjectPage:
        result = (
            self._invoke(lambda: self._bucket.list(prefix=prefix, limit=limit))
            if cursor is None
            else self._invoke(
                lambda: self._bucket.list(prefix=prefix, limit=limit, cursor=cursor)
            )
        )
        return ObjectPage(
            [_stored(item) for item in result.objects],
            result.cursor if result.truncated else None,
        )

    def signed_url(self, key: str, seconds: int) -> str:
        return self._signer.sign(key, seconds)


def _stored(value: R2Object) -> StoredObject:
    modified = value.uploaded
    if not isinstance(modified, datetime):
        modified = datetime.fromtimestamp(modified.getTime() / 1000, UTC)
    modified = (
        modified.replace(tzinfo=UTC)
        if modified.tzinfo is None
        else modified.astimezone(UTC)
    )
    return StoredObject(value.key, value.size, modified, value.customMetadata or {})
