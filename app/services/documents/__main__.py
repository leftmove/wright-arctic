"""Explicit schema setup against the existing Supabase version ledger.

No setup runs during web startup. The admin connection is never used by the API.
"""

import argparse
import hashlib
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import TypeAdapter

from app.dependencies import get_settings
from app.services.documents.database import PostgresSession, Session

VERSION = "20261107000600"
MIGRATION = Path(__file__).with_name("sql") / f"{VERSION}_documents.sql"


def setup(
    connection: Session,
    migration: str,
    grant_to: str | None,
    runtime_url: str | None = None,
) -> None:
    """Apply one recorded version and provision the private runtime login."""
    connection.execute("create schema if not exists supabase_migrations")
    connection.execute("""create table if not exists supabase_migrations.schema_migrations
      (version text primary key,statements text[],name text)""")
    existing = connection.execute(
        "select statements from supabase_migrations.schema_migrations where version=%s",
        (VERSION,),
    )
    if existing:
        if existing[0]["statements"] != [migration]:
            raise RuntimeError(
                "Document ledger version exists with different source; no changes applied"
            )
    else:
        if (
            connection.execute("select to_regnamespace('documents') as name")[0]["name"]
            is not None
        ):
            raise RuntimeError("Untracked document schema exists; no changes applied")
        connection.execute(migration)
        connection.execute(
            "insert into supabase_migrations.schema_migrations(version,name,statements) values(%s,'documents',%s)",
            (VERSION, [migration]),
        )
    if grant_to is not None:
        role = connection.execute(
            """select r.rolsuper,r.rolbypassrls,
          exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='documents' and c.relowner=r.oid)
          from pg_roles r where r.rolname=%s and r.rolcanlogin""",
            (grant_to,),
        )
        if not role:
            if runtime_url is None:
                raise RuntimeError(
                    "Set DOCUMENTS_DATABASE_URL to provision a runtime login"
                )
            credentials = conninfo_to_dict(runtime_url)
            username, password = (
                credentials.get("user", ""),
                credentials.get("password"),
            )
            if (
                not isinstance(username, str)
                or not isinstance(password, str)
                or not password
                or not (username == grant_to or username.startswith(grant_to + "."))
            ):
                raise RuntimeError(
                    "Runtime URL must contain the requested login and its password"
                )
            command = connection.execute(
                """select format('create role %%I login inherit nosuperuser
              nocreatedb nocreaterole noreplication nobypassrls password %%L',%s::text,%s::text) as command""",
                (grant_to, password),
            )
            connection.execute(TypeAdapter(str).validate_python(command[0]["command"]))
        elif any(role[0].values()):
            raise RuntimeError(
                "Runtime login must not own storage or bypass permissions"
            )
        command = connection.execute(
            "select format('grant arctic_documents to %%I',%s::text) as command",
            (grant_to,),
        )
        connection.execute(TypeAdapter(str).validate_python(command[0]["command"]))


def verify(connection: Session) -> None:
    tables = connection.execute("""select c.relname,c.relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace
      where n.nspname='documents' and c.relkind='r' order by c.relname""")
    if [row["relname"] for row in tables] != [
        "access",
        "changes",
        "documents",
        "names",
        "slugs",
        "snapshots",
        "spaces",
    ] or any(row["relrowsecurity"] for row in tables):
        raise RuntimeError("The seven-table server-side schema is not ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "verify"])
    parser.add_argument(
        "--grant-to",
        help="Runtime login; create it from DOCUMENTS_DATABASE_URL credentials when absent",
    )
    arguments = parser.parse_args()
    settings = get_settings()
    if settings.documents_setup_url is None:
        parser.error(
            "WRIGHT_ARCTIC_DOCUMENTS_SETUP_URL is required; it is never used by the API"
        )
    migration = MIGRATION.read_text()
    try:
        with (
            psycopg.connect(
                settings.documents_setup_url.get_secret_value(), autocommit=True
            ) as connection,
            connection.transaction(),
        ):
            session = PostgresSession(connection)
            session.execute(
                "select pg_advisory_xact_lock(hashtextextended('wright/documents/setup',0))"
            )
            if arguments.command == "setup":
                setup(
                    session,
                    migration,
                    arguments.grant_to,
                    None
                    if settings.documents_database_url is None
                    else settings.documents_database_url.get_secret_value(),
                )
            verify(session)
    except psycopg.Error:
        parser.exit(
            1,
            "Document setup failed; transaction rolled back. Check the database connection and privileges.\n",
        )
    except RuntimeError as error:
        parser.exit(1, f"{error}. Transaction rolled back.\n")
    print(
        f"Document schema ready: {VERSION}; source SHA256 {hashlib.sha256(migration.encode()).hexdigest()}"
    )


if __name__ == "__main__":
    main()
