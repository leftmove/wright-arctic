"""Bound per-operation fan-out and await cancellation of every sibling."""

import asyncio
from collections.abc import Awaitable, Callable


async def bounded_map[T, R](
    function: Callable[[T], Awaitable[R]], values: list[T], *, limit: int
) -> list[R]:
    semaphore = asyncio.Semaphore(limit)

    async def invoke(value: T) -> R:
        async with semaphore:
            return await function(value)

    tasks = [asyncio.create_task(invoke(value)) for value in values]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
