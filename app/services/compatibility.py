"""Explicit legacy Worker/test ports. Native production never imports this module."""

from collections.abc import Sequence

from app.execution import Executor
from app.services.database import Database, Row
from app.services.storage.objects import (
    CheckpointObjects,
    ImageObjects,
    ObjectMetadata,
)


class LegacyDatabase:
    def __init__(self, database: Database, executor: Executor) -> None:
        self.database, self.executor = database, executor

    async def open(self) -> None:
        await self.executor.run(self.database.open)

    async def close(self) -> None:
        await self.executor.run(self.database.close)

    async def fetch(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        return await self.executor.run(self.database.fetch, query, parameters)

    async def write(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        return await self.executor.run(self.database.write, query, parameters)


class LegacyCheckpoints:
    def __init__(self, objects: CheckpointObjects, executor: Executor) -> None:
        self.objects, self.executor = objects, executor

    async def head(self, key: str) -> ObjectMetadata:
        return await self.executor.run(self.objects.head, key)

    async def delete(self, key: str) -> None:
        await self.executor.run(self.objects.delete, key)


class LegacyImages(LegacyCheckpoints):
    def __init__(self, objects: ImageObjects, executor: Executor) -> None:
        super().__init__(objects, executor)
        self.images = objects

    async def prefix(self, key: str, maximum: int) -> bytes:
        return await self.executor.run(self.images.prefix, key, maximum)
