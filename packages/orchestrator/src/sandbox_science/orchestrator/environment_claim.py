# SPDX-License-Identifier: MIT
"""Keep active research from losing its sandbox to idle suspension."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Protocol

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorKind,
    ExecutionContext,
    wait_or_sleep,
)
from sandbox_science.contracts.environment_claims import (
    CLAIM_SECONDS,
    EnvironmentClaim,
    EnvironmentClaimResult,
)

from .aio import held, owned
from .worker_client import WorkerAPIError

_log = logging.getLogger(__name__)


class ClaimClient(Protocol):
    async def claim_environment(self, request: EnvironmentClaim) -> EnvironmentClaimResult: ...


@asynccontextmanager
async def environment_claim(
    client: ClaimClient,
    context: ExecutionContext,
    holder: str,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> AsyncGenerator[None]:
    """Retain one root's environment through its entire live tree.

    The client bounds each request's time. Any failed renewal interrupts the root;
    cleanup joins the renewal before releasing, so it cannot renew after release.
    """
    request = EnvironmentClaim(context=context, holder=holder)
    initial, cancelled = await held(client.claim_environment(request))
    if initial.environment_id != context.environment_id:
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker returned another environment's claim")
    if initial.state == "unmanaged":
        if cancelled:
            raise asyncio.CancelledError
        yield
        return
    if initial.state != "held":
        raise WorkerAPIError(ErrorKind.UNAVAILABLE, "worker did not retain the environment")
    request = request.model_copy(update={"sandbox_id": initial.sandbox_id})
    stopping = asyncio.Event()
    lost: list[Exception] = []
    owner = asyncio.current_task()

    async def renewals() -> None:
        renewed, wall = clock.monotonic(), clock.now()
        while not await wait_or_sleep(clock, stopping, CLAIM_SECONDS / 4):
            try:
                # A host suspend can stop monotonic time while the remote claim expires.
                if (
                    max(clock.monotonic() - renewed, (clock.now() - wall).total_seconds())
                    >= CLAIM_SECONDS / 2
                ):
                    raise WorkerAPIError(ErrorKind.UNAVAILABLE, "environment claim renewal lapsed")
                reply = await client.claim_environment(request)
                if (
                    reply.state != "held"
                    or reply.sandbox_id != initial.sandbox_id
                    or reply.environment_id != context.environment_id
                ):
                    raise WorkerAPIError(
                        ErrorKind.UNAVAILABLE, "environment claim identity changed"
                    )
                renewed, wall = clock.monotonic(), clock.now()
            except Exception as error:
                if not stopping.is_set():
                    lost.append(error)
                    if owner is not None:
                        owner.cancel()
                return

    renewing = asyncio.create_task(renewals())

    async def release() -> None:
        stopping.set()
        await renewing
        try:
            await client.claim_environment(request.model_copy(update={"seconds": 0}))
        except Exception:
            # The worker's durable expiry remains the bound after an unavailable release.
            _log.warning(
                "environment claim release failed; it expires at the worker", exc_info=True
            )

    try:
        if cancelled:
            raise asyncio.CancelledError
        yield
        if lost:
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "environment claim was lost") from lost[0]
    except asyncio.CancelledError:
        if lost and owner is not None:
            owner.uncancel()
            raise WorkerAPIError(ErrorKind.UNAVAILABLE, "environment claim was lost") from lost[0]
        raise
    finally:
        await owned(release())
