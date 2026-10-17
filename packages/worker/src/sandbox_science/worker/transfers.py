# SPDX-License-Identifier: MIT
"""Cancellation-safe offloads while a file transfer holds its memory admission gate."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable


async def offload[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Keep admission until the actual thread ends, then propagate caller cancellation."""
    work = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    interrupted: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as error:
            interrupted = interrupted or error
        except BaseException:
            if interrupted is None:
                raise
            break
    if interrupted is not None:
        with contextlib.suppress(BaseException):
            work.result()
        raise interrupted
    return work.result()
