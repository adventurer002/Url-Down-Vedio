"""Bounded, ordered async work with sibling cleanup before propagating errors."""
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def ordered_map(items: Sequence[T], operation: Callable[[T], Awaitable[R]], limit: int) -> list[R]:
    if limit < 1:
        raise ValueError("concurrency must be positive")
    iterator = iter(enumerate(items))
    results = [None] * len(items)

    async def worker():
        for index, item in iterator:
            results[index] = await operation(item)

    workers = [asyncio.create_task(worker()) for _ in range(min(limit, len(items)))]
    try:
        await asyncio.gather(*workers)
        return results
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        # A cancelled upload must stop reading before its temporary directory is removed.
        await asyncio.gather(*workers, return_exceptions=True)
