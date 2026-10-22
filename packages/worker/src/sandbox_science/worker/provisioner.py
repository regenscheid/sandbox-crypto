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
from typing import Any, Final

from pydantic import ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorInfo,
    ErrorKind,
    ExecutionContext,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    new_id,
)
from sandbox_science.contracts.provisioning import (
    MAX_QUEUED,
)

from . import installer_records
from .catalog import Catalog
from .errors import WorkerError
from .identity import provision_digest, same_provision
from .manager import ExecutionManager
from .permissions import Permissions
from .provision_events import ProvisionEvents
from .provision_permissions import ProvisionPermissions
from .provision_reconciliation import ProvisionReconciliation
from .provision_stages import ProvisionStages
from .provision_types import MAX_WAIT as MAX_WAIT
from .provision_types import (
    PERMISSION_POLL,
    STAGES,
    Completion,
    Operation,
    Stop,
    append_notes,
    clip,
)
from .provision_types import Governing as Governing
from .provision_types import PolicySource as PolicySource
from .provision_types import Restorer as Restorer
from .recovery_fence import control_context
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
        self.catalog = catalog
        self.policies = policies
        self.permissions = permissions
        self.clock = clock
        """Stamps the operations' records and times permission deadlines."""
        poll = seconds("poll", poll)
        lockfile_wait = seconds("lockfile_wait", lockfile_wait)
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
            permissions, clock, poll, self._update, self._observe
        )

        self.reconciliation = ProvisionReconciliation(
            manager, restorer, clock, lockfile_wait, self._update
        )

        self.stages = ProvisionStages(
            manager,
            catalog,
            policies,
            cache,
            clock,
            self.installer_logs,
            self.permission_flow,
            self.reconciliation,
            self._update,
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
        await self.permission_flow.restart(self.store.provision_ids())
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
        for op in operations:
            self._retire(op)

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

    def _retire(self, op: Operation) -> None:
        """Release only drained owners whose terminal record was saved successfully."""
        if (
            op.record.state is ProvisionState.FINISHED
            and (op.task is None or op.task.done())
            and self._ops.get(op.record.provision_id) is op
        ):
            self._ops.pop(op.record.provision_id)

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
            asyncio.get_running_loop().call_later(self.retain, self._retire, op)

        # forget() locates environment owners through _ops. A terminal append
        # can outlive retention, so begin eviction only after its task drains.
        # Failed terminal writes retain their owner until forget can retry them.
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

    async def _operate(self, op: Operation) -> Completion:
        return await self.stages.run(op)

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
        self.permission_flow.stopped(op.record)
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
