"""Separate checkpoint metadata access from image byte delivery."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from app.services.errors import ServiceError


class ObjectMissing(ServiceError):
    def __init__(self) -> None:
        super().__init__("upload_missing", "The object has not been uploaded.", 409)


class StorageUnavailable(ServiceError):
    def __init__(self) -> None:
        super().__init__("storage_unavailable", "Object storage is unavailable.", 503)


@dataclass(frozen=True)
class ObjectMetadata:
    size: int
    media_type: str
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class ImageBody:
    size: int
    chunks: Iterator[bytes]
    close: Callable[[], None]


class CheckpointObjects(Protocol):
    """The sync service cannot fetch document bytes through this interface."""

    def head(self, key: str) -> ObjectMetadata: ...
    def delete(self, key: str) -> None: ...
    def close(self) -> None: ...


class ImageObjects(CheckpointObjects, Protocol):
    def prefix(self, key: str, maximum: int) -> bytes: ...
    def stream(self, key: str) -> ImageBody: ...
