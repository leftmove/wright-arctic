"""Process-owned PostgreSQL pool and single-exchange catalog operations."""

import asyncio
import logging
from collections.abc import Sequence
from math import inf
from typing import cast

from psycopg import Error, OperationalError
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests
from pydantic import ValidationError

from app.observability import span
from app.services.catalog.copy import CHANGES, CatalogCopy, Change
from app.services.catalog.listener import (
    CONNECT_TIMEOUT_SECONDS,
    KEEPALIVE_COUNT,
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    CatalogListener,
    Connection,
    verify_catalog_role,
)
from app.services.database import Row, raise_database_error
from app.services.errors import Unavailable
from app.services.operations import check_rejection

logger = logging.getLogger(__name__)
_POOL_CHECK_SECONDS = 30


class NativeDatabase:
    def __init__(
        self,
        url: str,
        *,
        max_connections: int = 4,
        acquisition_seconds: float = 0.25,
        catalog: CatalogCopy | None = None,
        listener_url: str | None = None,
    ) -> None:
        self._url = url
        self._listener_url = listener_url or url
        self._capacity = max_connections
        self._catalog = catalog
        self._maintenance: asyncio.Task[None] | None = None
        self._listener = (
            CatalogListener(self._listener_url, catalog)
            if catalog is not None
            else None
        )
        self._pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
            url,
            min_size=max_connections,
            max_size=max_connections,
            max_lifetime=inf,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 5,
                "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                "keepalives": 1,
                "keepalives_idle": KEEPALIVE_IDLE_SECONDS,
                "keepalives_interval": KEEPALIVE_INTERVAL_SECONDS,
                "keepalives_count": KEEPALIVE_COUNT,
            },
            configure=self._configure,
            timeout=acquisition_seconds,
            max_waiting=16,
            reconnect_timeout=5,
            num_workers=max_connections,
            name="catalog-native",
        )

    async def _configure(self, connection: Connection) -> None:
        connection.prepared_max = 100
        await verify_catalog_role(connection)
        await connection.execute(
            "set default_transaction_isolation='read committed'; set statement_timeout='8s'; "
            "set lock_timeout='2s'; set idle_in_transaction_session_timeout='10s'"
        )

    @property
    def ready(self) -> bool:
        if self._pool.closed or (
            self._catalog is not None and not self._catalog.loaded
        ):
            return False
        return self._pool.get_stats()["pool_size"] == self._capacity

    async def open(self) -> None:
        try:
            for url in (self._url, self._listener_url):
                # A transaction pooler cannot preserve prepared statements or LISTEN.
                if "6543" in str(conninfo_to_dict(url).get("port", "")).split(","):
                    raise Unavailable()
            # The listening session is independent of the query pool.
            async with asyncio.TaskGroup() as opening:
                opening.create_task(self._pool.open(wait=True, timeout=10))
                if self._listener is not None:
                    opening.create_task(self._listener.open())
            self._maintenance = asyncio.create_task(
                self._maintain(), name="catalog-pool"
            )
        except ExceptionGroup as group:
            await self.close()
            if all(
                isinstance(error, PoolTimeout | Error | Unavailable)
                for error in group.exceptions
            ):
                raise Unavailable() from None
            raise
        except BaseException:
            await self.close()
            raise

    async def _maintain(self) -> None:
        while True:
            try:
                await asyncio.sleep(_POOL_CHECK_SECONDS)
                if self._pool.closed:
                    return
                # Replace sessions the pooler dropped while this instance was idle.
                # Checking out a connection does not pay for that probe.
                await self._pool.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Catalog pool check failed. The next pass replaces dead sessions."
                )

    async def close(self) -> None:
        task, self._maintenance = self._maintenance, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            if self._listener is not None:
                await self._listener.close()
        finally:
            await self._pool.close(timeout=3)

    async def fetch(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        return await self._exchange(query, parameters, write=False)

    async def write(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        return await self._exchange(query, parameters, write=True)

    async def _exchange(
        self,
        query: str,
        parameters: Sequence[object],
        *,
        write: bool,
    ) -> list[Row]:
        try:
            with span("pool.acquire"):
                connection = await self._pool.getconn()
            try:
                return await self._execute(connection, query, parameters, write=write)
            finally:
                await self._release(connection)
        except PoolTimeout, TooManyRequests:
            raise Unavailable() from None
        except Error as error:
            check_rejection(error.sqlstate, error.diag.message_detail)
            raise_database_error(error.sqlstate)
        except ValidationError:
            raise Unavailable() from None

    async def _execute(
        self,
        connection: Connection,
        query: str,
        parameters: Sequence[object],
        *,
        write: bool,
    ) -> list[Row]:
        copy = self._catalog if write else None
        try:
            with span("sql"):
                async with (
                    connection.cursor(row_factory=dict_row) as work,
                    connection.cursor(row_factory=dict_row) as changes,
                ):
                    # Fetch only after pipeline exit. A read uses the same path
                    # so preparing a hot statement never adds a synchronous wait.
                    async with connection.pipeline():
                        if write:
                            await connection.execute(
                                "begin isolation level read committed", prepare=False
                            )
                        await work.execute(query, parameters or None)
                        if write:
                            await connection.execute("commit", prepare=False)
                        if copy is not None:
                            # Deferred log rows are visible after COMMIT. The
                            # query ships with COMMIT, so there is no second wait.
                            await changes.execute(CHANGES, (copy.applied,))
                    if copy is not None:
                        copy.apply(map(Change.model_validate, await changes.fetchall()))
                    return (
                        cast(list[Row], await work.fetchall())
                        if work.description
                        else []
                    )
        except OperationalError, ValidationError, asyncio.CancelledError:
            if copy is not None:
                copy.connected = False
            raise

    async def _release(self, connection: Connection) -> None:
        # A failed pipeline must not go back into the pool for the next request.
        if (
            connection.closed
            or connection.pgconn.transaction_status != TransactionStatus.IDLE
        ):
            await connection.close()
        await self._pool.putconn(connection)
