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
import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    Lockfile,
    ProbeResult,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ResolvedFile,
    ShellRequest,
    new_id,
)
from sandbox_science.contracts.provisioning import (
    MAX_FILES,
    MAX_TEXT,
    InstallerLabel,
    InstallerStep,
    Reason,
    Stage,
)
from sandbox_science.contracts.revisions import MAX_PROBE_OUTPUT
from sandbox_science.contracts.worker import MAX_TIMEOUT_SECONDS
from sandbox_science.policy import Action, HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.approvals import VALID_UNTIL, RequestKey
from sandbox_science.policy.ceiling import effective_resources

from . import installer_records, resolution
from .catalog import CapabilitySpec, Catalog, ProbeSpec
from .errors import WorkerError
from .manager import ExecutionManager
from .permissions import Permissions, PermissionStoreError
from .resolution import Decided, Resolved, ResolveError
from .revisions import read_lockfile

_log = logging.getLogger(__name__)

RESERVED_OPERATION: Final = re.compile(r"prv-[0-9a-f]{24}\..*")
"""Execution operation IDs the worker keeps for provisioning steps; clients may not use them."""
DRY_RUN_TIMEOUT: Final = 900.0
"""Seconds a dry run may take."""
INSTALL_TIMEOUT: Final = 3600.0
"""Seconds an install may take when the policy's resources name no wall time."""
DRY_RUN_OUTPUT: Final = 16 * 1024 * 1024
"""Output a dry run may print: its JSON names every package to install."""
STEP_OUTPUT: Final = 1024 * 1024
PERMISSION_POLL: Final = 2.0
"""Seconds between looks at the event store while an operation waits for permission."""
LOCKFILE_WAIT: Final = 10.0
"""Seconds an operation waits for a revision's lockfile to arrive."""
LOOK_TIMEOUT: Final = 120.0
"""Seconds a look at the packages may take, after an install step that reported no revision."""
GRANT_MARGIN: Final = timedelta(minutes=30)
"""How long a permission request grants beyond its wait and its install's time limit: its
own deadline (``valid_until``), which holds even if the worker fails to withdraw it."""
MAX_WAIT: Final = 60.0
"""The longest a client may ask ``wait`` to wait for a change."""
MAX_QUEUED: Final = 32
"""Operations an environment may have that have not finished; more are refused for now."""
RETAIN: Final = 600.0
"""Seconds a finished operation stays in memory for clients still waiting on it."""
ERROR_TEXT: Final = 1500
"""Characters of a step's output an error quotes."""
MAX_NOTES: Final = 32
"""Notes a record holds (``ProvisionOperation.notes``)."""
MAX_CHANGES: Final = 2 * MAX_FILES
"""Package changes a record holds (``ProvisionOperation.changes``)."""
_PROBE_ID: Final = re.compile(r"[^a-z0-9:._-]")
_STAGES: Final[dict[ProvisionState, Stage]] = {
    ProvisionState.QUEUED: "queue",
    ProvisionState.CHECKING: "check",
    ProvisionState.RESOLVING: "resolve",
    ProvisionState.AWAITING_PERMISSION: "permission",
    ProvisionState.INSTALLING: "link",
    ProvisionState.PROBING: "probe",
    ProvisionState.FINISHED: "probe",
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Governing:
    """What decides an environment's packages: its policy revision, the pool's ceiling and
    floor, and the sandbox the policy middleware knows it by."""

    policy: LoadedPolicy
    ceiling: PoolCeiling
    floor: HardFloor
    sandbox_id: str | None


class PolicySource(Protocol):
    """Finds what governs an environment: the worker's environments (environments.py)."""

    def governing(self, environment_id: str) -> Governing | None: ...


class Restorer(Protocol):
    """Puts an environment back to a revision after a failed install (CP-13). The next
    slice provides one, from a volume snapshot or by rebuilding from the revision's
    lockfile; without one, such an operation ends ``needs_recovery``."""

    async def restore(self, environment_id: str, revision: int) -> bool: ...


class _Stop(Exception):  # noqa: N818 - it ends an operation, as a return would
    """How an operation ends: its outcome and the fields it records."""

    def __init__(
        self,
        outcome: ProvisionOutcome,
        reason: Reason | None = None,
        stage: Stage | None = None,
        error: ErrorInfo | None = None,
        next_step: str | None = None,
        **updates: Any,
    ) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.reason = reason
        self.stage = stage
        self.error = error
        self.next_step = next_step
        self.updates = updates


@dataclass
class _Op:
    record: ProvisionOperation
    changed: asyncio.Future[None]
    turn: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    step: str | None = None
    """The execution running for it now."""
    task: asyncio.Task[None] | None = None
    files: Decided | None = None


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
        clock: Callable[[], datetime] = _now,
        poll: float = PERMISSION_POLL,
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
        self.poll = poll
        self.installer_logs = installer_records.InstallerArtifacts(
            self.store, manager.blobs, manager.max_artifact_size
        )
        self._ops: dict[str, _Op] = {}
        self._queues: dict[str, list[str]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._owed: set[str] = set()
        """Operations that ended while their permission requests could not be withdrawn:
        tried again as each operation ends, and when the worker starts."""

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
                    self.clock(),
                )
            except PermissionStoreError as exc:
                _log.warning("could not withdraw the permission requests left open: %s", exc)
        for record in unfinished:
            stop = self._cut_short(record, "restarted")
            finished = _validated(record, **self._fields(record, stop), seq=record.seq + 1)
            self.store.update_provision(finished)
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

    async def submit(self, request: ProvisionRequest) -> tuple[ProvisionOperation, bool]:
        """The operation for ``request.operation_id``, created and queued if it is new."""
        environment_id = request.context.environment_id
        if request.capability is not None and self.catalog.get(request.capability) is None:
            names = ", ".join(sorted(self.catalog.capabilities))
            raise WorkerError(
                ErrorKind.INVALID_REQUEST,
                f"the catalog has no capability {request.capability!r}; it has {names}. "
                "Ask for packages by their conda-forge names instead",
            )
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        found = self.store.find_provision(request.context.project_id, request.operation_id)
        if found is not None:
            provision_id, existing = found
            if existing != digest:
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
            submitted_at=self.clock(),
            installer_steps=(),
        )
        self.store.insert_provision(record, digest)
        op = _Op(record, asyncio.get_running_loop().create_future())
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

    async def cancel(self, provision_id: str, project_id: str | None = None) -> ProvisionOperation:
        """Cancel the operation: one waiting ends at once, a running step is cancelled like
        any execution, and an install that changed the packages follows CP-13."""
        record = self.get(provision_id, project_id)
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

    # Bookkeeping.

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _view(self, op: _Op) -> ProvisionOperation:
        """The record as clients see it, with its place in the queue while it waits."""
        record = op.record
        if record.state is not ProvisionState.QUEUED:
            return record
        queue = self._queues.get(record.request.context.environment_id, [])
        position = queue.index(record.provision_id) if record.provision_id in queue else None
        return record.model_copy(update={"queue_position": position})

    def _update(self, op: _Op, **changes: Any) -> None:
        """Change the operation's record, store it, and wake whoever waits on it."""
        record = _validated(op.record, **changes, seq=op.record.seq + 1)
        self.store.update_provision(record)
        op.record = record
        changed, op.changed = op.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def _release(self, op: _Op) -> None:
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
        asyncio.get_running_loop().call_later(RETAIN, self._ops.pop, op.record.provision_id, None)

    # The operation.

    async def _run(self, op: _Op) -> None:
        try:
            try:
                if not await self._take_turn(op):
                    raise _Stop(
                        ProvisionOutcome.FAILED,
                        "cancelled",
                        "queue",
                        ErrorInfo(
                            kind=ErrorKind.CANCELLED, message="cancelled before its turn came"
                        ),
                        next_step="Nothing was installed.",
                    )
                await self._operate(op)
            except _Stop as stop:
                await self._end(op, stop)
        except asyncio.CancelledError:
            self._stopped(op)
            raise
        except Exception:
            # Whatever failed, recording the end included, the operation must end.
            _log.exception("provisioning operation %s failed", op.record.provision_id)
            error = ErrorInfo(
                kind=ErrorKind.INTERNAL,
                message="the worker failed during this operation; its executions and "
                "the environment's revisions tell what happened",
            )
            failed = _Stop(
                ProvisionOutcome.FAILED, "install_failed", _STAGES[op.record.state], error
            )
            try:
                if op.record.state is ProvisionState.INSTALLING:
                    failed = self._unseen_install(
                        op.record, "the worker failed during its install", "install_failed", error
                    )
                await self._end(op, failed)
            except Exception:
                _log.exception("could not record the end of %s", op.record.provision_id)
                await self._close(
                    op.record.provision_id, op.record.permission_request, "its operation failed"
                )
        finally:
            self._release(op)

    async def _take_turn(self, op: _Op) -> bool:
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

    async def _operate(self, op: _Op) -> None:
        request = op.record.request
        environment_id = request.context.environment_id
        try:
            self.manager.package_admission(environment_id)
        except WorkerError as exc:
            raise _Stop(
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
            started_at=self.clock(),
            policy=governing.policy.digest,
            queue_position=None,
        )
        if request.system:
            names = ", ".join(request.system)
            raise _Stop(
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
            raise _Stop(
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
            await self._permission(op, governing, decided)
        governing = self._unchanged(op)
        before, after = await self._install(op, governing, conda, decided)
        await self._settle(op, governing, capability, decided, before, after)

    def _governing(self, environment_id: str) -> Governing:
        governing = self.policies.governing(environment_id) if self.policies is not None else None
        if governing is None:
            raise _Stop(
                ProvisionOutcome.FAILED,
                "policy_changed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.UNAVAILABLE,
                    message=f"environment {environment_id!r} has no policy the worker can read",
                ),
            )
        return governing

    def _unchanged(self, op: _Op) -> Governing:
        """What governs the environment now, if it is still the policy the decision used."""
        governing = self._governing(op.record.request.context.environment_id)
        if governing.policy.digest != op.record.policy:
            raise _Stop(
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
        op: _Op,
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
            raise _cancelled(stage)
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
        self.store.authorize_provision_step(
            op.record.provision_id,
            request.operation_id,
            hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        )
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

    def _installer_step(self, op: _Op, step: InstallerStep) -> None:
        previous = op.record.installer_steps or ()
        self._update(
            op,
            installer_steps=(
                tuple(step if old.label == step.label else old for old in previous)
                if any(old.label == step.label for old in previous)
                else (*previous, step)
            ),
        )

    async def _installer_log(self, op: _Op, execution: Execution, label: str) -> None:
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
                notes=_notes(
                    op.record.notes,
                    "A sanitized installer log is unavailable. Original execution facts remain; "
                    "do not repeat the installer to recreate it.",
                ),
            )
            return
        self._installer_step(op, step)
        if interrupted is not None:
            raise interrupted

    async def _installer_version(self, op: _Op, conda: bool) -> None:
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
            raise _cancelled("resolve")
        if execution.state in (ExecutionState.LOST, ExecutionState.TIMED_OUT):
            raise _Stop(
                ProvisionOutcome.FAILED,
                "resolve_failed",
                "resolve",
                ErrorInfo(
                    kind=ErrorKind.UNAVAILABLE,
                    message="Installer version observation is unknown; "
                    "no resolve/install was dispatched.",
                ),
            )

    async def _check(self, op: _Op, name: str, capability: CapabilitySpec) -> None:
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
                raise _Stop(
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
            raise _cancelled("check")
        self.revisions.record_probes(environment_id, results)
        self._update(op, probes=(*op.record.probes, *results))
        if results and all(result.status == "passed" for result in results):
            revision = results[-1].revision
            raise _Stop(
                ProvisionOutcome.ALREADY_SATISFIED,
                revision_before=revision,
                revision_after=revision,
            )
        self._update(op, state=ProvisionState.RESOLVING)

    async def _probe(
        self, op: _Op, capability: str, probe: ProbeSpec, label: str, stage: Stage
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
            revision=_started_revision(execution) if execution.started_at is not None else None,
            execution_id=execution.execution_id,
            output=_tail(output, MAX_PROBE_OUTPUT),
            duration_seconds=result.elapsed_seconds if result is not None else 0.0,
            operation=op.record.provision_id,
            recorded_at=self.clock(),
        )

    def _unrun(self, op: _Op, capability: str, probe: ProbeSpec, why: str) -> ProbeResult:
        """A probe that never ran, so at no revision."""
        return ProbeResult(
            capability=capability,
            probe=probe.id,
            version=probe.version,
            kind=probe.kind,
            status="error",
            output=why,
            operation=op.record.provision_id,
            recorded_at=self.clock(),
        )

    async def _resolve(self, op: _Op, conda: Sequence[str], pip: Sequence[str]) -> list[Resolved]:
        """Dry runs in the sandbox: the exact files the install would bring (CP-7)."""
        resolved: list[Resolved] = []
        if conda or pip:
            await self._installer_version(op, bool(conda))
        steps = [
            (
                "resolve-conda",
                resolution.conda_dry_run(conda),
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
                raise _cancelled("resolve")
            result = execution.result
            stdout = result.stdout if result is not None else None
            if execution.state is not ExecutionState.SUCCEEDED or stdout is None:
                raise _Stop(
                    ProvisionOutcome.FAILED,
                    "resolve_failed",
                    "resolve",
                    _step_error(execution, "the dry run"),
                    next_step="The request could not be resolved. Check the names and versions "
                    "(conda-forge's, for Conda specs), then request it again.",
                )
            if stdout.truncated:
                raise _Stop(
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
                raise _Stop(
                    ProvisionOutcome.FAILED,
                    "resolve_failed",
                    "resolve",
                    ErrorInfo(kind=ErrorKind.INVALID_REQUEST, message=str(exc)),
                ) from None
        if len(resolved) > MAX_FILES:
            raise _Stop(
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

    async def _nothing_to_install(
        self, op: _Op, request: ProvisionRequest, capability: CapabilitySpec | None
    ) -> None:
        """End an operation whose dry runs found nothing to install."""
        environment_id = request.context.environment_id
        current = self.store.revision(environment_id)
        revision = current.revision if current is not None else None
        at = f"revision {revision}" if revision is not None else "the current revision"
        if capability is not None:
            raise _Stop(
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
        checks = await self._version_checks(op, request, revision)
        self.revisions.record_probes(environment_id, checks)
        unseen = [check for check in checks if check.status != "passed"]
        if unseen:
            names = ", ".join(f"{check.probe} ({check.status})" for check in unseen)
            raise _Stop(
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
        raise _Stop(
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
            raise _Stop(
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
            raise _Stop(
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

    async def _permission(self, op: _Op, governing: Governing, decided: Decided) -> None:
        """Ask once for the exact resolved file set, and wait for the answer (CP-8)."""
        request = op.record.request
        escalated = [decided.decisions[index] for index in decided.with_action(Action.ESCALATE)]
        if self.permissions is None or governing.sandbox_id is None:
            raise _Stop(
                ProvisionOutcome.DENIED,
                "permission_unavailable",
                "permission",
                ErrorInfo(
                    kind=ErrorKind.POLICY_DENIED,
                    message=f"the policy asks for permission to download {len(escalated)} "
                    "package files, and this worker cannot record permission requests",
                ),
            )
        timeout = min(
            (decision.escalation.timeout for decision in escalated if decision.escalation),
            default=governing.policy.policy.escalation.timeout,
        )
        key = RequestKey.packages(
            governing.sandbox_id, governing.policy.digest, op.record.provision_id, decided.paths
        )
        payload: dict[str, object] = {
            "sandbox_id": governing.sandbox_id,
            "project_id": request.context.project_id,
            "environment_id": request.context.environment_id,
            "provision_id": op.record.provision_id,
            "policy": {
                "name": governing.policy.policy.metadata.name,
                "digest": governing.policy.digest,
            },
            "rule": escalated[0].rule,
            "reason": f"install {len(decided.files)} package files",
            # The agent's words, as text the store can hold whatever the client sent.
            "purpose": request.purpose[:500].encode("utf-8", "replace").decode("utf-8"),
            "escalation": {"timeout_seconds": int(timeout.total_seconds()), "on_timeout": "deny"},
            "asked": _asked(request),
            VALID_UNTIL: (
                self.clock()
                + timeout
                + timedelta(seconds=_install_timeout(governing))
                + GRANT_MARGIN
            ).isoformat(timespec="seconds"),
            "files": [
                {"path": path, **_described(file)}
                for path, file in zip(decided.paths, decided.files, strict=True)
            ],
        }
        try:
            seq = await asyncio.to_thread(self.permissions.request, key, payload)
        except PermissionStoreError as exc:
            raise _Stop(
                ProvisionOutcome.DENIED,
                "permission_unavailable",
                "permission",
                ErrorInfo(kind=ErrorKind.UNAVAILABLE, message=str(exc)),
            ) from None
        minutes = max(1, round(timeout.total_seconds() / 60))
        self._update(
            op,
            state=ProvisionState.AWAITING_PERMISSION,
            outcome=ProvisionOutcome.AWAITING_PERMISSION,
            permission_request=seq,
            next_step=f"Permission request {seq} asks to download the {len(decided.files)} "
            "package files listed. On the pool host, the user reviews them with "
            f"`sandbox-science-policy-middleware pending --request {seq}`, then approves the "
            f"request (`approve --request {seq}`) or denies it. The operation installs once "
            f"it is approved, and ends denied if it is refused or nobody answers within "
            f"{minutes} minutes.",
        )
        _log.info("provisioning %s waits for permission request %d", op.record.provision_id, seq)
        await self._await_answer(op, seq, self.permissions, self.clock() + timeout, timeout)

    async def _await_answer(
        self,
        op: _Op,
        seq: int,
        permissions: Permissions,
        deadline: datetime,
        timeout: timedelta,
    ) -> None:
        """Wait for the answer, until ``deadline``: its own, so that a store the worker can
        no longer read cannot hold the environment's turn for ever."""
        while True:
            if op.cancelled.is_set():
                await self._withdraw(seq, "the operation was cancelled")
                raise _Stop(
                    ProvisionOutcome.FAILED,
                    "cancelled",
                    "permission",
                    ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled while it waited"),
                    next_step="Nothing was installed; the permission request was withdrawn.",
                )
            try:
                await asyncio.to_thread(permissions.refresh)
            except PermissionStoreError as exc:
                _log.warning("provisioning %s: %s", op.record.provision_id, exc)
            answer = permissions.answer(seq)
            now = self.clock()
            if answer is not None and answer.kind == "approval":
                if answer.expires is not None and answer.expires <= now:
                    raise _Stop(
                        ProvisionOutcome.DENIED,
                        "permission_timed_out",
                        "permission",
                        ErrorInfo(
                            kind=ErrorKind.POLICY_DENIED,
                            message=f"the approval of permission request {seq} expired before "
                            "the install",
                        ),
                    )
                self._update(op, outcome=None, next_step=None)
                return
            if answer is not None:
                raise _Stop(
                    ProvisionOutcome.DENIED,
                    "permission_denied",
                    "permission",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"permission request {seq} was denied",
                    ),
                    next_step="The user refused these packages. Do not ask again for them "
                    "unless the user says so.",
                )
            if now >= deadline:
                await self._withdraw(seq, "nobody answered it in time")
                minutes = max(1, round(timeout.total_seconds() / 60))
                raise _Stop(
                    ProvisionOutcome.DENIED,
                    "permission_timed_out",
                    "permission",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"nobody answered permission request {seq} within {minutes} "
                        "minutes",
                    ),
                    next_step="Ask the user about it, then request the install again.",
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(op.cancelled.wait(), self.poll)

    async def _withdraw(self, seq: int, reason: str) -> None:
        """Close a permission request for good, as its wait ends; its operation's end closes
        it again if this fails."""
        if self.permissions is None:
            return
        try:
            await asyncio.to_thread(self.permissions.withdraw, seq, reason, self.clock())
        except PermissionStoreError as exc:
            _log.warning("could not withdraw permission request %d: %s", seq, exc)

    async def _close(self, provision_id: str, seq: int | None, reason: str) -> None:
        """Close every permission request of an operation that ends, and those still owed
        by operations before it; one that cannot be closed now stays owed."""
        if self.permissions is None:
            return
        for owed in sorted(self._owed - {provision_id}):
            try:
                await asyncio.to_thread(
                    self.permissions.withdraw_operations,
                    [owed],
                    "its operation ended",
                    self.clock(),
                )
                self._owed.discard(owed)
            except PermissionStoreError:
                break  # the store is still out of reach
        if seq is None and provision_id not in self._owed:
            return  # it asked nothing: a request a stop cut short is closed at the next start
        try:
            await asyncio.to_thread(
                self.permissions.close_operation, provision_id, seq, reason, self.clock()
            )
            self._owed.discard(provision_id)
        except PermissionStoreError as exc:
            self._owed.add(provision_id)
            _log.warning("could not close the permission requests of %s: %s", provision_id, exc)

    async def _install(
        self, op: _Op, governing: Governing, conda: Sequence[str], decided: Decided
    ) -> tuple[int | None, int | None]:
        """Install in the environment's turn, as an execution, under the policy's wall time
        (CP-10, CP-14); return the revisions before and after. A request names Conda or pip
        packages, never both, so one step installs them: micromamba pinned to the decided
        files, or pip with exactly them."""
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
                label, argv = "install-conda", resolution.conda_install(conda, conda_files)
            else:
                pip_files = [file for file in decided.files if file.ecosystem == "pypi"]
                label, argv = "install-pip", resolution.pip_install(pip_files)
        except ValueError as exc:
            raise _Stop(
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
            raise _Stop(
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
            timeout=_install_timeout(governing),
            limit=STEP_OUTPUT,
        )
        started, after = _started_revision(execution), _ended_revision(execution)
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
            error = _step_error(execution, "the install")
            await self._failed_install(op, error, cancelled, before, after, uncertain)
        return before, after

    async def _look(self, op: _Op, label: str) -> int | None:
        """The environment's revision now, from a step that does nothing: an execution's
        start reports the packages' fingerprint. None if it could not start."""
        request = ShellRequest(
            operation_id=f"{op.record.provision_id}.{label}-look",
            context=op.record.request.context,
            argv=("true",),
            timeout_seconds=LOOK_TIMEOUT,
            output_limit_bytes=1024,
        )
        self.store.authorize_provision_step(
            op.record.provision_id,
            request.operation_id,
            hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        )
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
        ended = _ended_revision(execution)
        return ended if ended is not None else _started_revision(execution)

    async def _failed_install(
        self,
        op: _Op,
        error: ErrorInfo,
        cancelled: bool,
        before: int | None,
        after: int | None,
        uncertain: bool = False,
    ) -> None:
        """End an install that failed or was cancelled: ``failed`` if the packages are as
        before, else ``rolled_back`` once restored, or ``needs_recovery`` (CP-13).
        ``uncertain`` says its effect on the packages could not be seen, which counts as a
        change."""
        environment_id = op.record.request.context.environment_id
        if cancelled:
            error = error.model_copy(update={"kind": ErrorKind.CANCELLED})
        text = error.message
        changed = before is not None and after is not None and before != after
        # Unseen, linking may have begun: the stage is the later one.
        stage = resolution.failure_stage(text, changed or uncertain)
        reason: Reason = "cancelled" if cancelled else "install_failed"
        if uncertain and not changed:
            raise self._unseen_install(
                op.record,
                "the install's step reported no revision at its end, and nothing could look "
                "at the packages after it",
                reason,
                error,
                stage,
            )
        if changed and before is not None and after is not None:
            if self.restorer is not None and await self.restorer.restore(environment_id, before):
                raise _Stop(
                    ProvisionOutcome.ROLLED_BACK,
                    reason,
                    stage,
                    error,
                    next_step=f"The environment is back at the packages of revision {before}.",
                )
            good = self._mark(environment_id, op.record.provision_id, before)
            raise _Stop(
                ProvisionOutcome.NEEDS_RECOVERY,
                "environment_changed",
                stage,
                error,
                next_step=self._recovery_step(
                    environment_id,
                    f"The failed install changed the environment's packages (revision {before} "
                    f"to {after}).",
                    good,
                ),
            )
        next_step = "Nothing was installed."
        if stage == "download" and ("403" in text or "approval_required" in text):
            next_step = (
                "A download was refused: the install wanted a file outside the decided set, "
                "as when the index changed meanwhile. Request the install again."
            )
        elif stage == "resolve" and not cancelled:
            next_step = (
                "Nothing was installed: the install could not link the decided builds, as when "
                "the index changed since the dry run. Request the install again."
            )
        raise _Stop(ProvisionOutcome.FAILED, reason, stage, error, next_step=next_step)

    def _unseen_install(
        self,
        record: ProvisionOperation,
        what: str,
        reason: Reason,
        error: ErrorInfo,
        stage: Stage = "link",
    ) -> _Stop:
        """How an install ends whose effect nobody saw (``what`` says why): it may have
        changed the packages, so the environment needs recovery from the last revision
        known good (CP-13)."""
        environment_id = record.request.context.environment_id
        before = record.revision_before
        good = self._mark(
            environment_id,
            record.provision_id,
            before if before is not None else record.planned_revision,
        )
        return _Stop(
            ProvisionOutcome.NEEDS_RECOVERY,
            reason,
            stage,
            error,
            next_step=self._recovery_step(
                environment_id, f"{what[:1].upper()}{what[1:]}: it may have changed packages.", good
            ),
            notes=_notes(record.notes, f"{what}, so the packages may have changed"),
        )

    def _cut_short(self, record: ProvisionOperation, how: str) -> _Stop:
        """How an operation ends that the worker's stop or restart cut short (``how``):
        ``failed``, or ``needs_recovery`` if it was installing."""
        error = ErrorInfo(
            kind=ErrorKind.INTERNAL,
            message=f"the worker {how} during this operation; request it again",
        )
        if record.state is ProvisionState.INSTALLING and not self._unchanged_install(record):
            return self._unseen_install(
                record, f"the worker {how} during its install", "worker_restarted", error
            )
        return _Stop(ProvisionOutcome.FAILED, "worker_restarted", _STAGES[record.state], error)

    def _unchanged_install(self, record: ProvisionOperation) -> bool:
        """Whether an install the worker cut short is known to have changed nothing: its end
        was seen without a change, or its step never started."""
        if record.revision_after is not None:
            return record.revision_after == record.revision_before
        steps: list[Execution] = []
        for execution_id in record.executions:
            try:
                step = self.store.execution(execution_id)
            except ValidationError:
                return False  # unreadable: nothing is known
            if step is not None and step.operation_id.rsplit(".", 1)[-1].startswith("install-"):
                steps.append(step)
        if not steps:
            return True  # the install step was never submitted
        return (
            steps[-1].started_at is None
            and steps[-1].state in TERMINAL_STATES
            and steps[-1].state is not ExecutionState.LOST
        )

    def _mark(self, environment_id: str, provision_id: str, good: int | None) -> int | None:
        """Mark the environment as needing recovery (CP-13), unless an earlier operation
        did, whose mark holds; return the mark's last revision known good."""
        self.store.set_package_status(environment_id, "needs_recovery", provision_id, good)
        self.manager.refresh_package_gate(environment_id)
        marked = self.store.package_status(environment_id)
        return marked[2] if marked is not None else good

    def _recovery_step(self, environment_id: str, what: str, good: int | None) -> str:
        """What to tell the user about an environment whose packages need recovery."""
        if good is None:
            known = "no earlier revision of its packages is known"
        elif self.revisions.lockfile_path(environment_id, good) is not None:
            known = f"revision {good}'s lockfile lists the packages as they were"
        else:
            known = f"the worker holds no lockfile of revision {good}, the last known good"
        return f"{what} The worker cannot restore an earlier revision yet. Tell the user: {known}."

    async def _settle(
        self,
        op: _Op,
        governing: Governing,
        capability: CapabilitySpec | None,
        decided: Decided,
        before: int | None,
        after: int | None,
    ) -> None:
        """Record the package changes and probe the result (CP-16, CP-21 to CP-24)."""
        request = op.record.request
        environment_id = request.context.environment_id
        self._update(op, state=ProvisionState.PROBING)
        notes: list[str] = []
        old = await self._lockfile(environment_id, before) if before is not None else None
        new = await self._lockfile(environment_id, after) if after is not None else None
        changes = resolution.package_changes(old, new) if new is not None else []
        if after is None:
            notes.append(
                "the install reported no revision at its end, so its changes are not listed"
            )
        elif new is None:
            notes.append(
                f"revision {after}'s lockfile is not available, so its changes are not listed"
            )
        elif old is None:
            which = "the revision before the install" if before is None else f"revision {before}"
            notes.append(f"{which} has no lockfile available: every package is listed")
        if len(changes) > MAX_CHANGES:
            notes.append(f"{len(changes) - MAX_CHANGES} more package changes are not listed")
            changes = changes[:MAX_CHANGES]
        if before is not None and after is not None and after - before > 1:
            notes.append(f"other changes came between revision {before} and {after}")
        extra = resolution.unplanned(changes, decided.files, new)
        if extra:
            notes.append(f"installed beyond the decided set: {', '.join(extra[:20])}")
            names = {item.split(" ", 1)[0].removeprefix("conda:") for item in extra}
            excepted = governing.policy.policy.packages.agpl_exceptions.conda
            agpl = [
                package.name
                for package in (new.conda if new is not None else ())
                if package.name in names
                and package.name not in excepted
                and resolution.is_agpl(package.license)
            ]
            if agpl:
                # Refused before download is the rule (CP-9): one came all the same.
                good = self._mark(environment_id, op.record.provision_id, before)
                listed = ", ".join(_clip(name, 80) for name in agpl[:5])
                if len(agpl) > 5:
                    listed += f" and {len(agpl) - 5} more"
                raise _Stop(
                    ProvisionOutcome.NEEDS_RECOVERY,
                    "agpl_license",
                    "link",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"the install linked AGPL-licensed packages the decision never "
                        f"saw, which the project does not allow: {listed}",
                    ),
                    next_step=self._recovery_step(
                        environment_id,
                        f"The install linked AGPL-licensed packages ({listed}) against the "
                        "project's licensing policy.",
                        good,
                    ),
                    changes=tuple(changes),
                    notes=_notes((), *notes),
                )
        if capability is not None and request.capability is not None:
            probes = [
                await self._probe(op, request.capability, probe, f"probe-{index}", "probe")
                for index, probe in enumerate(capability.probes)
            ]
        else:
            probes = self._lockfile_checks(op, request, new, after)
        if op.cancelled.is_set():
            notes.append("cancelled while it probed: the install had completed")
        self.revisions.record_probes(environment_id, probes)
        failing = [probe for probe in probes if probe.status != "passed"]
        next_step = None
        if failing:
            at = f"revision {after}" if after is not None else "the revision after it"
            names = ", ".join(f"{probe.probe} ({probe.status})" for probe in failing)
            notes.append(f"probes that did not pass at {at}: {names}")
            next_step = (
                f"The install completed, and what it installed stays, but {names} did not "
                "pass (CP-23). Read the probes' output; nothing was uninstalled."
            )
        raise _Stop(
            ProvisionOutcome.COMPLETED,
            next_step=_clip(next_step) if next_step is not None else None,
            changes=tuple(changes),
            probes=(*op.record.probes, *probes),
            notes=_notes((), *notes),
        )

    async def _version_checks(
        self, op: _Op, request: ProvisionRequest, revision: int | None
    ) -> list[ProbeResult]:
        lockfile = (
            await self._lockfile(request.context.environment_id, revision)
            if revision is not None
            else None
        )
        return self._lockfile_checks(op, request, lockfile, revision)

    def _lockfile_checks(
        self,
        op: _Op,
        request: ProvisionRequest,
        lockfile: Lockfile | None,
        revision: int | None,
    ) -> list[ProbeResult]:
        """A version check for each requested package, from the revision's lockfile: it
        shows the package installed, not working (``import_check``, CP-22)."""
        checks: list[ProbeResult] = []
        at = f"revision {revision}" if revision is not None else "the unknown revision"
        for kind, name in resolution.requested_names(request.conda, request.pip):
            version = resolution.installed_version(lockfile, kind, name) if lockfile else None
            if lockfile is None:
                status, output = "error", f"{at} has no lockfile to check"
            elif version is None:
                status, output = "failed", f"{at}'s lockfile does not list {name}"
            else:
                status, output = "passed", f"{name} {version}"
            checks.append(
                ProbeResult(
                    probe=_PROBE_ID.sub("-", f"installed:{kind}:{name}")[:128],
                    kind="import_check",
                    status=status,
                    revision=revision,
                    output=output,
                    operation=op.record.provision_id,
                    recorded_at=self.clock(),
                )
            )
        return checks

    async def _lockfile(self, environment_id: str, revision: int | None) -> Lockfile | None:
        """A revision's lockfile, waiting a little for it to arrive."""
        if revision is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCKFILE_WAIT
        while True:
            # The store is read here, on the event loop; only the file in a thread.
            path = self.revisions.lockfile_path(environment_id, revision)
            found = await asyncio.to_thread(read_lockfile, path) if path is not None else None
            if found is not None or loop.time() >= deadline:
                return found
            await asyncio.sleep(0.2)

    # Ending.

    def _fields(self, record: ProvisionOperation, stop: _Stop) -> dict[str, Any]:
        """The record's fields once ``stop`` ends it."""
        completed = stop.outcome in (ProvisionOutcome.COMPLETED, ProvisionOutcome.ALREADY_SATISFIED)
        fields: dict[str, Any] = {
            "state": ProvisionState.FINISHED,
            "outcome": stop.outcome,
            "reason": stop.reason,
            "stage": None if completed else stop.stage,
            "error": stop.error,
            "next_step": _clip(stop.next_step) if stop.next_step is not None else None,
            "ended_at": self.clock(),
            "queue_position": None,
            **stop.updates,
        }
        marked = self.store.package_status(record.request.context.environment_id)
        if marked is not None and marked[1] != record.provision_id:
            good = f": revision {marked[2]} is the last known good" if marked[2] is not None else ""
            note = f"the environment needs recovery since operation {marked[1]}{good}"
            fields["notes"] = _notes(fields.get("notes", record.notes), note)
        return fields

    async def _end(self, op: _Op, stop: _Stop) -> None:
        """Close the operation's permission requests for good, whatever the outcome, so its
        grant ends with it; then record the end, which clients see."""
        reason = f"its operation ended {stop.outcome.value}"
        await self._close(op.record.provision_id, op.record.permission_request, reason)
        self._update(op, **self._fields(op.record, stop))
        self.manager.refresh_package_gate(op.record.request.context.environment_id)
        _log.info(
            "provisioning %s ended %s%s",
            op.record.provision_id,
            stop.outcome.value,
            f" at {stop.stage} ({stop.reason})" if op.record.stage is not None else "",
        )

    def _stopped(self, op: _Op) -> None:
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
                    self.clock(),
                )
        with contextlib.suppress(Exception):
            self._update(op, **self._fields(op.record, self._cut_short(op.record, "stopped")))


def _validated(record: ProvisionOperation, **changes: Any) -> ProvisionOperation:
    """``record`` with ``changes``, checked as a whole: the stored record must read back."""
    try:
        return ProvisionOperation.model_validate({**record.model_dump(), **changes})
    except ValidationError:
        _log.exception(
            "an update to provisioning operation %s does not validate", record.provision_id
        )
        raise


def _asked(request: ProvisionRequest) -> str:
    """What an operation asks for, as its permission request shows the operator."""
    if request.capability is not None:
        return f"the capability {request.capability}"
    if request.conda:
        return "Conda: " + ", ".join(request.conda)
    return "pip: " + ", ".join(request.pip)


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _notes(notes: Sequence[str], *more: str) -> tuple[str, ...]:
    """Notes as a record holds them: each at most MAX_TEXT characters, the newest 32."""
    return tuple(_clip(note) for note in (*notes, *more) if note)[-MAX_NOTES:]


def _cancelled(stage: Stage) -> _Stop:
    return _Stop(
        ProvisionOutcome.FAILED,
        "cancelled",
        stage,
        ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled on request"),
        next_step="Nothing was installed.",
    )


def _started_revision(execution: Execution) -> int | None:
    location = execution.location
    text = location.environment_revision if location is not None else None
    return int(text) if text is not None and text.isdigit() else None


def _ended_revision(execution: Execution) -> int | None:
    result = execution.result
    text = result.environment_revision if result is not None else None
    return int(text) if text is not None and text.isdigit() else None


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _step_error(execution: Execution, what: str) -> ErrorInfo:
    """Why a step failed, with the end of its output, which is where package managers say."""
    result = execution.result
    stderr = result.stderr.text if result is not None and result.stderr is not None else ""
    stdout = result.stdout.text if result is not None and result.stdout is not None else ""
    error = result.error if result is not None else None
    if execution.state is ExecutionState.TIMED_OUT:
        kind, summary = ErrorKind.TIMED_OUT, f"{what} ran out of time"
    elif execution.state is ExecutionState.CANCELLED:
        kind, summary = ErrorKind.CANCELLED, f"{what} was cancelled"
    elif error is not None and result is not None and result.exit_code is None:
        kind, summary = error.kind, f"{what} failed: {error.message[:300]}"
    else:
        code = result.exit_code if result is not None else None
        kind, summary = ErrorKind.EXCEPTION, f"{what} exited with {code}"
    if "approval_required" in stderr or "approval_required" in stdout:
        kind = ErrorKind.POLICY_DENIED
    detail = _tail((stderr.strip() + "\n" + stdout.strip()).strip(), ERROR_TEXT)
    return ErrorInfo(kind=kind, message=f"{summary}: {detail}" if detail else summary)


def _described(file: ResolvedFile) -> dict[str, object]:
    """A resolved file as a permission request lists it, for the operator."""
    return {
        key: value
        for key, value in file.model_dump(
            include={"ecosystem", "name", "version", "build", "channel", "subdir", "file"}
            | {"sha256", "license", "action", "rule"}
        ).items()
        if value is not None
    }


def _install_timeout(governing: Governing) -> float:
    """The install's time limit: the policy's wall time under the pool's cap (CP-14)."""
    resources, _ = effective_resources(governing.policy.policy, governing.ceiling)
    if resources.wall_time is None:
        return INSTALL_TIMEOUT
    return min(resources.wall_time.total_seconds(), float(MAX_TIMEOUT_SECONDS))
