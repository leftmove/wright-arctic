"""Opt-in hosted PostgreSQL checks; document fixtures always roll back.

Run with WRIGHT_ARCTIC_TEST_HOSTED_DOCUMENTS=1 after explicit schema setup.
An existing profile supplies the account FK; no Auth users or profiles are edited.
"""

import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar
from uuid import UUID, uuid4, uuid7

import psycopg
from psycopg import Connection
from psycopg.errors import ForeignKeyViolation, LockNotAvailable
from pydantic import JsonValue, TypeAdapter

from app.accounts import Account
from app.dependencies import get_settings
from app.schemas import CreateDocument, RenameDocument, ShareDocument
from app.services.documents import native
from app.services.documents.database import PostgresDatabase, PostgresSession, Session
from app.services.documents.errors import Conflict, NotFound
from app.services.documents.service import DocumentService
from app.settings import Settings


class _RollbackDatabase:
    """Join a test-owned outer transaction using real PostgreSQL savepoints."""

    def __init__(
        self, connection: Connection[tuple[object, ...]], *, lock_timeout: str = "5s"
    ) -> None:
        self.connection = connection
        self.lock_timeout = lock_timeout

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.connection.transaction():
            session = PostgresSession(self.connection)
            session.execute("set local statement_timeout='15s'")
            session.execute(
                "select set_config('lock_timeout',%s,true)", (self.lock_timeout,)
            )
            yield session


def _model(text: str) -> dict[str, JsonValue]:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class HostedDocumentTests(unittest.TestCase):
    settings: ClassVar[Settings]
    account: ClassVar[Account]
    space: ClassVar[UUID]

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("WRIGHT_ARCTIC_TEST_HOSTED_DOCUMENTS") != "1":
            raise unittest.SkipTest("Hosted PostgreSQL checks require explicit opt-in")
        cls.settings = get_settings()
        if (
            cls.settings.documents_setup_url is None
            or cls.settings.documents_database_url is None
        ):
            raise RuntimeError(
                "Configure setup/runtime database URLs before hosted verification"
            )
        with psycopg.connect(
            cls.settings.documents_setup_url.get_secret_value(),
            connect_timeout=10,
            autocommit=True,
        ) as connection:
            rows = PostgresSession(connection).execute(
                "select owner_user_id,id from documents.spaces order by id limit 1"
            )
        if not rows:
            raise RuntimeError(
                "Hosted verification requires an existing profile; no test accounts are provisioned"
            )
        cls.account = Account(
            id=str(rows[0]["owner_user_id"]),
            issuer="https://jhacsyfrssyqukdqwxgc.supabase.co/auth/v1",
        )
        cls.space = TypeAdapter(UUID).validate_python(rows[0]["id"])

    def connect(self) -> Connection[tuple[object, ...]]:
        assert self.settings.documents_database_url is not None
        return psycopg.connect(
            self.settings.documents_database_url.get_secret_value(),
            connect_timeout=10,
            autocommit=True,
            prepare_threshold=None,
        )

    def counts(self, session: Session) -> tuple[int, ...]:
        row = session.execute(
            """select
          (select count(*) from documents.documents) as documents,
          (select count(*) from documents.changes) as changes,
          (select count(*) from documents.slugs) as slugs,
          (select count(*) from documents.snapshots) as snapshots,
          (select next_slug from documents.spaces where id=%s) as next_slug""",
            (self.space,),
        )[0]
        return tuple(
            TypeAdapter(int).validate_python(row[key])
            for key in ("documents", "changes", "slugs", "snapshots", "next_slug")
        )

    def test_runtime_pool_opens_and_client_roles_cannot_access_storage(self) -> None:
        assert self.settings.documents_database_url is not None
        database = PostgresDatabase(
            self.settings.documents_database_url.get_secret_value(), max_connections=1
        )
        self.addCleanup(database.close)
        database.open()
        with database.transaction() as session:
            rows = session.execute("""select c.relname,c.relrowsecurity,c.relowner=(select oid from pg_roles where rolname=current_user) as owned
              from pg_class c join pg_namespace n on n.oid=c.relnamespace
              where n.nspname='documents' and c.relkind='r' order by c.relname""")
            self.assertEqual(
                [row["relname"] for row in rows],
                [
                    "access",
                    "changes",
                    "documents",
                    "names",
                    "slugs",
                    "snapshots",
                    "spaces",
                ],
            )
            self.assertFalse(any(row["relrowsecurity"] or row["owned"] for row in rows))
            for role in ("anon", "authenticated"):
                privileges = session.execute(
                    "select has_schema_privilege(%s,'documents','usage') as allowed",
                    (role,),
                )
                self.assertEqual(privileges, [{"allowed": False}])
            self.assertEqual(
                session.execute(
                    "select has_table_privilege(current_user,'documents.changes','UPDATE') as update,has_table_privilege(current_user,'documents.changes','DELETE') as delete"
                ),
                [{"update": False, "delete": False}],
            )

    def test_document_lifecycle_and_native_history_leave_no_fixture_data(self) -> None:
        with self.connect() as connection:
            session = PostgresSession(connection)
            before = self.counts(session)
            with connection.transaction(force_rollback=True):
                service = DocumentService(_RollbackDatabase(connection))
                command = CreateDocument(
                    operation_id=uuid4(), content=_model("Hosted initial")
                )
                created = service.create(self.account, command)
                target = created.document_id
                self.assertEqual(service.create(self.account, command), created)
                initial = service.state(self.account, target)[1]
                edited = native.restore(
                    "wright-rich-text",
                    initial.state,
                    native.create("wright-rich-text", _model("Hosted edited")).state,
                )
                operation = uuid4()
                accepted = service.append(
                    self.account, target, [operation], [edited.update]
                )
                self.assertEqual(accepted[0].head_seq, 1)
                self.assertEqual(
                    service.append(self.account, target, [operation], [edited.update]),
                    accepted,
                )
                self.assertEqual(service.change(self.account, target, 1), edited.update)
                rename = service.rename(
                    self.account,
                    target,
                    RenameDocument(
                        operation_id=uuid4(),
                        expected_metadata_seq=0,
                        title="Hosted explicit",
                    ),
                )
                self.assertEqual(rename.head_seq, 2)
                # PostgreSQL enforces the managed-account FK; the inner savepoint
                # rolls back without changing the previously committed prefix.
                with self.assertRaises(ForeignKeyViolation):
                    service.grant(self.account, target, uuid4(), uuid4(), "viewer")
                self.assertEqual(service.metadata(self.account, target).head_seq, 2)
                shared = service.share(
                    self.account, target, ShareDocument(operation_id=uuid4())
                )
                self.assertIsNotNone(shared.path)
                self.assertEqual(service.snapshot(self.account, target, 1), 1)
                restored = service.restore(self.account, target, uuid4(), 0)
                self.assertEqual(restored.head_seq, 4)
                self.assertEqual(
                    service.state(self.account, target)[1].projection,
                    initial.projection,
                )
                self.assertEqual(
                    service.metadata(self.account, target).title, "Hosted explicit"
                )
                service.lifecycle(self.account, target, uuid4(), True)
                with self.assertRaises(NotFound):
                    service.metadata(self.account, target)
                service.lifecycle(self.account, target, uuid4(), False)
                self.assertEqual(service.metadata(self.account, target).head_seq, 6)
                stranger = Account(id=str(uuid4()), issuer=self.account.issuer)
                with self.assertRaises(NotFound):
                    service.content(stranger, target)
                self.assertEqual(
                    [
                        event.seq
                        for event in service.history(self.account, target, -1, 128)
                    ],
                    list(range(7)),
                )
            self.assertEqual(self.counts(session), before)

    def test_independent_connections_serialize_identical_create_operations(
        self,
    ) -> None:
        with self.connect() as first, self.connect() as second:
            baseline = self.counts(PostgresSession(first))
            command = CreateDocument(
                operation_id=uuid4(), content=_model("Hosted concurrent rollback")
            )
            first_service = DocumentService(_RollbackDatabase(first))
            second_service = DocumentService(
                _RollbackDatabase(second, lock_timeout="250ms")
            )
            with second.transaction(force_rollback=True):
                with first.transaction(force_rollback=True):
                    first_service.create(self.account, command)
                    with self.assertRaises(LockNotAvailable):
                        second_service.create(self.account, command)
                # The first transaction was rolled back. The waiting command can
                # retry unchanged after its savepoint recovers from the timeout.
                receipt = second_service.create(self.account, command)
                self.assertEqual(receipt.head_seq, 0)
                self.assertEqual(second_service.create(self.account, command), receipt)
            self.assertEqual(self.counts(PostgresSession(first)), baseline)

    def test_deferred_commit_failure_rolls_back_through_production_pool(self) -> None:
        assert self.settings.documents_database_url is not None
        database = PostgresDatabase(
            self.settings.documents_database_url.get_secret_value(), max_connections=1
        )
        self.addCleanup(database.close)
        database.open()
        with database.transaction() as session:
            before = self.counts(session)
        with self.assertRaises(Conflict), database.transaction() as session:
            # Omit event zero deliberately: the deferred committed-head FK fails
            # at COMMIT, after INSERT has returned, and the pool must roll it back.
            session.execute(
                """insert into documents.documents(id,space_id,created_by,title,title_is_user_set,
              format_version,schema_name,schema_version) values(%s,%s,%s,%s,false,1,'wright-rich-text',1)""",
                (uuid7(), self.space, UUID(self.account.id), "Hosted rollback probe"),
            )
        with database.transaction() as session:
            self.assertEqual(self.counts(session), before)
