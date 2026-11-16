"""Listen on a dedicated session and replay the durable catalog feed."""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from typing import cast

from psycopg import AsyncConnection, Error
from psycopg.rows import dict_row
from pydantic import ValidationError

from app.services.catalog.copy import (
    CHANGES,
    CHANNEL,
    SNAPSHOT,
    CatalogCopy,
    Change,
    Snapshot,
)
from app.services.database import Row
from app.services.errors import Unavailable

type Connection = AsyncConnection[tuple[object, ...]]

logger = logging.getLogger(__name__)
# Probe before Cloud Run's idle path, or the pooler, drops a quiet session.
CONNECT_TIMEOUT_SECONDS = 5
KEEPALIVE_IDLE_SECONDS = 30
KEEPALIVE_INTERVAL_SECONDS = 10
KEEPALIVE_COUNT = 3
ROLE_QUERY = """select r.rolsuper,r.rolbypassrls,
    exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
      where (n.nspname in ('documents','coordination')
        or (n.nspname='public' and c.relname in ('accounts','images')))
        and c.relowner=r.oid) as owns_storage,
    pg_has_role(current_user,'arctic_catalog','member') as catalog_member
    from pg_roles r where r.rolname=current_user"""


async def fetch(
    connection: Connection, query: str, parameters: Sequence[object] = ()
) -> list[Row]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, parameters or None)
        return cast(list[Row], await cursor.fetchall())


async def verify_catalog_role(connection: Connection) -> None:
    rows = await fetch(connection, ROLE_QUERY)
    if (
        not rows
        or any(rows[0][key] for key in ("rolsuper", "rolbypassrls", "owns_storage"))
        or rows[0]["catalog_member"] is not True
    ):
        raise Unavailable()


class CatalogListener:
    def __init__(self, url: str, copy: CatalogCopy) -> None:
        self._url = url
        self._copy = copy
        self._task: asyncio.Task[None] | None = None
        self._connection: Connection | None = None

    async def _connect(self) -> Connection:
        connection = await AsyncConnection.connect(
            self._url,
            autocommit=True,
            prepare_threshold=None,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            keepalives=1,
            keepalives_idle=KEEPALIVE_IDLE_SECONDS,
            keepalives_interval=KEEPALIVE_INTERVAL_SECONDS,
            keepalives_count=KEEPALIVE_COUNT,
        )
        try:
            await verify_catalog_role(connection)
            await connection.execute("set statement_timeout='8s'")
            await connection.execute(f"listen {CHANNEL}")
        except BaseException:
            await connection.close()
            raise
        return connection

    async def open(self) -> None:
        try:
            async with asyncio.timeout(15):
                self._connection = connection = await self._connect()
                # LISTEN is effective before the statement snapshot is taken.
                rows = await fetch(connection, SNAPSHOT)
            self._copy.load(Snapshot.model_validate(rows[0]))
            self._copy.connected = True
            self._task = asyncio.create_task(self._run(), name="catalog-listener")
        except Error, ValidationError, TimeoutError:
            await self.close()
            raise Unavailable() from None
        except BaseException:
            await self.close()
            raise

    async def _disconnect(self) -> None:
        self._copy.connected = False
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await connection.close()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._disconnect()

    async def _catch_up(self, connection: Connection) -> None:
        batch_size = 512
        while True:
            async with asyncio.timeout(10):
                rows = await fetch(
                    connection, CHANGES + " limit %s", (self._copy.applied, batch_size)
                )
            self._copy.apply(map(Change.model_validate, rows))
            if len(rows) < batch_size:
                self._copy.connected = True
                return

    async def _notices(self, connection: Connection) -> AsyncIterator[int]:
        async with aclosing(connection.notifies(timeout=30, stop_after=1)) as notices:
            async for notice in notices:
                if (
                    notice.payload.isascii()
                    and notice.payload.isdecimal()
                    and len(notice.payload) <= 19
                ):
                    number = int(notice.payload)
                    if 0 < number < 2**63:
                        yield number

    async def _run(self) -> None:
        delay = 0.25
        try:
            while True:
                try:
                    if self._connection is None:
                        async with asyncio.timeout(10):
                            self._connection = await self._connect()
                        await self._catch_up(self._connection)
                    connection = self._connection
                    received = False
                    async for number in self._notices(connection):
                        received = True
                        self._copy.notified = max(self._copy.notified, number)
                    if (
                        not received
                        or not self._copy.connected
                        or self._copy.applied < self._copy.notified
                    ):
                        await self._catch_up(connection)
                    delay = 0.25
                except Error, Unavailable, ValidationError, TimeoutError:
                    await self._disconnect()
                    logger.warning(
                        "Catalog listener disconnected. Reads will use Postgres during catch-up."
                    )
                    await asyncio.sleep(delay)
                    delay = min(2 * delay, 5)
        finally:
            await self._disconnect()
