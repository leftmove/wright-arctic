"""Install or verify the fresh metadata catalog using a separate admin login.

Setup intentionally drops the previous document schema on its first application.
Subsequent invocations verify the exact recorded source and preserve new data.
"""

import argparse
import hashlib
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import TypeAdapter

from app.dependencies import get_settings
from app.services.catalog.migrations import migrate
from app.services.database import PostgresSession, Session, verify_catalog
from app.services.errors import ServiceError

VERSION = "20261115000100"
SOURCE = Path(__file__).with_name("sql") / f"{VERSION}_catalog.sql"


def setup(session: Session, source: str) -> None:
    session.execute("create schema if not exists supabase_migrations")
    session.execute("""create table if not exists supabase_migrations.schema_migrations
        (version text primary key,statements text[],name text)""")
    existing = session.execute(
        "select statements from supabase_migrations.schema_migrations where version=%s",
        (VERSION,),
    )
    if existing:
        if existing[0]["statements"] != [source]:
            raise RuntimeError("Catalog version has different source; nothing applied")
        return
    session.execute(source)
    session.execute(
        """insert into supabase_migrations.schema_migrations(version,name,statements)
            values(%s,'catalog',%s)""",
        (VERSION, [source]),
    )


def grant_runtime(session: Session, name: str, runtime_url: str | None) -> None:
    rows = session.execute(
        """select r.rolsuper,r.rolbypassrls,not r.rolcanlogin as cannot_login,
            exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
              where (n.nspname in ('documents','coordination')
                or (n.nspname='public' and c.relname in ('accounts','images')))
                and c.relowner=r.oid) as owns_storage
            from pg_roles r where r.rolname=%s""",
        (name,),
    )
    if rows and any(rows[0].values()):
        raise RuntimeError("Runtime login must not own storage or bypass permissions")
    if not rows:
        credentials = conninfo_to_dict(runtime_url or "")
        username, password = credentials.get("user", ""), credentials.get("password")
        if (
            not isinstance(password, str)
            or not password
            or not isinstance(username, str)
            or not (username == name or username.startswith(name + "."))
        ):
            raise RuntimeError("Runtime URL must contain this login and its password")
        query = session.execute(
            """select format('create role %%I login inherit nosuperuser nocreatedb
                nocreaterole noreplication nobypassrls password %%L',%s::text,%s::text) as sql""",
            (name, password),
        )
        session.execute(TypeAdapter(str).validate_python(query[0]["sql"]))
    query = session.execute(
        "select format('grant arctic_catalog to %%I',%s::text) as sql", (name,)
    )
    session.execute(TypeAdapter(str).validate_python(query[0]["sql"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "migrate", "verify"))
    parser.add_argument("--grant-to", help="Private runtime login to provision")
    arguments = parser.parse_args()
    settings = get_settings()
    if settings.documents_setup_url is None:
        parser.error("WRIGHT_ARCTIC_DOCUMENTS_SETUP_URL is required for this command")
    source = SOURCE.read_text()
    try:
        with (
            psycopg.connect(
                settings.documents_setup_url.get_secret_value()
            ) as connection,
            connection.transaction(),
        ):
            session = PostgresSession(connection)
            session.execute(
                "select pg_advisory_xact_lock(hashtextextended('wright/catalog/setup',0))"
            )
            if arguments.command == "setup":
                setup(session, source)
                if arguments.grant_to is not None:
                    grant_runtime(
                        session,
                        arguments.grant_to,
                        settings.documents_database_url.get_secret_value()
                        if settings.documents_database_url is not None
                        else None,
                    )
            if arguments.command == "migrate":
                migrate(session)
            rows = session.execute(
                "select statements from supabase_migrations.schema_migrations where version=%s",
                (VERSION,),
            )
            if not rows or rows[0]["statements"] != [source]:
                raise RuntimeError("Catalog has not been provisioned from this source")
            verify_catalog(session)
    except psycopg.Error:
        parser.exit(
            1,
            "Catalog setup failed; transaction rolled back. Check credentials and privileges.\n",
        )
    except RuntimeError as error:
        parser.exit(1, f"{error}. Transaction rolled back.\n")
    except ServiceError:
        parser.exit(
            1, "Catalog shape does not match this source. Transaction rolled back.\n"
        )
    print(
        f"Catalog ready: {VERSION}; source SHA256 {hashlib.sha256(source.encode()).hexdigest()}"
    )


if __name__ == "__main__":
    main()
