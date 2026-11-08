"""Run Arctic's real setup SQL against a fresh in-memory PostgreSQL fixture."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.documents.__main__ import MIGRATION, VERSION, setup, verify
from app.tests.document_database import FixtureDatabase
from app.tests.test_authentication import configuration


class DocumentSetupTests(unittest.TestCase):
    def test_application_lifespan_opens_and_closes_document_resources(self) -> None:
        database = FixtureDatabase()
        self.addCleanup(database.close)
        with (
            patch(
                "app.main.get_settings",
                return_value=configuration(
                    auth_issuer=None,
                    auth_jwks_url=None,
                    documents_database_url="postgresql://unused/postgres",
                ),
            ),
            patch("app.main.PostgresDatabase", return_value=database),
            patch("app.main.build_image_service", return_value=None),
        ):
            app = create_app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/documents").status_code, 401)
            self.assertFalse(database.closed)
        self.assertTrue(database.closed)

    def test_setup_provisions_runtime_role_backfills_profiles_and_replays_safely(
        self,
    ) -> None:
        database = FixtureDatabase(initialize=False)
        self.addCleanup(database.close)
        actor = uuid4()
        database.execute("insert into auth.users values(%s)", (actor,))
        database.execute(
            "insert into public.profiles values(%s,'existing_profile')", (actor,)
        )
        migration = MIGRATION.read_text()
        with database.admin_transaction() as session:
            setup(
                session,
                migration,
                "arctic_api",
                "postgresql://arctic_api:test-only-password@unused/postgres",
            )
            verify(session)
        with database.admin_transaction() as session:
            setup(session, migration, "arctic_api")
            verify(session)
        self.assertEqual(
            database.execute(
                "select owner_user_id,canonical_name from documents.spaces"
            ),
            [{"owner_user_id": str(actor), "canonical_name": "existing_profile"}],
        )
        self.assertEqual(
            database.execute("select name from documents.names"),
            [{"name": "existing_profile"}],
        )
        self.assertEqual(
            database.execute(
                "select version,statements from supabase_migrations.schema_migrations"
            ),
            [{"version": VERSION, "statements": [migration]}],
        )
        self.assertEqual(
            database.execute(
                "select rolcanlogin,rolsuper,rolbypassrls,pg_has_role('arctic_api','arctic_documents','member') as member from pg_roles where rolname='arctic_api'"
            ),
            [
                {
                    "rolcanlogin": True,
                    "rolsuper": False,
                    "rolbypassrls": False,
                    "member": True,
                }
            ],
        )
        with (
            self.assertRaisesRegex(RuntimeError, "different source"),
            database.admin_transaction() as session,
        ):
            setup(session, migration + "\n-- drift", "arctic_api")

    def test_failed_runtime_provisioning_rolls_back_schema_and_version(self) -> None:
        database = FixtureDatabase(initialize=False)
        self.addCleanup(database.close)
        with (
            self.assertRaisesRegex(RuntimeError, "Runtime URL"),
            database.admin_transaction() as session,
        ):
            setup(
                session,
                MIGRATION.read_text(),
                "arctic_api",
                "postgresql://someone_else:password@unused/postgres",
            )
        self.assertEqual(
            database.execute("select to_regnamespace('documents') as name"),
            [{"name": None}],
        )
        self.assertEqual(
            database.execute("select to_regnamespace('supabase_migrations') as name"),
            [{"name": None}],
        )
