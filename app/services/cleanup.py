"""Bounded, restart-safe exact-key cleanup for the Worker cron and ASGI operators."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import UUID7, AwareDatetime, TypeAdapter

from app.services.concurrency import bounded_map
from app.services.database import AsyncDatabase
from app.services.errors import Conflict, Unavailable
from app.services.limits import UPLOAD_TIMEOUT_SECONDS
from app.services.models import Record
from app.services.storage.native import AsyncStorage as Storage
from app.services.storage.objects import StorageUnavailable


class CheckpointJob(Record):
    document_id: UUID7
    revision: UUID7
    key: str
    expired: bool
    unpublished: bool


class ImageJob(Record):
    id: UUID7
    key: str
    expired: bool


class Claims(Record):
    checkpoints: list[CheckpointJob]
    images: list[ImageJob]
    cutoff: AwareDatetime
    trash_cutoff: AwareDatetime


@dataclass(frozen=True)
class CleanupResult:
    checkpoints: int
    images: int
    documents: int
    failures: int


def _jobs(values: Sequence[Record]) -> str:
    return json.dumps([job.model_dump(mode="json") for job in values])


class CleanupService:
    def __init__(
        self, database: AsyncDatabase, storage: Storage, *, trash_days: int = 3
    ) -> None:
        self._database = database
        self._storage = storage
        self._trash_days = trash_days

    async def run(self) -> CleanupResult:
        rows = await self._database.write(
            "select coordination.claim_cleanup(%s,%s) as claims",
            (self._trash_days, UPLOAD_TIMEOUT_SECONDS),
        )
        result = rows[0]["claims"]
        claims = (
            Claims.model_validate_json(result)
            if isinstance(result, str)
            else Claims.model_validate(result)
        )

        async def remove(job: CheckpointJob | ImageJob) -> bool:
            try:
                objects = (
                    self._storage.checkpoints
                    if isinstance(job, CheckpointJob)
                    else self._storage.images
                )
                await objects.delete(job.key)
                return True
            except StorageUnavailable:
                return False

        jobs: list[CheckpointJob | ImageJob] = [*claims.checkpoints, *claims.images]
        results = await bounded_map(remove, jobs, limit=4)
        successful = [job for job, done in zip(jobs, results, strict=True) if done]
        failed = [job for job, done in zip(jobs, results, strict=True) if not done]
        deleted_checkpoints = [
            job for job in successful if isinstance(job, CheckpointJob)
        ]
        deleted_images = [job for job in successful if isinstance(job, ImageJob)]
        failures = len(failed)
        if failed:
            await self._database.write(
                "select coordination.defer_cleanup(%s::jsonb,%s::jsonb)",
                (
                    _jobs([job for job in failed if isinstance(job, CheckpointJob)]),
                    _jobs([job for job in failed if isinstance(job, ImageJob)]),
                ),
            )
        checkpoint_count = image_count = 0
        if deleted_checkpoints or deleted_images:
            try:
                await self._finish_deletes(deleted_checkpoints, deleted_images)
                checkpoint_count = len(deleted_checkpoints)
                image_count = len(deleted_images)
            except Conflict, Unavailable:
                # Exact-key deletes are safe to repeat if this commit fails.
                failures += len(deleted_checkpoints) + len(deleted_images)
        rows = await self._database.write(
            "select coordination.purge_cleanup(%s,%s) as purged",
            (claims.cutoff, claims.trash_cutoff),
        )
        return CleanupResult(
            checkpoint_count,
            image_count,
            TypeAdapter(int).validate_python(rows[0]["purged"]),
            failures,
        )

    async def _finish_deletes(
        self, checkpoints: list[CheckpointJob], images: list[ImageJob]
    ) -> None:
        await self._database.write(
            "select coordination.finish_cleanup(%s::jsonb,%s::jsonb)",
            (_jobs(checkpoints), _jobs(images)),
        )


def main() -> None:
    from app.dependencies import get_settings
    from app.services.native_database import NativeDatabase
    from app.services.storage.native import build_native_storage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = get_settings()
    if settings.documents_database_url is None:
        parser.error("WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL is required")
    storage = build_native_storage(settings)
    if storage is None:
        parser.error("Both private storage buckets must be configured")
    database = NativeDatabase(
        settings.documents_database_url.get_secret_value(), max_connections=1
    )

    async def invoke() -> None:
        assert storage is not None
        try:
            await database.open()
            print(
                await CleanupService(
                    database, storage, trash_days=settings.trash_retention_days
                ).run()
            )
        finally:
            await database.close()
            await storage.close()

    asyncio.run(invoke())


if __name__ == "__main__":
    main()
