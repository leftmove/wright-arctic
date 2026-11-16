"""In-memory object transports; PostgreSQL policy remains production code."""

from collections.abc import Iterator
from threading import Barrier

from app.schemas import ImageUploadResponse, UploadSlot
from app.services.storage.objects import (
    ImageBody,
    ObjectMetadata,
    ObjectMissing,
    StorageUnavailable,
)


class Checkpoints:
    def __init__(self) -> None:
        self.objects: dict[str, ObjectMetadata] = {}
        self.heads: list[str] = []
        self.deleted: list[str] = []
        self.fail_delete = False
        self.barrier: Barrier | None = None

    def close(self) -> None:
        pass

    def head(self, key: str) -> ObjectMetadata:
        self.heads.append(key)
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        if key not in self.objects:
            raise ObjectMissing()
        return self.objects[key]

    def upload(self, slot: UploadSlot) -> None:
        assert slot.upload is not None
        headers = slot.upload.headers
        self.objects[slot.key] = ObjectMetadata(
            int(headers["content-length"]),
            headers["content-type"],
            {"sha256": headers["x-amz-meta-sha256"]},
        )

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise StorageUnavailable()
        self.objects.pop(key, None)
        self.deleted.append(key)


class Images(Checkpoints):
    def __init__(self) -> None:
        super().__init__()
        self.bytes: dict[str, bytes] = {}
        self.prefixes: list[str] = []
        self.reads: list[str] = []

    def upload_image(
        self, slot: ImageUploadResponse, data: bytes, *, media_type: str = "image/avif"
    ) -> None:
        assert slot.upload is not None
        self.objects[slot.key] = ObjectMetadata(
            len(data), media_type, {"sha256": slot.upload.headers["x-amz-meta-sha256"]}
        )
        self.bytes[slot.key] = data

    def prefix(self, key: str, maximum: int) -> bytes:
        self.prefixes.append(key)
        return self.bytes[key][:maximum]

    def stream(self, key: str) -> ImageBody:
        self.reads.append(key)
        if key not in self.objects:
            raise ObjectMissing()
        data = self.bytes[key]

        def chunks() -> Iterator[bytes]:
            yield data

        return ImageBody(len(data), chunks(), lambda: None)

    def delete(self, key: str) -> None:
        super().delete(key)
        self.bytes.pop(key, None)
