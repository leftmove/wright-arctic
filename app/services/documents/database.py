"""Private server-side SQL sessions; verified actors are passed as parameters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, NoReturn, Protocol, cast

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool

from app.services.documents.errors import Conflict, Unavailable
from app.services.documents.values import JsonParameter

type Row = dict[str, object]


class Session(Protocol):
    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]: ...


class Database(Protocol):
    def transaction(self) -> AbstractContextManager[Session]: ...
    def open(self) -> None: ...
    def close(self) -> None: ...


class PostgresSession:
    def __init__(self, connection: Connection[tuple[object, ...]]) -> None:
        self._connection = connection

    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        parameters = [
            Jsonb(value.obj) if isinstance(value, JsonParameter) else value
            for value in parameters
        ]
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
            name="documents",
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
                session.execute("set local statement_timeout='15s'")
                session.execute("set local lock_timeout='5s'")
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
    rows = session.execute("""select r.rolsuper, r.rolbypassrls,
            exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
              where n.nspname='documents' and c.relowner=r.oid) as owns_storage
            from pg_roles r where r.rolname=current_user""")
    if not rows or any(rows[0].values()):
        raise Unavailable()
    flags = session.execute("""select c.relrowsecurity from pg_class c join pg_namespace n
            on n.oid=c.relnamespace where n.nspname='documents' and c.relkind='r'""")
    if len(flags) != 7 or any(row["relrowsecurity"] for row in flags):
        raise Unavailable()
    session.execute("select id from documents.documents limit 0")
