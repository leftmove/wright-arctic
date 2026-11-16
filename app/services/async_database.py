"""Request-owned asyncpg connections bridged into the synchronous domain service."""

import importlib
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Protocol, cast

from app.services.database import (
    TRANSACTION_SETTINGS,
    Row,
    Session,
    raise_database_error,
    verify_catalog,
    verify_role,
)


class Wait(Protocol):
    def __call__[T](self, value: Awaitable[T]) -> T: ...


class Transaction(Protocol):
    def start(self) -> Awaitable[None]: ...
    def commit(self) -> Awaitable[None]: ...
    def rollback(self) -> Awaitable[None]: ...


class Connection(Protocol):
    def execute(self, query: str) -> Awaitable[str]: ...
    def fetch(
        self, query: str, *parameters: object
    ) -> Awaitable[Sequence[Mapping[str, object]]]: ...
    def transaction(self, *, isolation: str) -> Transaction: ...
    def close(self, *, timeout: float) -> Awaitable[None]: ...


class Driver(Protocol):
    PostgresError: type[Exception]
    InterfaceError: type[Exception]

    def connect(
        self, dsn: str, *, timeout: float, statement_cache_size: int
    ) -> Awaitable[Connection]: ...


_driver = cast(Driver, importlib.import_module("asyncpg"))


class AsyncPostgresSession:
    def __init__(self, connection: Connection, wait: Wait) -> None:
        self._connection = connection
        self._wait = wait

    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        parts = query.split("%s")
        if len(parts) != len(parameters) + 1:
            raise ValueError("SQL parameter count mismatch")
        query = parts[0] + "".join(
            f"${index}{part}" for index, part in enumerate(parts[1:], 1)
        )
        return [
            dict(row) for row in self._wait(self._connection.fetch(query, *parameters))
        ]


class RequestPostgresDatabase:
    """The request owns its socket; Hyperdrive owns the origin connection pool."""

    def __init__(self, url: str, wait: Wait) -> None:
        self._url = url
        self._wait = wait
        self._driver = _driver
        self._connection: Connection | None = None
        self._verified = False

    def open(self) -> None:
        with self.transaction() as session:
            verify_catalog(session)

    def close(self) -> None:
        if self._connection is not None:
            self._wait(self._connection.close(timeout=5))
            self._connection = None
        self._verified = False

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        try:
            if self._connection is None:
                self._connection = self._wait(
                    self._driver.connect(self._url, timeout=5, statement_cache_size=100)
                )
            transaction = self._connection.transaction(isolation="read_committed")
            self._wait(transaction.start())
            try:
                session = AsyncPostgresSession(self._connection, self._wait)
                self._wait(self._connection.execute(TRANSACTION_SETTINGS))
                if not self._verified:
                    verify_role(session)
                    self._verified = True
                yield session
            except BaseException:
                self._wait(transaction.rollback())
                raise
            else:
                self._wait(transaction.commit())
        except (
            self._driver.PostgresError,
            self._driver.InterfaceError,
            OSError,
            TimeoutError,
        ) as error:
            state: object = getattr(error, "sqlstate", None)
            raise_database_error(state if isinstance(state, str) else None)
