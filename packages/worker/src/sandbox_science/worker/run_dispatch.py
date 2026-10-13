# SPDX-License-Identifier: MIT
"""Internal original-run dispatch; no route, launcher, tool or caller authentication."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    KernelInfo,
    KernelLineageEntry,
    KernelStatusRequest,
    new_id,
)
from sandbox_science.contracts.kernel_lineage import KernelBirthPin
from sandbox_science.contracts.run_cells import (
    RunCellExecution,
    RunCellRequest,
    RunKernelObservation,
    run_cell_bytes,
    run_kernel_id,
)
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    checked_run_kernel_binding,
    run_kernel_binding_sha256,
)

from .errors import WorkerError
from .run_kernel_state import RunControlReceipt, unavailable
from .run_kernels import RunKernelBindings

if TYPE_CHECKING:
    from .channel import ExecutorChannel
    from .manager import ExecutionManager, _Environment, _Live

# This internal component deliberately composes the existing Manager lifetime.
# pyright: reportPrivateUsage=false


def _context(binding: RunKernelBinding) -> ExecutionContext:
    return ExecutionContext(
        project_id=binding.scope.project_id,
        session_id=binding.scope.session_id,
        environment_id=binding.scope.environment_id,
    )


def _wire(binding: RunKernelBinding) -> dict[str, object]:
    return {
        "run_binding": binding.model_dump(mode="json"),
        "run_binding_sha256": run_kernel_binding_sha256(binding),
    }


class RunKernelDispatcher:
    """Requires explicitly supplied controller admission; never manufactures it.

    Effects use only the original already-connected managed channel. Completed
    controls replay historical receipts; pending unknown effects are never resent.
    """

    def __init__(self, manager: ExecutionManager) -> None:
        self.manager = manager
        self.state = manager.store.run_kernel_state
        self.bindings = RunKernelBindings(manager.store)
        self._controls: dict[str, asyncio.Task[RunControlReceipt]] = {}

    def initialize(self) -> None:
        self.state.initialize()

    def proof(
        self, binding: RunKernelBinding, *, recovery: bool = False, allow_closed: bool = False
    ) -> tuple[_Environment, ExecutorChannel]:
        binding = checked_run_kernel_binding(binding)
        m, scope = self.manager, binding.scope
        env = m._environments.get(scope.environment_id)
        channel = env.channel if env is not None else None
        if (
            m._closing
            or env is None
            or channel is None
            or channel.closed
            or channel.detaching
            or env.lock.locked()
            or scope.environment_id in m._daemon_fences
            or scope.environment_id in m._quarantined
            or scope.environment_id in m._released_environments
            or (not recovery and scope.environment_id in m._recovering)
            or (scope.project_id, scope.environment_id, scope.session_id) in m._closing_sessions
            or m.store.recycle_needed(scope.environment_id)
        ):
            raise unavailable()
        if (
            channel.daemon_token is None
            or channel.incarnation != binding.incarnation
            or type(channel.hello.get("run_kernel_protocol")) is not int
            or channel.hello.get("run_kernel_protocol") != 1
        ):
            raise unavailable()
        expected = (binding.incarnation, binding.sandbox, binding.sandbox_id)
        identity = m.daemon_identity(scope.environment_id) if m.daemon_identity else None
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or any(type(item) is not str for item in identity)
            or identity != (binding.sandbox, binding.sandbox_id)
            or self.bindings.endpoint(scope.environment_id) != expected
            or self.bindings.get(scope).binding != binding
        ):
            raise unavailable()
        if not allow_closed and self.state.closed(binding):
            raise unavailable()
        return env, channel

    def check(
        self,
        binding: RunKernelBinding,
        environment: _Environment,
        channel: ExecutorChannel,
        *,
        recovery: bool = False,
        allow_closed: bool = False,
    ) -> None:
        current = self.proof(binding, recovery=recovery, allow_closed=allow_closed)
        if current[0] is not environment or current[1] is not channel:
            raise unavailable()

    @staticmethod
    def admission(admitted: Callable[[], bool]) -> None:
        if not callable(admitted) or admitted() is not True:
            raise unavailable()

    async def submit(
        self, request: RunCellRequest, *, admitted: Callable[[], bool]
    ) -> tuple[RunCellExecution, bool]:
        try:
            raw = run_cell_bytes(request)
            request = RunCellRequest.model_validate_json(raw, strict=True)
        except (ValueError, TypeError, UnicodeError, ValidationError):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid internal child request") from None
        m = self.manager
        self.admission(admitted)
        found = m.store.run_operation(request.context.project_id, request.operation_id)
        if found is not None:
            execution_id, digest = found
            if digest != hashlib.sha256(raw).hexdigest():
                raise WorkerError(ErrorKind.CONFLICT, "internal child operation already differs")
            execution = m.store.run_execution(execution_id)
            saved = self.state.request(execution)
            if saved != request:
                raise unavailable()
            return self.wrap(request.binding, execution), False
        environment, channel = self.proof(request.binding)
        m._package_origin(request.cell(), None)
        execution = Execution(
            execution_id=new_id("exe"),
            attempt_id=new_id("att"),
            operation_id=request.operation_id,
            kind="cell",
            context=request.context,
            state=ExecutionState.QUEUED,
            submitted_at=datetime.now(UTC),
        )

        def validate() -> None:
            self.admission(admitted)
            self.check(request.binding, environment, channel)

        m.store.insert_run_execution(execution, request, validate=validate)
        live = m._track(execution, request.output_limit_bytes)
        live.is_run, live.run_request, live.run_admission = True, request, admitted
        m._state(live, ExecutionState.QUEUED)
        m.store.update_execution(live.execution)
        try:
            validate()
        except WorkerError:
            m._lose(live, "the original child channel before dispatch")
            raise
        m._spawn(m._dispatch(live, request))
        return self.wrap(request.binding, live.execution), True

    @staticmethod
    def wrap(binding: RunKernelBinding, execution: Execution) -> RunCellExecution:
        return RunCellExecution(
            binding=binding, binding_sha256=run_kernel_binding_sha256(binding), execution=execution
        )

    def get(
        self, binding: RunKernelBinding, execution_id: str, attempt_id: str
    ) -> RunCellExecution:
        binding = checked_run_kernel_binding(binding)
        if (
            type(execution_id) is not str
            or re.fullmatch(r"exe-[0-9a-f]{24}", execution_id) is None
            or type(attempt_id) is not str
            or re.fullmatch(r"att-[0-9a-f]{24}", attempt_id) is None
        ):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid original child attempt")
        execution = self.manager.store.run_execution(execution_id)
        request = self.state.request(execution)
        if execution.attempt_id != attempt_id or request.binding != binding:
            raise WorkerError(ErrorKind.NOT_FOUND, "no selected child attempt")
        return self.wrap(binding, execution)

    async def cancel(
        self,
        binding: RunKernelBinding,
        execution_id: str,
        attempt_id: str,
        *,
        admitted: Callable[[], bool],
    ) -> RunCellExecution:
        self.admission(admitted)
        result = self.get(binding, execution_id, attempt_id)
        if result.execution.state not in TERMINAL_STATES:
            self.proof(binding, allow_closed=True)
            live = self.manager._live.get(execution_id)
            if live is None:
                raise unavailable()
            await self.manager._cancel_selected(live)
        return self.get(binding, execution_id, attempt_id)

    def kernel(
        self,
        live: _Live,
        raw: object,
        *,
        remember: bool = False,
        restarted: bool = False,
        fingerprint: object = None,
    ) -> tuple[KernelInfo | None, bool]:
        request = live.run_request
        if request is None:
            return None, live.execution.kernel_state_lost
        try:
            info = self.map_kernel(
                request.binding,
                raw,
                live.channel,
                remember=remember,
                owning_execution=live.execution.execution_id if not live.reattached else None,
                restarted=restarted,
                fingerprint=fingerprint,
            )
        except (WorkerError, ValueError, ValidationError, sqlite3.Error):
            if remember:
                raise
            return None, live.execution.kernel_state_lost
        return info, live.execution.kernel_state_lost or (
            type(raw) is dict and cast(dict[str, object], raw).get("state_lost") is True
        )

    def map_kernel(
        self,
        binding: RunKernelBinding,
        raw: object,
        channel: ExecutorChannel | None,
        *,
        remember: bool = False,
        owning_execution: str | None = None,
        restarted: bool = False,
        fingerprint: object = None,
    ) -> KernelInfo:
        environment, current_channel = self.proof(binding, recovery=True, allow_closed=True)
        del environment
        if current_channel is not channel or type(raw) is not dict:
            raise unavailable()
        raw = cast(dict[str, object], raw)
        generation, language, kernel_id = (
            raw.get("generation"),
            raw.get("language"),
            raw.get("kernel_id"),
        )
        binding_key = raw.get("run_binding_sha256")
        birth_id = raw.get("birth_id")
        if (
            type(binding_key) is not str
            or len(binding_key) != 64
            or type(generation) is not int
            or not 1 <= generation < 2**31
            or type(language) is not str
            or language not in ("python", "sage")
            or type(kernel_id) is not str
            or len(kernel_id) > 128
            or type(birth_id) is not str
            or re.fullmatch(r"birth-[0-9a-f]{24}", birth_id) is None
        ):
            raise unavailable()
        for name, maximum in (("start_environment", 32), ("start_execution", 128)):
            item = raw.get(name)
            if item is not None and (type(item) is not str or len(item) > maximum):
                raise unavailable()
        epoch = raw.get("start_package_epoch")
        if epoch is not None and (type(epoch) is not int or not 0 <= epoch < 2**62):
            raise unavailable()
        expected_id = run_kernel_id(binding, language)
        if binding_key != run_kernel_binding_sha256(binding) or kernel_id != expected_id:
            raise unavailable()
        current = self.manager.store.kernel_starts.current(binding.scope.environment_id)
        revision = current[0] if current is not None else None
        info = KernelInfo(
            kernel_id=kernel_id, language=language, generation=generation, current_revision=revision
        )
        start_environment = raw.get("start_environment")
        pin = (
            current is not None
            and current[1] == start_environment
            and self.manager.store.kernel_starts.epoch(
                binding.scope.environment_id,
                binding.incarnation,
                raw.get("start_package_epoch"),
                start_environment,
            )
            == revision
            and (
                (
                    owning_execution is not None
                    and raw.get("start_execution") == owning_execution
                    and fingerprint == start_environment
                )
                or restarted
            )
        )
        birth = KernelBirthPin(
            project_id=binding.scope.project_id,
            environment_id=binding.scope.environment_id,
            session_id=binding.scope.session_id,
            language=language,
            incarnation=binding.incarnation,
            kernel_id=kernel_id,
            sandbox=binding.sandbox,
            sandbox_id=binding.sandbox_id,
            generation=generation,
            birth_id=birth_id,
            start_revision=revision if pin else None,
        )
        return self.state.observe(binding, birth, info, remember=remember)

    async def observe(self, binding: RunKernelBinding) -> RunKernelObservation:
        binding = checked_run_kernel_binding(binding)
        self.proof(binding, allow_closed=True)

        def build(
            context: ExecutionContext, channel: ExecutorChannel, raw: object
        ) -> RunKernelObservation:
            del context
            self.proof(binding, allow_closed=True)
            if type(raw) is not list or len(cast(list[object], raw)) > 2:
                raise unavailable()
            entries: list[KernelLineageEntry] = []
            for item in cast(list[object], raw):
                info = self.map_kernel(binding, item, channel)
                birth = self.state.birth(binding, info.language)
                if (
                    birth is not None
                    and (birth.kernel_id, birth.generation, birth.start_revision)
                    == (info.kernel_id, info.generation, info.start_revision)
                    and info.start_revision is not None
                ):
                    entries.append(
                        KernelLineageEntry(kernel=info, birth=birth, lineage_status="proved")
                    )
                else:
                    entries.append(
                        KernelLineageEntry(
                            kernel=info, lineage_status="unknown", unknown_reason="missing_birth"
                        )
                    )
            current = self.manager.store.kernel_starts.current(binding.scope.environment_id)
            return RunKernelObservation(
                binding=binding,
                current_revision=current[0] if current else None,
                kernels=tuple(entries),
                observed_at=datetime.now(UTC),
            )

        return await self.manager._passive_kernels(
            KernelStatusRequest(context=_context(binding)),
            build,
            operation="run_kernels",
            fields=_wire(binding),
            protocol="run_kernel_protocol",
        )

    async def restart(
        self,
        binding: RunKernelBinding,
        operation_id: str,
        language: Literal["python", "sage"],
        *,
        admitted: Callable[[], bool],
    ) -> RunControlReceipt:
        return await self._control(binding, operation_id, "restart", language, admitted)

    async def close(
        self, binding: RunKernelBinding, operation_id: str, *, admitted: Callable[[], bool]
    ) -> RunControlReceipt:
        return await self._control(binding, operation_id, "close", None, admitted)

    async def _control(
        self,
        binding: RunKernelBinding,
        operation_id: str,
        action: Literal["restart", "close"],
        language: Literal["python", "sage"] | None,
        admitted: Callable[[], bool],
    ) -> RunControlReceipt:
        binding = checked_run_kernel_binding(binding)
        self.admission(admitted)
        key = run_kernel_binding_sha256(binding)
        if len(self._controls) >= 8 or key in self._controls:
            raise unavailable()
        previous = self.state.lookup_control(binding, operation_id, action, language)
        if previous is not None:
            return previous
        environment, channel = self.proof(binding, allow_closed=action == "close")
        receipt, new = self.state.begin_control(binding, operation_id, action, language)
        if not new:
            return receipt  # pending is unknown, never resend; done is historical only
        observer = self.manager._kernel_observations.get(binding.scope.environment_id)
        if action == "close" and observer is not None:
            observer.valid = False
        withdrawn = False

        async def actual() -> RunControlReceipt:
            environment.requests += 1
            try:
                self.admission(admitted)
                if withdrawn:
                    raise unavailable()
                self.check(binding, environment, channel, allow_closed=action == "close")
                if action == "restart":
                    self.manager.package_admission(binding.scope.environment_id)
                    if not await self.manager._sync_package_gate(
                        binding.scope.environment_id, channel
                    ):
                        raise unavailable()
                    self.manager.package_admission(binding.scope.environment_id)
                    self.check(binding, environment, channel)
                    self.admission(admitted)
                    if withdrawn:
                        raise unavailable()
                reply = await channel.request(
                    "run_" + action,
                    session=binding.scope.session_id,
                    **_wire(binding),
                    **({"language": language} if language is not None else {}),
                    reply_timeout=180,
                )
                self.check(binding, environment, channel, allow_closed=action == "close")
                with self.manager.store._transaction():
                    kernel = (
                        self.map_kernel(
                            binding, reply.get("kernel"), channel, remember=True, restarted=True
                        )
                        if action == "restart"
                        else None
                    )
                    if action == "close" and reply.get("kernels") != []:
                        raise unavailable()
                    return self.state.finish_control(receipt.request, kernel)
            finally:
                environment.requests -= 1
                # Controls are explicit activity; passive reads never use this path.
                environment.last_used = time.monotonic()

        task = asyncio.create_task(actual())
        self._controls[key] = task
        first: asyncio.CancelledError | None = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    if first is None:
                        first = exc
                    withdrawn = True
            if first is not None:
                # Retrieve any failure without replacing original caller cancellation.
                with contextlib.suppress(BaseException):
                    task.result()
                raise first
            return task.result()
        finally:
            self._controls.pop(key)

    async def drain(self) -> None:
        if self._controls:
            await asyncio.gather(*tuple(self._controls.values()), return_exceptions=True)

    def close_session(self, context: ExecutionContext) -> None:
        """Persist tombstones before guest cleanup; missing namespace is never initialized."""
        db = self.manager.store._db
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='run_dispatch_meta'").fetchone()
            is None
        ):
            return
        rows = db.execute(
            (
                "SELECT CASE WHEN typeof(record)='text' THEN substr(CAST(reco"
                "rd AS BLOB),1,4097) END FROM run_kernel_bindings WHERE proje"
                "ct=? AND session=? AND environment=? LIMIT 1025"
            ),
            (context.project_id, context.session_id, context.environment_id),
        ).fetchall()
        if len(rows) > 1024:
            raise unavailable()
        from sandbox_science.contracts.run_kernels import RunKernelEnrollment

        with self.manager.store._transaction():
            self.state.budget()
            for row in rows:
                enrollment = RunKernelEnrollment.model_validate_json(row[0], strict=True)
                if (
                    enrollment.binding.scope.project_id != context.project_id
                    or enrollment.binding.scope.session_id != context.session_id
                    or enrollment.binding.scope.environment_id != context.environment_id
                ):
                    raise unavailable()
                db.execute(
                    "INSERT OR IGNORE INTO run_kernel_closures VALUES(?,?,?)",
                    (
                        run_kernel_binding_sha256(enrollment.binding),
                        context.project_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            self.state.budget()
