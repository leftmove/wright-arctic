"""Opt-in independent-connection PostgreSQL races against a provisioned test catalog.

WRIGHT_ARCTIC_TEST_POSTGRES_URL must name a private runtime login, never the
administrator. Only accounts/documents allocated by each test are removed.
"""

import os
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import UUID, uuid7

import psycopg
from pydantic import SecretStr, TypeAdapter

from app.accounts import Identity
from app.schemas import (
    CheckpointUpload,
    CreateDocument,
    PublishBatch,
    PublishItem,
    StartBatch,
    UploadSlot,
)
from app.services.database import (
    PostgresDatabase,
    PostgresSession,
    Session,
    verify_runtime,
)
from app.services.documents.service import DocumentService
from app.services.identity.service import IdentityService
from app.services.models import Account
from app.services.storage.signing import S3Signer
from app.services.sync.service import SyncService
from app.tests import async_support
from app.tests.image_storage import Checkpoints


class ConnectionDatabase:
    def __init__(self, connection: psycopg.Connection[tuple[object, ...]]) -> None:
        self.connection = connection
        self.backend_pid: int | None = None

    def open(self) -> None:
        verify_runtime(self)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.connection.transaction():
            session = PostgresSession(self.connection)
            session.execute("set transaction isolation level read committed")
            session.execute("set local lock_timeout='5s'")
            session.execute("set local statement_timeout='10s'")
            self.backend_pid = TypeAdapter(int).validate_python(
                session.execute("select pg_backend_pid() as pid")[0]["pid"]
            )
            yield session


@unittest.skipUnless(
    os.environ.get("WRIGHT_ARCTIC_TEST_POSTGRES_URL"),
    "Independent PostgreSQL connections require an explicit test URL",
)
class PostgresConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.url = SecretStr(os.environ["WRIGHT_ARCTIC_TEST_POSTGRES_URL"])
        self.database = PostgresDatabase(self.url.get_secret_value())
        self.addCleanup(self.database.close)
        self.database.open()
        self.accounts: list[UUID] = []
        self.addCleanup(self.remove_records)
        identity = IdentityService(async_support.database(self.database))
        self.owner = async_support.run(
            identity.resolve(
                Identity(issuer="https://postgres-test.invalid", subject=str(uuid7()))
            )
        )
        self.accounts.append(self.owner.id)
        self.editor = async_support.run(
            identity.resolve(
                Identity(issuer="https://postgres-test.invalid", subject=str(uuid7()))
            )
        )
        self.accounts.append(self.editor.id)
        self.documents = DocumentService(async_support.database(self.database))
        self.document = async_support.run(
            self.documents.create(
                self.owner,
                CreateDocument(
                    id=uuid7(),
                    space=async_support.run(self.documents.spaces(self.owner))[0].id,
                ),
            )
        )
        async_support.run(
            self.documents.grant(self.owner, self.document.id, self.editor.id, "editor")
        )
        self.objects = Checkpoints()
        self.signer = S3Signer(
            "https://test.r2.cloudflarestorage.com",
            "wright-documents",
            "auto",
            "access",
            "secret",
        )
        self.sync = SyncService(
            async_support.database(self.database),
            async_support.checkpoints(self.objects),
            self.signer,
        )

    def remove_records(self) -> None:
        ids = self.accounts
        with self.database.transaction() as session:
            session.execute(
                """update documents.documents set revision=null,key=null,published_at=null
                where space in(select id from documents.spaces where owner=any(%s::uuid[]))""",
                (ids,),
            )
            session.execute(
                "delete from documents.documents where space in(select id from documents.spaces where owner=any(%s::uuid[]))",
                (ids,),
            )
            session.execute(
                "delete from coordination.batches where account=any(%s::uuid[])", (ids,)
            )
            session.execute(
                "delete from documents.spaces where owner=any(%s::uuid[])", (ids,)
            )
            session.execute(
                "delete from public.accounts where id=any(%s::uuid[])", (ids,)
            )

    def issue(self, account: Account) -> tuple[UUID, UploadSlot]:
        batch = async_support.run(
            self.sync.start(
                account,
                StartBatch(
                    id=uuid7(),
                    documents=[
                        CheckpointUpload(
                            document_id=self.document.id, size=10, hash="a" * 64
                        )
                    ],
                ),
            )
        )
        slot = batch.documents[0]
        self.objects.upload(slot)
        return batch.id, slot

    def command(self, slot: UploadSlot) -> PublishBatch:
        return PublishBatch(
            documents=[
                PublishItem(
                    document_id=slot.document_id,
                    base_revision=slot.base_revision,
                    revision=slot.revision,
                    key=slot.key,
                )
            ]
        )

    def test_two_publish_connections_commit_one_winner(self) -> None:
        owner_batch, first = self.issue(self.owner)
        editor_batch, second = self.issue(self.editor)
        self.objects.barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(
                lambda: async_support.run(
                    self.sync.publish(self.owner, owner_batch, self.command(first))
                )
            )
            editor = pool.submit(
                lambda: async_support.run(
                    self.sync.publish(self.editor, editor_batch, self.command(second))
                )
            )
            results = [owner.result(timeout=15), editor.result(timeout=15)]
        self.objects.barrier = None
        self.assertEqual(
            sorted(result.documents[0].result for result in results),
            ["conflict", "published"],
        )
        loser = first if results[0].documents[0].result == "conflict" else second
        self.assertNotIn(loser.key, self.objects.objects)

    def test_grant_revocation_during_lock_wait_is_read_after_the_lock(self) -> None:
        batch, slot = self.issue(self.editor)
        with (
            psycopg.connect(
                self.url.get_secret_value(), autocommit=True, prepare_threshold=None
            ) as publisher,
            psycopg.connect(
                self.url.get_secret_value(), autocommit=True, prepare_threshold=None
            ) as revoker,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            database = ConnectionDatabase(publisher)
            service = SyncService(
                async_support.database(database),
                async_support.checkpoints(self.objects),
                self.signer,
            )
            with revoker.transaction():
                session = PostgresSession(revoker)
                session.execute(
                    "select id from documents.documents where id=%s for update",
                    (self.document.id,),
                )
                session.execute(
                    "delete from documents.grants where document_id=%s and account_id=%s",
                    (self.document.id, self.editor.id),
                )
                future = pool.submit(
                    lambda: async_support.run(
                        service.publish(self.editor, batch, self.command(slot))
                    )
                )
                deadline = time.monotonic() + 3
                waiting = False
                while time.monotonic() < deadline:
                    with self.database.transaction() as observer:
                        rows = observer.execute(
                            "select wait_event_type from pg_stat_activity where pid=%s",
                            (database.backend_pid,),
                        )
                    if rows and rows[0]["wait_event_type"] == "Lock":
                        waiting = True
                        break
                    time.sleep(0.01)
                self.assertTrue(waiting, "Publisher did not reach the document lock")
            result = future.result(timeout=10)
        self.assertEqual(result.documents[0].result, "forbidden")
        self.assertIsNone(
            async_support.run(
                self.sync.open(self.owner, self.document.id, None)
            ).revision
        )
        self.assertNotIn(slot.key, self.objects.objects)

    def test_publish_rechecks_checkpoint_state_after_cleanup_commits(self) -> None:
        batch, slot = self.issue(self.owner)
        with (
            psycopg.connect(
                self.url.get_secret_value(), autocommit=True, prepare_threshold=None
            ) as publisher,
            psycopg.connect(
                self.url.get_secret_value(), autocommit=True, prepare_threshold=None
            ) as cleaner,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            database = ConnectionDatabase(publisher)
            service = SyncService(
                async_support.database(database),
                async_support.checkpoints(self.objects),
                self.signer,
            )
            with cleaner.transaction():
                session = PostgresSession(cleaner)
                session.execute(
                    """update documents.checkpoints set state='pending'
                    where document_id=%s and revision=%s""",
                    (slot.document_id, slot.revision),
                )
                future = pool.submit(
                    lambda: async_support.run(
                        service.publish(self.owner, batch, self.command(slot))
                    )
                )
                deadline = time.monotonic() + 3
                waiting = False
                while time.monotonic() < deadline:
                    with self.database.transaction() as observer:
                        rows = observer.execute(
                            "select wait_event_type from pg_stat_activity where pid=%s",
                            (database.backend_pid,),
                        )
                    if rows and rows[0]["wait_event_type"] == "Lock":
                        waiting = True
                        break
                    time.sleep(0.01)
                self.assertTrue(waiting, "Publisher did not reach the checkpoint lock")
            result = future.result(timeout=10)
        self.assertEqual(result.documents[0].result, "expired")
        self.assertIsNone(
            async_support.run(
                self.sync.open(self.owner, self.document.id, None)
            ).revision
        )
        self.assertNotIn(slot.key, self.objects.objects)
