"""The object operations needed by image storage, independent of its provider."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    modified: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectPage:
    objects: list[StoredObject]
    cursor: str | None = None


class ObjectStorage(Protocol):
    def close(self) -> None: ...
    def head(self, key: str) -> StoredObject: ...
    def read(self, key: str, maximum: int) -> bytes: ...
    def put(
        self,
        key: str,
        payload: bytes,
        *,
        media_type: str,
        cache_control: str,
        metadata: dict[str, str],
        if_absent: bool = False,
    ) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str, limit: int, cursor: str | None) -> ObjectPage: ...
    def signed_url(self, key: str, seconds: int) -> str: ...
