"""Exercise real PostgreSQL metadata, authorization, publish, and cleanup transactions."""

import hashlib
import unittest
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4, uuid7

from pydantic import ValidationError

from app.accounts import Identity, VerifiedActor
from app.schemas import (
    CheckpointUpload,
    CreateDocument,
    IssueImage,
    PublishBatch,
    PublishItem,
    RetryUpload,
    StartBatch,
    UploadSlot,
)
from app.services.catalog.copy import CHANGES, SNAPSHOT, CatalogCopy, Change, Snapshot
from app.services.catalog.reader import CatalogReader
from app.services.cleanup import CheckpointJob, CleanupService, ImageJob
from app.services.database import Row, verify_runtime
from app.services.documents.service import DocumentService
from app.services.errors import Conflict, Forbidden, NotFound, ServiceError, Unavailable
from app.services.identity.service import IdentityService
from app.services.images.service import ImageService
from app.services.limits import MAX_CHECKPOINT_BYTES, MAX_IMAGE_BYTES
from app.services.models import Account
from app.services.storage.factory import Storage
from app.services.storage.objects import ObjectMetadata
from app.services.storage.signing import S3Signer
from app.services.sync.service import Attempt, SyncService
from app.tests import async_support
from app.tests.document_database import FixtureDatabase
from app.tests.image_storage import Checkpoints, Images

AVIF = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1" + b"opaque-avif-payload"
DIGEST = "a" * 64


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FixtureDatabase()
        self.addCleanup(self.db.close)
        self.identity = IdentityService(async_support.database(self.db))
        self.owner = async_support.run(
            self.identity.resolve(
                Identity(issuer="https://issuer.example", subject="provider-user")
            )
        )
        self.editor = async_support.run(
            self.identity.resolve(
                Identity(issuer="https://issuer.example", subject="editor")
            )
        )
        self.viewer = async_support.run(
            self.identity.resolve(
                Identity(issuer="https://issuer.example", subject="viewer")
            )
        )
        self.documents = DocumentService(async_support.database(self.db))
        self.space = async_support.run(self.documents.spaces(self.owner))[0].id
        self.document = async_support.run(
            self.documents.create(
                self.owner, CreateDocument(id=uuid7(), space=self.space)
            )
        )
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, "editor")
        )
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.viewer.id, "viewer")
        )
        self.checkpoints = Checkpoints()
        self.images = Images()
        self.signer = S3Signer(
            "https://account.r2.cloudflarestorage.com",
            "wright-documents",
            "auto",
            "access",
            "secret",
        )
        self.image_signer = S3Signer(
            "https://account.r2.cloudflarestorage.com",
            "wright-images",
            "auto",
            "image-access",
            "image-secret",
        )
        self.storage = Storage(
            self.checkpoints, self.signer, self.images, self.image_signer
        )
        self.sync = SyncService(
            async_support.database(self.db),
            async_support.checkpoints(self.checkpoints),
            self.signer,
        )
        self.media = ImageService(
            async_support.database(self.db),
            async_support.images(self.images),
            self.image_signer,
        )
        self.cleanup = CleanupService(
            async_support.database(self.db), async_support.storage(self.storage)
        )

    def start(
        self,
        account: Account | None = None,
        document: UUID | None = None,
        *,
        size: int = 10,
    ) -> tuple[UUID, UploadSlot]:
        batch = async_support.run(
            self.sync.start(
                account or self.owner,
                StartBatch(
                    id=uuid7(),
                    documents=[
                        CheckpointUpload(
                            document_id=document or self.document.id,
                            size=size,
                            hash=DIGEST,
                        )
                    ],
                ),
            )
        )
        return batch.id, batch.documents[0]

    def publish(
        self,
        batch: UUID,
        slot: UploadSlot,
        account: Account | None = None,
        images: list[UUID] | None = None,
    ) -> str:
        return (
            async_support.run(
                self.sync.publish(
                    account or self.owner,
                    batch,
                    PublishBatch(
                        documents=[
                            PublishItem(
                                document_id=slot.document_id,
                                base_revision=slot.base_revision,
                                revision=slot.revision,
                                key=slot.key,
                                images=images or [],
                            )
                        ]
                    ),
                )
            )
            .documents[0]
            .result
        )

    def clear_window(self, account: Account | None = None) -> None:
        with self.db.admin_transaction() as session:
            session.execute(
                "update public.accounts set last_sync=clock_timestamp()-interval '5 minutes' where id=%s",
                ((account or self.owner).id,),
            )

    def image(self, account: Account | None = None, data: bytes = AVIF) -> UUID:
        account = account or self.owner
        slot = async_support.run(
            self.media.issue(
                account,
                IssueImage(
                    id=uuid7(), size=len(data), hash=hashlib.sha256(data).hexdigest()
                ),
            )
        )
        self.images.upload_image(slot, data)
        return async_support.run(self.media.confirm(account, slot.id)).id

    def test_uuid7_identity_is_issuer_scoped_and_default_space_is_atomic(self) -> None:
        same = async_support.run(
            self.identity.resolve(
                Identity(issuer=self.owner.issuer, subject=self.owner.subject)
            )
        )
        other = async_support.run(
            self.identity.resolve(
                Identity(issuer="https://another.example", subject=self.owner.subject)
            )
        )
        self.assertEqual(same.id, self.owner.id)
        self.assertNotEqual(same.id, other.id)
        self.assertEqual(self.owner.id.version, 7)
        self.assertEqual(self.space.version, 7)
        self.assertEqual(len(async_support.run(self.documents.spaces(self.owner))), 1)
        verify_runtime(self.db)
        with self.assertRaises(ValidationError):
            CreateDocument(id=uuid4(), space=self.space)
        with self.db.admin_transaction() as session, self.assertRaises(Conflict):
            session.execute(
                "insert into public.accounts(id,issuer,subject) values(%s,'bad','bad')",
                (uuid4(),),
            )

    def test_fresh_catalog_setup_drops_legacy_content_and_is_idempotent(self) -> None:
        from app.services.catalog.__main__ import SOURCE, setup

        source = SOURCE.read_text()
        with self.db.admin_transaction() as session:
            session.execute("create table documents.changes(payload bytea)")
            session.execute("create table documents.snapshots(state bytea)")
            setup(session, source)
            setup(session, source)
            self.assertEqual(session.execute("select * from public.accounts"), [])
            tables = session.execute(
                "select tablename from pg_tables where schemaname='documents' order by tablename"
            )
            self.assertEqual(
                [row["tablename"] for row in tables],
                ["checkpoints", "documents", "grants", "images", "spaces"],
            )
            with self.assertRaises(RuntimeError):
                setup(session, source + "\n-- changed source")
        verify_runtime(self.db)

    def test_batch_retry_keeps_the_global_window_and_reservation(self) -> None:
        command = StartBatch(
            id=uuid7(),
            documents=[
                CheckpointUpload(document_id=self.document.id, size=10, hash=DIGEST)
            ],
        )
        first = async_support.run(self.sync.start(self.owner, command))
        start = async_support.run(self.identity.current(self.owner)).last_sync
        second = async_support.run(self.sync.start(self.owner, command))
        self.assertEqual(first.documents[0].revision, second.documents[0].revision)
        self.assertEqual(
            start, async_support.run(self.identity.current(self.owner)).last_sync
        )
        with self.assertRaises(ServiceError) as failure:
            self.start()
        self.assertEqual(failure.exception.code, "sync_window")
        self.assertIn("Retry-After", failure.exception.headers)
        with self.assertRaises(Forbidden):
            self.start(self.viewer)
        self.assertIsNone(
            async_support.run(self.identity.current(self.viewer)).last_sync
        )

    def test_eight_document_slots_are_batched_and_retry_identically(self) -> None:
        from unittest.mock import patch

        documents = [
            self.document,
            *[
                async_support.run(
                    self.documents.create(
                        self.owner, CreateDocument(id=uuid7(), space=self.space)
                    )
                )
                for _ in range(7)
            ],
        ]
        command = StartBatch(
            id=uuid7(),
            documents=[
                CheckpointUpload(
                    document_id=document.id, size=index + 1, hash=f"{index:064x}"
                )
                for index, document in enumerate(reversed(documents))
            ],
        )
        with patch.object(self.db, "execute", wraps=self.db.execute) as execute:
            batch = async_support.run(self.sync.start(self.owner, command))
        self.assertLessEqual(execute.call_count, 14)
        self.assertEqual(
            [slot.document_id for slot in batch.documents],
            [item.document_id for item in command.documents],
        )
        with self.db.transaction() as session:
            attempts = session.execute(
                "select document_id,revision,size,hash from coordination.attempts where batch=%s",
                (batch.id,),
            )
        stored = {UUID(str(row["document_id"])): row for row in attempts}
        for item, slot in zip(command.documents, batch.documents, strict=True):
            self.assertEqual(
                UUID(str(stored[item.document_id]["revision"])), slot.revision
            )
            self.assertEqual(stored[item.document_id]["size"], item.size)
            self.assertEqual(stored[item.document_id]["hash"], item.hash)
        again = async_support.run(self.sync.start(self.owner, command))
        self.assertEqual(
            [slot.revision for slot in again.documents],
            [slot.revision for slot in batch.documents],
        )
        self.assertEqual(again.started_at, batch.started_at)

    def test_eight_document_reservations_roll_back_together(self) -> None:
        documents = [
            self.document,
            *[
                async_support.run(
                    self.documents.create(
                        self.owner, CreateDocument(id=uuid7(), space=self.space)
                    )
                )
                for _ in range(7)
            ],
        ]
        command = StartBatch(
            id=uuid7(),
            documents=[
                CheckpointUpload(document_id=document.id, size=10, hash=DIGEST)
                for document in documents
            ],
        )
        self.db.fail_commit = True
        with self.assertRaises(Unavailable):
            async_support.run(self.sync.start(self.owner, command))
        with self.db.transaction() as session:
            self.assertEqual(session.execute("select * from documents.checkpoints"), [])
            self.assertEqual(session.execute("select * from coordination.batches"), [])
        self.assertIsNone(
            async_support.run(self.identity.current(self.owner)).last_sync
        )
        self.assertEqual(
            len(async_support.run(self.sync.start(self.owner, command)).documents), 8
        )

    def test_batched_publish_keeps_each_result_and_image_link(self) -> None:
        documents = [
            async_support.run(
                self.documents.create(
                    self.owner, CreateDocument(id=uuid7(), space=self.space)
                )
            )
            for _ in range(8)
        ]
        image = self.image()
        batch = async_support.run(
            self.sync.start(
                self.owner,
                StartBatch(
                    id=uuid7(),
                    documents=[
                        CheckpointUpload(document_id=document.id, size=10, hash=DIGEST)
                        for document in reversed(documents)
                    ],
                ),
            )
        )
        for slot in batch.documents[2:]:
            self.checkpoints.upload(slot)
        command = PublishBatch(
            documents=[
                PublishItem(
                    document_id=slot.document_id,
                    revision=slot.revision,
                    base_revision=slot.base_revision,
                    key=slot.key,
                    images=[image],
                )
                for slot in batch.documents
            ]
        )
        result = async_support.run(self.sync.publish(self.owner, batch.id, command))
        self.assertEqual(
            [item.document_id for item in result.documents],
            [slot.document_id for slot in batch.documents],
        )
        self.assertEqual(
            [item.result for item in result.documents],
            ["invalid", "invalid", *["published"] * 6],
        )
        self.assertFalse(result.cleanup_pending)
        reads = len(self.checkpoints.heads)
        self.assertEqual(
            async_support.run(self.sync.publish(self.owner, batch.id, command)), result
        )
        self.assertEqual(len(self.checkpoints.heads), reads)
        with self.db.transaction() as session:
            linked = session.execute(
                "select document_id from documents.images where image_id=%s", (image,)
            )
            attempts = session.execute(
                "select result from coordination.attempts where batch=%s order by result",
                (batch.id,),
            )
            refusals = session.execute(
                "select refusals from coordination.items where batch=%s order by refusals",
                (batch.id,),
            )
        self.assertEqual(
            {UUID(str(row["document_id"])) for row in linked},
            {slot.document_id for slot in batch.documents[2:]},
        )
        self.assertEqual(
            [row["result"] for row in attempts],
            ["invalid", "invalid", *["published"] * 6],
        )
        self.assertEqual([row["refusals"] for row in refusals], [0] * 6 + [1, 1])

    def test_large_batch_uses_bounded_admission_chunks_without_another_window(
        self,
    ) -> None:
        batch, first = self.start()
        next_document = async_support.run(
            self.documents.create(
                self.owner, CreateDocument(id=uuid7(), space=self.space)
            )
        )
        extended = async_support.run(
            self.sync.start(
                self.owner,
                StartBatch(
                    id=batch,
                    documents=[
                        CheckpointUpload(
                            document_id=next_document.id, size=20, hash=DIGEST
                        )
                    ],
                ),
            )
        )
        self.assertEqual(extended.id, batch)
        self.checkpoints.upload(first)
        self.checkpoints.upload(extended.documents[0])
        result = async_support.run(
            self.sync.publish(
                self.owner,
                batch,
                PublishBatch(
                    documents=[
                        PublishItem(
                            document_id=slot.document_id,
                            base_revision=slot.base_revision,
                            revision=slot.revision,
                            key=slot.key,
                        )
                        for slot in [first, *extended.documents]
                    ]
                ),
            )
        )
        self.assertEqual(
            [item.result for item in result.documents], ["published", "published"]
        )

    def test_upload_exists_before_publish_and_unchanged_open_never_touches_r2(
        self,
    ) -> None:
        batch, slot = self.start(size=MAX_CHECKPOINT_BYTES)
        self.checkpoints.upload(slot)
        self.assertEqual(self.publish(batch, slot), "published")
        count = len(self.checkpoints.heads)
        pointer = async_support.run(
            self.sync.open(self.viewer, self.document.id, slot.revision)
        )
        self.assertIsNone(pointer.read)
        self.assertEqual(len(self.checkpoints.heads), count)
        changed = async_support.run(self.sync.open(self.viewer, self.document.id, None))
        self.assertIsNotNone(changed.read)
        self.assertEqual(
            changed.key, f"documents/{self.space}/{self.document.id}/{slot.revision}"
        )
        self.assertEqual(self.publish(batch, slot), "published")
        self.assertEqual(len(self.checkpoints.heads), count)

    def test_two_simultaneous_publishers_have_one_winner_and_delete_the_loser(
        self,
    ) -> None:
        a_batch, a = self.start()
        b_batch, b = self.start(self.editor)
        self.checkpoints.upload(a)
        self.checkpoints.upload(b)
        self.checkpoints.barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.publish, a_batch, a)
            second = pool.submit(self.publish, b_batch, b, self.editor)
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.checkpoints.barrier = None
        self.assertEqual(sorted(results), ["conflict", "published"])
        loser = a if results[0] == "conflict" else b
        winner = b if results[0] == "conflict" else a
        self.assertIn(loser.key, self.checkpoints.deleted)
        self.assertNotIn(loser.key, self.checkpoints.objects)
        self.assertEqual(
            async_support.run(
                self.sync.open(self.owner, self.document.id, None)
            ).revision,
            winner.revision,
        )

    def test_retention_keeps_three_and_retired_publish_retry_still_succeeds(
        self,
    ) -> None:
        saved: list[tuple[UUID, UploadSlot]] = []
        for _ in range(4):
            self.clear_window()
            batch, slot = self.start()
            self.checkpoints.upload(slot)
            self.assertEqual(self.publish(batch, slot), "published")
            saved.append((batch, slot))
        self.assertEqual(len(self.checkpoints.objects), 3)
        self.assertIn(saved[0][1].key, self.checkpoints.deleted)
        self.assertEqual(self.publish(*saved[0]), "published")
        pointer = async_support.run(
            self.sync.open(self.owner, self.document.id, saved[0][1].revision)
        )
        self.assertEqual(pointer.revision, saved[-1][1].revision)
        self.assertIsNotNone(pointer.read)
        reads = len(self.checkpoints.heads)
        retained = async_support.run(
            self.sync.checkpoints(self.viewer, self.document.id)
        )
        self.assertEqual(
            [value.revision for value in retained],
            [slot.revision for _, slot in reversed(saved[1:])],
        )
        self.assertTrue(all(value.read is not None for value in retained))
        self.assertEqual(len(self.checkpoints.heads), reads)

    def test_failed_delete_remains_pending_and_cron_retries_it(self) -> None:
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, None)
        )
        self.checkpoints.objects[slot.key] = ObjectMetadata(
            MAX_CHECKPOINT_BYTES + 1, "application/octet-stream", {"sha256": DIGEST}
        )
        self.checkpoints.fail_delete = True
        response = async_support.run(
            self.sync.publish(
                self.owner,
                batch,
                PublishBatch(
                    documents=[
                        PublishItem(
                            document_id=slot.document_id,
                            base_revision=slot.base_revision,
                            revision=slot.revision,
                            key=slot.key,
                        )
                    ]
                ),
            )
        )
        self.assertTrue(response.cleanup_pending)
        self.assertEqual(response.documents[0].result, "invalid")
        self.checkpoints.fail_delete = False
        result = async_support.run(self.cleanup.run())
        self.assertEqual(result.checkpoints, 1)
        self.assertNotIn(slot.key, self.checkpoints.objects)

    def expire_cleanup_claims(self) -> None:
        # Exercise the next scheduled attempt without waiting for its lease/backoff.
        with self.db.admin_transaction() as session:
            session.execute(
                "update documents.checkpoints set cleanup_after='-infinity'"
            )
            session.execute("update coordination.uploads set cleanup_after='-infinity'")

    def test_cleanup_database_conflicts_preserve_pending_work(self) -> None:
        from unittest.mock import patch

        batch, slot = self.start()
        original = self.db.execute

        def fail_delete_recording(
            query: str, parameters: Sequence[object] = ()
        ) -> list[Row]:
            if "set state='deleted'" in query or "coordination.finish_cleanup" in query:
                raise Conflict("retry_transaction")
            return original(query, parameters)

        with patch.object(self.db, "execute", side_effect=fail_delete_recording):
            result = async_support.run(
                self.sync.publish(
                    self.owner,
                    batch,
                    PublishBatch(
                        documents=[
                            PublishItem(
                                document_id=slot.document_id,
                                base_revision=slot.base_revision,
                                revision=slot.revision,
                                key=slot.key,
                            )
                        ]
                    ),
                )
            )
            self.assertTrue(result.cleanup_pending)
            cleanup = async_support.run(self.cleanup.run())
            self.assertEqual((cleanup.checkpoints, cleanup.failures), (0, 1))
        with self.db.transaction() as session:
            self.assertEqual(
                session.execute("select state from documents.checkpoints"),
                [{"state": "pending"}],
            )
        self.expire_cleanup_claims()
        self.assertEqual(async_support.run(self.cleanup.run()).checkpoints, 1)

    def test_pointer_and_image_links_roll_back_when_commit_fails(self) -> None:
        image = self.image()
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        # Fail the second transaction: the preflight does no mutations.
        original = self.sync._uploaded

        async def fail_at_commit(attempt: Attempt) -> bool:
            result = await original(attempt)
            self.db.fail_commit = True
            return result

        from unittest.mock import patch

        with (
            patch.object(self.sync, "_uploaded", side_effect=fail_at_commit),
            self.assertRaises(Unavailable),
        ):
            self.publish(batch, slot, images=[image])
        self.assertIsNone(
            async_support.run(
                self.sync.open(self.owner, self.document.id, None)
            ).revision
        )
        with self.db.transaction() as session:
            self.assertEqual(session.execute("select * from documents.images"), [])
        self.assertIn(slot.key, self.checkpoints.objects)

    def test_revocation_refuses_upload_and_read_without_content_access(self) -> None:
        batch, slot = self.start(self.editor)
        self.checkpoints.upload(slot)
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, None)
        )
        self.assertEqual(self.publish(batch, slot, self.editor), "forbidden")
        self.assertEqual(self.checkpoints.heads, [])
        self.assertIn(slot.key, self.checkpoints.deleted)
        with self.assertRaises(NotFound):
            async_support.run(self.sync.open(self.editor, self.document.id, None))

    def test_checkpoint_and_image_orphans_expire_after_24_hours(self) -> None:
        _, slot = self.start()
        self.checkpoints.upload(slot)
        image = async_support.run(
            self.media.issue(
                self.owner, IssueImage(id=uuid7(), size=len(AVIF), hash=DIGEST)
            )
        )
        self.images.upload_image(image, AVIF)
        self.assertEqual(async_support.run(self.cleanup.run()).checkpoints, 0)
        with self.db.admin_transaction() as session:
            session.execute(
                "update documents.checkpoints set issued_at=clock_timestamp()-interval '25 hours'"
            )
            session.execute(
                "update coordination.uploads set issued_at=clock_timestamp()-interval '25 hours'"
            )
        result = async_support.run(self.cleanup.run())
        self.assertEqual((result.checkpoints, result.images), (1, 1))
        with self.db.transaction() as session:
            self.assertEqual(session.execute("select * from documents.checkpoints"), [])
            self.assertEqual(session.execute("select * from coordination.uploads"), [])

    def test_cleanup_retries_all_deletes_after_a_bookkeeping_commit_failure(
        self,
    ) -> None:
        from unittest.mock import patch

        _, slot = self.start()
        self.checkpoints.upload(slot)
        image = async_support.run(
            self.media.issue(
                self.owner, IssueImage(id=uuid7(), size=len(AVIF), hash=DIGEST)
            )
        )
        self.images.upload_image(image, AVIF)
        with self.db.admin_transaction() as session:
            session.execute(
                "update documents.checkpoints set issued_at=clock_timestamp()-interval '25 hours'"
            )
            session.execute(
                "update coordination.uploads set issued_at=clock_timestamp()-interval '25 hours'"
            )
        original = self.cleanup._finish_deletes

        async def fail_commit(
            checkpoints: list[CheckpointJob], images: list[ImageJob]
        ) -> None:
            self.db.fail_commit = True
            await original(checkpoints, images)

        with patch.object(self.cleanup, "_finish_deletes", side_effect=fail_commit):
            result = async_support.run(self.cleanup.run())
        self.assertEqual(
            (result.checkpoints, result.images, result.failures), (0, 0, 2)
        )
        with self.db.transaction() as session:
            self.assertEqual(
                session.execute("select state from documents.checkpoints"),
                [{"state": "pending"}],
            )
            self.assertEqual(
                session.execute("select state from coordination.uploads"),
                [{"state": "pending"}],
            )
        self.expire_cleanup_claims()
        result = async_support.run(self.cleanup.run())
        self.assertEqual(
            (result.checkpoints, result.images, result.failures), (1, 1, 0)
        )

    def test_trash_purges_only_after_retention_and_preserves_failed_deletes(
        self,
    ) -> None:
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        self.publish(batch, slot)
        async_support.run(
            self.documents.lifecycle(self.owner, self.document.id, trashed=True)
        )
        self.assertEqual(async_support.run(self.cleanup.run()).documents, 0)
        with self.db.admin_transaction() as session:
            session.execute(
                "update documents.documents set trashed_at=clock_timestamp()-interval '4 days' where id=%s",
                (self.document.id,),
            )
        self.checkpoints.fail_delete = True
        self.assertEqual(async_support.run(self.cleanup.run()).documents, 0)
        with self.assertRaises(ServiceError):
            async_support.run(
                self.documents.lifecycle(self.owner, self.document.id, trashed=False)
            )
        self.checkpoints.fail_delete = False
        self.expire_cleanup_claims()
        self.assertEqual(async_support.run(self.cleanup.run()).documents, 1)
        self.assertNotIn(slot.key, self.checkpoints.objects)
        with self.assertRaises(NotFound):
            async_support.run(self.documents.metadata(self.owner, self.document.id))

    def test_image_links_require_editor_uploads_and_preserve_existing_history_links(
        self,
    ) -> None:
        editor_image = self.image(self.editor)
        viewer_image = self.image(self.viewer, AVIF + b"different")
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        self.assertEqual(self.publish(batch, slot, images=[viewer_image]), "invalid")
        retry = async_support.run(
            self.sync.retry(
                self.owner,
                batch,
                self.document.id,
                RetryUpload(
                    previous_revision=slot.revision,
                    base_revision=None,
                    size=10,
                    hash=DIGEST,
                ),
            )
        )
        self.checkpoints.upload(retry)
        self.assertEqual(self.publish(batch, retry, images=[editor_image]), "published")
        download = async_support.run(self.media.content(self.viewer, editor_image))
        self.assertEqual(download.method, "GET")
        self.assertTrue(urlsplit(download.url).path.endswith(f"/{editor_image}"))
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, None)
        )
        self.clear_window()
        new_batch, new_slot = self.start()
        self.checkpoints.upload(new_slot)
        self.assertEqual(
            self.publish(new_batch, new_slot, images=[editor_image]), "published"
        )
        with self.assertRaises(ServiceError):
            async_support.run(self.media.delete(self.editor, editor_image))

    def test_catalog_reads_each_use_one_autocommit_statement(self) -> None:
        image = self.image()
        async_support.run(self.identity.select_avatar(self.owner, image))
        actor = VerifiedActor(
            Identity(subject=self.owner.subject, issuer=self.owner.issuer)
        )
        with (
            patch.object(self.db, "fetch", wraps=self.db.fetch) as fetch,
            patch.object(
                self.db,
                "transaction",
                side_effect=AssertionError("read opened a transaction"),
            ),
        ):
            reads = (
                self.identity.current(actor),
                self.documents.spaces(actor),
                self.documents.list_documents(actor, trashed=False, limit=10, offset=0),
                self.documents.metadata(actor, self.document.id),
                self.documents.grants(actor, self.document.id),
                self.sync.open(actor, self.document.id, None),
                self.sync.checkpoints(actor, self.document.id),
                self.media.list_images(actor, limit=10, offset=0),
                self.media.metadata(actor, image),
                self.media.avatar(self.owner.id),
            )
            for read in reads:
                fetch.reset_mock()
                async_support.run(read)
                fetch.assert_called_once()
        first_visit = VerifiedActor(
            Identity(subject="new-visitor", issuer=self.owner.issuer)
        )
        self.assertEqual(len(async_support.run(self.documents.spaces(first_visit))), 1)

    def test_catalog_snapshot_replays_only_committed_changes(self) -> None:
        image = self.image()
        async_support.run(self.identity.select_avatar(self.owner, image))
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        self.assertEqual(self.publish(batch, slot, images=[image]), "published")

        copy = CatalogCopy()
        copy.load(Snapshot.model_validate(self.db.fetch(SNAPSHOT)[0]))
        copy.connected = True
        reader = CatalogReader(async_support.database(self.db), copy)
        actor = VerifiedActor(
            Identity(subject=self.viewer.subject, issuer=self.viewer.issuer)
        )
        self.db.notifications()
        with patch.object(
            self.db,
            "fetch",
            side_effect=AssertionError("snapshot should serve the read"),
        ):
            self.assertEqual(
                async_support.run(reader.document(actor, self.document.id)).revision,
                slot.revision,
            )
            self.assertEqual(
                async_support.run(reader.checkpoints(actor, self.document.id))[
                    0
                ].revision,
                slot.revision,
            )
            self.assertEqual(async_support.run(reader.image(actor, image)).id, image)
            self.assertEqual(async_support.run(reader.avatar(self.owner.id)).id, image)

        def revoke() -> None:
            async_support.run(
                self.documents.grant(self.owner, self.document.id, self.viewer.id, None)
            )

        self.db.fail_commit = True
        with self.assertRaises(Unavailable):
            revoke()
        self.assertEqual(self.db.notifications(), [])
        self.assertEqual(self.db.fetch(CHANGES, (copy.applied,)), [])
        revoke()
        copy.notified = max(self.db.notifications())
        self.assertIsNone(copy.readable)
        with (
            patch.object(self.db, "fetch", wraps=self.db.fetch) as fetch,
            self.assertRaises(NotFound),
        ):
            async_support.run(reader.image(actor, image))
        fetch.assert_called_once()

        copy.connected = (
            False  # The listener dropped before it could read the noticed change.
        )
        async_support.run(
            self.documents.rename(
                self.owner, self.document.id, "Committed while disconnected"
            )
        )
        copy.apply(map(Change.model_validate, self.db.fetch(CHANGES, (copy.applied,))))
        copy.connected = True
        with patch.object(
            self.db,
            "fetch",
            side_effect=AssertionError("catch-up should serve the read"),
        ):
            self.assertEqual(
                async_support.run(reader.document(self.owner, self.document.id)).title,
                "Committed while disconnected",
            )
            with self.assertRaises(NotFound):
                async_support.run(reader.image(actor, image))

    def test_avif_size_format_quota_and_confirm_idempotency(self) -> None:
        data = AVIF + b"\x00" * (MAX_IMAGE_BYTES - len(AVIF))
        image = self.image(data=data)
        heads = len(self.images.heads)
        self.assertEqual(
            async_support.run(self.media.confirm(self.owner, image)).id, image
        )
        self.assertEqual(len(self.images.heads), heads)
        with self.assertRaises(ValidationError):
            IssueImage(id=uuid7(), size=MAX_IMAGE_BYTES + 1, hash=DIGEST)
        for kind in ("image/gif", "image/jxl"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                IssueImage.model_validate(
                    {"id": str(uuid7()), "size": 10, "hash": DIGEST, "media_type": kind}
                )
        slot = async_support.run(
            self.media.issue(self.owner, IssueImage(id=uuid7(), size=10, hash=DIGEST))
        )
        self.images.upload_image(slot, b"GIF89a1234")
        with self.assertRaises(ServiceError) as failure:
            async_support.run(self.media.confirm(self.owner, slot.id))
        self.assertEqual(failure.exception.code, "invalid_avif")
        self.assertNotIn(slot.key, self.images.objects)
        limited = ImageService(
            async_support.database(self.db),
            async_support.images(self.images),
            self.image_signer,
            daily_quota=1,
        )
        with self.assertRaises(ServiceError):
            async_support.run(
                limited.issue(
                    self.owner, IssueImage(id=uuid7(), size=10, hash="b" * 64)
                )
            )

    def test_avatar_is_an_ordinary_owned_image_and_references_prevent_deletion(
        self,
    ) -> None:
        image = self.image()
        with self.assertRaises(NotFound):
            async_support.run(self.media.metadata(self.viewer, image))
        async_support.run(self.identity.select_avatar(self.owner, image))
        download = async_support.run(self.media.avatar(self.owner.id))
        self.assertEqual(download.method, "GET")
        self.assertTrue(urlsplit(download.url).path.endswith(f"/{image}"))
        with self.assertRaises(NotFound):
            async_support.run(self.identity.select_avatar(self.viewer, image))
        with self.assertRaises(ServiceError):
            async_support.run(self.media.delete(self.owner, image))
        async_support.run(self.identity.select_avatar(self.owner, None))
        async_support.run(self.media.delete(self.owner, image))
        with self.assertRaises(NotFound):
            async_support.run(self.media.avatar(self.owner.id))

    def test_three_refusals_stop_retries_without_resetting_the_batch_window(
        self,
    ) -> None:
        batch, slot = self.start()
        started = async_support.run(self.identity.current(self.owner)).last_sync
        for index in range(3):
            self.checkpoints.upload(slot)
            self.checkpoints.objects[slot.key] = ObjectMetadata(
                11, "application/octet-stream", {"sha256": DIGEST}
            )
            self.assertEqual(self.publish(batch, slot), "invalid")
            command = RetryUpload(
                previous_revision=slot.revision,
                base_revision=None,
                size=10,
                hash=DIGEST,
            )
            if index < 2:
                slot = async_support.run(
                    self.sync.retry(self.owner, batch, self.document.id, command)
                )
                duplicate = async_support.run(
                    self.sync.retry(self.owner, batch, self.document.id, command)
                )
                self.assertEqual(slot.revision, duplicate.revision)
            else:
                with self.assertRaises(ServiceError) as failure:
                    async_support.run(
                        self.sync.retry(self.owner, batch, self.document.id, command)
                    )
                self.assertEqual(failure.exception.code, "retry_limit")
        self.assertEqual(
            async_support.run(self.identity.current(self.owner)).last_sync, started
        )

    def test_retry_revision_order_survives_a_different_hosts_older_clock(self) -> None:
        from unittest.mock import patch

        batch, slot = self.start()
        older = UUID(int=slot.revision.int - (1 << 80))
        command = RetryUpload(
            previous_revision=slot.revision, base_revision=None, size=10, hash=DIGEST
        )
        with patch("app.services.identifiers.uuid7", return_value=older):
            retried = async_support.run(
                self.sync.retry(self.owner, batch, self.document.id, command)
            )
        self.assertGreater(retried.revision, slot.revision)
        self.assertEqual(
            async_support.run(
                self.sync.retry(self.owner, batch, self.document.id, command)
            ).revision,
            retried.revision,
        )

    def test_concurrent_image_reservations_cannot_exceed_total_quota(self) -> None:
        limited = ImageService(
            async_support.database(self.db),
            async_support.images(self.images),
            self.image_signer,
            total_quota=1,
        )

        def reserve(digest: str) -> str:
            try:
                async_support.run(
                    limited.issue(
                        self.owner, IssueImage(id=uuid7(), size=10, hash=digest)
                    )
                )
                return "issued"
            except ServiceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ["a" * 64, "b" * 64]))
        self.assertEqual(sorted(results), ["image_quota_exceeded", "issued"])

    def test_large_image_manifests_are_paged_and_become_visible_only_on_publish(
        self,
    ) -> None:
        image_ids = [uuid7() for _ in range(400)]
        # Seed confirmed catalog rows for two collaborators, each below their
        # account quota. Upload confirmation itself is exercised separately.
        with self.db.admin_transaction() as session:
            for account, ids in (
                (self.owner, image_ids[:200]),
                (self.editor, image_ids[200:]),
            ):
                session.execute(
                    """insert into public.images(id,account,key,size,hash)
                    select id,%s,'images/'||%s::text||'/'||id::text,24,%s
                    from unnest(%s::uuid[]) as id""",
                    (account.id, account.id, DIGEST, ids),
                )
        batch, slot = self.start()
        for ids in (image_ids[:200], image_ids[200:], image_ids[:200]):
            async_support.run(
                self.sync.stage_images(
                    self.owner, batch, self.document.id, slot.revision, ids
                )
            )
        with self.assertRaises(NotFound):
            async_support.run(self.media.metadata(self.viewer, image_ids[0]))
        self.checkpoints.upload(slot)
        self.assertEqual(self.publish(batch, slot), "published")
        self.assertEqual(
            async_support.run(self.media.metadata(self.viewer, image_ids[0])).id,
            image_ids[0],
        )
        with self.db.transaction() as session:
            self.assertEqual(
                len(session.execute("select * from documents.images")), 400
            )
            self.assertEqual(session.execute("select * from coordination.images"), [])
        with self.assertRaises(Conflict):
            async_support.run(
                self.sync.stage_images(
                    self.owner, batch, self.document.id, slot.revision, image_ids
                )
            )

    def test_staged_images_recheck_the_uploaders_edit_grant_at_publish(self) -> None:
        image = self.image(self.editor)
        batch, slot = self.start()
        async_support.run(
            self.sync.stage_images(
                self.owner, batch, self.document.id, slot.revision, [image]
            )
        )
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, None)
        )
        self.checkpoints.upload(slot)
        self.assertEqual(self.publish(batch, slot), "invalid")
        self.assertIsNone(
            async_support.run(
                self.sync.open(self.owner, self.document.id, None)
            ).revision
        )

    def test_daily_quota_counts_deleted_uploads_and_dedup_still_works_when_full(
        self,
    ) -> None:
        image = self.image()
        limited = ImageService(
            async_support.database(self.db),
            async_support.images(self.images),
            self.image_signer,
            daily_quota=1,
            total_quota=1,
        )
        reused = async_support.run(
            limited.issue(
                self.owner,
                IssueImage(
                    id=uuid7(), size=len(AVIF), hash=hashlib.sha256(AVIF).hexdigest()
                ),
            )
        )
        self.assertEqual(reused.id, image)
        self.assertIsNone(reused.upload)
        async_support.run(self.media.delete(self.owner, image))
        with self.assertRaises(ServiceError):
            async_support.run(
                limited.issue(self.owner, IssueImage(id=uuid7(), size=10, hash=DIGEST))
            )

    def test_deleted_objects_are_reaped_again_after_upload_permission_expiry(
        self,
    ) -> None:
        batch, slot = self.start()
        self.checkpoints.upload(slot)
        self.checkpoints.objects[slot.key] = ObjectMetadata(
            11, "application/octet-stream", {"sha256": DIGEST}
        )
        self.assertEqual(self.publish(batch, slot), "invalid")
        self.checkpoints.upload(slot)  # A presigned PUT is reusable until expiry.
        with self.db.admin_transaction() as session:
            session.execute(
                "update documents.checkpoints set issued_at=clock_timestamp()-interval '25 hours'"
            )
        async_support.run(self.cleanup.run())
        self.assertNotIn(slot.key, self.checkpoints.objects)

    def test_http_routes_keep_avatars_public_and_sync_bodies_metadata_only(
        self,
    ) -> None:
        from unittest.mock import Mock

        from fastapi.testclient import TestClient

        from app.authentication.service import AuthenticationService
        from app.execution import InlineExecutor
        from app.main import create_app
        from app.settings import Settings
        from app.state import legacy_resources

        image = self.image()
        async_support.run(self.identity.select_avatar(self.owner, image))
        authentication = Mock(spec=AuthenticationService)
        authentication.authenticate.return_value = Identity(
            issuer=self.owner.issuer, subject=self.owner.subject
        )
        application = create_app(
            legacy_resources(
                Settings(_env_file=None),
                InlineExecutor(),
                database=self.db,
                storage=self.storage,
                authentication=authentication,
            )
        )
        with TestClient(application, follow_redirects=False) as client:
            public = client.get(f"/avatars/{self.owner.id}")
            self.assertEqual(public.status_code, 307)
            self.assertEqual(public.content, b"")
            self.assertEqual(public.headers["cache-control"], "private, no-store")
            self.assertNotIn("authorization", public.headers.get("vary", "").lower())
            self.assertEqual(client.get(f"/images/{image}/content").status_code, 401)
            headers = {"Authorization": "Bearer test"}
            self.assertEqual(
                client.get(f"/images/{image}/content", headers=headers).status_code, 307
            )
            self.assertEqual(
                client.get(
                    f"/documents/{self.document.id}/grants", headers=headers
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get(f"/documents/{uuid4()}", headers=headers).status_code, 422
            )
            for path in ("state", "content", "changes", "snapshots"):
                self.assertEqual(
                    client.get(
                        f"/documents/{self.document.id}/{path}", headers=headers
                    ).status_code,
                    404,
                )
            response = client.post(
                "/sync/batches",
                headers=headers,
                json={
                    "id": str(uuid7()),
                    "documents": [
                        {
                            "document_id": str(self.document.id),
                            "size": 10,
                            "hash": DIGEST,
                        }
                    ],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["documents"][0]["upload"]["method"], "PUT")
            oversized = client.post(
                "/sync/batches", content=b"x" * 128001, headers=headers
            )
            self.assertEqual(oversized.status_code, 413)

    def test_download_redirects_sign_only_the_authorized_object(
        self,
    ) -> None:
        from unittest.mock import Mock

        from fastapi.testclient import TestClient

        from app.authentication.service import AuthenticationService
        from app.execution import InlineExecutor
        from app.main import create_app
        from app.settings import Settings
        from app.state import legacy_resources

        image = self.image()
        async_support.run(self.identity.select_avatar(self.owner, image))
        authentication = Mock(spec=AuthenticationService)
        authentication.authenticate.return_value = Identity(
            issuer=self.owner.issuer, subject=self.owner.subject
        )
        application = create_app(
            legacy_resources(
                Settings(_env_file=None),
                InlineExecutor(),
                database=self.db,
                storage=self.storage,
                authentication=authentication,
            )
        )
        heads, prefixes = self.images.heads[:], self.images.prefixes[:]
        headers = {"Authorization": "Bearer test"}
        with TestClient(application, follow_redirects=False) as client:
            responses = (
                client.get(f"/avatars/{self.owner.id}"),
                client.get(f"/images/{image}/content", headers=headers),
            )
            for response in responses:
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.content, b"")
                url = urlsplit(response.headers["location"])
                self.assertEqual(url.netloc, "account.r2.cloudflarestorage.com")
                self.assertEqual(
                    url.path, f"/wright-images/images/{self.owner.id}/{image}"
                )
                query = parse_qs(url.query)
                self.assertEqual(query["X-Amz-Expires"], ["300"])
                self.assertEqual(query["X-Amz-SignedHeaders"], ["host"])
                self.assertEqual(len(query["X-Amz-Signature"][0]), 64)
            self.assertEqual(responses[1].headers["vary"], "Authorization")
            authentication.authenticate.return_value = Identity(
                issuer=self.viewer.issuer, subject=self.viewer.subject
            )
            self.assertEqual(
                client.get(f"/images/{image}/content", headers=headers).status_code, 404
            )
        self.assertEqual(self.images.heads, heads)
        self.assertEqual(self.images.prefixes, prefixes)


if __name__ == "__main__":
    unittest.main()
