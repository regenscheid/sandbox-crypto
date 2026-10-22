# SPDX-License-Identifier: MIT
"""Provisioning operations: an agent asks for a capability or packages, and the worker
decides, installs in the environment's turn, records and probes (Conda provisioning).

docs/specs/conda-provisioning.md defines the behavior; docs/WORKER.md ("Provisioning
operations") describes this implementation. An operation:

1. waits its turn: operations on one environment run one at a time, in the order they
   were requested, and the waiting ones are visible as a queue (CP-11);
2. for a capability, runs its probes first, and ends ``already_satisfied`` if they pass
   at the current revision (CP-2);
3. refuses system packages (CP-5), and channels or ecosystems whose index the policy
   denies (CP-6);
4. resolves the request in a dry run inside the sandbox, as an execution (CP-7, CP-10);
5. decides each resolved file with the policy library, as the policy middleware will
   decide its download, and refuses AGPL packages the policy does not except (CP-6, CP-9);
6. when the policy escalates any file, asks once for the exact set and waits in
   ``awaiting_permission`` (CP-8);
7. installs, in the environment's turn, as an execution under the policy's wall time
   (CP-10, CP-11, CP-14); the revision it makes is a managed one, attributed to it;
8. probes the capability, or checks the requested packages' versions (CP-21 to CP-24);
9. records everything, completed or not, with the stage where it stopped (CP-16, CP-17).

A failed install that changed the packages ends ``needs_recovery``: restoring the
previous revision is the next slice's (CP-13), and ``Restorer`` is its seam.

Steps are executions in the operation's own context, with the worker's operation IDs
(``prv-<hex>.<step>``), so the harness can read and cancel each like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    ProbeResult,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ShellRequest,
    new_id,
)
from sandbox_science.contracts.provisioning import (
    MAX_FILES,
    MAX_QUEUED,
    InstallerLabel,
    InstallerStep,
    Stage,
)
from sandbox_science.contracts.revisions import MAX_PROBE_OUTPUT
from sandbox_science.contracts.worker import MAX_TIMEOUT_SECONDS
from sandbox_science.policy import Action

from . import installer_records, resolution
from .catalog import CapabilitySpec, Catalog, ProbeSpec
from .errors import WorkerError
from .identity import execution_digest, provision_digest, same_execution, same_provision
from .manager import ExecutionManager
from .permissions import Permissions, PermissionStoreError
from .provision_events import ProvisionEvents
from .provision_permissions import ProvisionPermissions
from .provision_reconciliation import ProvisionReconciliation
from .provision_types import (
    DRY_RUN_OUTPUT,
    DRY_RUN_TIMEOUT,
    LOOK_TIMEOUT,
    MAX_WAIT,
    PERMISSION_POLL,
    STAGES,
    STEP_OUTPUT,
    Completion,
    Operation,
    Stop,
    append_notes,
    cancelled,
    clip,
    ended_revision,
    install_timeout,
    started_revision,
    step_error,
    tail,
)
from .provision_types import Governing as Governing
from .provision_types import PolicySource as PolicySource
from .provision_types import Restorer as Restorer
from .recovery_fence import check_dispatch, control_context
from .resolution import Decided, Resolved, ResolveError
from .timeouts import seconds

_log = logging.getLogger(__name__)

RESERVED_OPERATION: Final = re.compile(r"prv-[0-9a-f]{24}\..*")
"""Execution operation IDs the worker keeps for provisioning steps; clients may not use them."""


class Provisioner:
    """Runs provisioning operations for the environments the worker governs."""

    def __init__(
        self,
        manager: ExecutionManager,
        *,
        catalog: Catalog,
        policies: PolicySource | None,
        permissions: Permissions | None,
        cache: str | None,
        restorer: Restorer | None = None,
        clock: Clock = SYSTEM_CLOCK,
        poll: float = PERMISSION_POLL,
        lockfile_wait: float = 10.0,
        retain: float = 600.0,
    ) -> None:
        self.manager = manager
        self.store = manager.store
        self.revisions = manager.revisions
        self.catalog = catalog
        self.policies = policies
        self.permissions = permissions
        self.cache = cache
        self.restorer = restorer
        self.clock = clock
        """Stamps the operations' records and times permission deadlines."""
        self.poll = seconds("poll", poll)
        self.lockfile_wait = seconds("lockfile_wait", lockfile_wait)
        """Seconds an operation waits for a revision's lockfile to arrive."""
        self.retain = seconds("retain", retain)
        """Seconds a finished operation stays in memory for clients still waiting on it."""
        self.installer_logs = installer_records.InstallerArtifacts(
            self.store, manager.blobs, manager.max_artifact_size
        )
        self.events = ProvisionEvents(permissions)
        self._ops: dict[str, Operation] = {}
        self._queues: dict[str, list[str]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.permission_flow = ProvisionPermissions(
            permissions, clock, self.poll, self._update, self._observe
        )

        self.reconciliation = ProvisionReconciliation(
            manager, restorer, clock, self.lockfile_wait, self._update
        )

    # The API.

    def supports(self, environment_id: str) -> bool:
        """Whether the worker can provision the environment: it governs its policy."""
        return self.policies is not None and self.policies.governing(environment_id) is not None

    async def start(self) -> None:
        """End the operations a previous run left unfinished: they never run again. Their
        permission requests are withdrawn, those a crash left unnoted included, and an
        install cut short leaves its environment needing recovery (CP-13)."""
        unfinished = self.store.unfinished_provisions()
        if self.permissions is not None:
            # Nothing runs yet: no request of an operation this worker knows may stay open,
            # whether a crash, a stop or a failed withdrawal left it.
            try:
                await asyncio.to_thread(
                    self.permissions.withdraw_operations,
                    self.store.provision_ids(),
                    "the worker restarted",
                    self.clock.now(),
                )
            except PermissionStoreError as exc:
                _log.warning("could not withdraw the permission requests left open: %s", exc)
        for index, record in enumerate(unfinished):
            stop = self.reconciliation.cut_short(record, "restarted").completion
            finished = _validated(record, **self._fields(record, stop), seq=record.seq + 1)
            self.store.update_provision(finished)
            unfinished[index] = finished
        # No ready ACK between classifications: a later unfinished install may
        # establish recovery even when the first queued operation never started.
        for environment in {record.request.context.environment_id for record in unfinished}:
            try:
                await self.manager.sync_package_gate(environment)
            except WorkerError:
                _log.warning(
                    "environment %s remains admission-closed after startup classification",
                    environment,
                )
        for finished in unfinished:
            await self._observe(finished, "provision.finished", startup=True)

    async def submit(self, request: ProvisionRequest) -> tuple[ProvisionOperation, bool]:
        """The operation for ``request.operation_id``, created and queued if it is new."""
        # Refuse copied/constructed unknown actions before encoding or replay;
        # an unrecognized action can never silently take the install path.
        if type(request.action) is not str or request.action not in ("install", "upgrade"):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "unsupported provisioning action")
        if request.action == "upgrade" and (
            not request.conda or request.pip or request.system or request.capability is not None
        ):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "upgrade requires named Conda specs only")
        with self.store.recovery_fences.bind(request.context):
            return await self._submit_owned(request)

    async def _submit_owned(self, request: ProvisionRequest) -> tuple[ProvisionOperation, bool]:
        environment_id = request.context.environment_id
        if request.capability is not None and self.catalog.get(request.capability) is None:
            names = ", ".join(sorted(self.catalog.capabilities))
            raise WorkerError(
                ErrorKind.INVALID_REQUEST,
                f"the catalog has no capability {request.capability!r}; it has {names}. "
                "Ask for packages by their conda-forge names instead",
            )
        found = self.store.find_provision(request.context.project_id, request.operation_id)
        if found is not None:
            provision_id, existing = found
            if not same_provision(existing, request):
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"operation {request.operation_id} was submitted with a different request",
                )
            return self.get(provision_id), False
        self.manager.package_admission(environment_id)
        if not self.supports(environment_id):
            raise WorkerError(
                ErrorKind.UNSUPPORTED,
                f"environment {environment_id!r} cannot be provisioned here: the worker "
                "provisions the environments it created, under their stored policy (D73)",
            )
        if len(self._queues.get(environment_id, ())) >= MAX_QUEUED:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"environment {environment_id!r} has {MAX_QUEUED} provisioning operations "
                "waiting, its limit; wait for them, or cancel some",
            )
        current = self.store.revision(environment_id)
        record = ProvisionOperation(
            provision_id=new_id("prv"),
            request=request,
            state=ProvisionState.QUEUED,
            catalog=self.catalog.ref,
            planned_revision=current.revision if current is not None else None,
            submitted_at=self.clock.now(),
            installer_steps=(),
        )
        self.store.insert_provision(record, provision_digest(request))
        op = Operation(record, asyncio.get_running_loop().create_future())
        self._ops[record.provision_id] = op
        queue = self._queues.setdefault(environment_id, [])
        queue.append(record.provision_id)
        if len(queue) == 1:
            op.turn.set()
        op.task = self._spawn(self._run(op))
        _log.info(
            "provisioning %s for %s in %s: %s",
            record.provision_id,
            request.context.project_id,
            environment_id,
            request.capability or ", ".join([*request.conda, *request.pip, *request.system]),
        )
        return self._view(op), True

    def get(self, provision_id: str, project_id: str | None = None) -> ProvisionOperation:
        """The operation, which must belong to ``project_id`` when one is given."""
        op = self._ops.get(provision_id)
        record = op.record if op is not None else self.store.provision(provision_id)
        if record is None or (
            project_id is not None and record.request.context.project_id != project_id
        ):
            raise WorkerError(ErrorKind.NOT_FOUND, f"no provisioning operation {provision_id}")
        return self._view(op) if op is not None else record

    async def wait(
        self, provision_id: str, after: int, timeout: float, project_id: str | None = None
    ) -> ProvisionOperation:
        """The operation once its record is newer than ``after``, it has finished, or
        ``timeout`` seconds have passed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(timeout, MAX_WAIT))
        while True:
            record = self.get(provision_id, project_id)
            op = self._ops.get(provision_id)
            remaining = deadline - loop.time()
            if (
                op is None
                or record.seq > after
                or record.state is ProvisionState.FINISHED
                or remaining <= 0
            ):
                return record
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(op.changed), remaining)

    async def cancel(
        self,
        provision_id: str,
        project_id: str | None = None,
        *,
        context: ExecutionContext | None = None,
    ) -> ProvisionOperation:
        """Cancel the operation: one waiting ends at once, a running step is cancelled like
        any execution, and an install that changed the packages follows CP-13."""
        record = self.get(provision_id, project_id)
        with self.store.recovery_fences.bind(control_context(record.request.context, context)):
            return await self._cancel_owned(provision_id, record)

    async def _cancel_owned(
        self, provision_id: str, record: ProvisionOperation
    ) -> ProvisionOperation:
        op = self._ops.get(provision_id)
        if op is None or record.state is ProvisionState.FINISHED:
            return record
        op.cancelled.set()
        if op.step is not None:
            with contextlib.suppress(WorkerError):
                await self.manager.cancel(op.step)
        return self._view(op)

    def queue(self, environment_id: str) -> ProvisioningQueue:
        """The environment's operations that have not finished, in request order (CP-11)."""
        ops = [self._ops[pid] for pid in self._queues.get(environment_id, ()) if pid in self._ops]
        current = self.store.revision(environment_id)
        status = "ready"
        if any(op.record.state is ProvisionState.INSTALLING for op in ops):
            status = "changing"
        elif (marked := self.store.package_status(environment_id)) is not None:
            status = marked[0]
        return ProvisioningQueue.model_validate(
            {
                "environment_id": environment_id,
                "status": status,
                "revision": current.revision if current is not None else None,
                "operations": [self._view(op) for op in ops],
            }
        )

    async def forget(self, environment_id: str) -> None:
        """Drain old operations before their binding/fence can be forgotten or reused.

        The environment owns this required hook through repeated caller cancellation.
        A terminal task with a failed classification write is not a durable drain.
        """
        operations = [
            op
            for op in self._ops.values()
            if op.record.request.context.environment_id == environment_id
        ]
        failure: WorkerError | None = None
        for op in operations:
            op.cancelled.set()
            try:
                if op.step is not None:
                    await self.manager.cancel(op.step)
            except WorkerError as exc:
                failure = exc
            finally:
                if op.task is not None and not op.task.done():
                    op.task.cancel()
        await asyncio.gather(
            *(op.task for op in operations if op.task is not None), return_exceptions=True
        )
        for op in operations:
            self._stopped(op)  # retry a classification whose first SQL write failed
        if not self.store.provisioning_finished(environment_id):
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "old provisioning classification is not durably finished"
            )
        if failure is not None:
            raise failure

    async def close(self) -> None:
        """End every operation still running: each is recorded as failed."""
        tasks = [op.task for op in self._ops.values() if op.task is not None and not op.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.installer_logs.close()
        await self.events.close()

    # Bookkeeping.

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _view(self, op: Operation) -> ProvisionOperation:
        """The record as clients see it, with its place in the queue while it waits."""
        record = op.record
        if record.state is not ProvisionState.QUEUED:
            return record
        queue = self._queues.get(record.request.context.environment_id, [])
        position = queue.index(record.provision_id) if record.provision_id in queue else None
        return record.model_copy(update={"queue_position": position})

    def _update(self, op: Operation, **changes: Any) -> None:
        """Change the operation's record, store it, and wake whoever waits on it."""
        record = _validated(op.record, **changes, seq=op.record.seq + 1)
        self.store.update_provision(record)
        op.record = record
        changed, op.changed = op.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def _release(self, op: Operation) -> None:
        """Give the environment's turn to the next operation."""
        environment_id = op.record.request.context.environment_id
        queue = self._queues.get(environment_id, [])
        if op.record.provision_id in queue:
            queue.remove(op.record.provision_id)
        if queue:
            following = self._ops.get(queue[0])
            if following is not None:
                following.turn.set()
            for provision_id in queue:  # their places changed
                waiting = self._ops.get(provision_id)
                if waiting is not None:
                    changed, waiting.changed = (
                        waiting.changed,
                        asyncio.get_running_loop().create_future(),
                    )
                    changed.set_result(None)
        else:
            self._queues.pop(environment_id, None)

        def retire(_task: asyncio.Task[None] | None = None) -> None:
            asyncio.get_running_loop().call_later(
                self.retain, self._ops.pop, op.record.provision_id, None
            )

        # forget() locates environment owners through _ops. A terminal append
        # can outlive retention, so begin eviction only after its task drains.
        if op.task is not None and not op.task.done():
            op.task.add_done_callback(retire)
        else:
            retire()

    # The operation.

    async def _observe(
        self, record: ProvisionOperation, kind: str, *, startup: bool = False
    ) -> None:
        delivery, interrupted = await self.events.emit(record, kind, startup=startup)
        if delivery.status in ("unknown", "unavailable"):
            # Fixed text only: original EventStore errors can contain private paths.
            _log.warning("provision observation unavailable; no append retry is scheduled")
        if interrupted is not None:
            raise interrupted

    async def _run(self, op: Operation) -> None:
        interrupted: asyncio.CancelledError | None = None
        try:
            try:
                await self._observe(op.record, "provision.requested")
                if not await self._take_turn(op):
                    raise Stop(
                        ProvisionOutcome.FAILED,
                        "cancelled",
                        "queue",
                        ErrorInfo(
                            kind=ErrorKind.CANCELLED, message="cancelled before its turn came"
                        ),
                        next_step="Nothing was installed.",
                    )
                completion = await self._operate(op)
                await self._end(op, completion)
            except Stop as stop:
                await self._end(op, stop.completion)
        except asyncio.CancelledError as error:
            interrupted = error
            self._stopped(op)
        except Exception:
            # Whatever failed, recording the end included, the operation must end.
            _log.exception("provisioning operation %s failed", op.record.provision_id)
            error = ErrorInfo(
                kind=ErrorKind.INTERNAL,
                message="the worker failed during this operation; its executions and "
                "the environment's revisions tell what happened",
            )
            failed = Stop(ProvisionOutcome.FAILED, "install_failed", STAGES[op.record.state], error)
            try:
                if op.record.state is ProvisionState.INSTALLING:
                    failed = self.reconciliation.unseen_install(
                        op.record, "the worker failed during its install", "install_failed", error
                    )
                await self._end(op, failed.completion)
            except Exception:
                _log.exception("could not record the end of %s", op.record.provision_id)
                await self.permission_flow.close(
                    op.record.provision_id, op.record.permission_request, "its operation failed"
                )
        finally:
            # The terminal record already classifies the installer. Observation
            # persistence must not retain its queue turn; this task still owns
            # and drains the append before close/forget can release its stores.
            self._release(op)
            try:
                if op.record.state is ProvisionState.FINISHED:
                    await self._observe(op.record, "provision.finished")
            except asyncio.CancelledError as error:
                interrupted = interrupted or error
        if interrupted is not None:
            raise interrupted

    async def _take_turn(self, op: Operation) -> bool:
        """Wait for the environment's turn; False if the operation was cancelled first."""
        if not op.turn.is_set():
            waits = [
                asyncio.ensure_future(op.turn.wait()),
                asyncio.ensure_future(op.cancelled.wait()),
            ]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
        return op.turn.is_set() and not op.cancelled.is_set()

    async def _operate(self, op: Operation) -> Completion:
        request = op.record.request
        environment_id = request.context.environment_id
        try:
            self.manager.package_admission(environment_id)
        except WorkerError as exc:
            raise Stop(
                ProvisionOutcome.FAILED,
                "environment_changed",
                "queue",
                exc.info(),
                next_step="The environment needs recovery; this operation did not run.",
            ) from None
        governing = self._governing(environment_id)
        self._update(
            op,
            state=ProvisionState.CHECKING if request.capability else ProvisionState.RESOLVING,
            started_at=self.clock.now(),
            policy=governing.policy.digest,
            queue_position=None,
        )
        if request.system:
            names = ", ".join(request.system)
            raise Stop(
                ProvisionOutcome.DENIED,
                "base_image_change_required",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.POLICY_DENIED,
                    message=f"base_image_change_required: {names} would be system packages, "
                    "which cannot be installed at runtime (packages.system_packages: deny, D48)",
                ),
                next_step="System packages come only with the base image. Ask the user to add "
                "them to it, or look for the same libraries on conda-forge, which has many.",
            )
        capability = self.catalog.get(request.capability) if request.capability else None
        conda = capability.conda if capability is not None else request.conda
        if capability is not None and request.capability is not None:
            await self._check(op, request.capability, capability)
        refusal = resolution.index_refusal(
            conda, request.pip, governing.policy, governing.ceiling, governing.floor
        )
        if refusal is not None:
            rule = refusal.rule or refusal.layer
            raise Stop(
                ProvisionOutcome.DENIED,
                "policy_denied",
                "resolve",
                ErrorInfo(kind=ErrorKind.POLICY_DENIED, message=f"{refusal.reason} ({rule})"),
                next_step="The project policy refuses this source. Ask the user to change the "
                "policy, or find the package on an allowed channel.",
            )
        resolved = await self._resolve(op, conda, request.pip)
        if not resolved:
            await self._nothing_to_install(op, request, capability)
        decided = resolution.decide(resolved, governing.policy, governing.ceiling, governing.floor)
        op.files = decided
        self._update(op, files=decided.files)
        self._refuse(decided, governing)
        if decided.with_action(Action.ESCALATE):
            await self.permission_flow.request(op, governing, decided)
        governing = self._unchanged(op)
        before, after = await self._install(op, governing, conda, decided)
        return await self.reconciliation.settle(
            op, governing, capability, decided, before, after, self._probe
        )

    def _governing(self, environment_id: str) -> Governing:
        governing = self.policies.governing(environment_id) if self.policies is not None else None
        if governing is None:
            raise Stop(
                ProvisionOutcome.FAILED,
                "policy_changed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.UNAVAILABLE,
                    message=f"environment {environment_id!r} has no policy the worker can read",
                ),
            )
        return governing

    def _unchanged(self, op: Operation) -> Governing:
        """What governs the environment now, if it is still the policy the decision used."""
        governing = self._governing(op.record.request.context.environment_id)
        if governing.policy.digest != op.record.policy:
            raise Stop(
                ProvisionOutcome.FAILED,
                "policy_changed",
                "permission",
                ErrorInfo(
                    kind=ErrorKind.CONFLICT,
                    message=f"the environment's policy changed from {op.record.policy} to "
                    f"{governing.policy.digest} before the install",
                ),
                next_step="Request the install again: it is decided under the new policy.",
            )
        return governing

    # Steps.

    async def _execute(
        self,
        op: Operation,
        argv: Sequence[str],
        *,
        label: str,
        stage: Stage,
        timeout: float,
        limit: int,
    ) -> Execution:
        """Run one step as an execution in the operation's context, to its end. Its origin
        names the operation; a change to the packages is the operation's only when the
        step's own package commands made it (managed), as only the install's do."""
        if op.cancelled.is_set():
            raise cancelled(stage)
        check_dispatch()
        if label in installer_records.LABELS:
            if any(step.label == label for step in op.record.installer_steps or ()):
                raise WorkerError(ErrorKind.CONFLICT, "installer step already has retained intent")
            self._installer_step(
                op,
                InstallerStep(
                    label=cast(InstallerLabel, label),
                    installer="micromamba" if label.endswith("conda") else "pip",
                    operation_id=f"{op.record.provision_id}.{label}",
                ),
            )
        request = ShellRequest(
            operation_id=f"{op.record.provision_id}.{label}",
            context=op.record.request.context,
            argv=tuple(argv),
            timeout_seconds=min(timeout, float(MAX_TIMEOUT_SECONDS)),
            output_limit_bytes=limit,
        )
        self._authorize(op, request)
        execution, _ = await self.manager.submit(request, origin=op.record.provision_id)
        updates: dict[str, Any] = {"executions": (*op.record.executions, execution.execution_id)}
        if label in installer_records.LABELS:
            pending = next(step for step in op.record.installer_steps or () if step.label == label)
            updates["installer_steps"] = tuple(
                step.model_copy(
                    update={
                        "execution_id": execution.execution_id,
                        "attempt_id": execution.attempt_id,
                    }
                )
                if step.label == pending.label
                else step
                for step in op.record.installer_steps or ()
            )
        self._update(op, **updates)
        op.step = execution.execution_id
        try:
            if op.cancelled.is_set():
                await self.manager.cancel(execution.execution_id)
            terminal = await self.manager.wait(execution.execution_id)
            if label in installer_records.LABELS:
                await self._installer_log(op, terminal, label)
            return terminal
        finally:
            op.step = None

    def _installer_step(self, op: Operation, step: InstallerStep) -> None:
        previous = op.record.installer_steps or ()
        self._update(
            op,
            installer_steps=(
                tuple(step if old.label == step.label else old for old in previous)
                if any(old.label == step.label for old in previous)
                else (*previous, step)
            ),
        )

    async def _installer_log(self, op: Operation, execution: Execution, label: str) -> None:
        pending = next(step for step in op.record.installer_steps or () if step.label == label)
        try:
            prepared = installer_records.prepare(op.record, execution, label)
        except installer_records.InstallerLogError:
            self._installer_step(
                op,
                pending.model_copy(
                    update={
                        "log_status": "unavailable",
                        "unavailable_reason": "metadata_unavailable",
                    }
                ),
            )
            raise WorkerError(
                ErrorKind.UNAVAILABLE, "original installer metadata unavailable"
            ) from None
        self._installer_step(
            op, prepared.step.model_copy(update={"log_status": "pending", "log": None})
        )
        try:
            step, interrupted = await self.installer_logs.publish(prepared)
        except installer_records.InstallerLogError:
            self._installer_step(
                op,
                prepared.step.model_copy(
                    update={
                        "log_status": "unavailable",
                        "log": None,
                        "unavailable_reason": "artifact_unavailable",
                    }
                ),
            )
            self._update(
                op,
                notes=append_notes(
                    op.record.notes,
                    "A sanitized installer log is unavailable. Original execution facts remain; "
                    "do not repeat the installer to recreate it.",
                ),
            )
            return
        self._installer_step(op, step)
        if interrupted is not None:
            raise interrupted

    async def _installer_version(self, op: Operation, conda: bool) -> None:
        label = "version-conda" if conda else "version-pip"
        execution = await self._execute(
            op,
            ("micromamba" if conda else "pip", "--version"),
            label=label,
            stage="resolve",
            timeout=10.0,
            limit=4096,
        )
        if execution.state is ExecutionState.CANCELLED or op.cancelled.is_set():
            raise cancelled("resolve")
        if execution.state in (ExecutionState.LOST, ExecutionState.TIMED_OUT):
            raise Stop(
                ProvisionOutcome.FAILED,
                "resolve_failed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.UNAVAILABLE,
                    message="Installer version observation is unknown; "
                    "no resolve/install was dispatched.",
                ),
            )

    async def _check(self, op: Operation, name: str, capability: CapabilitySpec) -> None:
        """End ``already_satisfied`` if the capability's probes pass at the current revision:
        recorded there already, or run now (CP-2)."""
        environment_id = op.record.request.context.environment_id
        current = self.store.revision(environment_id)
        wanted = {(probe.id, probe.version) for probe in capability.probes}
        if current is not None:
            recorded = [
                probe
                for probe in self.revisions.probes(environment_id, current.revision)
                if probe.capability == name and (probe.probe, probe.version) in wanted
            ]
            passing = {
                (probe.probe, probe.version) for probe in recorded if probe.status == "passed"
            }
            if passing >= wanted:
                raise Stop(
                    ProvisionOutcome.ALREADY_SATISFIED,
                    probes=tuple(recorded),
                    revision_before=current.revision,
                    revision_after=current.revision,
                )
        results = [
            await self._probe(op, name, probe, f"check-{index}", "check")
            for index, probe in enumerate(capability.probes)
        ]
        if op.cancelled.is_set():
            raise cancelled("check")
        self.revisions.record_probes(environment_id, results)
        self._update(op, probes=(*op.record.probes, *results))
        if results and all(result.status == "passed" for result in results):
            revision = results[-1].revision
            raise Stop(
                ProvisionOutcome.ALREADY_SATISFIED,
                revision_before=revision,
                revision_after=revision,
            )
        self._update(op, state=ProvisionState.RESOLVING)

    async def _probe(
        self, op: Operation, capability: str, probe: ProbeSpec, label: str, stage: Stage
    ) -> ProbeResult:
        """Run one probe as an execution and judge its output (CP-21). A probe cancelled, or
        not run because the operation was, is an ``error``: the caller decides what that
        means for the operation."""
        if op.cancelled.is_set():
            return self._unrun(op, capability, probe, "cancelled before it ran")
        execution = await self._execute(
            op,
            probe.argv,
            label=label,
            stage=stage,
            timeout=probe.timeout_seconds,
            limit=STEP_OUTPUT,
        )
        result = execution.result
        stdout = result.stdout.text if result is not None and result.stdout is not None else ""
        stderr = result.stderr.text if result is not None and result.stderr is not None else ""
        if execution.state is ExecutionState.SUCCEEDED:
            status = "passed" if probe.passes(stdout) else "failed"
        elif (
            execution.state is ExecutionState.FAILED
            and result is not None
            and result.error is None
            and (result.exit_code is not None or result.signal is not None)
        ):
            status = "failed"  # it ran, and its program failed: exit 127 if absent
        else:
            status = "error"  # it timed out, was cancelled, or could not run
        output = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}"
        if execution.state is ExecutionState.CANCELLED:
            output = f"cancelled\n{output}"
        return ProbeResult(
            capability=capability,
            probe=probe.id,
            version=probe.version,
            kind=probe.kind,
            status=status,
            # Unstarted, it ran at no revision: the one noted at dispatch is not proof.
            revision=started_revision(execution) if execution.started_at is not None else None,
            execution_id=execution.execution_id,
            output=tail(output, MAX_PROBE_OUTPUT),
            duration_seconds=result.elapsed_seconds if result is not None else 0.0,
            operation=op.record.provision_id,
            recorded_at=self.clock.now(),
        )

    def _unrun(self, op: Operation, capability: str, probe: ProbeSpec, why: str) -> ProbeResult:
        """A probe that never ran, so at no revision."""
        return ProbeResult(
            capability=capability,
            probe=probe.id,
            version=probe.version,
            kind=probe.kind,
            status="error",
            output=why,
            operation=op.record.provision_id,
            recorded_at=self.clock.now(),
        )

    async def _resolve(
        self, op: Operation, conda: Sequence[str], pip: Sequence[str]
    ) -> list[Resolved]:
        """Dry runs in the sandbox: the exact files the install would bring (CP-7)."""
        resolved: list[Resolved] = []
        if conda or pip:
            await self._installer_version(op, bool(conda))
        if op.record.request.action == "upgrade":
            await self._upgrade_targets(op, conda)
        steps = [
            (
                "resolve-conda",
                resolution.conda_upgrade_dry_run(conda)
                if op.record.request.action == "upgrade"
                else resolution.conda_dry_run(conda),
                resolution.parse_conda_dry_run,
                conda,
            ),
            ("resolve-pip", resolution.pip_dry_run(pip), resolution.parse_pip_report, pip),
        ]
        for label, argv, parse, specs in steps:
            if not specs:
                continue
            execution = await self._execute(
                op,
                argv,
                label=label,
                stage="resolve",
                timeout=DRY_RUN_TIMEOUT,
                limit=DRY_RUN_OUTPUT,
            )
            if execution.state is ExecutionState.CANCELLED or op.cancelled.is_set():
                raise cancelled("resolve")
            result = execution.result
            stdout = result.stdout if result is not None else None
            if execution.state is not ExecutionState.SUCCEEDED or stdout is None:
                raise Stop(
                    ProvisionOutcome.FAILED,
                    "resolve_failed",
                    "resolve",
                    step_error(execution, "the dry run"),
                    next_step="The request could not be resolved. Check the names and versions "
                    "(conda-forge's, for Conda specs), then request it again.",
                )
            if stdout.truncated:
                raise Stop(
                    ProvisionOutcome.FAILED,
                    "resolve_failed",
                    "resolve",
                    ErrorInfo(
                        kind=ErrorKind.INVALID_REQUEST,
                        message="the dry run printed more than the worker reads",
                    ),
                )
            try:
                # Up to 16 MiB of untrusted JSON: read in a thread, off the event loop.
                resolved += await asyncio.to_thread(parse, stdout.text, self.cache)
            except ResolveError as exc:
                raise Stop(
                    ProvisionOutcome.FAILED,
                    "resolve_failed",
                    "resolve",
                    ErrorInfo(kind=ErrorKind.INVALID_REQUEST, message=str(exc)),
                ) from None
        if len(resolved) > MAX_FILES:
            raise Stop(
                ProvisionOutcome.FAILED,
                "resolve_failed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.INVALID_REQUEST,
                    message=f"the install would bring {len(resolved)} package files, more than "
                    f"the {MAX_FILES} one operation may",
                ),
                next_step="Ask for fewer packages at a time.",
            )
        return resolved

    async def _upgrade_targets(self, op: Operation, specs: Sequence[str]) -> None:
        """An upgrade names already installed targets; unknown history is no fallback."""
        environment = op.record.request.context.environment_id
        current = self.store.revision(environment)
        lockfile, interrupted = await installer_records.owned(
            asyncio.create_task(
                self.reconciliation.lockfile(
                    environment, current.revision if current is not None else None
                )
            )
        )
        if interrupted is not None:
            raise interrupted
        installed: set[str] = (
            {package.name for package in lockfile.conda} if lockfile is not None else set()
        )
        if (
            current is None
            or lockfile is None
            or self.store.revision(environment) != current
            or any(resolution.spec_name(spec)[1] not in installed for spec in specs)
        ):
            raise Stop(
                ProvisionOutcome.FAILED,
                "resolve_failed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.UNAVAILABLE,
                    message="Upgrade targets are absent from the current retained Conda lockfile "
                    "or that revision is unknown/changed; no update was dispatched.",
                ),
            )

    async def _nothing_to_install(
        self, op: Operation, request: ProvisionRequest, capability: CapabilitySpec | None
    ) -> None:
        """End an operation whose dry runs found nothing to install."""
        environment_id = request.context.environment_id
        current = self.store.revision(environment_id)
        revision = current.revision if current is not None else None
        at = f"revision {revision}" if revision is not None else "the current revision"
        if capability is not None:
            raise Stop(
                ProvisionOutcome.FAILED,
                "probe_failed",
                "probe",
                ErrorInfo(
                    kind=ErrorKind.EXCEPTION,
                    message=f"{request.capability}'s packages are installed, but its probes "
                    f"fail at {at}",
                ),
                next_step="Read the probes' output: the capability is installed but does not "
                "work as the catalog expects.",
                revision_before=revision,
                revision_after=revision,
            )
        checks = await self.reconciliation.version_checks(op, request, revision)
        self.revisions.record_probes(environment_id, checks)
        unseen = [check for check in checks if check.status != "passed"]
        if unseen:
            names = ", ".join(f"{check.probe} ({check.status})" for check in unseen)
            raise Stop(
                ProvisionOutcome.FAILED,
                "probe_failed",
                "probe",
                ErrorInfo(
                    kind=ErrorKind.EXCEPTION,
                    message=f"the dry runs found nothing to install, but the version checks "
                    f"at {at} did not pass: {names}",
                ),
                next_step="Read the checks' output: the packages may be installed under other "
                "names, or the environment's lockfile may be missing.",
                probes=(*op.record.probes, *checks),
                revision_before=revision,
                revision_after=revision,
            )
        raise Stop(
            ProvisionOutcome.ALREADY_SATISFIED,
            probes=(*op.record.probes, *checks),
            revision_before=revision,
            revision_after=revision,
        )

    def _refuse(self, decided: Decided, governing: Governing) -> None:
        """Refuse an AGPL package the policy does not except (CP-9), or a file the policy
        denies (CP-6), before anything downloads."""
        refused = resolution.agpl_refused(decided.files, governing.policy)
        if refused:
            names = "; ".join(
                f"{file.ecosystem} package {file.name} {file.version} ({file.license or 'AGPL'})"
                for file in refused
            )
            raise Stop(
                ProvisionOutcome.DENIED,
                "agpl_license",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.POLICY_DENIED,
                    message=f"the resolved packages include AGPL-licensed software: {names}",
                ),
                next_step="The project refuses AGPL packages unless its policy names them "
                "under packages.agpl_exceptions. Use another package, or ask the user.",
            )
        denied = decided.with_action(Action.DENY)
        if denied:
            first = decided.decisions[denied[0]]
            file = decided.files[denied[0]]
            raise Stop(
                ProvisionOutcome.DENIED,
                "policy_denied",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.POLICY_DENIED,
                    message=f"the policy refuses {file.name} {file.version}: {first.reason} "
                    f"({first.rule or first.layer}), and {len(denied) - 1} other files",
                ),
                next_step="The project policy refuses these files. Ask the user to change the "
                "policy, or find another package.",
            )

    async def _install(
        self, op: Operation, governing: Governing, conda: Sequence[str], decided: Decided
    ) -> tuple[int | None, int | None]:
        """Install in the environment's turn, as an execution, under the policy's wall time
        (CP-10, CP-14); return the revisions before and after. A request names Conda or pip
        packages, never both, so one step installs them: micromamba pinned to the decided
        files, or pip with exactly them."""
        check_dispatch()
        environment_id = op.record.request.context.environment_id
        current = self.store.revision(environment_id)
        before = current.revision if current is not None else None
        self._update(
            op,
            state=ProvisionState.INSTALLING,
            outcome=None,
            next_step=None,
            revision_before=before,
        )
        conda_files = [file for file in decided.files if file.ecosystem == "conda"]
        try:
            if conda_files:
                label = "install-conda"
                argv = (
                    resolution.conda_upgrade(conda, conda_files)
                    if op.record.request.action == "upgrade"
                    else resolution.conda_install(conda, conda_files)
                )
            else:
                pip_files = [file for file in decided.files if file.ecosystem == "pypi"]
                label, argv = "install-pip", resolution.pip_install(pip_files)
        except ValueError as exc:
            raise Stop(
                ProvisionOutcome.FAILED,
                "resolve_failed",
                "resolve",
                ErrorInfo(kind=ErrorKind.INTERNAL, message=str(exc)),
                next_step="Nothing was installed.",
            ) from None
        # INSTALLING and its hold were one SQL commit. No installer can be
        # submitted until the exact guest incarnation acknowledges that hold.
        try:
            await self.manager.sync_package_gate(environment_id)
        except WorkerError as exc:
            raise Stop(
                ProvisionOutcome.FAILED,
                "install_failed",
                "download",
                ErrorInfo(kind=exc.kind, message=str(exc)),
                next_step="The admission hold was not acknowledged; nothing was installed.",
            ) from None
        execution = await self._execute(
            op,
            argv,
            label=label,
            stage="download",
            timeout=install_timeout(governing),
            limit=STEP_OUTPUT,
        )
        started, after = started_revision(execution), ended_revision(execution)
        uncertain = after is None and (
            execution.started_at is not None or execution.state is ExecutionState.LOST
        )
        if uncertain:
            # It may have run, but its end reported no revision, as when the executor is
            # lost: a look at the packages tells whether they changed (CP-13).
            after = await self._look(op, label)
            uncertain = after is None
        before = started if started is not None else before
        self._update(op, revision_before=before, revision_after=after)
        if execution.state is not ExecutionState.SUCCEEDED:
            cancelled = execution.state is ExecutionState.CANCELLED or op.cancelled.is_set()
            error = step_error(execution, "the install")
            await self.reconciliation.failed_install(op, error, cancelled, before, after, uncertain)
        return before, after

    def _authorize(self, op: Operation, request: ShellRequest) -> None:
        """Let the manager run ``request`` as a step of ``op``; once is enough."""
        provision_id = op.record.provision_id
        if not same_execution(
            self.store.provision_step(provision_id, request.operation_id), request
        ):
            self.store.authorize_provision_step(
                provision_id, request.operation_id, execution_digest(request)
            )

    async def _look(self, op: Operation, label: str) -> int | None:
        """The environment's revision now, from a step that does nothing: an execution's
        start reports the packages' fingerprint. None if it could not start."""
        request = ShellRequest(
            operation_id=f"{op.record.provision_id}.{label}-look",
            context=op.record.request.context,
            argv=("true",),
            timeout_seconds=LOOK_TIMEOUT,
            output_limit_bytes=1024,
        )
        self._authorize(op, request)
        try:
            execution, _ = await self.manager.submit(request, origin=op.record.provision_id)
            self._update(op, executions=(*op.record.executions, execution.execution_id))
            execution = await self.manager.wait(execution.execution_id)
        except WorkerError as exc:
            _log.warning(
                "provisioning %s could not look at the packages: %s", op.record.provision_id, exc
            )
            return None
        # Its end reports the fingerprint whatever its start could; the revision noted at
        # dispatch is the old one, so a look that never started tells nothing.
        if execution.state is not ExecutionState.SUCCEEDED or execution.started_at is None:
            return None
        ended = ended_revision(execution)
        return ended if ended is not None else started_revision(execution)

    # Ending.

    def _fields(self, record: ProvisionOperation, stop: Completion) -> dict[str, Any]:
        """The record's fields once ``stop`` ends it."""
        completed = stop.outcome in (ProvisionOutcome.COMPLETED, ProvisionOutcome.ALREADY_SATISFIED)
        fields: dict[str, Any] = {
            "state": ProvisionState.FINISHED,
            "outcome": stop.outcome,
            "reason": stop.reason,
            "stage": None if completed else stop.stage,
            "error": stop.error,
            "next_step": clip(stop.next_step) if stop.next_step is not None else None,
            "ended_at": self.clock.now(),
            "queue_position": None,
            **stop.updates,
        }
        marked = self.store.package_status(record.request.context.environment_id)
        if marked is not None and marked[1] != record.provision_id:
            good = f": revision {marked[2]} is the last known good" if marked[2] is not None else ""
            note = f"the environment needs recovery since operation {marked[1]}{good}"
            fields["notes"] = append_notes(fields.get("notes", record.notes), note)
        return fields

    async def _end(self, op: Operation, stop: Completion) -> None:
        """Close the operation's permission requests for good, whatever the outcome, so its
        grant ends with it; then record the end, which clients see."""
        reason = f"its operation ended {stop.outcome.value}"
        await self.permission_flow.close(
            op.record.provision_id, op.record.permission_request, reason
        )
        self._update(op, **self._fields(op.record, stop))
        self.manager.refresh_package_gate(op.record.request.context.environment_id)
        _log.info(
            "provisioning %s ended %s%s",
            op.record.provision_id,
            stop.outcome.value,
            f" at {stop.stage} ({stop.reason})" if op.record.stage is not None else "",
        )

    def _stopped(self, op: Operation) -> None:
        """Record an operation the worker's shutdown cut short, and close its permission
        request. The worker is stopping, so both happen here, without waiting."""
        if op.record.state is ProvisionState.FINISHED:
            return
        if self.permissions is not None:
            # A request still being written is closed when the worker starts again.
            with contextlib.suppress(PermissionStoreError):
                self.permissions.close_operation(
                    op.record.provision_id,
                    op.record.permission_request,
                    "the worker stopped",
                    self.clock.now(),
                )
        with contextlib.suppress(Exception):
            self._update(
                op,
                **self._fields(
                    op.record, self.reconciliation.cut_short(op.record, "stopped").completion
                ),
            )


def _validated(record: ProvisionOperation, **changes: Any) -> ProvisionOperation:
    """``record`` with ``changes``, checked as a whole: the stored record must read back."""
    try:
        return ProvisionOperation.model_validate({**record.model_dump(), **changes})
    except ValidationError:
        _log.exception(
            "an update to provisioning operation %s does not validate", record.provision_id
        )
        raise


# Historical private test imports; production collaborators use the named values.
_Op = Operation
_Stop = Stop
