"""Private server-side SQL sessions; verified actors are passed as parameters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
)
from typing import TYPE_CHECKING, NoReturn, Protocol, cast

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

from app.services.errors import Conflict, Unavailable

type Row = dict[str, object]


class AsyncSession(Protocol):
    async def execute(
        self, query: str, parameters: Sequence[object] = ()
    ) -> list[Row]: ...


class AsyncDatabase(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]: ...
    def read(self) -> AbstractAsyncContextManager[AsyncSession]: ...
    async def open(self) -> None: ...
    async def close(self) -> None: ...


TRANSACTION_SETTINGS = "set local statement_timeout='15s'; set local lock_timeout='5s'"


class Session(Protocol):
    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]: ...


class Database(Protocol):
    """Transactions use READ COMMITTED so permission reads follow acquired locks."""

    def transaction(self) -> AbstractContextManager[Session]: ...
    def open(self) -> None: ...
    def close(self) -> None: ...


class PostgresSession:
    def __init__(self, connection: Connection[tuple[object, ...]]) -> None:
        self._connection = connection

    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        from psycopg.rows import dict_row

        with self._connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, parameters or None)
            if cursor.description is None:
                return []
            return cast(list[Row], cursor.fetchall())


class PostgresDatabase:
    def __init__(self, url: str, *, max_connections: int = 4) -> None:
        from psycopg_pool import ConnectionPool

        self._pool: ConnectionPool[Connection[tuple[object, ...]]] = ConnectionPool(
            url,
            min_size=0,
            max_size=max_connections,
            open=False,
            kwargs={
                "autocommit": True,
                "connect_timeout": 5,
                "prepare_threshold": None,
            },
            timeout=10,
            max_waiting=32,
            name="catalog",
        )

    def open(self) -> None:
        self._pool.open()
        verify_runtime(self)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        from psycopg import Error, OperationalError
        from psycopg_pool import PoolTimeout, TooManyRequests

        try:
            with self._pool.connection() as connection, connection.transaction():
                session = PostgresSession(connection)
                session.execute("set transaction isolation level read committed")
                session.execute(TRANSACTION_SETTINGS)
                yield session
        except PoolTimeout, TooManyRequests, OperationalError:
            raise Unavailable() from None
        except Error as error:
            raise_database_error(error.sqlstate)


def raise_database_error(sqlstate: str | None) -> NoReturn:
    if sqlstate in {"40001", "40P01", "55P03"}:
        raise Conflict("retry_transaction") from None
    if sqlstate in {"23505", "23503", "23514", "23001"}:
        raise Conflict() from None
    raise Unavailable() from None


def verify_runtime(database: Database) -> None:
    """Verify the existing private schema using only the runtime login."""
    with database.transaction() as session:
        verify_session(session)


def verify_session(session: Session) -> None:
    verify_role(session)
    verify_catalog(session)


def verify_role(session: Session) -> None:
    rows = session.execute("""select r.rolsuper, r.rolbypassrls,
            exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
              where (n.nspname in ('documents','coordination')
                or (n.nspname='public' and c.relname in ('accounts','images')))
                and c.relowner=r.oid) as owns_storage
            from pg_roles r where r.rolname=current_user""")
    if not rows or any(rows[0].values()):
        raise Unavailable()


def verify_catalog(session: Session) -> None:
    """Check the same catalog shape with either an admin or a runtime login."""
    flags = session.execute("""select n.nspname,c.relname,c.relrowsecurity
            from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where (n.nspname in ('documents','coordination')
              or (n.nspname='public' and c.relname in ('accounts','images')))
              and c.relkind='r'""")
    expected = {
        ("documents", "spaces"),
        ("documents", "documents"),
        ("documents", "checkpoints"),
        ("documents", "grants"),
        ("documents", "images"),
        ("public", "accounts"),
        ("public", "images"),
        ("coordination", "batches"),
        ("coordination", "items"),
        ("coordination", "attempts"),
        ("coordination", "images"),
        ("coordination", "uploads"),
    }
    if {(row["nspname"], row["relname"]) for row in flags} != expected or any(
        row["relrowsecurity"] for row in flags
    ):
        raise Unavailable()
    session.execute("select id,revision,key from documents.documents limit 0")
    session.execute("select id,avatar_id,last_sync from public.accounts limit 0")
