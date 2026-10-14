# SPDX-License-Identifier: MIT
"""Kernels as the worker reports them, for the execution manager.

An executor numbers each kernel's generations from 1 again whenever it starts.
``KernelRegistry`` maps them onto generations that never go back, records each
kernel's birth and the package revision it started at, and answers kernel status:
explicitly, connecting if it must, or passively, reading only a healthy connection
that already exists, without keeping it alive or waiting on its lifecycle. Passive
observations run at most one at a time for each environment, eight in all, and a
close of the observed session invalidates one in flight.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    Clock,
    ErrorKind,
    ExecutionContext,
    KernelInfo,
    KernelListing,
    KernelObservation,
    KernelStatusRequest,
    SessionClosed,
    SessionCloseRequest,
)
from sandbox_science.contracts.worker import LANGUAGES

from .channel import ExecutorChannel
from .connections import Connection, EnvironmentConnections
from .errors import WorkerError
from .kernel_revisions import KernelBirth
from .replies import integer
from .store import KernelRecord, Store
from .usability import Usability

MAX_KERNEL_OBSERVATIONS = 8
"""No queued observers, and at most one in flight for each environment."""

DaemonIdentity = Callable[[str], tuple[str, str] | None]
"""An environment's trusted sandbox binding; None when the worker does not know one."""


@dataclass
class _Observer:
    scope: tuple[str, str, str]
    valid: bool = True
    """A matching session close invalidates only this active, bounded observation."""


class KernelRegistry:
    """Kernel generations, births and status, for every environment."""

    def __init__(
        self,
        store: Store,
        connections: EnvironmentConnections,
        usability: Usability,
        *,
        clock: Clock,
        observation_timeout: Callable[[], float],
        daemon_identity: Callable[[], DaemonIdentity | None],
    ) -> None:
        self.store = store
        self.connections = connections
        self.usability = usability
        self.clock = clock
        self.observation_timeout = observation_timeout
        """The whole budget of a passive kernel observation, the transport write lock
        included, as the manager's timeouts give it when the observation starts."""
        self.daemon_identity = daemon_identity
        """The manager's daemon identity, which is set after it is built; None for none."""
        self._observations: dict[str, _Observer] = {}

    def invalidate(self, context: ExecutionContext) -> None:
        """The session is closing: an observation of it in flight must not report."""
        observer = self._observations.get(context.environment_id)
        if observer is not None and observer.scope == (
            context.project_id,
            context.environment_id,
            context.session_id,
        ):
            observer.valid = False  # close may finish before its old reply arrives

    def kernel(
        self,
        environment_id: str,
        session_id: str,
        raw: object,
        state_lost: bool,
        *,
        project_id: str | None = None,
        birth_execution: str | None = None,
        fingerprint: object = None,
        restarted: bool = False,
        expected_channel: ExecutorChannel | None = None,
        remember: bool = True,
    ) -> tuple[KernelInfo | None, bool]:
        """Map the executor's kernel generation onto one that never goes back."""
        if not isinstance(raw, dict):
            return None, state_lost
        raw = cast(dict[str, Any], raw)
        language = raw.get("language", "python")
        generation = integer(raw.get("generation", 1), 1, 2**31 - 1)
        if language not in LANGUAGES or generation is None:
            return None, state_lost
        environment = self.connections.get(environment_id)
        channel = environment.channel if environment is not None else None
        if expected_channel is not None and channel is not expected_channel:
            return None, state_lost  # old bridge cannot rebind a replacement's kernel
        incarnation = channel.incarnation if channel is not None else "unknown"
        try:
            record = self.store.kernel(environment_id, session_id, language)
        except ValueError:
            return None, state_lost  # damaged derived metadata cannot fail a shell result
        if record is None:
            base = 0
        elif record.incarnation != incarnation:
            # A new executor: its kernels start over, and earlier state is gone.
            base, state_lost = record.last, state_lost or record.last > 0
        else:
            base = record.base
        if base + generation > 2**62:
            return None, state_lost
        if remember:
            self.store.save_kernel(
                environment_id,
                session_id,
                language,
                KernelRecord(
                    incarnation, base, max(base + generation, record.last if record else 0)
                ),
            )
        info = {
            "kernel_id": raw.get("kernel_id"),
            "language": language,
            "generation": base + generation,
        }
        try:
            kernel = KernelInfo.model_validate(info)
        except ValidationError:
            # Not an identifier the worker can report: name the kernel after its session.
            digest = hashlib.sha256(f"{session_id}\0{language}".encode()).hexdigest()[:24]
            kernel = KernelInfo.model_validate({**info, "kernel_id": f"krn-{digest}"})
        current = self.store.kernel_starts.current(environment_id)
        revision = current[0] if current is not None else None
        kernel = kernel.model_copy(update={"current_revision": revision})
        start_environment = raw.get("start_environment")
        can_pin = (
            current is not None
            and current[1] == start_environment
            and self.store.kernel_starts.epoch(
                environment_id, incarnation, raw.get("start_package_epoch"), start_environment
            )
            == revision
            and (
                (
                    birth_execution is not None
                    and raw.get("start_execution") == birth_execution
                    and fingerprint == start_environment
                )
                or restarted
            )
        )
        if project_id is not None and channel is not None and not channel.closed:
            binding = None
            if channel.daemon_token is not None:
                endpoint = self.store.lineage_endpoint(environment_id)
                if endpoint is not None and endpoint[0] == incarnation:
                    binding = endpoint[1:]
                if binding is None and self.daemon_identity() is not None:
                    can_pin = False  # a managed VM's original binding must be known
            try:
                birth = KernelBirth.model_validate(
                    {
                        "project_id": project_id,
                        "environment_id": environment_id,
                        "session_id": session_id,
                        "language": language,
                        "incarnation": incarnation,
                        "kernel_id": kernel.kernel_id,
                        "sandbox": binding[0] if binding else None,
                        "sandbox_id": binding[1] if binding else None,
                        "generation": generation,
                        "birth_id": raw.get("birth_id"),
                        "start_revision": revision if can_pin else None,
                    }
                )
            except ValidationError:
                pass  # legacy or malformed sandbox birth: do not infer it from today's packages
            else:
                kernel = self.store.kernel_starts.observe(
                    birth, kernel, revision, remember=remember
                )
        return kernel, state_lost

    def session_kernels(
        self,
        context: ExecutionContext,
        raw: object,
        *,
        expected_channel: ExecutorChannel | None = None,
    ) -> tuple[KernelInfo, ...]:
        if not isinstance(raw, list):
            return ()
        raw = cast(list[object], raw)
        if len(raw) > len(LANGUAGES):
            return ()
        found: dict[str, KernelInfo] = {}
        for item in raw:
            kernel, _ = self.kernel(
                context.environment_id,
                context.session_id,
                item,
                False,
                project_id=context.project_id,
                expected_channel=expected_channel,
                remember=False,
            )
            if kernel is not None:
                found[kernel.language] = kernel
        return tuple(found.values())

    async def status(self, request: KernelStatusRequest) -> KernelListing:
        """Observe only the authenticated context's kernels; admission gates stay unchanged."""
        context = request.context
        async with self.connections.using(context.environment_id) as channel:
            if (
                type(channel.hello.get("kernel_revision_protocol")) is not int
                or channel.hello.get("kernel_revision_protocol") != 1
            ):
                raise WorkerError(
                    ErrorKind.UNSUPPORTED, "legacy executor lacks scoped kernel status"
                )
            reply = await channel.request("kernels", session=context.session_id)
            return self._listing(context, channel, reply.get("kernels"))

    def _listing(
        self, context: ExecutionContext, channel: ExecutorChannel, raw: object
    ) -> KernelListing:
        if not isinstance(raw, list) or len(cast(list[object], raw)) > len(LANGUAGES):
            raise WorkerError(ErrorKind.UNAVAILABLE, "executor sent invalid kernel status")
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                raise WorkerError(ErrorKind.UNAVAILABLE, "executor sent invalid kernel identity")
            item = cast(dict[str, object], item)
            generation = item.get("generation")
            if type(generation) is not int or not 1 <= generation < 2**31:
                raise WorkerError(ErrorKind.UNAVAILABLE, "executor sent invalid kernel generation")
            try:
                KernelInfo.model_validate(
                    {key: item.get(key) for key in ("kernel_id", "language", "generation")}
                )
            except ValidationError as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "executor sent invalid kernel identity"
                ) from exc
        kernels = self.session_kernels(context, cast(list[object], raw), expected_channel=channel)
        if len(kernels) != len(cast(list[object], raw)):
            raise WorkerError(ErrorKind.UNAVAILABLE, "executor sent invalid kernel identities")
        current = self.store.kernel_starts.current(context.environment_id)
        return KernelListing(
            environment_id=context.environment_id,
            session_id=context.session_id,
            current_revision=current[0] if current is not None else None,
            kernels=kernels,
        )

    def _observed(self, context: ExecutionContext) -> tuple[Connection, ExecutorChannel]:
        environment_id = context.environment_id
        environment = self.connections.get(environment_id)
        channel = environment.channel if environment is not None else None
        if (
            not self.usability.usable(context)
            or environment is None
            or environment.lock.locked()
            or channel is None
            or channel.closed
            or channel.detaching
        ):
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "kernel observation needs an existing healthy connection"
            )
        return environment, channel

    async def observe(self, request: KernelStatusRequest) -> KernelObservation:
        """Read an existing channel without keeping it alive or waiting on its lifecycle."""
        return await self._passive(request, self._observation)

    def _observation(
        self, context: ExecutionContext, channel: ExecutorChannel, raw: object
    ) -> KernelObservation:
        listing = self._listing(context, channel, raw)
        return KernelObservation(**listing.model_dump(), observed_at=self.clock.now())

    async def _passive[Observation](
        self,
        request: KernelStatusRequest,
        build: Callable[[ExecutionContext, ExecutorChannel, object], Observation],
        *,
        operation: str = "kernels",
        fields: dict[str, object] | None = None,
        protocol: str = "kernel_revision_protocol",
    ) -> Observation:
        """One bounded admission lane and close token for every passive kernel consumer."""
        context = request.context
        environment_id = context.environment_id
        environment, channel = self._observed(context)
        if (
            environment_id in self._observations
            or len(self._observations) >= MAX_KERNEL_OBSERVATIONS
        ):
            raise WorkerError(ErrorKind.UNAVAILABLE, "kernel observation capacity is busy")
        if type(channel.hello.get(protocol)) is not int or channel.hello.get(protocol) != 1:
            raise WorkerError(ErrorKind.UNSUPPORTED, "legacy executor lacks scoped kernel status")
        incarnation = channel.incarnation
        budget = self.observation_timeout()
        observer = _Observer((context.project_id, environment_id, context.session_id))
        self._observations[environment_id] = observer
        try:
            try:
                async with asyncio.timeout(budget):
                    reply = await channel.request(
                        operation,
                        session=context.session_id,
                        **(fields or {}),
                        reply_timeout=budget,
                    )
            except TimeoutError as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "kernel observation deadline expired"
                ) from exc
            current_environment, current_channel = self._observed(context)
            if (
                not observer.valid
                or current_environment is not environment
                or current_channel is not channel
                or channel.incarnation != incarnation
            ):
                raise WorkerError(ErrorKind.UNAVAILABLE, "kernel observation connection changed")
            try:
                return build(context, channel, reply.get("kernels"))
            except ValidationError as exc:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "executor sent incoherent kernel observation"
                ) from exc
        finally:
            self._observations.pop(environment_id)

    async def close_session(self, request: SessionCloseRequest) -> SessionClosed:
        """Stop the session's kernels in its environment's executor, if one runs."""
        context = request.context
        environment = self.connections.get(context.environment_id)
        channel = environment.channel if environment is not None else None
        if environment is None or channel is None or channel.closed:
            return SessionClosed()
        environment.requests += 1
        try:
            reply = await channel.request(
                "close_session", session=context.session_id, reply_timeout=180
            )
        finally:
            environment.requests -= 1
            environment.last_used = self.clock.monotonic()
        raw: object = reply.get("kernels")
        items = cast(list[object], raw) if isinstance(raw, list) else []
        if len(items) > len(LANGUAGES):
            items = []  # bounded like every kernel list an executor sends
        stopped: dict[str, KernelInfo] = {}
        for item in items:
            kernel, _ = self.kernel(context.environment_id, context.session_id, item, False)
            if kernel is not None:
                stopped[kernel.language] = kernel
        return SessionClosed(kernels=tuple(stopped.values()))
