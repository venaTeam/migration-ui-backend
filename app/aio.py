"""asyncio helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

# How many Grafana calls one request makes at a time.
GRAFANA_CONCURRENCY = 8


async def gather_limited[T](
    calls: Iterable[Callable[[], Awaitable[T]]], limit: int = GRAFANA_CONCURRENCY
) -> list[T]:
    """Run the calls with at most `limit` in flight; results keep the input order."""
    semaphore = asyncio.Semaphore(limit)

    async def run(call: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await call()

    return await asyncio.gather(*(run(call) for call in calls))
