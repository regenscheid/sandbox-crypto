# SPDX-License-Identifier: MIT
"""Keep active research from losing its sandbox to idle suspension."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Final, Protocol

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorKind,
    ExecutionContext,
    wait_or_sleep,
)
from sandbox_science.contracts.environment_claims import (
    CLAIM_RENEW_SECONDS,
    MAX_CLAIM_SECONDS,
    WORKER_OUTAGE_SECONDS,
    EnvironmentClaim,
    EnvironmentClaimResult,
)

from .aio import held, owned
from .worker_client import WorkerAPIError

_log = logging.getLogger(__name__)


class ClaimClient(Protocol):
    async def claim_environment(self, request: EnvironmentClaim) -> EnvironmentClaimResult: ...


RENEW_SECONDS: float = CLAIM_RENEW_SECONDS
"""How often a root's claim is renewed while the worker answers."""
OUTAGE_SECONDS: float = WORKER_OUTAGE_SECONDS
"""How long renewals are retried, from the last one that succeeded, before the root is
interrupted (``WORKER_OUTAGE_SECONDS``)."""
LOST: Final = "environment claim was lost"
UNREACHABLE: Final = "worker_unreachable"
"""A claim given up after the outage window: the worker did not answer."""
ENDED: Final = "environment_claim_ended"
"""A claim the worker ended or refused: its sandbox replaced, another refusal."""
LAPSED: Final = "environment_claim_lapsed"
"""A claim not renewed for longer than it lasts, as across a host's suspend."""
RETRY_SECONDS: Final = 2.0
"""The first wait before a failed renewal is tried again; it doubles, up to
``RENEW_SECONDS``."""


def unreachable(error: BaseException) -> bool:
    """Whether a renewal failed for the worker's absence, which it outlasts, rather than for
    an answer that ends the claim: an unreachable worker, or one that cannot retain the
    environment now (``unavailable``)."""
    if isinstance(error, WorkerAPIError):
        return error.kind is ErrorKind.UNAVAILABLE and not isinstance(error, _EndedError)
    return isinstance(error, OSError | TimeoutError)


class ClaimLostError(WorkerAPIError):
    """A root's environment claim was lost, which interrupted it: what ends the root, rather
    than the cancellation that stopped its work."""


class _EndedError(WorkerAPIError):
    """The worker answered a renewal with a claim that is not this root's any more."""


@asynccontextmanager
async def environment_claim(
    client: ClaimClient,
    context: ExecutionContext,
    holder: str,
    *,
    clock: Clock = SYSTEM_CLOCK,
    follow: Callable[[], Awaitable[str | None]] | None = None,
    followed: Callable[[str], None] | None = None,
    outage: Callable[[datetime | None, datetime | None], None] | None = None,
    lost: Callable[[str, str, Exception], None] | None = None,
) -> AsyncGenerator[None]:
    """Retain one root's environment through its entire live tree.

    The client bounds each request's time. The first grant, or a renewal, that the worker
    does not answer, or answers ``unavailable``, is tried again with a growing wait for
    ``OUTAGE_SECONDS`` from the last one that succeeded (or from the first try), as a
    worker Pod's restart takes minutes: ``outage``
    hears when the worker stopped answering and when the run gives up waiting for it, and
    ``None`` once it answers again. Each renewal asks for ``MAX_CLAIM_SECONDS``, so the
    worker still holds the claim when the run gives up. Past the window, or on an answer
    that ends the claim (its sandbox replaced, another refusal), or a claim that lapsed
    unrenewed, the root is interrupted: ``lost`` hears first which of these it was
    (``UNREACHABLE``, ``ENDED``, ``LAPSED``), why, and the error, so that the run's end
    says so, and only a worker that was unreachable makes it a transient loss that
    automatic recovery takes up once the worker answers. Cleanup joins the renewal before
    releasing, so it cannot renew after release.

    A policy change during the run need not end it. When a renewal is refused because
    the environment's policy is no longer the claim's, ``follow`` reads the project's
    current policy revision; if the project has moved to a revision other than the
    claim's, the claim takes it and is renewed again (``followed`` says so), since the
    worker enforces the environment's current policy on every effect. Any other
    refusal, or one the project's policy does not explain, still ends the root.
    """
    request = EnvironmentClaim(context=context, holder=holder, seconds=MAX_CLAIM_SECONDS)
    begun, retry = clock.monotonic(), RETRY_SECONDS
    while True:
        try:
            # Held through a cancel, so that a granted claim is always released.
            initial, cancelled = await held(client.claim_environment(request))
            break
        except Exception as error:
            # The first grant rides out an unreachable worker as renewals do; a Stop
            # meanwhile ends the wait at once, with nothing granted.
            waited = clock.monotonic() - begun
            if not unreachable(error) or waited >= OUTAGE_SECONDS:
                raise
            _log.warning("environment claim failed; retrying: %s", error)
            await clock.sleep(max(min(retry, OUTAGE_SECONDS - waited), 0.001))
            retry = min(retry * 2, RENEW_SECONDS)
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
    failures: list[Exception] = []
    owner = asyncio.current_task()
    renew_every, window = RENEW_SECONDS, OUTAGE_SECONDS

    async def renew() -> EnvironmentClaimResult:
        nonlocal request
        try:
            return await client.claim_environment(request)
        except WorkerAPIError as error:
            if error.kind is not ErrorKind.POLICY_DENIED or follow is None:
                raise
            current = await follow()
            if current is None or current == request.context.policy_revision:
                raise
            context = request.context.model_copy(update={"policy_revision": current})
            request = request.model_copy(update={"context": context})
            reply = await client.claim_environment(request)
            if followed is not None:
                followed(current)
            return reply

    def end(error: Exception, reason: str, why: str) -> None:
        if stopping.is_set():
            return
        failures.append(error)
        if lost is not None:
            lost(reason, why, error)
        if owner is not None:
            owner.cancel()

    async def renewals() -> None:
        renewed, wall = clock.monotonic(), clock.now()
        away: datetime | None = None
        wait, retry = renew_every, RETRY_SECONDS
        while not await wait_or_sleep(clock, stopping, wait):
            # A host suspend can stop monotonic time while the remote claim expires.
            since = max(clock.monotonic() - renewed, (clock.now() - wall).total_seconds())
            if since >= window + renew_every:
                # The claim last granted has expired at the worker: what it kept may be gone.
                lapsed = WorkerAPIError(ErrorKind.CONFLICT, "environment claim renewal lapsed")
                end(lapsed, LAPSED, "the environment claim lapsed before it was renewed")
                return
            try:
                reply = await renew()
                if (
                    reply.state != "held"
                    or reply.sandbox_id != initial.sandbox_id
                    or reply.environment_id != context.environment_id
                ):
                    raise _EndedError(ErrorKind.CONFLICT, "environment claim identity changed")
            except Exception as error:
                if not unreachable(error):
                    end(error, ENDED, f"the environment claim ended: {str(error)[:200]}")
                    return
                if since >= window:
                    end(
                        error,
                        UNREACHABLE,
                        f"the worker did not answer for {window:g} s: {str(error)[:200]}",
                    )
                    return
                if away is None:
                    away = clock.now()
                    _log.warning("environment claim renewal failed; retrying: %s", error)
                    if outage is not None:
                        outage(away, wall + timedelta(seconds=window))
                wait = max(min(retry, renew_every, window - since), 0.001)
                retry = min(retry * 2, renew_every)
                continue
            renewed, wall = clock.monotonic(), clock.now()
            wait, retry = renew_every, RETRY_SECONDS
            if away is not None:
                away = None
                if outage is not None:
                    outage(None, None)

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
        if failures:
            if owner is not None and owner.cancelling():
                owner.uncancel()  # the root ended before the claim's cancel reached it
            raise ClaimLostError(ErrorKind.UNAVAILABLE, LOST) from failures[0]
    except asyncio.CancelledError:
        if failures and owner is not None:
            owner.uncancel()
            raise ClaimLostError(ErrorKind.UNAVAILABLE, LOST) from failures[0]
        raise
    finally:
        await owned(release())
