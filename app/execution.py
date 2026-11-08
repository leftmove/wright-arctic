"""Run synchronous services in the execution model supplied by the host."""

import asyncio
from collections.abc import Callable
from typing import Protocol


class Executor(Protocol):
    async def run[**P, T](
        self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T: ...


class ThreadExecutor:
    """Keep blocking work off an ordinary ASGI server's event loop."""

    async def run[**P, T](
        self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        return await asyncio.to_thread(function, *args, **kwargs)


class InlineExecutor:
    """Use a host with cooperative synchronous I/O and no operating-system threads."""

    async def run[**P, T](
        self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        return function(*args, **kwargs)
