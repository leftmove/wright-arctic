"""Process-owned native async Psycopg sessions with bounded acquisition."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import cast

from psycopg import AsyncConnection, Error, OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests

from app.observability import span
from app.services.database import AsyncSession, Row, raise_database_error
from app.services.errors import Unavailable


class NativeSession:
    def __init__(self, connection: AsyncConnection[tuple[object, ...]]) -> None:
        self.connection = connection

    async def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        with span("sql"):
            async with self.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, parameters or None)
                return (
                    cast(list[Row], await cursor.fetchall())
                    if cursor.description
                    else []
                )


ROLE_QUERY = """select r.rolsuper,r.rolbypassrls,
    exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
      where (n.nspname in ('documents','coordination')
        or (n.nspname='public' and c.relname in ('accounts','images')))
        and c.relowner=r.oid) as owns_storage,
    pg_has_role(current_user,'arctic_catalog','member') as catalog_member
    from pg_roles r where r.rolname=current_user"""


class NativeDatabase:
    def __init__(
        self, url: str, *, max_connections: int = 4, acquisition_seconds: float = 0.25
    ) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[tuple[object, ...]]] = (
            AsyncConnectionPool(
                url,
                min_size=1,
                max_size=max_connections,
                open=False,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 5,
                    "prepare_threshold": 5,
                },
                configure=self._configure,
                timeout=acquisition_seconds,
                max_waiting=16,
                reconnect_timeout=5,
                name="catalog-native",
            )
        )

    async def _configure(self, connection: AsyncConnection[tuple[object, ...]]) -> None:
        connection.prepared_max = 100
        session = NativeSession(connection)
        rows = await session.execute(ROLE_QUERY)
        if (
            not rows
            or any(rows[0][key] for key in ("rolsuper", "rolbypassrls", "owns_storage"))
            or rows[0]["catalog_member"] is not True
        ):
            raise Unavailable()
        await session.execute(
            "set default_transaction_isolation='read committed'; set statement_timeout='8s'; set lock_timeout='2s'; set idle_in_transaction_session_timeout='10s'"
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=6)

    async def close(self) -> None:
        await self._pool.close(timeout=4)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        try:
            with span("pool.acquire"):
                connection = await self._pool.getconn()
            try:
                yield NativeSession(connection)
            finally:
                await self._pool.putconn(connection)
        except PoolTimeout, TooManyRequests, OperationalError:
            raise Unavailable() from None
        except Error as error:
            raise_database_error(error.sqlstate)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.read() as session:
            assert isinstance(session, NativeSession)
            # Psycopg rolls back on cancellation; its pool discards broken
            # connections. Never automatically retry an ambiguous COMMIT.
            with span("transaction"):
                async with session.connection.transaction():
                    yield session
