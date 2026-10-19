"""Image values shared by processing and storage."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

type ImageMediaType = Literal["image/avif", "image/webp"]
type ImageExtension = Literal["avif", "webp"]


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """Bytes that are safe to store, with the pixel size after recompression."""

    data: bytes
    width: int
    height: int
    media_type: ImageMediaType
    extension: ImageExtension


@dataclass(frozen=True, slots=True)
class ImageObject:
    """An image object that already lives in storage."""

    id: UUID
    media_type: ImageMediaType
    extension: ImageExtension
    byte_size: int
    created_at: datetime
    width: int | None = None
    height: int | None = None
