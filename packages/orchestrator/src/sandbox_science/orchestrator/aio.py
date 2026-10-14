# SPDX-License-Identifier: MIT
"""Off-loop work for the orchestrator: owned waits, store calls in threads, and leases."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock, wait_or_sleep

_log = logging.getLogger(__name__)


async def owned[T](work: Awaitable[T]) -> T:
    """Retain actual work ownership through repeated caller cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


async def threaded[T](call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``call`` in a thread, which a cancelled caller still waits for."""
    return await owned(asyncio.to_thread(call, *args, **kwargs))


@asynccontextmanager
async def lease(
    renew: Callable[[], object], *, interval: float, name: str, clock: Clock = SYSTEM_CLOCK
) -> AsyncGenerator[None]:
    """Renew a lease in a thread every ``interval`` seconds of ``clock`` time while held.

    A failed renewal is logged and tried again at the next interval; the store's expiry
    decides whether the lease lapses meanwhile. Leaving waits for a renewal in progress,
    so none can renew the lease after its holder releases it, and never raises a
    renewal's failure: the holder's release always runs.
    """
    stopping = asyncio.Event()

    async def renewals() -> None:
        while not await wait_or_sleep(clock, stopping, interval):
            try:
                await threaded(renew)
            except Exception:
                _log.warning("renewing the %s lease failed; retrying", name, exc_info=True)

    task = asyncio.create_task(renewals())
    try:
        yield
    finally:
        stopping.set()
        await owned(task)
