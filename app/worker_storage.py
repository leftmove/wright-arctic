"""R2 binding transport. Intentionally exposes no writes, listings, or multipart API."""

import importlib
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from app.services.limits import MAX_IMAGE_BYTES
from app.services.storage.objects import (
    ImageBody,
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


class ReadResult(Protocol):
    done: bool
    value: Buffer


class Reader(Protocol):
    def read(self) -> Awaitable[ReadResult]: ...
    def cancel(self) -> Awaitable[None]: ...
    def releaseLock(self) -> None: ...


class ReadableStream(Protocol):
    def getReader(self) -> Reader: ...
    def cancel(self) -> Awaitable[None]: ...


class R2Body(R2Object, Protocol):
    body: ReadableStream

    def arrayBuffer(self) -> Awaitable[memoryview]: ...


class R2Bucket(Protocol):
    def head(self, key: str) -> Awaitable[R2Object | None]: ...
    def get(
        self, key: str, *, range: dict[str, int] = ...
    ) -> Awaitable[R2Body | None]: ...
    def delete(self, key: str) -> Awaitable[None]: ...


@dataclass(frozen=True)
class R2ImageBody(ImageBody):
    native_stream: ReadableStream


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

    def stream(self, key: str) -> ImageBody:
        value = self._invoke(lambda: self._bucket.get(key))
        if value is None:
            raise ObjectMissing()
        if (
            not 0 < value.size <= MAX_IMAGE_BYTES
            or value.httpMetadata.contentType != "image/avif"
        ):
            raise StorageUnavailable()
        reader: Reader | None = None
        finished = False

        def close() -> None:
            nonlocal finished
            if not finished:
                if reader is None:
                    self._invoke(value.body.cancel)
                else:
                    self._invoke(reader.cancel)
                    reader.releaseLock()
                finished = True

        def chunks() -> Iterator[bytes]:
            nonlocal reader
            reader = value.body.getReader()
            remaining = value.size
            while remaining:
                chunk = self._invoke(reader.read)
                if chunk.done:
                    raise StorageUnavailable()
                data = chunk.value.to_bytes()
                remaining -= len(data)
                if remaining < 0:
                    raise StorageUnavailable()
                yield data

        return R2ImageBody(value.size, chunks(), close, value.body)
