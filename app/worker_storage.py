"""R2 binding transport. Intentionally exposes no writes, listings, or multipart API."""

import importlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from app.services.storage.objects import (
    ObjectMetadata,
    ObjectMissing,
    StorageUnavailable,
)


class Buffer(Protocol):
    def to_bytes(self) -> bytes: ...


class HTTPMetadata(Protocol):
    contentType: str


class R2Object(Protocol):
    size: int
    httpMetadata: HTTPMetadata
    customMetadata: Mapping[str, str] | None


class R2Body(R2Object, Protocol):
    def arrayBuffer(self) -> Awaitable[memoryview]: ...


class R2Bucket(Protocol):
    def head(self, key: str) -> Awaitable[R2Object | None]: ...
    def get(
        self, key: str, *, range: dict[str, int] = ...
    ) -> Awaitable[R2Body | None]: ...
    def delete(self, key: str) -> Awaitable[None]: ...


class Wait(Protocol):
    def __call__[T](self, value: Awaitable[T]) -> T: ...


class R2Objects:
    def __init__(self, bucket: R2Bucket) -> None:
        self._bucket = bucket
        self._wait = cast(Wait, importlib.import_module("pyodide.ffi").run_sync)

    def close(self) -> None:
        pass

    def _invoke[T](self, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return self._wait(call())
        except Exception:
            raise StorageUnavailable() from None

    def head(self, key: str) -> ObjectMetadata:
        value = self._invoke(lambda: self._bucket.head(key))
        if value is None:
            raise ObjectMissing()
        return ObjectMetadata(
            value.size, value.httpMetadata.contentType, value.customMetadata or {}
        )

    def delete(self, key: str) -> None:
        self._invoke(lambda: self._bucket.delete(key))

    def prefix(self, key: str, maximum: int) -> bytes:
        value = self._invoke(
            lambda: self._bucket.get(key, range={"offset": 0, "length": maximum})
        )
        if value is None:
            raise ObjectMissing()
        prefix = bytes(self._invoke(value.arrayBuffer))
        if len(prefix) > maximum:
            raise StorageUnavailable()
        return prefix
