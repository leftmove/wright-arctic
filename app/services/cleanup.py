"""Bounded, restart-safe exact-key cleanup for the Worker cron and ASGI operators."""

import argparse
from dataclasses import dataclass
from datetime import timedelta

from pydantic import UUID7

from app.services.database import Database
from app.services.errors import Conflict, Unavailable
from app.services.limits import UPLOAD_TIMEOUT_SECONDS
from app.services.models import Record
from app.services.storage.factory import Storage
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

    def run(self) -> CleanupResult:
        # Sixteen deletes per invocation, sequentially. Each category gets a
        # share of the budget so abandoned images cannot starve trash purging.
        with self._database.transaction() as session:
            now = database_time(session)
            cutoff = now - timedelta(seconds=UPLOAD_TIMEOUT_SECONDS)
            trash_cutoff = now - timedelta(days=self._trash_days)
            trash = session.execute(
                """select id from documents.documents where trashed_at<=%s
                order by trashed_at,id limit 4 for update skip locked""",
                (trash_cutoff,),
            )
            if trash:
                document_ids = [row["id"] for row in trash]
                session.execute(
                    "update documents.documents set revision=null,key=null,published_at=null where id=any(%s::uuid[])",
                    (document_ids,),
                )
                session.execute(
                    "update documents.checkpoints set state='pending' where document_id=any(%s::uuid[]) and state in ('issued','published')",
                    (document_ids,),
                )
            session.execute(
                """with expired as (
                select document_id,revision from documents.checkpoints
                where state='issued' and issued_at<=%s order by issued_at limit 16 for update skip locked)
                update documents.checkpoints c set state='pending' from expired e
                where c.document_id=e.document_id and c.revision=e.revision""",
                (cutoff,),
            )
            session.execute(
                """with expired as (
                select id from coordination.uploads where state='issued' and issued_at<=%s
                order by issued_at limit 16 for update skip locked)
                update coordination.uploads u set state='pending' from expired e where u.id=e.id""",
                (cutoff,),
            )
            checkpoints = [
                CheckpointJob.model_validate(row)
                for row in session.execute(
                    """select c.document_id,c.revision,c.key,c.issued_at<=%s as expired,
                    c.published_at is null as unpublished from documents.checkpoints c
                    left join coordination.attempts a using(document_id,revision)
                    where (c.state='pending' or (c.state='deleted' and c.issued_at<=%s and a.reaped_at is null))
                      and not exists(select 1 from documents.documents d where d.id=c.document_id and d.revision=c.revision)
                    order by c.issued_at,c.document_id,c.revision limit 8""",
                    (cutoff, cutoff),
                )
            ]
            images = [
                ImageJob.model_validate(row)
                for row in session.execute(
                    """select id,key,issued_at<=%s as expired from coordination.uploads
                    where state='pending' or (state='deleted' and issued_at<=%s)
                    order by issued_at,id limit 8""",
                    (cutoff, cutoff),
                )
            ]

        failures = 0
        deleted_checkpoints: list[CheckpointJob] = []
        deleted_images: list[ImageJob] = []
        for job in checkpoints:
            try:
                self._storage.checkpoints.delete(job.key)
                deleted_checkpoints.append(job)
            except StorageUnavailable:
                failures += 1
        for image_job in images:
            try:
                self._storage.images.delete(image_job.key)
                deleted_images.append(image_job)
            except StorageUnavailable:
                failures += 1
        checkpoint_count = image_count = 0
        if deleted_checkpoints or deleted_images:
            try:
                self._finish_deletes(deleted_checkpoints, deleted_images)
                checkpoint_count = len(deleted_checkpoints)
                image_count = len(deleted_images)
            except Conflict, Unavailable:
                # Exact-key deletes are safe to repeat if this commit fails.
                failures += len(deleted_checkpoints) + len(deleted_images)
        with self._database.transaction() as session:
            purged = session.execute(
                """delete from documents.documents d where d.id in (
                select id from documents.documents where trashed_at<=%s order by trashed_at,id limit 4)
                and not exists(select 1 from documents.checkpoints c where c.document_id=d.id and c.state<>'deleted')
                returning id""",
                (trash_cutoff,),
            )
            session.execute(
                "delete from coordination.uploads where state='confirmed' and issued_at<=%s",
                (cutoff,),
            )
        return CleanupResult(checkpoint_count, image_count, len(purged), failures)

    def _finish_deletes(
        self, checkpoints: list[CheckpointJob], images: list[ImageJob]
    ) -> None:
        with self._database.transaction() as session:
            if checkpoints:
                ids = [job.document_id for job in checkpoints]
                revisions = [job.revision for job in checkpoints]
                expired = [job.expired for job in checkpoints]
                unpublished = [job.unpublished for job in checkpoints]
                session.execute(
                    """delete from documents.checkpoints c using
                    unnest(%s::uuid[],%s::uuid[],%s::boolean[],%s::boolean[]) as jobs(document_id,revision,expired,unpublished)
                    where c.document_id=jobs.document_id and c.revision=jobs.revision
                    and jobs.expired and jobs.unpublished and c.state in ('pending','deleted')""",
                    (ids, revisions, expired, unpublished),
                )
                session.execute(
                    """update documents.checkpoints set state='deleted'
                    where (document_id,revision) in(select * from unnest(%s::uuid[],%s::uuid[]))
                    and state='pending'""",
                    (ids, revisions),
                )
                session.execute(
                    """update coordination.attempts a set reaped_at=clock_timestamp() from
                    unnest(%s::uuid[],%s::uuid[],%s::boolean[]) as jobs(document_id,revision,expired)
                    where a.document_id=jobs.document_id and a.revision=jobs.revision and jobs.expired""",
                    (ids, revisions, expired),
                )
            if images:
                ids = [job.id for job in images]
                session.execute(
                    """delete from coordination.uploads u using
                    unnest(%s::uuid[],%s::boolean[]) as jobs(id,expired)
                    where u.id=jobs.id and jobs.expired and u.state in ('pending','deleted')""",
                    (ids, [job.expired for job in images]),
                )
                session.execute(
                    "update coordination.uploads set state='deleted' where id=any(%s::uuid[]) and state='pending'",
                    (ids,),
                )


def main() -> None:
    from app.dependencies import get_settings
    from app.services.database import PostgresDatabase
    from app.services.storage.factory import build_storage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = get_settings()
    if settings.documents_database_url is None:
        parser.error("WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL is required")
    storage = build_storage(settings)
    if storage is None:
        parser.error("Both private storage buckets must be configured")
    database = PostgresDatabase(
        settings.documents_database_url.get_secret_value(), max_connections=1
    )
    try:
        database.open()
        print(
            CleanupService(
                database, storage, trash_days=settings.trash_retention_days
            ).run()
        )
    finally:
        database.close()
        storage.close()


if __name__ == "__main__":
    main()
