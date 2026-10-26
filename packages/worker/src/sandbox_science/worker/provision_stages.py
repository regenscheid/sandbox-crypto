# SPDX-License-Identifier: MIT
"""Provisioning sandbox stages: resolve, decide, install and probe."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, cast

from sandbox_science.contracts import (
    Clock,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionState,
    ProbeResult,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ShellRequest,
)
from sandbox_science.contracts.provisioning import (
    MAX_FILES,
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
from .identity import execution_digest, same_execution
from .manager import ExecutionManager
from .provision_permissions import ProvisionPermissions
from .provision_reconciliation import ProvisionReconciliation
from .provision_types import (
    DRY_RUN_OUTPUT,
    DRY_RUN_TIMEOUT,
    LOOK_TIMEOUT,
    STEP_OUTPUT,
    Completion,
    Governing,
    Installed,
    Operation,
    PolicySource,
    RecordUpdate,
    Stop,
    append_notes,
    cancelled,
    ended_revision,
    install_timeout,
    started_revision,
    step_error,
    tail,
)
from .recovery_fence import check_dispatch
from .resolution import Decided, Resolved, ResolveError

_log = logging.getLogger(__name__)


class ProvisionStages:
    """Execute provisioning steps without owning operation tasks or terminal writes."""

    def __init__(
        self,
        manager: ExecutionManager,
        catalog: Catalog,
        policies: PolicySource | None,
        cache: str | None,
        clock: Clock,
        installer_logs: installer_records.InstallerArtifacts,
        permissions: ProvisionPermissions,
        reconciliation: ProvisionReconciliation,
        update: RecordUpdate,
    ) -> None:
        self.manager = manager
        self.store = manager.store
        self.revisions = manager.revisions
        self.catalog = catalog
        self.policies = policies
        self.cache = cache
        self.clock = clock
        self.installer_logs = installer_logs
        self.permission_flow = permissions
        self.reconciliation = reconciliation
        self._update = update

    async def run(self, op: Operation) -> Completion:
        """Execute the stages, returning their classified completion to the coordinator."""
        try:
            return await self._operate(op)
        except Stop as stop:
            return stop.completion

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
        installed = await self._install(op, governing, conda, decided)
        return await self.reconciliation.settle(
            op, governing, capability, decided, installed.before, installed.after, self._probe
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

    async def execute(
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
        execution = await self.execute(
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
        execution = await self.execute(
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
            execution = await self.execute(
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
                stderr = result.stderr if result is not None else None
                frozen = (
                    resolution.frozen_refusal(
                        (stdout.text if stdout is not None else "")
                        + "\n"
                        + (stderr.text if stderr is not None else "")
                    )
                    if label == "resolve-conda" and execution.state is ExecutionState.FAILED
                    else None
                )
                if frozen is not None:
                    raise Stop(
                        ProvisionOutcome.FAILED,
                        "resolve_failed",
                        "resolve",
                        ErrorInfo(kind=ErrorKind.INVALID_REQUEST, message=frozen),
                        next_step=resolution.FROZEN_NEXT_STEP,
                    )
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
            if label == "resolve-conda":
                try:
                    removed = await asyncio.to_thread(resolution.conda_removals, stdout.text)
                except ResolveError:
                    removed = []  # the parse below refuses the same document, with its reason
                if removed:
                    raise Stop(
                        ProvisionOutcome.FAILED,
                        "resolve_failed",
                        "resolve",
                        ErrorInfo(
                            kind=ErrorKind.INVALID_REQUEST,
                            message=resolution.removal_refusal(removed, specs),
                        ),
                        next_step=resolution.REMOVAL_NEXT_STEP,
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
    ) -> Installed:
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
        execution = await self.execute(
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
        return Installed(before, after)

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
