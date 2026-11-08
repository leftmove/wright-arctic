"""Private server-side SQL sessions; verified actors are passed as parameters."""

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol, cast

from psycopg import Connection, Error, OperationalError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests

from app.services.documents.errors import Conflict, Unavailable

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
        with self._connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, parameters or None)
            if cursor.description is None:
                return []
            return cast(list[Row], cursor.fetchall())


class PostgresDatabase:
    def __init__(self, url: str, *, max_connections: int = 4) -> None:
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
        with self.transaction() as session:
            # Authentication is enforced by Arctic. Storage must be private to
            # its backend role; neither a Supabase client nor schema owner.
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

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        try:
            with self._pool.connection() as connection, connection.transaction():
                session = PostgresSession(connection)
                session.execute("set local statement_timeout='15s'")
                session.execute("set local lock_timeout='5s'")
                yield session
        except PoolTimeout, TooManyRequests, OperationalError:
            raise Unavailable() from None
        except Error as error:
            if error.sqlstate in {"40001", "40P01", "55P03"}:
                raise Conflict("retry_transaction") from None
            if error.sqlstate in {"23505", "23503", "23514", "23001"}:
                raise Conflict() from None
            raise Unavailable() from None
