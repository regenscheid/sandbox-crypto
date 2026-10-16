# SPDX-License-Identifier: MIT
"""Executions: submission, dispatch to executors, events, cancellation and files.

docs/specs/tools.md defines the behavior. Every execution is persisted before
it is dispatched, and every event before it is delivered, so a client can
reconnect and replay. A restarted worker reconciles daemon attempts and reports
unrecoverable attempts as ``lost`` instead of running them again. What an executor
sends is untrusted: it is checked and bounded before it changes any state, and every execution
reaches a terminal state whatever the executor does.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    TERMINAL_STATES,
    ArtifactRef,
    Capabilities,
    Clock,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    FileContent,
    FileListing,
    JobListing,
    JobStatus,
    KernelInfo,
    KernelListing,
    KernelObservation,
    KernelRestartRequest,
    KernelStatusRequest,
    ListFilesRequest,
    PublishRequest,
    ReadFileRequest,
    SessionClosed,
    SessionCloseRequest,
    StageCheck,
    StageCheckRequest,
    StagePutRequest,
    StagePutResult,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.worker import (
    FEATURES,
    LANGUAGES,
    PROTOCOL_VERSION,
)

from . import capture
from .artifacts import Blobs
from .backends import Backend
from .channel import ExecutorChannel
from .connections import Activity, Connection, ConnectionHooks, EnvironmentConnections
from .dispatcher import Dispatcher
from .errors import WorkerError
from .files import Files
from .journal import ExecutionJournal
from .kernels import KernelRegistry
from .lifecycle import (
    UNMANAGED,
    ConfirmedLoss,
    DaemonIdentity,
    EnvironmentLifecycle,
    JobResources,
    Notices,
    Recycle,
)
from .package_admission import PackageAdmission, guard_reuse
from .package_changes import PackageChanges
from .package_gate import PackageGate
from .publication import Publication
from .recovery import DaemonRecovery, RecoveryHooks
from .revisions import Revisions
from .shared_sync import SharedSync
from .store import Store
from .timeouts import MANAGER_TIMEOUTS, ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)


class ExecutionManager:
    def __init__(
        self,
        store: Store,
        backend: Backend,
        blobs: Blobs,
        *,
        pool_id: str,
        max_artifact_size: int,
        state_dir: Path,
        shared: SharedSync | None = None,
        package_cache: str | None = None,
        capture_timeout: float = 30.0,
        capture_ack_timeout: float = 1.0,
        timeouts: ManagerTimeouts = MANAGER_TIMEOUTS,
        lifecycle: EnvironmentLifecycle = UNMANAGED,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        for name, value in (
            ("capture_timeout", capture_timeout),
            ("capture_ack_timeout", capture_ack_timeout),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or not 0.05 <= value <= 60:
                raise ValueError(f"{name} must be between 0.05 and 60 seconds")
        self.store = store
        self.backend = backend
        self._timeouts = timeouts
        self._lifecycle = lifecycle
        self.clock = clock
        """Time as the manager sees it: idle times, deadlines and execution watchdogs."""
        self.blobs = blobs
        self._shared = shared
        self.revisions = Revisions(store, Blobs(state_dir / "lockfiles"), cache=package_cache)
        """Each environment's revisions, with their lockfiles (Conda provisioning, W3).
        ``package_cache`` is the cache's URL as sandboxes reach it, which lockfiles never
        name."""
        self.pool_id = pool_id
        self._closing_task: asyncio.Task[None] | None = None
        self.state_dir = state_dir
        self._tasks: set[asyncio.Task[Any]] = set()
        self._usability = Usability(store)
        """What stands in the way of using each environment, and the worker's shutdown."""
        self._connections = EnvironmentConnections(
            backend,
            store,
            self._usability,
            # Each hook looks the manager's method up when it is called, so that one
            # replaced on the manager (as tests do) takes effect at the next call.
            ConnectionHooks(
                connect=lambda environment_id: self._environment(environment_id),
                on_event=lambda environment_id, message: self._dispatcher.on_event(
                    environment_id, message
                ),
                on_closed=lambda environment_id, channel: self._on_closed(environment_id, channel),
                incarnation_lost=lambda environment_id, previous: self._recovery.incarnation_lost(
                    environment_id, previous
                ),
                recycle=lambda environment_id: self._spawn(self._recovery.recycle(environment_id)),
                sync_package_gate=lambda environment_id, channel: self._admission.sync(
                    environment_id, channel
                ),
                hello=lambda environment_id, channel: self._packages.hello(environment_id, channel),
                connected=lambda environment_id, channel: self._publication.reconcile(
                    environment_id, channel
                ),
            ),
            lifecycle,
            clock=clock,
            pool_id=pool_id,
            state_dir=state_dir,
        )
        self._journal = ExecutionJournal(store, self._connections, clock=clock, timeouts=timeouts)
        self._files = Files(
            self._connections,
            self._journal,
            shared,
            timeouts=timeouts,
        )
        self._packages = PackageChanges(
            store,
            self.revisions,
            self._connections,
            self._journal,
            spawn=self._spawn,
            timeouts=timeouts,
        )
        self._admission = PackageAdmission(
            store,
            backend,
            self._usability,
            connect=lambda environment_id: self._environment(environment_id),
            spawn=self._spawn,
            sync=lambda environment_id: self.sync_package_gate(environment_id),
            lifecycle=lifecycle,
        )
        self._kernels = KernelRegistry(
            store,
            self._connections,
            self._usability,
            self._admission,
            clock=clock,
            timeouts=timeouts,
            lifecycle=lifecycle,
        )
        self._publication = Publication(
            store,
            blobs,
            self._connections,
            self._usability,
            clock=clock,
            max_artifact_size=max_artifact_size,
            timeout=capture_timeout,
            ack_timeout=capture_ack_timeout,
        )
        self._recovery = DaemonRecovery(
            store,
            self._connections,
            self._usability,
            self._publication,
            RecoveryHooks(
                lives=self._journal.live.values,
                lose=lambda live, what: self._dispatcher.lose(live, what),
                spawn=self._spawn,
                watch=lambda live, timeout: self._dispatcher.watch(live, timeout),
                watch_cancel=lambda live: self._dispatcher.watch_cancel(live),
                send_cancel=lambda live: self._dispatcher.send_cancel(live),
                connect=lambda environment_id: self._environment(environment_id),
            ),
            clock=clock,
            timeouts=timeouts,
            lifecycle=lifecycle,
        )
        self._dispatcher = Dispatcher(
            store,
            self._connections,
            self._usability,
            self._journal,
            self._admission,
            self._kernels,
            self._recovery,
            self._files,
            self._packages,
            clock=clock,
            connect=lambda environment_id: self._environment(environment_id),
            spawn=self._spawn,
            timeouts=timeouts,
            shared=shared,
            lifecycle=lifecycle,
        )

    # Settings, fixed when the manager is built.

    @property
    def timeouts(self) -> ManagerTimeouts:
        return self._timeouts

    @property
    def lifecycle(self) -> EnvironmentLifecycle:
        """What the managed environments answer; nothing without any."""
        return self._lifecycle

    # The lifecycle's answers, by the names the manager's attributes had before they were
    # given at construction; reading them is allowed, assigning them is refused.

    @property
    def daemon_identity(self) -> DaemonIdentity | None:
        return self._lifecycle.daemon_identity

    @property
    def confirmed_daemon_loss(self) -> ConfirmedLoss | None:
        return self._lifecycle.confirmed_daemon_loss

    @property
    def recycle_daemon(self) -> Recycle | None:
        return self._lifecycle.recycle_daemon

    @property
    def notices(self) -> Notices | None:
        return self._lifecycle.notices

    @property
    def job_resources(self) -> JobResources | None:
        return self._lifecycle.job_resources

    @property
    def shared(self) -> SharedSync | None:
        """Keeps the project's shared files in step across its environments (D79)."""
        return self._shared

    @property
    def max_artifact_size(self) -> int:
        return self._publication.max_artifact_size

    @property
    def capture_timeout(self) -> float:
        return self._publication.timeout

    @property
    def capture_ack_timeout(self) -> float:
        """Seconds the executor gets to confirm it cleaned up a cancelled capture."""
        return self._publication.ack_timeout

    def package_admission(self, environment_id: str) -> PackageGate:
        """The environment's package gate; refused while it needs recovery."""
        return self._admission.admission(environment_id)

    async def sync_package_gate(self, environment_id: str) -> None:
        """Bring the environment's executor in step with its package gate."""
        await self._admission.sync_environment(environment_id)

    def refresh_package_gate(self, environment_id: str) -> None:
        """Bounded retry of release/classification ACK; uncertainty leaves the guest closed."""
        self._admission.refresh(environment_id)

    async def start(self) -> None:
        """Recover known daemon executions without ever resubmitting a command.

        One worker runs for each state directory (state_lock.py), so the capture leases
        a stopped worker left active are settled first.
        """
        self._publication.settle_orphans()
        await self._dispatcher.restore(daemon_enabled=self.backend.daemon)

    async def release(self, environment_id: str) -> None:
        """Stop an environment's executor before its sandbox goes away.

        Attempts still running there end as the executor's channel closes.
        """
        await self.guard_package_deletion(environment_id)
        with self._usability.releasing_executor(environment_id):
            await self._release(environment_id)

    async def _release(self, environment_id: str) -> None:
        await self._publication.cancel(environment_id)
        jobs = self._journal.running_jobs(environment_id)
        await self._dispatcher.cancel_jobs(jobs)
        await self._connections.release(environment_id)

    async def activity(self, environment_id: str) -> Activity | None:
        """What an environment is doing now; None when its executor cannot say."""
        return await self._connections.activity(
            environment_id, [job.execution_id for job in self._journal.running_jobs(environment_id)]
        )

    async def release_if_idle(self, environment_id: str, idle_since: float) -> bool:
        """Stop an environment's executor if nothing ran in it or used it since ``idle_since``.

        Returns whether it did (or there was none), so that its sandbox may stop.
        """
        if self._journal.running_jobs(environment_id) or self._publication.holds(environment_id):
            return False
        return await self._connections.release_if_idle(environment_id, idle_since)

    async def close(self) -> None:
        """Detach daemon bridges; stop stdio executors and report their attempts lost."""
        self._usability.close()
        if self._closing_task is None:
            self._closing_task = asyncio.create_task(self._close())
        _, cancelled = await capture.owned(self._closing_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        await self._publication.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(
                channel.detach() if channel.daemon_token else channel.close()
                for channel in self._connections.channels
            )
        )
        for live in list(self._journal.live.values()):
            saved = self.store.recovery(live.execution.execution_id)
            recoverable = (
                self.backend.daemon and live.dispatched and saved is not None and bool(saved[1])
            )
            if live.execution.state not in TERMINAL_STATES and not recoverable:
                self._dispatcher.lose(live, "the worker")

    # Bookkeeping.

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        """Forget a background task; log how it failed, if it did.

        A ``WorkerError`` is a failure the worker expects, such as an unreachable
        executor; any other is a fault, logged with its traceback.
        """
        self._tasks.discard(task)
        if task.cancelled() or (error := task.exception()) is None:
            return
        name = getattr(task.get_coro(), "__qualname__", task.get_name())
        if isinstance(error, WorkerError):
            _log.warning("background task %s failed: %s", name, error)
        else:
            _log.error("background task %s failed", name, exc_info=error)

    # Environments.

    async def _environment(self, environment_id: str) -> Connection:
        """The environment's connection, with its executor open."""
        return await self._connections.connect(environment_id)

    async def forget_revisions(self, environment_id: str) -> None:
        """Forget a deleted environment's revisions, and delete the lockfiles only they
        named: environments that are gone keep nothing in the store."""
        self.revisions.forget(environment_id)
        self.store.kernel_starts.forget(environment_id)

    async def confirm_capture_cleanup(
        self, environment_id: str, sandbox: str, sandbox_id: str
    ) -> None:
        """Trusted lifecycle callback after proof of this exact original VM's deletion."""
        self._publication.confirm_cleanup(environment_id, sandbox, sandbox_id)

    async def forget_package_gate(self, environment_id: str) -> None:
        """Only confirmed deletion, after provisioning drain, may remove this fence."""
        self._admission.forget(environment_id)

    async def guard_package_deletion(self, environment_id: str) -> None:
        """Require original-binding proof before any explicit guest teardown."""
        self._admission.guard_deletion(environment_id)

    async def guard_package_reuse(self, environment_id: str) -> None:
        """A new VM cannot replace the unknown original held package binding."""
        guard_reuse(self.store, environment_id)

    async def _on_closed(self, environment_id: str, channel: ExecutorChannel) -> None:
        self._connections.channels.discard(channel)
        self._admission.channel_closed(channel)
        if channel.daemon_token is not None and (self._usability.closing or not channel.detaching):
            self._recovery.bridge_lost(environment_id, channel)
            return
        # The executions sent to this executor end with it, whether or not its
        # environment's record still names it.
        for live in list(self._journal.live.values()):
            if live.channel is channel and live.execution.state not in TERMINAL_STATES:
                self._dispatcher.lose(
                    live, "the worker" if self._usability.closing else "the executor"
                )
        self._connections.closed(environment_id, channel)

    # Submission and cancellation.

    async def submit(
        self, request: ExecutionRequest, *, origin: str | None = None
    ) -> tuple[Execution, bool]:
        """Return the execution for ``request.operation_id``, creating it if it is new.

        ``origin`` names the provisioning operation the execution is an install step of: a
        managed change its package commands make is recorded as that operation's.
        """
        return await self._dispatcher.submit(request, origin=origin)

    async def cancel(self, execution_id: str, project_id: str | None = None) -> Execution:
        return await self._dispatcher.cancel(execution_id, project_id)

    async def kernel_status(self, request: KernelStatusRequest) -> KernelListing:
        """Observe only the authenticated context's kernels; admission gates stay unchanged."""
        return await self._kernels.status(request)

    async def observe_kernels(self, request: KernelStatusRequest) -> KernelObservation:
        """Read an existing channel without keeping it alive or waiting on its lifecycle."""
        return await self._kernels.observe(request)

    # Queries and control.

    # Queries.

    def get(self, execution_id: str, project_id: str | None = None) -> Execution:
        """The execution, which must belong to ``project_id`` when one is given."""
        return self._journal.get(execution_id, project_id)

    def jobs(
        self, context: ExecutionContext, limit: int = 100, after: str | None = None
    ) -> JobListing:
        """List only the requested session's jobs, including recently ended attempts."""
        return self._journal.jobs(context, limit, after)

    async def job_status(self, execution_id: str, project_id: str | None = None) -> JobStatus:
        """Read bounded live output without opening a new executor or waiting for the job."""
        return await self._journal.job_status(execution_id, project_id)

    def events(
        self, execution_id: str, after: int, *, heartbeat: float | None = None
    ) -> AsyncGenerator[Event | None, None]:
        """Yield the events after ``after``, then new ones, ending with the result."""
        return self._journal.events(execution_id, after, heartbeat=heartbeat)

    def end_streams(self) -> None:
        """End every event stream, so that shutdown does not wait for its readers."""
        self._journal.end_streams()

    async def wait(self, execution_id: str) -> Execution:
        """The execution once it is terminal."""
        return await self._journal.wait(execution_id)

    async def restart_kernel(self, request: KernelRestartRequest) -> KernelInfo:
        return await self._kernels.restart(request)

    async def close_session(self, request: SessionCloseRequest) -> SessionClosed:
        """Cancel and reap jobs, then stop kernels. Starts racing the close are refused."""
        context = request.context
        self._kernels.invalidate(context)
        with self._usability.closing_session(context):
            jobs = [
                job
                for job in self._journal.running_jobs(context.environment_id)
                if job.context.project_id == context.project_id
                and job.context.session_id == context.session_id
            ]
            await self._dispatcher.cancel_jobs(jobs)
            return await self._close_session(request)

    async def _close_session(self, request: SessionCloseRequest) -> SessionClosed:
        """Stop the session's kernels; a seam that tests replace to hold a close open."""
        return await self._kernels.close_session(request)

    async def capabilities(self, environment_id: str) -> Capabilities:
        environment = await self._environment(environment_id)
        if environment.location is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"environment {environment_id} has no executor"
            )
        return Capabilities(
            protocol=PROTOCOL_VERSION,
            languages=tuple(name for name in environment.languages if name in LANGUAGES),
            location=self._packages.located(environment_id, environment) or environment.location,
            features=tuple(name for name in environment.features if name in FEATURES),
        )

    # Files. Replies are checked like events: a malformed one is an internal error.

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        return await self._files.read(request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        return await self._files.write(request)

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        return await self._files.listing(request)

    async def stage_check(self, request: StageCheckRequest) -> StageCheck:
        return await self._files.stage_check(request)

    async def stage_put(self, request: StagePutRequest) -> StagePutResult:
        return await self._files.stage_put(request)

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        """Publish a file of the environment as an artifact, within ``capture_timeout``."""
        return await self._publication.publish(request)

    def artifact(self, artifact_id: str, project_id: str | None = None) -> ArtifactRef:
        return self._publication.artifact(artifact_id, project_id)
