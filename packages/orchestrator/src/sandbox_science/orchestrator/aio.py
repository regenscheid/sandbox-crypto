# SPDX-License-Identifier: MIT
"""Off-loop work for the orchestrator: owned waits, store calls in threads, leases, and
an executor whose admitted work outlives its callers' cancellation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock, wait_or_sleep

_log = logging.getLogger(__name__)


async def held[T](work: Awaitable[T]) -> tuple[T, bool]:
    """``work``'s result, waited for through repeated caller cancellation, and whether the
    caller was cancelled meanwhile: a caller that must clean up what ``work`` returned
    (a process to reap) does so, then raises ``CancelledError`` itself. When ``work``
    fails, its exception is raised, even if the caller was cancelled meanwhile: the
    failure wins over the cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


async def owned[T](work: Awaitable[T]) -> T:
    """Retain actual work ownership through repeated caller cancellation."""
    result, cancelled = await held(work)
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


class OwnedExecutor:
    """Admits at most ``max_pending`` pieces of work at once and owns them: a caller's
    cancellation detaches the caller, not the work, and ``aclose`` stops admission and
    waits for all of it, even when ``aclose`` itself is cancelled again and again. Its
    one thread (``thread``) serializes the store calls of the work it admits, so the
    owner can close a borrowed store once ``aclose`` returns."""

    def __init__(self, max_pending: int, name: str) -> None:
        self.max_pending = max_pending
        self._threads = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._work: set[asyncio.Task[Any]] = set()
        self._detached: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def admits(self) -> bool:
        """Whether ``submit`` would admit more work now."""
        return not self._closing and len(self._work) < self.max_pending

    async def submit[T](self, work: Coroutine[Any, Any, T]) -> T:
        """Run ``work`` as owned work; check ``admits`` first."""
        if not self.admits:
            work.close()
            raise RuntimeError("the executor is closing or at its admission limit")
        task = asyncio.create_task(work)
        self._work.add(task)
        task.add_done_callback(self._finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                self._detached.add(task)  # nobody awaits it now; _finished reports it
            raise

    @property
    def pending(self) -> int:
        """Admitted work not finished yet."""
        return len(self._work)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._work.discard(task)
        detached = task in self._detached
        self._detached.discard(task)
        if task.cancelled():
            return
        # A caller still awaiting gets the exception; a detached one never would, so
        # it is logged here instead of being lost.
        failure = task.exception()
        if failure is not None and detached:
            _log.error("owned work failed after its caller left", exc_info=failure)

    async def thread[T](self, call: Callable[[], T]) -> T:
        """``call`` in this executor's one thread."""
        return await asyncio.get_running_loop().run_in_executor(self._threads, call)

    async def aclose(self) -> None:
        """Stop admission and drain the admitted work, under repeated cancellation."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        await owned(self._close_task)

    async def _drain(self) -> None:
        if self._work:
            await asyncio.gather(*tuple(self._work), return_exceptions=True)
        self._threads.shutdown(wait=True)
