# SPDX-License-Identifier: MIT
"""Provisioning revision reconciliation, verification and recovery classification."""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    Clock,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    Lockfile,
    ProbeResult,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
)
from sandbox_science.contracts.provisioning import (
    MAX_CHANGES,
    Reason,
    Stage,
)
from sandbox_science.policy.reasons import APPROVAL_REQUIRED

from . import resolution
from .catalog import CapabilitySpec
from .manager import ExecutionManager
from .provision_types import (
    PROBE_ID,
    STAGES,
    Completion,
    Operation,
    Probe,
    RecordUpdate,
    Stop,
    append_notes,
    clip,
)
from .provision_types import Governing as Governing
from .provision_types import PolicySource as PolicySource
from .provision_types import Restorer as Restorer
from .resolution import Decided
from .revisions import read_lockfile

_log = logging.getLogger(__name__)


class ProvisionReconciliation:
    """Classify installer effects without owning operations or queue turns."""

    def __init__(
        self,
        manager: ExecutionManager,
        restorer: Restorer | None,
        clock: Clock,
        lockfile_wait: float,
        update: RecordUpdate,
    ) -> None:
        self.manager = manager
        self.store = manager.store
        self.revisions = manager.revisions
        self.restorer = restorer
        self.clock = clock
        self.lockfile_wait = lockfile_wait
        self._update = update

    async def failed_install(
        self,
        op: Operation,
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
            raise self.unseen_install(
                op.record,
                "the install's step reported no revision at its end, and nothing could look "
                "at the packages after it",
                reason,
                error,
                stage,
            )
        if changed and before is not None and after is not None:
            if self.restorer is not None and await self.restorer.restore(environment_id, before):
                raise Stop(
                    ProvisionOutcome.ROLLED_BACK,
                    reason,
                    stage,
                    error,
                    next_step=f"The environment is back at the packages of revision {before}.",
                )
            good = self.mark(environment_id, op.record.provision_id, before)
            raise Stop(
                ProvisionOutcome.NEEDS_RECOVERY,
                "environment_changed",
                stage,
                error,
                next_step=self.recovery_step(
                    environment_id,
                    f"The failed install changed the environment's packages (revision {before} "
                    f"to {after}).",
                    good,
                ),
            )
        next_step = "Nothing was installed."
        if stage == "download" and ("403" in text or APPROVAL_REQUIRED in text):
            next_step = (
                "A download was refused: the install wanted a file outside the decided set, "
                "as when the index changed meanwhile. Request the install again."
            )
        elif stage == "resolve" and not cancelled:
            next_step = (
                "Nothing was installed: the install could not link the decided builds, as when "
                "the index changed since the dry run. Request the install again."
            )
        raise Stop(ProvisionOutcome.FAILED, reason, stage, error, next_step=next_step)

    def unseen_install(
        self,
        record: ProvisionOperation,
        what: str,
        reason: Reason,
        error: ErrorInfo,
        stage: Stage = "link",
    ) -> Stop:
        """How an install ends whose effect nobody saw (``what`` says why): it may have
        changed the packages, so the environment needs recovery from the last revision
        known good (CP-13)."""
        environment_id = record.request.context.environment_id
        before = record.revision_before
        good = self.mark(
            environment_id,
            record.provision_id,
            before if before is not None else record.planned_revision,
        )
        return Stop(
            ProvisionOutcome.NEEDS_RECOVERY,
            reason,
            stage,
            error,
            next_step=self.recovery_step(
                environment_id, f"{what[:1].upper()}{what[1:]}: it may have changed packages.", good
            ),
            notes=append_notes(record.notes, f"{what}, so the packages may have changed"),
        )

    def cut_short(self, record: ProvisionOperation, how: str) -> Stop:
        """How an operation ends that the worker's stop or restart cut short (``how``):
        ``failed``, or ``needs_recovery`` if it was installing."""
        error = ErrorInfo(
            kind=ErrorKind.INTERNAL,
            message=f"the worker {how} during this operation; request it again",
        )
        if record.state is ProvisionState.INSTALLING and not self.unchanged_install(record):
            return self.unseen_install(
                record, f"the worker {how} during its install", "worker_restarted", error
            )
        return Stop(ProvisionOutcome.FAILED, "worker_restarted", STAGES[record.state], error)

    def unchanged_install(self, record: ProvisionOperation) -> bool:
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

    def mark(self, environment_id: str, provision_id: str, good: int | None) -> int | None:
        """Mark the environment as needing recovery (CP-13), unless an earlier operation
        did, whose mark holds; return the mark's last revision known good."""
        self.store.set_package_status(environment_id, "needs_recovery", provision_id, good)
        self.manager.refresh_package_gate(environment_id)
        marked = self.store.package_status(environment_id)
        return marked[2] if marked is not None else good

    def recovery_step(self, environment_id: str, what: str, good: int | None) -> str:
        """What to tell the user about an environment whose packages need recovery."""
        if good is None:
            known = "no earlier revision of its packages is known"
        elif self.revisions.lockfile_path(environment_id, good) is not None:
            known = f"revision {good}'s lockfile lists the packages as they were"
        else:
            known = f"the worker holds no lockfile of revision {good}, the last known good"
        return f"{what} The worker cannot restore an earlier revision yet. Tell the user: {known}."

    async def settle(
        self,
        op: Operation,
        governing: Governing,
        capability: CapabilitySpec | None,
        decided: Decided,
        before: int | None,
        after: int | None,
        probe: Probe,
    ) -> Completion:
        """Record the package changes and probe the result (CP-16, CP-21 to CP-24)."""
        request = op.record.request
        environment_id = request.context.environment_id
        self._update(op, state=ProvisionState.PROBING)
        notes: list[str] = []
        old = await self.lockfile(environment_id, before) if before is not None else None
        new = await self.lockfile(environment_id, after) if after is not None else None
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
                good = self.mark(environment_id, op.record.provision_id, before)
                listed = ", ".join(clip(name, 80) for name in agpl[:5])
                if len(agpl) > 5:
                    listed += f" and {len(agpl) - 5} more"
                raise Stop(
                    ProvisionOutcome.NEEDS_RECOVERY,
                    "agpl_license",
                    "link",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"the install linked AGPL-licensed packages the decision never "
                        f"saw, which the project does not allow: {listed}",
                    ),
                    next_step=self.recovery_step(
                        environment_id,
                        f"The install linked AGPL-licensed packages ({listed}) against the "
                        "project's licensing policy.",
                        good,
                    ),
                    changes=tuple(changes),
                    notes=append_notes((), *notes),
                )
        if capability is not None and request.capability is not None:
            probes = [
                await probe(op, request.capability, spec, f"probe-{index}", "probe")
                for index, spec in enumerate(capability.probes)
            ]
        else:
            probes = self.lockfile_checks(op, request, new, after)
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
        return Completion(
            ProvisionOutcome.COMPLETED,
            next_step=clip(next_step) if next_step is not None else None,
            updates={
                "changes": tuple(changes),
                "probes": (*op.record.probes, *probes),
                "notes": append_notes((), *notes),
            },
        )

    async def version_checks(
        self, op: Operation, request: ProvisionRequest, revision: int | None
    ) -> list[ProbeResult]:
        lockfile = (
            await self.lockfile(request.context.environment_id, revision)
            if revision is not None
            else None
        )
        return self.lockfile_checks(op, request, lockfile, revision)

    def lockfile_checks(
        self,
        op: Operation,
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
                    probe=PROBE_ID.sub("-", f"installed:{kind}:{name}")[:128],
                    kind="import_check",
                    status=status,
                    revision=revision,
                    output=output,
                    operation=op.record.provision_id,
                    recorded_at=self.clock.now(),
                )
            )
        return checks

    async def lockfile(self, environment_id: str, revision: int | None) -> Lockfile | None:
        """A revision's lockfile, waiting a little for it to arrive."""
        if revision is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.lockfile_wait
        while True:
            # The store is read here, on the event loop; only the file in a thread.
            path = self.revisions.lockfile_path(environment_id, revision)
            found = await asyncio.to_thread(read_lockfile, path) if path is not None else None
            if found is not None or loop.time() >= deadline:
                return found
            await asyncio.sleep(0.2)
