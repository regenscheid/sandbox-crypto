# SPDX-License-Identifier: MIT
"""Waiting for a condition instead of sleeping for a fixed time."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import cast


async def eventually[T](
    predicate: Callable[[], Awaitable[T | None]] | Callable[[], T | None],
    *,
    timeout: float = 30.0,
    interval: float = 0.005,
    describe: str = "the condition",
) -> T:
    """Poll ``predicate`` until it returns a true value, and return that value.

    ``predicate`` may be a plain or an async function; ``None`` counts as not yet. The
    timeout is real time and only bounds a broken test: a passing test returns as soon
    as the condition holds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        raw = predicate()
        value = cast(T | None, await raw) if inspect.isawaitable(raw) else raw
        if value:
            return value
        if loop.time() >= deadline:
            raise AssertionError(f"{describe} did not hold within {timeout} s")
        await asyncio.sleep(interval)
