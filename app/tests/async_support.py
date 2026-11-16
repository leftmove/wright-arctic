"""Keep synchronous SQL fixtures usable while exercising the async domain API."""

import asyncio
from collections.abc import Coroutine

from app.execution import InlineExecutor
from app.services.compatibility import LegacyCheckpoints, LegacyDatabase, LegacyImages
from app.services.database import Database
from app.services.storage.factory import Storage
from app.services.storage.native import AsyncStorage
from app.services.storage.objects import CheckpointObjects, ImageObjects


def run[T](operation: Coroutine[object, object, T]) -> T:
    return asyncio.run(operation)


def database(value: Database) -> LegacyDatabase:
    return LegacyDatabase(value, InlineExecutor())


def checkpoints(value: CheckpointObjects) -> LegacyCheckpoints:
    return LegacyCheckpoints(value, InlineExecutor())


def images(value: ImageObjects) -> LegacyImages:
    return LegacyImages(value, InlineExecutor())


def storage(value: Storage) -> AsyncStorage:
    return AsyncStorage(
        checkpoints(value.checkpoints),
        value.checkpoint_signer,
        images(value.images),
        value.image_signer,
    )
