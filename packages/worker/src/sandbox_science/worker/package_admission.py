# SPDX-License-Identifier: MIT
"""Package admission for the execution manager: the gate each executor must acknowledge.

The store keeps one ``PackageGate`` for each environment: ready, held by one
provisioning operation, or needing recovery. Before protected work, the executor
of the environment must acknowledge the current gate, bound to its original
sandbox, so that a package command cannot run in a guest that has not seen it.
``PackageAdmission`` checks the gate before admitting work, brings each executor's
acknowledgement in step, and guards the gate's binding when a sandbox is deleted
or reused.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionRequest,
    ProvisionState,
    ShellRequest,
)

from .backends import Backend
from .channel import ExecutorChannel
from .connections import Connection
from .errors import WorkerError
from .identity import same_execution
from .lifecycle import EnvironmentLifecycle
from .package_gate import PackageGate
from .package_gate import refusal as recovery_refusal
from .store import Store
from .usability import Usability

LEGACY = "legacy executor lacks package admission fencing; protected work refused"
"""The refusal when an executor cannot acknowledge the gate."""


class PackageAdmission:
    """Each environment's package gate, as the store keeps it and its executor saw it."""

    def __init__(
        self,
        store: Store,
        backend: Backend,
        usability: Usability,
        *,
        connect: Callable[[str], Awaitable[Connection]],
        spawn: Callable[[Any], asyncio.Task[Any]],
        sync: Callable[[str], Awaitable[None]],
        lifecycle: EnvironmentLifecycle,
    ) -> None:
        self.store = store
        self.backend = backend
        self.usability = usability
        self.connect = connect
        """How a request connects to an environment's executor: the manager's way."""
        self.spawn = spawn
        """Starts a background task that the manager owns and cancels when it closes."""
        self.sync_through = sync
        """How a retry syncs an environment: the manager's ``sync_package_gate``."""
        self.lifecycle = lifecycle
        """The managed environments' bindings, which a gate is bound to when known."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._acked: dict[ExecutorChannel, PackageGate] = {}
        self._retries: dict[str, asyncio.Task[Any]] = {}

    def admission(self, environment_id: str) -> PackageGate:
        gate = self.store.package_gate(environment_id)
        if gate.mode == "needs_recovery":
            raise recovery_refusal(gate)
        return gate

    def check_origin(self, request: ExecutionRequest, origin: str | None) -> None:
        if origin is None:
            self.admission(request.context.environment_id)
            return
        saved = self.store.provision_authority(origin)
        if (
            not isinstance(request, ShellRequest)
            or saved is None
            or saved[0] != request.context
            or saved[1] is ProvisionState.FINISHED
            or not same_execution(self.store.provision_step(origin, request.operation_id), request)
            or not request.operation_id.startswith(origin + ".")
            or re.fullmatch(r"[a-z][a-z0-9-]{0,95}", request.operation_id[len(origin) + 1 :])
            is None
        ):
            raise WorkerError(ErrorKind.UNAVAILABLE, "invalid internal provisioning operation")
        gate = self.admission(request.context.environment_id)
        if gate.mode == "hold" and gate.operation != origin:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "another provisioning operation holds admission"
            )

    async def sync(self, environment_id: str, channel: ExecutorChannel) -> bool:
        identity = channel.hello.get("package_gate")
        if not isinstance(identity, str) or re.fullmatch(r"gate-[0-9a-f]{24}", identity) is None:
            return False
        lock = self._locks.setdefault(environment_id, asyncio.Lock())
        async with lock:
            daemon_identity = self.lifecycle.daemon_identity
            binding = daemon_identity(environment_id) if daemon_identity else None
            try:
                self.store.bind_package_gate(
                    environment_id,
                    repr(binding) if binding else self.backend.binding_key(environment_id),
                )
            except WorkerError:
                return False  # no gate mutation on an unproven or replacement sandbox
            while True:
                gate = self.store.package_gate(environment_id)
                if self._acked.get(channel) == gate:
                    return True
                reply = await channel.request(
                    "package_gate",
                    incarnation=identity,
                    generation=gate.generation,
                    mode=gate.mode,
                    operation=gate.operation,
                    revision=gate.revision,
                )
                daemon_identity = self.lifecycle.daemon_identity
                if channel.closed or (
                    daemon_identity is not None and daemon_identity(environment_id) != binding
                ):
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE, "package admission binding changed during ACK"
                    )
                if reply.get("incarnation") != identity or any(
                    reply.get(key) != value or type(reply.get(key)) is not type(value)
                    for key, value in gate.fields().items()
                ):
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE, "invalid package admission acknowledgement"
                    )
                self._acked[channel] = gate
                if gate == self.store.package_gate(environment_id):
                    return True

    def channel_closed(self, channel: ExecutorChannel) -> None:
        """Forget what a closed channel acknowledged."""
        self._acked.pop(channel, None)

    async def sync_environment(self, environment_id: str) -> None:
        environment = await self.connect(environment_id)
        channel = environment.channel
        if channel is None or not await self.sync(environment_id, channel):
            raise WorkerError(ErrorKind.UNAVAILABLE, LEGACY)

    def refresh(self, environment_id: str) -> None:
        """Bounded retry of release/classification ACK; uncertainty leaves the guest closed."""
        if self.usability.closing or environment_id in self._retries:
            return

        async def retry() -> None:
            try:
                for delay in (0, 0.1, 0.5, 1, 2):
                    await asyncio.sleep(delay)
                    try:
                        await self.sync_through(environment_id)
                        return
                    except WorkerError:
                        pass
            finally:
                self._retries.pop(environment_id, None)

        self._retries[environment_id] = self.spawn(retry())

    def _deletion_binding(self, environment_id: str) -> str | None:
        daemon_identity = self.lifecycle.daemon_identity
        if daemon_identity is not None:
            identity = daemon_identity(environment_id)
            return repr(identity) if identity is not None else None
        return self.backend.binding_key(environment_id)

    def forget(self, environment_id: str) -> None:
        """Only confirmed deletion, after provisioning drain, may remove this fence."""
        self.store.forget_package_gate(
            environment_id, binding=self._deletion_binding(environment_id)
        )

    def guard_deletion(self, environment_id: str) -> None:
        """Require original-binding proof before any explicit guest teardown."""
        self.store.guard_package_deletion(environment_id, self._deletion_binding(environment_id))


def guard_reuse(store: Store, environment_id: str) -> None:
    """A new VM cannot replace the unknown original held package binding.

    It needs only the store, so that the environments can ask before any executor runs.
    """
    if store.package_gate(environment_id).mode != "ready" or store.package_gate_has_binding(
        environment_id
    ):
        raise WorkerError(
            ErrorKind.UNAVAILABLE,
            "original package binding requires confirmed sandbox deletion",
        )
