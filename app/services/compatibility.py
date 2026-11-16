"""Explicit legacy Worker/test ports. Native production never imports this module."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from app.execution import Executor
from app.services.database import AsyncSession, Database, Row, Session
from app.services.storage.objects import (
    AsyncImageBody,
    CheckpointObjects,
    ImageObjects,
    ObjectMetadata,
)


class LegacySession:
    def __init__(self, session: Session, executor: Executor) -> None:
        self._session, self._executor = session, executor

    async def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        return await self._executor.run(self._session.execute, query, parameters)


class LegacyDatabase:
    def __init__(self, database: Database, executor: Executor) -> None:
        self.database, self.executor = database, executor

    async def open(self) -> None:
        await self.executor.run(self.database.open)

    async def close(self) -> None:
        await self.executor.run(self.database.close)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        # JSPI and fixtures use InlineExecutor. Threads must own a complete
        # legacy transaction, so do not use this port for a native DB pool.
        with self.database.transaction() as session:
            yield LegacySession(session, self.executor)

    read = transaction


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

    async def stream(self, key: str) -> AsyncImageBody:
        body = await self.executor.run(self.images.stream, key)

        async def chunks() -> AsyncIterator[bytes]:
            while (
                chunk := await self.executor.run(next, body.chunks, None)
            ) is not None:
                yield chunk

        async def close() -> None:
            await self.executor.run(body.close)

        return AsyncImageBody(body.size, chunks(), close, body)
