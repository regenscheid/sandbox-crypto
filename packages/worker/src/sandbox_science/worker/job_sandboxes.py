# SPDX-License-Identifier: MIT
"""Job sandboxes (D93, docs/specs/job-sandboxes.md): a big job in a new sandbox of its own.

A job that names a job profile does not run in its session's environment. The worker
creates a new environment of kind ``job`` for it, through OpenShell, from the session
environment's recorded image, policy revision, providers and admitted hosts, sized and
placed by the profile (trusted settings, never the agent's). In that sandbox it

1. probes isolation: a tier below the request's minimum or the session environment's
   fails the job before its command (JS-11);
2. replays the session's recorded package changes at their recorded versions, as
   provisioning operations through the package cache and the session's policy, and
   checks the result against the session's lockfile; a failure fails the job before
   its command (owner's decision, 2026-10-22);
3. copies the request's inputs in (JS-13);
4. runs the command as a job of the job environment, and waits for it, watching for the
   loss of its sandbox: a lost sandbox makes the job ``lost``, and nothing reruns it
   (JS-20);
5. copies the logs and the files it wrote back into the session workspace, under
   ``.jobs/<ID>/files/``, within the profile's byte cap (JS-15), and stores an
   immutable result manifest (JS-16);
6. deletes the sandbox after a complete success, and otherwise stops it and keeps it for
   the pool's ``job_retention``, at most ``job_retention_per_project`` a project (JS-25).

The job's execution keeps the session's context (JS-2), so ownership checks, Resume and
the GUI see an ordinary job; it stays ``queued`` until its command runs and ends only
after collection. The steps that need an executor in a sandbox go through ``JobPort``;
``ManagerPort`` is the worker's, over the execution manager and the provisioner.

A worker restart does not end the job (JS-19, JS-26). The command's own execution is an
ordinary daemon job of the job environment, which the worker's recovery reattaches or
reports lost like any other; ``resume`` then takes the job up from its recorded phase.
Before its command was dispatched, the job fails (``worker_restarted``) and its sandbox
is released: nothing ran. Once it was, the job waits for that execution again and
collects. A command lost with its sandbox, or with a daemon that came back without it,
makes the job ``lost``, and nothing runs it again. Collection records each file it
copies back or leaves out, so a restart in the middle copies none of them twice, and an
ended job's manifest, once stored, is the job's outcome however often it is resumed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import posixpath
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    TERMINAL_STATES,
    Clock,
    CondaPackage,
    EnvironmentState,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    FileContent,
    FileListing,
    JobRequest,
    JobResultManifest,
    JobSandbox,
    JobSandboxReleased,
    ListFilesRequest,
    Location,
    Lockfile,
    ManifestFile,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ReadFileRequest,
    Result,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.provisioning import MAX_SPECS, CondaSpec, PipSpec
from sandbox_science.contracts.worker import (
    MAX_FILE_WRITE_BYTES,
    MAX_MANIFEST_FILES,
    MAX_READ_BYTES,
    CollectionStatus,
    JobFailureReason,
    JobProfileOffer,
    JobSandboxPhase,
    ReplayedPackages,
    UncollectedFile,
    validate_relative_path,
)
from sandbox_science.policy.model import Resources

from .environments import Environments, ScheduleTimeoutError, job_environment_id
from .errors import WorkerError
from .job_records import ACTIVE, JobSandboxRecord, manifest_digest
from .revisions import read_lockfile
from .settings import JOB_SANDBOX_LABEL, EnvironmentSettings, JobProfile

if TYPE_CHECKING:
    from .journal import ExecutionJournal
    from .live import Live
    from .manager import ExecutionManager
    from .provisioner import Provisioner
    from .store import Store

_log = logging.getLogger(__name__)

OPERATION_PREFIX: Final = "jsb-"
"""The worker's own operation IDs for a job sandbox's steps: ``jsb-<24 hex>.<step>``."""
RESERVED_OPERATION: Final = re.compile(r"jsb-[0-9a-f]{24}\..*")
"""Operation IDs no client may use, so none can take a job sandbox step's ID first."""
_CONDA: Final = TypeAdapter[str](CondaSpec)
_PIP: Final = TypeAdapter[str](PipSpec)
_KINDS: Final[dict[JobFailureReason, ErrorKind]] = {
    "capacity_unavailable": ErrorKind.UNAVAILABLE,
    "isolation_insufficient": ErrorKind.POLICY_DENIED,
    "package_replay_failed": ErrorKind.UNAVAILABLE,
    "environment_mismatch": ErrorKind.CONFLICT,
    "staging_failed": ErrorKind.INVALID_REQUEST,
    "sandbox_lost": ErrorKind.INTERNAL,
    "sandbox_unavailable": ErrorKind.UNAVAILABLE,
    "worker_restarted": ErrorKind.INTERNAL,
}
_REPLAY_PURPOSE: Final = "Replay the session's recorded package changes in its job sandbox (D93)"
_PIP_REASON: Final = (
    "The session installed these with pip; the job sandbox replays them at their versions"
)


class JobPort(Protocol):
    """What a job sandbox needs of executors: the worker's own, for its own steps."""

    async def location(self, environment_id: str) -> Location:
        """Where the environment's executor runs, with its probed isolation tier."""
        ...

    async def submit(self, request: JobRequest) -> Execution: ...

    async def find(self, context: ExecutionContext, operation_id: str) -> Execution | None:
        """The execution of an operation in ``context``'s project and environment, if one
        was submitted; never submits one."""
        ...

    async def wait(self, execution_id: str) -> Execution:
        """The execution once it has ended."""
        ...

    async def cancel(self, execution_id: str) -> None: ...

    async def read_file(self, request: ReadFileRequest) -> FileContent: ...

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult: ...

    async def list_files(self, request: ListFilesRequest) -> FileListing: ...

    async def provision(self, request: ProvisionRequest) -> ProvisionOperation:
        """Run a provisioning operation to its end."""
        ...

    async def revision(self, environment_id: str) -> int | None:
        """The environment's current package revision, if the worker has seen one."""
        ...

    async def lockfile(
        self, environment_id: str, revision: int | None
    ) -> tuple[Lockfile, str] | None:
        """A revision's lockfile and its SHA-256 (the current revision's for ``None``);
        None when the worker has none."""
        ...


class JobFailureError(Exception):
    """A job sandbox's step that ends the job before or around its command."""

    def __init__(self, reason: JobFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason: JobFailureReason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class Plan:
    """An admitted job sandbox: what ``Dispatcher.submit`` records with the job."""

    profile: str
    settings: JobProfile
    environment_id: str
    session_environment: str

    @property
    def sandbox(self) -> JobSandbox:
        return JobSandbox(
            environment_id=self.environment_id,
            profile=self.profile,
            phase="creating",
            cpu_millicores=self.settings.limits.cpu_millicores,
            memory_mib=self.settings.limits.memory_mib,
        )


@dataclass
class _Run:
    """What one job sandbox's run has found, for its result and manifest."""

    location: Location | None = None
    packages: ReplayedPackages = field(default_factory=ReplayedPackages)
    inputs: list[ManifestFile] = field(default_factory=list[ManifestFile])
    collected: list[ManifestFile] = field(default_factory=list[ManifestFile])
    not_collected: list[UncollectedFile] = field(default_factory=list[UncollectedFile])
    complete: bool = True
    notices: list[str] = field(default_factory=list[str])


class _Prepared(BaseModel):
    """What preparation found, recorded before the command (step ``prepared``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    location: Location | None = None
    packages: ReplayedPackages = ReplayedPackages()
    inputs: tuple[ManifestFile, ...] = ()


class _Copied(BaseModel):
    """One file of collection, copied back or left out (step ``copy-<n>``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    collected: ManifestFile | None = None
    left: UncollectedFile | None = None


def granted(settings: EnvironmentSettings, resources: Resources) -> list[str]:
    """The job profiles the project's effective policy grants that the pool offers."""
    return [name for name in resources.job_profiles or () if name in settings.job_profiles]


def offers(settings: EnvironmentSettings, resources: Resources) -> tuple[JobProfileOffer, ...]:
    """The granted profiles as the harness describes them to the agent (JS-5)."""
    if not resources.job_sandboxes:
        return ()
    return tuple(
        JobProfileOffer(
            name=name,
            description=settings.job_profiles[name].description,
            cpu_millicores=settings.job_profiles[name].limits.cpu_millicores,
            memory_mib=settings.job_profiles[name].limits.memory_mib,
            wall_time_max_seconds=settings.job_profiles[name].wall_time_max,
        )
        for name in granted(settings, resources)
    )


def launch_settings(profile: JobProfile) -> str:
    """The job profile's driver configuration, with the label a PodDisruptionBudget
    selects. The session's CPU and memory caps do not narrow it (JS-9)."""
    return profile.driver_json(Resources(), labels=dict([JOB_SANDBOX_LABEL]))


class JobSandboxes:
    """Admits, runs, collects, retains and releases job sandboxes (D93)."""

    def __init__(
        self,
        environments: Environments,
        store: Store,
        *,
        clock: Clock = SYSTEM_CLOCK,
        poll_seconds: float = 10.0,
        sweep_seconds: float = 3600.0,
        collect_timeout: float = 1800.0,
    ) -> None:
        settings = environments.settings
        self.environments = environments
        self.settings: EnvironmentSettings = settings
        self.store = store
        self.records = store.job_sandboxes
        self.clock = clock
        self.poll_seconds = poll_seconds
        """Between looks at a running job's sandbox, for its loss."""
        self.sweep_seconds = sweep_seconds
        """Between releases of retained sandboxes past their deadline."""
        self.collect_timeout = collect_timeout
        """Collection's own deadline, outside the job's time limit (JS-15)."""
        self.port: JobPort | None = None
        """The worker's executors; set once the manager and provisioner exist."""
        self._inner: dict[str, str] = {}
        """Each running job's own execution in its sandbox, by the job's ID."""
        self._sweeper: asyncio.Task[None] | None = None
        self._releasing = asyncio.Lock()
        """One early release at a time, so that two clicks do not both delete."""

    # Admission, before anything is created (JS-10).

    def owns(self, environment_id: str) -> bool:
        """Whether the environment is a job sandbox."""
        info = self.environments.records.get(environment_id)
        return info is not None and info.kind == "job"

    def admit(self, request: JobRequest, resources: Resources) -> Plan:
        """Check a job's profile, grant and counts; ``resources`` are the session
        environment's effective policy limits. Refuses with the reason; creates nothing."""
        profile = request.profile
        if profile is None:
            raise WorkerError(ErrorKind.INTERNAL, "a job without a profile needs no sandbox")
        context = request.context
        session = self.environments.records.get(context.environment_id)
        if session is None or session.kind != "session":
            raise WorkerError(
                ErrorKind.UNSUPPORTED,
                "job sandboxes start from an environment the worker created (D73)",
            )
        allowed = granted(self.settings, resources)
        settings = self.settings.job_profiles.get(profile)
        if settings is None or profile not in allowed or not resources.job_sandboxes:
            offered = ", ".join(allowed) if resources.job_sandboxes and allowed else "none"
            raise WorkerError(
                ErrorKind.POLICY_DENIED,
                f"job profile {profile!r} is not granted to this project (granted: {offered})",
            )
        active = self.records.listed(phases=ACTIVE)
        project = [record for record in active if record.project_id == context.project_id]
        same = [record for record in active if record.profile == profile]
        for count, limit, what in (
            (len(project), resources.job_sandboxes, "this project's"),
            (len(same), settings.max_concurrent, f"profile {profile!r}'s"),
            (len(active), self.settings.max_job_sandboxes, "the pool's"),
        ):
            if limit is not None and count >= limit:
                listed = ", ".join(record.execution_id for record in active) or "none"
                raise WorkerError(
                    ErrorKind.POLICY_DENIED,
                    f"{what} job sandbox limit of {limit} is reached; active job sandboxes:"
                    f" {listed}",
                )
        arch = settings.node_selector.get("kubernetes.io/arch")
        recorded = self.environments.architecture(context.environment_id)
        if recorded is not None and recorded != arch:
            raise WorkerError(
                ErrorKind.UNSUPPORTED,
                f"job profile {profile!r} runs on {arch}; the session environment's image"
                f" was placed on {recorded}",
            )
        declared = self.environments.isolation_tier
        if request.min_isolation_tier is not None and declared < request.min_isolation_tier:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"this pool declares isolation tier {declared}; tier"
                f" {request.min_isolation_tier} is required",
            )
        return Plan(
            profile=profile,
            settings=settings,
            environment_id=job_environment_id(context.project_id),
            session_environment=context.environment_id,
        )

    def record(self, execution: Execution, plan: Plan) -> None:
        """The job's row, before its sandbox exists (JS-7, JS-26)."""
        now = self.clock.now()
        self.records.insert(
            JobSandboxRecord(
                execution_id=execution.execution_id,
                project_id=execution.context.project_id,
                session_environment=plan.session_environment,
                environment_id=plan.environment_id,
                profile=plan.profile,
                phase="creating",
                collection="pending",
                retained_until=None,
                created_at=now,
                updated_at=now,
            )
        )

    # The run.

    async def run(self, live: Live, request: JobRequest, journal: ExecutionJournal) -> None:
        """Take the job from its sandbox's creation to its end; whatever happens, it ends."""
        await self._guarded(live, journal, self._run(live, request, journal))

    async def resume(self, live: Live, request: JobRequest, journal: ExecutionJournal) -> None:
        """Take up a job a previous worker left unfinished, from its recorded phase; its
        command is never submitted again. Whatever happens, it ends."""
        await self._guarded(live, journal, self._resume(live, request, journal))

    async def _guarded(
        self, live: Live, journal: ExecutionJournal, work: Coroutine[object, object, None]
    ) -> None:
        execution_id = live.execution.execution_id
        try:
            await work
        except Exception as exc:
            _log.exception("job sandbox of %s failed", execution_id)
            error = ErrorInfo(
                kind=ErrorKind.INTERNAL,
                message=f"the worker could not run this job sandbox ({type(exc).__name__})",
            )
            journal.finish(live, ExecutionState.FAILED, Result(error=error))
            with contextlib.suppress(Exception):
                await self._retain(execution_id)
        finally:
            self._inner.pop(execution_id, None)

    async def _run(self, live: Live, request: JobRequest, journal: ExecutionJournal) -> None:
        port = self._port()
        execution_id = live.execution.execution_id
        record = self._record(execution_id)
        profile = self.settings.job_profiles.get(record.profile)
        found = _Run()
        if profile is None:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "sandbox_unavailable",
                f"the pool no longer offers job profile {record.profile!r}", release=True,
            )  # fmt: skip
            return
        if live.cancel_requested:
            await self._cancelled(live, journal, found, release=True)
            return
        launch = launch_settings(profile)
        try:
            await self.environments.create_job(
                record.session_environment,
                record.environment_id,
                execution_id,
                launch,
                require_workspace_prefix=profile.require_workspace_prefix,
                schedule_timeout=profile.schedule_timeout,
            )
        except ScheduleTimeoutError as error:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "capacity_unavailable",
                f"{error.message}; try later or a smaller profile", release=True,
            )  # fmt: skip
            return
        except WorkerError as error:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "sandbox_unavailable",
                f"the job sandbox could not be created: {error.message}", release=True,
            )  # fmt: skip
            return
        job_context = request.context.model_copy(update={"environment_id": record.environment_id})
        self._phase(live, journal, "preparing")
        try:
            found.location = await port.location(record.environment_id)
            await self._check_tier(port, request, found.location)
        except JobFailureError as failure:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), failure.reason,
                failure.message, release=True,
            )  # fmt: skip
            return
        except WorkerError as error:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "sandbox_unavailable",
                f"the job sandbox's executor did not start: {error.message}", release=False,
            )  # fmt: skip
            return
        try:
            found.packages = await self._replay(execution_id, request.context, job_context)
            if live.cancel_requested:
                await self._cancelled(live, journal, found, release=True)
                return
            found.inputs = await self._stage(request, profile, job_context)
        except JobFailureError as failure:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), failure.reason,
                failure.message, release=False,
            )  # fmt: skip
            return
        except WorkerError as error:
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "sandbox_unavailable",
                f"preparing the job sandbox failed: {error.message}", release=False,
            )  # fmt: skip
            return
        if live.cancel_requested:
            await self._cancelled(live, journal, found, release=True)
            return
        prepared = _Prepared(
            location=found.location, packages=found.packages, inputs=tuple(found.inputs)
        )
        self.records.record_step(
            execution_id, "prepared", prepared.model_dump_json(), self.clock.now()
        )
        self._phase(live, journal, "running")
        journal.save(
            live,
            state=ExecutionState.RUNNING,
            started_at=self.clock.now(),
            location=found.location,
        )
        journal.state(live, ExecutionState.RUNNING)
        inner_request = JobRequest(
            operation_id=_run_operation(execution_id),
            context=job_context,
            script=request.script,
            argv=request.argv,
            purpose=request.purpose,
            expected_progress=request.expected_progress,
            cwd=request.cwd,
            timeout_seconds=request.timeout_seconds,
            output_limit_bytes=request.output_limit_bytes,
            bindings=request.bindings,
            binding_inventory=request.binding_inventory,
        )
        inner = await port.submit(inner_request)
        self._inner[execution_id] = inner.execution_id
        if live.cancel_requested:
            with contextlib.suppress(WorkerError):
                await port.cancel(inner.execution_id)
        await self._complete(live, request, journal, profile, found, inner.execution_id)

    async def _resume(self, live: Live, request: JobRequest, journal: ExecutionJournal) -> None:
        port = self._port()
        execution_id = live.execution.execution_id
        record = self._record(execution_id)
        found = self._prepared(execution_id)
        job_context = request.context.model_copy(update={"environment_id": record.environment_id})
        inner: Execution | None = None
        if record.phase not in ("creating", "preparing"):
            inner = await port.find(job_context, _run_operation(execution_id))
        stored = self.records.manifest(execution_id)
        if stored is not None:
            # It ended before the restart; only reporting it was cut short.
            manifest = stored[0]
            result = (
                inner.result
                if inner is not None and inner.state in TERMINAL_STATES and inner.result
                else Result(exit_code=manifest.exit_code, signal=manifest.signal)
            )
            await self._end(
                live, journal, found, manifest.outcome, result, manifest.reason,
                f"the job ended before the worker restarted; see .jobs/{execution_id}"
                "/manifest.json",
                release=manifest.outcome is ExecutionState.SUCCEEDED
                and manifest.collection == "complete",
            )  # fmt: skip
            return
        if inner is None:
            # Its command was never dispatched (JS-19): nothing ran, and nothing will.
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "worker_restarted",
                "the worker restarted before the job's command was dispatched; nothing ran",
                release=True,
            )  # fmt: skip
            return
        self._inner[execution_id] = inner.execution_id
        profile = self.settings.job_profiles.get(record.profile)
        if profile is None:
            with contextlib.suppress(WorkerError):
                await port.cancel(inner.execution_id)
            await self._end(
                live, journal, found, ExecutionState.FAILED, Result(), "sandbox_unavailable",
                f"the pool no longer offers job profile {record.profile!r}, so the job was"
                " cancelled and its sandbox kept", release=False,
            )  # fmt: skip
            return
        if live.cancel_requested and inner.state not in TERMINAL_STATES:
            with contextlib.suppress(WorkerError):
                await port.cancel(inner.execution_id)
        await self._complete(live, request, journal, profile, found, inner.execution_id)

    async def _complete(
        self,
        live: Live,
        request: JobRequest,
        journal: ExecutionJournal,
        profile: JobProfile,
        found: _Run,
        inner: str,
    ) -> None:
        """Wait for the job's own execution, collect, and end the job."""
        port = self._port()
        record = self._record(live.execution.execution_id)
        job_context = request.context.model_copy(update={"environment_id": record.environment_id})
        ended = await self._await(port, inner, record.environment_id)
        if ended is None or (
            ended.state is ExecutionState.LOST and await self._gone(record.environment_id)
        ):
            # The sandbox went with its daemon: the outcome is unknown, and the command
            # is never submitted again (JS-20). Nothing is left to collect or keep.
            await self._end(
                live, journal, found, ExecutionState.LOST, Result(), "sandbox_lost",
                "the job's sandbox was deleted or evicted while the job ran; its outcome is"
                " unknown and it is not run again", release=True,
            )  # fmt: skip
            return
        self._phase(live, journal, "collecting")
        await self._collect(live, ended, profile, request.context, job_context, found)
        result = ended.result or Result()
        reason: JobFailureReason | None = None
        message: str | None = None
        if ended.state is ExecutionState.LOST:
            reason, message = "sandbox_lost", "the job's executor in its sandbox was lost"
        await self._end(
            live, journal, found, ended.state, result, reason, message,
            release=ended.state is ExecutionState.SUCCEEDED and found.complete,
        )  # fmt: skip

    async def _check_tier(self, port: JobPort, request: JobRequest, location: Location) -> None:
        session = await port.location(request.context.environment_id)
        minimum = max(request.min_isolation_tier or 0, session.isolation_tier or 0)
        tier = location.isolation_tier
        if tier is None or tier < minimum:
            raise JobFailureError(
                "isolation_insufficient",
                f"the job sandbox runs at isolation tier {tier}; the request or the session"
                f" environment needs {minimum}",
            )

    async def _gone(self, environment_id: str) -> bool:
        """Whether the gateway says the job's own sandbox is gone (JS-20)."""
        try:
            return await self.environments.present(environment_id) is False
        except WorkerError:
            return False

    async def _await(self, port: JobPort, inner: str, environment_id: str) -> Execution | None:
        """The job's own execution once it ends; None when its sandbox is lost first."""
        waiting = asyncio.ensure_future(port.wait(inner))
        try:
            while True:
                pause = asyncio.ensure_future(self.clock.sleep(self.poll_seconds))
                try:
                    await asyncio.wait({waiting, pause}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    pause.cancel()
                if waiting.done():
                    return waiting.result()
                if await self.environments.present(environment_id) is False:
                    with contextlib.suppress(WorkerError):
                        await port.cancel(inner)
                    return None
        finally:
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError, WorkerError):
                    await waiting

    async def cancel(self, live: Live, journal: ExecutionJournal) -> None:
        """Cancel a job sandbox's job: before its command, the run ends it; once it runs,
        its own execution is cancelled, and what it wrote is still collected."""
        if live.execution.state not in (ExecutionState.CANCELLING, *TERMINAL_STATES):
            journal.save(live, state=ExecutionState.CANCELLING)
            journal.state(live, ExecutionState.CANCELLING)
        inner = self._inner.get(live.execution.execution_id)
        if inner is not None:
            await self._port().cancel(inner)

    # Packages (owner's decision: replayed in the first increment).

    async def _replay(
        self, execution_id: str, session: ExecutionContext, job: ExecutionContext
    ) -> ReplayedPackages:
        port = self._port()
        revision = await port.revision(session.environment_id)
        if not revision:
            return ReplayedPackages(session_revision=revision)
        current = await port.lockfile(session.environment_id, revision)
        base = await port.lockfile(job.environment_id, None) or await port.lockfile(
            session.environment_id, 0
        )
        if current is None or base is None:
            raise JobFailureError(
                "package_replay_failed",
                f"the session's lockfile of revision {revision}, or of the image's packages,"
                " is not available, so its packages cannot be replayed",
            )
        conda, pip = replay_specs(current[0], base[0])
        operations: list[str] = []
        prefix = _operation(execution_id)
        batches = [
            ("conda", conda[start : start + MAX_SPECS]) for start in range(0, len(conda), MAX_SPECS)
        ] + [("pip", pip[start : start + MAX_SPECS]) for start in range(0, len(pip), MAX_SPECS)]
        for index, (kind, specs) in enumerate(batches):
            request = ProvisionRequest(
                operation_id=f"{prefix}.replay-{index}",
                context=job,
                purpose=_REPLAY_PURPOSE,
                conda=tuple(specs) if kind == "conda" else (),
                pip=tuple(specs) if kind == "pip" else (),
                pip_reason=_PIP_REASON if kind == "pip" else None,
            )
            operation = await port.provision(request)
            operations.append(operation.provision_id)
            if operation.outcome not in (
                ProvisionOutcome.COMPLETED,
                ProvisionOutcome.ALREADY_SATISFIED,
            ):
                detail = operation.error.message if operation.error else operation.reason
                raise JobFailureError(
                    "package_replay_failed",
                    f"replaying the session's packages failed in {operation.provision_id}"
                    f" ({operation.outcome}: {detail})",
                )
        after = await port.lockfile(job.environment_id, None) if batches else base
        if after is None:
            raise JobFailureError(
                "environment_mismatch", "the job sandbox reported no lockfile after the replay"
            )
        missing = missing_packages(current[0], base[0], after[0])
        if missing:
            raise JobFailureError(
                "environment_mismatch",
                "after the replay the job sandbox lacks the session's "
                + ", ".join(missing[:10])
                + (f" and {len(missing) - 10} more" if len(missing) > 10 else ""),
            )
        return ReplayedPackages(
            conda=tuple(conda),
            pip=tuple(pip),
            operations=tuple(operations),
            session_revision=revision,
            session_lockfile=current[1],
            job_lockfile=after[1],
        )

    # Files.

    async def _stage(
        self, request: JobRequest, profile: JobProfile, job: ExecutionContext
    ) -> list[ManifestFile]:
        """Copy the inputs to the same paths in the job sandbox (JS-13)."""
        session = request.context
        paths: list[str] = []
        for path in request.inputs:
            paths += await self._files_of(session, path)
            if len(paths) > MAX_MANIFEST_FILES:
                raise JobFailureError(
                    "staging_failed", f"the inputs hold over {MAX_MANIFEST_FILES} files"
                )
        staged: list[ManifestFile] = []
        budget = profile.stage_max_bytes
        for path in paths:
            try:
                data, digest = await self._read_whole(session, path, budget)
            except _TooLargeError as error:
                raise JobFailureError("staging_failed", f"input {path}: {error}") from error
            except WorkerError as error:
                raise JobFailureError("staging_failed", f"input {path}: {error.message}") from error
            await self._port().write_file(
                WriteFileRequest(
                    context=job, path=path, content_b64=base64.b64encode(data).decode()
                )
            )
            budget -= len(data)
            staged.append(ManifestFile(path=path, size=len(data), sha256=digest))
        return staged

    async def _files_of(self, context: ExecutionContext, path: str) -> list[str]:
        """The regular files at ``path``: it, or those below it, following no symlink."""
        parent, name = posixpath.split(path)
        kind = await self._kind(context, parent or ".", name)
        if kind == "file":
            return [path]
        if kind != "directory":
            raise JobFailureError(
                "staging_failed",
                f"input {path} is {'missing' if kind is None else 'a ' + kind}; inputs are"
                " regular files or directories, and no symlink is followed",
            )
        found: list[str] = []
        pending = [path]
        while pending:
            directory = pending.pop()
            listing = await self._port().list_files(
                ListFilesRequest(context=context, path=directory)
            )
            if listing.truncated:
                raise JobFailureError("staging_failed", f"input {directory} has too many entries")
            for entry in listing.entries:
                child = f"{directory}/{entry.name}"
                if entry.type == "file":
                    found.append(child)
                elif entry.type == "directory":
                    pending.append(child)
                else:
                    raise JobFailureError(
                        "staging_failed", f"input {child} is a {entry.type}; no symlink is followed"
                    )
                if len(found) > MAX_MANIFEST_FILES:
                    raise JobFailureError(
                        "staging_failed", f"the inputs hold over {MAX_MANIFEST_FILES} files"
                    )
        return sorted(found)

    async def _kind(self, context: ExecutionContext, parent: str, name: str) -> str | None:
        try:
            listing = await self._port().list_files(ListFilesRequest(context=context, path=parent))
        except WorkerError as error:
            if error.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        for entry in listing.entries:
            if entry.name == name:
                return entry.type
        return None

    async def _read_whole(
        self, context: ExecutionContext, path: str, budget: int
    ) -> tuple[bytes, str]:
        """A whole file in ranges, checked against the size and digest its first range
        reported: at most 100 MiB (one write) and ``budget`` bytes."""
        port = self._port()
        first = await port.read_file(ReadFileRequest(context=context, path=path))
        if first.size > MAX_FILE_WRITE_BYTES:
            raise _TooLargeError(f"{first.size} bytes is over the 100 MiB a file copy takes")
        if first.size > budget:
            raise _TooLargeError(f"{first.size} bytes is over the remaining byte limit")
        data = bytearray(base64.b64decode(first.content_b64))
        while len(data) < first.size:
            part = await port.read_file(
                ReadFileRequest(context=context, path=path, offset=len(data), length=MAX_READ_BYTES)
            )
            chunk = base64.b64decode(part.content_b64)
            if part.sha256 != first.sha256 or not chunk:
                raise WorkerError(ErrorKind.CONFLICT, f"{path} changed while it was read")
            data += chunk
        if len(data) != first.size or hashlib.sha256(data).hexdigest() != first.sha256:
            raise WorkerError(ErrorKind.CONFLICT, f"{path} changed while it was read")
        return bytes(data), first.sha256

    async def _collect(
        self,
        live: Live,
        ended: Execution,
        profile: JobProfile,
        session: ExecutionContext,
        job: ExecutionContext,
        found: _Run,
    ) -> None:
        """Copy the logs and the written files back into the session (JS-15, JS-17)."""
        execution_id = live.execution.execution_id
        details, inner = live.execution.job, ended.job
        result = ended.result or Result()
        pairs: list[tuple[str, str]] = []
        if details is not None and inner is not None:
            pairs += [
                (inner.stdout_path, details.stdout_path),
                (inner.stderr_path, details.stderr_path),
            ]
        written = [
            path
            for path in result.written_files
            if not path.startswith((".jobs/", "shared/")) and path not in (".jobs", "shared")
        ]
        if result.written_files_truncated:
            found.complete = False
            found.not_collected.append(
                UncollectedFile(path=".", reason="the job wrote more files than its result names")
            )
        if len(written) > MAX_MANIFEST_FILES:
            found.complete = False
            for path in written[MAX_MANIFEST_FILES:]:
                if len(found.not_collected) < MAX_MANIFEST_FILES:
                    found.not_collected.append(
                        UncollectedFile(path=path, reason="over 1,000 files")
                    )
            written = written[:MAX_MANIFEST_FILES]
        pairs += [(path, f".jobs/{execution_id}/files/{path}") for path in written]
        # Each file's outcome is recorded once, in order, so a collection a restart cut
        # short goes on after the last one recorded and copies none of them again; its
        # deadline runs from when it first began.
        now = self.clock.now()
        began = datetime.fromisoformat(
            self.records.record_step(execution_id, "collecting", now.isoformat(), now)
        )
        steps = self.records.steps(execution_id)
        budget = profile.collect_max_bytes
        pending: list[tuple[int, str, str]] = []
        for index, (source, target) in enumerate(pairs):
            recorded = steps.get(f"copy-{index}")
            if recorded is None:
                pending.append((index, source, target))
            else:
                budget -= _apply(found, _Copied.model_validate_json(recorded))
        remaining = max(0.0, self.collect_timeout - (now - began).total_seconds())
        try:
            async with asyncio.timeout(remaining):
                for index, source, target in pending:
                    copied = await self._copy_back(source, target, budget, session, job)
                    recorded = self.records.record_step(
                        execution_id, f"copy-{index}", copied.model_dump_json(), self.clock.now()
                    )
                    budget -= _apply(found, _Copied.model_validate_json(recorded))
        except TimeoutError:
            found.complete = False
            found.not_collected.append(
                UncollectedFile(path=".", reason="collection ran past its 30-minute deadline")
            )

    async def _copy_back(
        self,
        source: str,
        target: str,
        budget: int,
        session: ExecutionContext,
        job: ExecutionContext,
    ) -> _Copied:
        """Copy one file back, or say why it was left out."""

        def leave(reason: str) -> _Copied:
            return _Copied(left=UncollectedFile(path=source[:4096], reason=reason[:300]))

        try:
            validate_relative_path(target)
        except ValueError:
            return leave("its path in the session would be too long")
        try:
            data, digest = await self._read_whole(job, source, budget)
        except _TooLargeError as error:
            return leave(str(error))
        except WorkerError as error:
            return leave(error.message)
        try:
            await self._port().write_file(
                WriteFileRequest(
                    context=session, path=target, content_b64=base64.b64encode(data).decode()
                )
            )
        except WorkerError as error:
            return leave(f"writing it to the session failed: {error.message}")
        return _Copied(collected=ManifestFile(path=target, size=len(data), sha256=digest))

    # The end.

    async def _cancelled(
        self, live: Live, journal: ExecutionJournal, found: _Run, *, release: bool
    ) -> None:
        error = ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
        await self._end(
            live, journal, found, ExecutionState.CANCELLED, Result(error=error), None, None,
            release=release,
        )  # fmt: skip

    async def _end(
        self,
        live: Live,
        journal: ExecutionJournal,
        found: _Run,
        state: ExecutionState,
        result: Result,
        reason: JobFailureReason | None,
        message: str | None,
        *,
        release: bool,
    ) -> None:
        """Store the manifest, release or retain the sandbox, then end the job. A manifest
        stored already, before a restart, is the job's outcome and is never rewritten."""
        execution = live.execution
        execution_id = execution.execution_id
        stored = self.records.manifest(execution_id)
        if stored is None:
            manifest, digest = self._save_manifest(execution, found, state, result, reason)
        else:
            manifest, digest = stored
            state, reason = manifest.outcome, manifest.reason
            found = _Run(
                location=found.location,
                packages=manifest.packages,
                inputs=list(manifest.inputs),
                collected=list(manifest.collected),
                not_collected=list(manifest.not_collected),
                complete=manifest.collection == "complete",
                notices=found.notices,
            )
        collection: CollectionStatus = "complete" if found.complete else "incomplete"
        self.records.update(execution_id, now=self.clock.now(), collection=collection)
        await self._write_copy(execution, manifest, found)
        if self._record(execution_id).phase not in ("released", "retained"):
            if release or self._closing(execution_id):
                await self._release(execution_id)
            else:
                await self._retain(execution_id)
        current = self._record(execution_id)
        phase = current.phase
        if execution.job is not None and execution.job.sandbox is not None:
            sandbox = execution.job.sandbox.model_copy(
                update={
                    "phase": phase,
                    "collection": collection,
                    "manifest_sha256": digest,
                    "retained_until": current.retained_until,
                }
            )
            journal.save(live, job=execution.job.model_copy(update={"sandbox": sandbox}))
        notices = [*result.notices, *found.notices]
        if found.packages.conda or found.packages.pip:
            notices.append(
                f"replayed {len(found.packages.conda)} Conda and {len(found.packages.pip)} pip"
                " packages of the session before the command"
            )
        if collection == "incomplete":
            notices.append(
                f"collection was incomplete: {len(found.not_collected)} files were not copied"
                f" back; see .jobs/{execution_id}/manifest.json"
            )
        if phase == "retained" and current.retained_until is not None:
            notices.append(
                f"the job's sandbox is stopped and kept until {current.retained_until.isoformat()}"
            )
        update: dict[str, object] = {
            "written_files": tuple(file.path for file in found.collected)[:1000],
            "written_files_truncated": collection == "incomplete",
            "deleted_files": (),
            "deleted_files_truncated": True,
            "notices": tuple(notices)[-20:],
        }
        if reason is not None and message is not None:
            update["error"] = ErrorInfo(kind=_KINDS[reason], message=f"{reason}: {message}")
        journal.finish(live, state, result.model_copy(update=update))

    def _save_manifest(
        self,
        execution: Execution,
        found: _Run,
        state: ExecutionState,
        result: Result,
        reason: JobFailureReason | None,
    ) -> tuple[JobResultManifest, str]:
        """Store the job's manifest (JS-16), once; return it with its digest."""
        execution_id = execution.execution_id
        record = self._record(execution_id)
        info = self.environments.records.get(record.environment_id)
        profile = self.settings.job_profiles.get(record.profile)
        driver = launch_settings(profile) if profile is not None else ""
        location = found.location
        manifest = JobResultManifest(
            job_id=execution_id,
            attempt_id=execution.attempt_id,
            profile=record.profile,
            driver_config_sha256=hashlib.sha256(driver.encode()).hexdigest(),
            image=info.image if info is not None else "",
            environment_id=record.environment_id,
            sandbox_id=info.sandbox_id if info is not None else None,
            isolation_tier=location.isolation_tier if location is not None else None,
            isolation_evidence=location.isolation_evidence if location is not None else None,
            submitted_at=execution.submitted_at,
            started_at=execution.started_at,
            ended_at=self.clock.now(),
            outcome=state,
            reason=reason,
            exit_code=result.exit_code,
            signal=result.signal,
            packages=found.packages,
            inputs=tuple(found.inputs),
            collected=tuple(found.collected),
            not_collected=tuple(found.not_collected),
            collection="complete" if found.complete else "incomplete",
        )
        return manifest, self.records.save_manifest(manifest, self.clock.now())

    async def _write_copy(
        self, execution: Execution, manifest: JobResultManifest, found: _Run
    ) -> None:
        """``.jobs/<ID>/manifest.json`` in the session: an untrusted copy (JS-16)."""
        text = manifest.model_dump_json(indent=2).encode()
        path = f".jobs/{execution.execution_id}/manifest.json"
        try:
            await self._port().write_file(
                WriteFileRequest(
                    context=execution.context,
                    path=path,
                    content_b64=base64.b64encode(text).decode(),
                )
            )
        except WorkerError as error:
            found.notices.append(
                f"the manifest's copy in the session was not written: {error.message}"
            )

    # Release and retention (JS-25, JS-26).

    async def _release(self, execution_id: str) -> None:
        """Delete the job's sandbox by its recorded identity; keep the row retained when the
        deletion is not confirmed, so the sweep tries again."""
        record = self._record(execution_id)
        info = self.environments.records.get(record.environment_id)
        now = self.clock.now()
        if info is None or info.state is EnvironmentState.DELETED:
            self.records.update(execution_id, now=now, phase="released")
            return
        try:
            await self.environments.delete(record.environment_id)
        except WorkerError as error:
            _log.warning("job sandbox %s was not released: %s", record.environment_id, error)
            self.records.update(execution_id, now=now, phase="retained", retained_until=now)
            return
        self.records.update(execution_id, now=self.clock.now(), phase="released")

    async def _retain(self, execution_id: str) -> None:
        """Stop the job's sandbox and keep it until the retention deadline, releasing the
        project's oldest retained ones beyond its limit."""
        record = self._record(execution_id)
        info = self.environments.records.get(record.environment_id)
        if info is None or info.state is EnvironmentState.DELETED:
            self.records.update(execution_id, now=self.clock.now(), phase="released")
            return
        if info.state is EnvironmentState.CREATING:
            await self._release(execution_id)
            return
        try:
            await self.environments.stop_job(record.environment_id)
        except WorkerError as error:
            _log.warning("job sandbox %s did not stop: %s", record.environment_id, error)
        now = self.clock.now()
        until = now + timedelta(seconds=self.settings.job_retention)
        self.records.update(execution_id, now=now, phase="retained", retained_until=until)
        retained = [
            row
            for row in self.records.listed(phases=frozenset({"retained"}))
            if row.project_id == record.project_id
        ]
        retained.sort(key=lambda row: (row.updated_at, row.execution_id))
        for row in retained[: max(0, len(retained) - self.settings.job_retention_per_project)]:
            await self._release(row.execution_id)

    async def release_now(self, execution_id: str, operation_id: str) -> JobSandboxReleased:
        """Delete a retained job sandbox now, at the user's request (JS-24, JS-25).

        Only a retained sandbox qualifies: one still holding a job's CPU and memory is
        the job's to end (cancel it first), and a released one is gone. The first
        request that succeeds is the receipt: the same operation again answers the same
        result, any other request for a sandbox no longer retained is a conflict. A
        deletion the gateway did not confirm leaves the sandbox retained, due at once,
        so the sweep tries again."""
        async with self._releasing:
            record = self.records.get(execution_id)
            if record is None:
                raise WorkerError(ErrorKind.NOT_FOUND, f"no job sandbox for {execution_id}")
            receipt = self.records.steps(execution_id).get(_RELEASE_NOW)
            if (
                receipt is not None
                and receipt == _quoted(operation_id)
                and record.phase == "released"
            ):
                return JobSandboxReleased(execution_id=execution_id)
            if record.phase != "retained":
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"the job sandbox is {record.phase}; only a retained one can be released now",
                )
            await self._release(execution_id)
            if self._record(execution_id).phase != "released":
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    "the job sandbox's deletion was not confirmed; it stays retained and is "
                    "released again shortly",
                )
            self.records.record_step(
                execution_id, _RELEASE_NOW, _quoted(operation_id), self.clock.now()
            )
            return JobSandboxReleased(execution_id=execution_id)

    async def sweep(self) -> list[str]:
        """Release the retained sandboxes past their deadline; return their jobs' IDs."""
        now = self.clock.now()
        released: list[str] = []
        for row in self.records.listed(phases=frozenset({"retained"})):
            if row.retained_until is not None and row.retained_until <= now:
                await self._release(row.execution_id)
                if self._record(row.execution_id).phase == "released":
                    released.append(row.execution_id)
        return released

    async def start(
        self,
        journal: ExecutionJournal,
        spawn: Callable[[Coroutine[object, object, None]], object],
    ) -> None:
        """After a restart, once the worker's recovery has reattached or lost the job
        environments' own executions: resume each job the previous worker left
        unfinished (``resume``, in a task ``spawn`` starts); retain the sandbox of an
        active one whose job ended without it, or release it if it was still being
        created; then release overdue retentions. A row whose job is gone is kept."""
        for row in self.records.listed():
            execution = self.store.execution(row.execution_id)
            if execution is None:
                if row.phase in ACTIVE:
                    _log.warning("job sandbox %s has no job record; it is kept", row.environment_id)
                continue
            if execution.state not in TERMINAL_STATES:
                live = journal.live.get(row.execution_id)
                request = self._request(row.execution_id)
                if live is None or request is None:
                    _log.warning("job %s cannot be resumed; its sandbox is kept", row.execution_id)
                    continue
                spawn(self.resume(live, request, journal))
                continue
            if row.phase not in ACTIVE:
                continue
            if self.records.manifest(row.execution_id) is None:
                info = self.environments.records.get(row.environment_id)
                self.records.save_manifest(
                    JobResultManifest(
                        job_id=execution.execution_id,
                        attempt_id=execution.attempt_id,
                        profile=row.profile,
                        driver_config_sha256=hashlib.sha256(b"").hexdigest(),
                        image=info.image if info is not None else "",
                        environment_id=row.environment_id,
                        sandbox_id=info.sandbox_id if info is not None else None,
                        submitted_at=execution.submitted_at,
                        started_at=execution.started_at,
                        ended_at=execution.ended_at or self.clock.now(),
                        outcome=execution.state,
                        reason="worker_restarted",
                        collection="incomplete",
                    ),
                    self.clock.now(),
                )
                self.records.update(row.execution_id, now=self.clock.now(), collection="incomplete")
            try:
                await self._retain(row.execution_id)
            except WorkerError as error:
                _log.warning("job sandbox %s is not reconciled: %s", row.environment_id, error)
        await self.sweep()
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep_forever())

    async def _sweep_forever(self) -> None:
        while True:
            await self.clock.sleep(self.sweep_seconds)
            try:
                await self.sweep()
            except Exception:
                _log.exception("releasing retained job sandboxes failed")

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    def release_on_close(self, execution: Execution) -> bool:
        """Have a job sandbox's job release its sandbox when it ends, collected but not
        retained, as closing its session asks (JS-25). Recorded, so a restarted worker
        keeps to it. Only the job's own row, of its project and session environment, is
        marked; returns whether it was."""
        record = self.records.get(execution.execution_id)
        if (
            record is None
            or record.project_id != execution.context.project_id
            or record.session_environment != execution.context.environment_id
        ):
            return False
        self.records.record_step(
            execution.execution_id, "release", '"session_closed"', self.clock.now()
        )
        return True

    # Reading.

    def sandbox(self, execution_id: str) -> JobSandboxRecord | None:
        return self.records.get(execution_id)

    def manifest(self, execution_id: str) -> JobResultManifest | None:
        found = self.records.manifest(execution_id)
        return None if found is None else found[0]

    # Helpers.

    def _port(self) -> JobPort:
        if self.port is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "job sandboxes are not ready yet")
        return self.port

    def _record(self, execution_id: str) -> JobSandboxRecord:
        record = self.records.get(execution_id)
        if record is None:
            raise WorkerError(ErrorKind.INTERNAL, f"no job sandbox record for {execution_id}")
        return record

    def _closing(self, execution_id: str) -> bool:
        """Whether closing the job's session asked for its sandbox's early release."""
        return "release" in self.records.steps(execution_id)

    def _prepared(self, execution_id: str) -> _Run:
        """What the job's preparation found, as recorded before its command."""
        recorded = self.records.steps(execution_id).get("prepared")
        if recorded is None:
            return _Run()
        prepared = _Prepared.model_validate_json(recorded)
        return _Run(
            location=prepared.location, packages=prepared.packages, inputs=list(prepared.inputs)
        )

    def _request(self, execution_id: str) -> JobRequest | None:
        """The job's request as the worker stored it at submission."""
        saved = self.store.recovery(execution_id)
        if saved is None:
            return None
        try:
            return JobRequest.model_validate_json(saved[0])
        except ValidationError:
            return None

    def _phase(self, live: Live, journal: ExecutionJournal, phase: JobSandboxPhase) -> None:
        self.records.update(live.execution.execution_id, now=self.clock.now(), phase=phase)
        job = live.execution.job
        if job is not None and job.sandbox is not None:
            sandbox = job.sandbox.model_copy(update={"phase": phase})
            journal.save(live, job=job.model_copy(update={"sandbox": sandbox}))


class _TooLargeError(Exception):
    """A file over the bytes a copy may take."""


_RELEASE_NOW: Final = "release-now"
"""The step that holds the operation of the user's early release (JS-24)."""


def _quoted(value: str) -> str:
    """A step's record is JSON; an operation ID is a plain token."""
    return json.dumps(value)


def _operation(execution_id: str) -> str:
    """The prefix of the worker's own operation IDs for the job's steps."""
    return OPERATION_PREFIX + execution_id.removeprefix("exe-")


def _run_operation(execution_id: str) -> str:
    """The operation ID of the job's own execution in its sandbox: its command."""
    return f"{_operation(execution_id)}.run"


def _apply(found: _Run, copied: _Copied) -> int:
    """Add one file's collection to what the run found; return the bytes it took."""
    if copied.collected is not None:
        found.collected.append(copied.collected)
        return copied.collected.size
    found.complete = False
    if copied.left is not None and len(found.not_collected) < MAX_MANIFEST_FILES:
        found.not_collected.append(copied.left)
    return 0


def _conda_key(package: CondaPackage) -> tuple[str, ...]:
    return (package.name, package.version, package.build, package.channel or "")


def replay_specs(current: Lockfile, base: Lockfile) -> tuple[list[str], list[str]]:
    """The session's package changes since the image's packages, as pinned specs: Conda
    packages by channel, name, version and build, then pip pins. Raises
    ``JobFailureError`` for a change a pinned install cannot reproduce."""
    have_conda = {_conda_key(package) for package in base.conda}
    have_pip = {(package.name, package.version, package.location) for package in base.pip}
    names = {package.name for package in current.conda} | {package.name for package in current.pip}
    removed = sorted(
        {package.name for package in (*base.conda, *base.pip) if package.name not in names}
    )
    if removed:
        raise JobFailureError(
            "package_replay_failed",
            "the session removed packages the image has ("
            + ", ".join(removed[:10])
            + "); a replay installs and never removes",
        )
    conda: list[str] = []
    for package in current.conda:
        if _conda_key(package) in have_conda:
            continue
        channel = package.channel or "conda-forge"
        spec = f"{channel}::{package.name}={package.version}={package.build}"
        try:
            conda.append(_CONDA.validate_python(spec))
        except ValidationError as exc:
            raise JobFailureError(
                "package_replay_failed",
                f"the session's {package.name} {package.version} cannot be named as a Conda spec",
            ) from exc
    pip: list[str] = []
    for package in current.pip:
        if (package.name, package.version, package.location) in have_pip:
            continue
        if package.url is not None or package.location != "prefix":
            raise JobFailureError(
                "package_replay_failed",
                f"the session installed {package.name} from a URL or with --user; only index"
                " installs into the environment are replayed",
            )
        try:
            pip.append(_PIP.validate_python(f"{package.name}=={package.version}"))
        except ValidationError as exc:
            raise JobFailureError(
                "package_replay_failed",
                f"the session's {package.name} {package.version} cannot be named as a pip pin",
            ) from exc
    return conda, pip


def missing_packages(current: Lockfile, base: Lockfile, after: Lockfile) -> list[str]:
    """The session's changes the job sandbox's lockfile lacks after the replay."""
    have_conda = {_conda_key(package) for package in base.conda}
    have_pip = {(package.name, package.version) for package in base.pip}
    got_conda = {_conda_key(package) for package in after.conda}
    got_pip = {(package.name, package.version) for package in after.pip}
    missing = [
        f"{package.name} {package.version}"
        for package in current.conda
        if _conda_key(package) not in have_conda and _conda_key(package) not in got_conda
    ]
    missing += [
        f"{package.name} {package.version} (pip)"
        for package in current.pip
        if (package.name, package.version) not in have_pip
        and (package.name, package.version) not in got_pip
    ]
    return missing


class ManagerPort:
    """The worker's ``JobPort``: its execution manager and provisioner, which open
    executors in the job sandbox like in any environment the worker created."""

    def __init__(self, manager: ExecutionManager, provisioner: Provisioner) -> None:
        self.manager = manager
        self.provisioner = provisioner

    async def location(self, environment_id: str) -> Location:
        return (await self.manager.capabilities(environment_id)).location

    async def submit(self, request: JobRequest) -> Execution:
        return (await self.manager.submit(request))[0]

    async def find(self, context: ExecutionContext, operation_id: str) -> Execution | None:
        found = self.manager.store.find_operation(context.project_id, operation_id)
        if found is None:
            return None
        execution = self.manager.get(found[0], context.project_id)
        if execution.context.environment_id != context.environment_id:
            return None
        return execution

    async def wait(self, execution_id: str) -> Execution:
        return await self.manager.wait(execution_id)

    async def cancel(self, execution_id: str) -> None:
        await self.manager.cancel(execution_id)

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        return await self.manager.read_file(request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        return await self.manager.write_file(request)

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        return await self.manager.list_files(request)

    async def provision(self, request: ProvisionRequest) -> ProvisionOperation:
        operation, _ = await self.provisioner.submit(request)
        while operation.state is not ProvisionState.FINISHED:
            operation = await self.provisioner.wait(operation.provision_id, operation.seq, 300.0)
        return operation

    async def revision(self, environment_id: str) -> int | None:
        record = self.manager.store.revision(environment_id)
        return None if record is None else record.revision

    async def lockfile(
        self, environment_id: str, revision: int | None
    ) -> tuple[Lockfile, str] | None:
        if revision is None:
            revision = await self.revision(environment_id)
            if revision is None:
                return None
        try:
            path, ref = self.manager.revisions.lockfile(environment_id, revision)
        except WorkerError:
            return None
        lockfile = await asyncio.to_thread(read_lockfile, path)
        return None if lockfile is None else (lockfile, ref.sha256)


__all__ = [
    "OPERATION_PREFIX",
    "RESERVED_OPERATION",
    "JobFailureError",
    "JobPort",
    "JobSandboxes",
    "ManagerPort",
    "Plan",
    "granted",
    "launch_settings",
    "manifest_digest",
    "missing_packages",
    "offers",
    "replay_specs",
]
