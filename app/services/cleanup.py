"""Bounded, restart-safe exact-key cleanup for the Worker cron and ASGI operators."""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import timedelta

from pydantic import UUID7

from app.services.concurrency import bounded_map
from app.services.database import AsyncDatabase as Database
from app.services.errors import Conflict, Unavailable
from app.services.limits import UPLOAD_TIMEOUT_SECONDS
from app.services.models import Record
from app.services.storage.native import AsyncStorage as Storage
from app.services.storage.objects import StorageUnavailable
from app.services.time import database_time


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


@dataclass(frozen=True)
class CleanupResult:
    checkpoints: int
    images: int
    documents: int
    failures: int


class CleanupService:
    def __init__(
        self, database: Database, storage: Storage, *, trash_days: int = 3
    ) -> None:
        self._database = database
        self._storage = storage
        self._trash_days = trash_days

    async def run(self) -> CleanupResult:
        # Sixteen deletes per invocation, with a four-operation fan-out. Each category gets a
        # share of the budget so abandoned images cannot starve trash purging.
        async with self._database.transaction() as session:
            now = await database_time(session)
            cutoff = now - timedelta(seconds=UPLOAD_TIMEOUT_SECONDS)
            trash_cutoff = now - timedelta(days=self._trash_days)
            trash = await session.execute(
                """select id from documents.documents where trashed_at<=%s
                order by trashed_at,id limit 4 for update skip locked""",
                (trash_cutoff,),
            )
            if trash:
                document_ids = [row["id"] for row in trash]
                (
                    await session.execute(
                        "update documents.documents set revision=null,key=null,published_at=null where id=any(%s::uuid[])",
                        (document_ids,),
                    )
                )
                (
                    await session.execute(
                        "update documents.checkpoints set state='pending' where document_id=any(%s::uuid[]) and state in ('issued','published')",
                        (document_ids,),
                    )
                )
            (
                await session.execute(
                    """with expired as (
                select document_id,revision from documents.checkpoints
                where state='issued' and issued_at<=%s order by issued_at limit 16 for update skip locked)
                update documents.checkpoints c set state='pending' from expired e
                where c.document_id=e.document_id and c.revision=e.revision""",
                    (cutoff,),
                )
            )
            (
                await session.execute(
                    """with expired as (
                select id from coordination.uploads where state='issued' and issued_at<=%s
                order by issued_at limit 16 for update skip locked)
                update coordination.uploads u set state='pending' from expired e where u.id=e.id""",
                    (cutoff,),
                )
            )
            checkpoints = [
                CheckpointJob.model_validate(row)
                for row in (
                    await session.execute(
                        """with jobs as (select c.document_id,c.revision,c.key,c.issued_at<=%s as expired,
                    c.published_at is null as unpublished from documents.checkpoints c
                    left join coordination.attempts a using(document_id,revision)
                    where c.cleanup_after<=clock_timestamp() and (c.state='pending' or (c.state='deleted' and c.issued_at<=%s and a.reaped_at is null))
                      and not exists(select 1 from documents.documents d where d.id=c.document_id and d.revision=c.revision)
                    order by c.issued_at,c.document_id,c.revision limit 8 for update of c skip locked)
                    update documents.checkpoints c set cleanup_after=clock_timestamp()+interval '30 seconds'
                    from jobs j where c.document_id=j.document_id and c.revision=j.revision returning j.*""",
                        (cutoff, cutoff),
                    )
                )
            ]
            images = [
                ImageJob.model_validate(row)
                for row in (
                    await session.execute(
                        """with jobs as (select id,key,issued_at<=%s as expired from coordination.uploads
                    where cleanup_after<=clock_timestamp() and (state='pending' or (state='deleted' and issued_at<=%s))
                    order by issued_at,id limit 8 for update skip locked)
                    update coordination.uploads u set cleanup_after=clock_timestamp()+interval '30 seconds'
                    from jobs j where u.id=j.id returning j.*""",
                        (cutoff, cutoff),
                    )
                )
            ]

        async def remove(job: CheckpointJob | ImageJob) -> bool:
            try:
                objects = self._storage.checkpoints if isinstance(job, CheckpointJob) else self._storage.images
                await objects.delete(job.key)
                return True
            except StorageUnavailable:
                return False

        jobs: list[CheckpointJob | ImageJob] = [*checkpoints, *images]
        results = await bounded_map(remove, jobs, limit=4)
        successful = [job for job, done in zip(jobs, results, strict=True) if done]
        failed = [job for job, done in zip(jobs, results, strict=True) if not done]
        deleted_checkpoints = [job for job in successful if isinstance(job, CheckpointJob)]
        deleted_images = [job for job in successful if isinstance(job, ImageJob)]
        failures = len(failed)
        if failed:
            async with self._database.transaction() as session:
                failed_checkpoints = [job for job in failed if isinstance(job, CheckpointJob)]
                failed_images = [job for job in failed if isinstance(job, ImageJob)]
                if failed_checkpoints:
                    await session.execute("""update documents.checkpoints set cleanup_failures=cleanup_failures+1,
                        cleanup_after=clock_timestamp()+make_interval(secs=>least(300,30*power(2,least(cleanup_failures,4)))::integer)
                        where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[])) and state in ('pending','deleted')""",
                        ([job.document_id for job in failed_checkpoints],[job.revision for job in failed_checkpoints]))
                if failed_images:
                    await session.execute("""update coordination.uploads set cleanup_failures=cleanup_failures+1,
                        cleanup_after=clock_timestamp()+make_interval(secs=>least(300,30*power(2,least(cleanup_failures,4)))::integer)
                        where id=any(%s::uuid[]) and state in ('pending','deleted')""", ([job.id for job in failed_images],))
        checkpoint_count = image_count = 0
        if deleted_checkpoints or deleted_images:
            try:
                (await self._finish_deletes(deleted_checkpoints, deleted_images))
                checkpoint_count = len(deleted_checkpoints)
                image_count = len(deleted_images)
            except Conflict, Unavailable:
                # Exact-key deletes are safe to repeat if this commit fails.
                failures += len(deleted_checkpoints) + len(deleted_images)
        async with self._database.transaction() as session:
            purged = await session.execute(
                """delete from documents.documents d where d.id in (
                select id from documents.documents where trashed_at<=%s order by trashed_at,id limit 4)
                and not exists(select 1 from documents.checkpoints c where c.document_id=d.id and c.state<>'deleted')
                returning id""",
                (trash_cutoff,),
            )
            (
                await session.execute(
                    "delete from coordination.uploads where state='confirmed' and issued_at<=%s",
                    (cutoff,),
                )
            )
        return CleanupResult(checkpoint_count, image_count, len(purged), failures)

    async def _finish_deletes(
        self, checkpoints: list[CheckpointJob], images: list[ImageJob]
    ) -> None:
        async with self._database.transaction() as session:
            if checkpoints:
                ids = [job.document_id for job in checkpoints]
                revisions = [job.revision for job in checkpoints]
                expired = [job.expired for job in checkpoints]
                unpublished = [job.unpublished for job in checkpoints]
                (
                    await session.execute(
                        """delete from documents.checkpoints c using
                    unnest(%s::uuid[],%s::uuid[],%s::boolean[],%s::boolean[]) as jobs(document_id,revision,expired,unpublished)
                    where c.document_id=jobs.document_id and c.revision=jobs.revision
                    and jobs.expired and jobs.unpublished and c.state in ('pending','deleted')""",
                        (ids, revisions, expired, unpublished),
                    )
                )
                (
                    await session.execute(
                    """update documents.checkpoints set state='deleted',cleanup_after='-infinity',cleanup_failures=0
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                    and state='pending'""",
                        (ids, revisions),
                    )
                )
                (
                    await session.execute(
                        """update coordination.attempts a set reaped_at=clock_timestamp() from
                    unnest(%s::uuid[],%s::uuid[],%s::boolean[]) as jobs(document_id,revision,expired)
                    where a.document_id=jobs.document_id and a.revision=jobs.revision and jobs.expired""",
                        (ids, revisions, expired),
                    )
                )
            if images:
                ids = [job.id for job in images]
                (
                    await session.execute(
                        """delete from coordination.uploads u using
                    unnest(%s::uuid[],%s::boolean[]) as jobs(id,expired)
                    where u.id=jobs.id and jobs.expired and u.state in ('pending','deleted')""",
                        (ids, [job.expired for job in images]),
                    )
                )
                (
                    await session.execute(
                    "update coordination.uploads set state='deleted',cleanup_after='-infinity',cleanup_failures=0 where id=any(%s::uuid[]) and state='pending'",
                        (ids,),
                    )
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
