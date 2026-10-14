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
import base64
import contextlib
import logging
import math
import posixpath
import re
import sqlite3
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    TERMINAL_STATES,
    ArtifactRef,
    Capabilities,
    CellRequest,
    Clock,
    ErrorInfo,
    ErrorKind,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    ExecutionState,
    FileContent,
    FileEntry,
    FileListing,
    JobDetails,
    JobListing,
    JobRequest,
    JobStatus,
    KernelInfo,
    KernelListing,
    KernelObservation,
    KernelRestartRequest,
    KernelStatusRequest,
    ListFilesRequest,
    Location,
    OutputEvent,
    PdfRequest,
    PublishRequest,
    ReadFileRequest,
    Result,
    ResultEvent,
    RichOutput,
    SessionClosed,
    SessionCloseRequest,
    ShellRequest,
    StateEvent,
    StreamEvent,
    StreamOutput,
    WriteFileRequest,
    WriteFileResult,
    new_id,
)
from sandbox_science.contracts.worker import (
    DEFAULT_OUTPUT_LIMIT,
    FEATURES,
    LANGUAGES,
    MAX_EVENT_BYTES,
    MAX_PATH_BYTES,
    PROTOCOL_VERSION,
)
from sandbox_science.policy.model import Resources

from . import capture
from .artifacts import Blobs
from .backends import Backend
from .channel import ExecutorChannel, Message
from .connections import Activity, Connection, ConnectionHooks, EnvironmentConnections
from .errors import ExecutorRefusalError, WorkerError
from .identity import execution_digest, same_execution
from .kernels import KernelRegistry
from .live import REQUEST, Live, time_limit
from .output_observations import normalize as normalize_observation
from .package_admission import LEGACY as PACKAGE_GATE_LEGACY
from .package_admission import PackageAdmission, guard_reuse
from .package_gate import PackageGate
from .publication import Publication
from .recovery import DaemonRecovery, RecoveryHooks
from .replies import Range, epoch, integer
from .revisions import Prepared, Revisions, package_commands
from .shared_sync import SharedSync
from .store import Store
from .timeouts import MANAGER_TIMEOUTS, ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)
_EVENT = TypeAdapter[Event](Event)
_SOURCE_EVENT: ContextVar[tuple[str, int] | None] = ContextVar("executor_source", default=None)

MAX_HELD_NOTICES = 20
"""Sync notices held for one call's or one session's next result."""
MAX_HELD_BYTES = 16 * 1024
MAX_HELD_SESSIONS = 256
_UNSYNCED = (
    "shared/ could not be synced with the project's other environments just now; "
    "it is synced again at the next call"
)
EVENT_PAGE = 64
"""Events read from the store at a time when a client replays a stream."""
OUTPUT_BUDGET = 4
"""Stream text and rich output an execution may store as events, in multiples of its
output limit (UTF-8 bytes). An honest executor stays under three: half the limit of each
stream, which can triple when invalid bytes become U+FFFD, plus the limit of outputs."""
MAX_EVENTS = 100_000
"""Stream and output events stored for one execution; later ones are dropped."""
MAX_ERROR_TEXT = 64 * 1024
MAX_TRACEBACK_LINES = 100
MAX_TRACEBACK_LINE = 8 * 1024
MAX_WRITTEN = 1000
MAX_LIST = 5000
MAX_REVISIONS = 64
"""``revision`` events one execution may report; later ones are dropped."""
MAX_EXECUTION_LOCKFILES = 4
"""Lockfiles one execution's ``revision`` events may bring or have asked for: one for each
attempt whose package commands made a change. A revision left without one gets the
current lockfile asked for instead, while it is current."""
_EXECUTION = re.compile(r"exe-[0-9a-f]{24}")


def _seconds(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return max(0.0, float(value))
    return 0.0


def _text(value: object, limit: int) -> str | None:
    """A string from the executor, keeping its head and tail if it is over ``limit``."""
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    half = limit // 2
    return f"{value[:half]}\n[… {len(value) - 2 * half} characters omitted …]\n{value[-half:]}"


Notices = Callable[[str, datetime], list[tuple[datetime, str]]]


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
        self.timeouts = timeouts
        self.clock = clock
        """Time as the manager sees it: idle times, deadlines and execution watchdogs."""
        self.blobs = blobs
        self.shared = shared
        """Keeps the project's shared files in step across its environments (D79)."""
        self.revisions = Revisions(store, Blobs(state_dir / "lockfiles"), cache=package_cache)
        """Each environment's revisions, with their lockfiles (Conda provisioning, W3).
        ``package_cache`` is the cache's URL as sandboxes reach it, which lockfiles never
        name."""
        self._lockfile_locks: dict[str, asyncio.Lock] = {}
        """One lockfile at a time is checked for each environment, in a thread."""
        self._fetching: set[str] = set()
        """Environments whose executor is asked for the current lockfile."""
        self.pool_id = pool_id
        self.max_artifact_size = max_artifact_size
        self.capture_timeout = capture_timeout
        self.capture_ack_timeout = capture_ack_timeout
        """Seconds the executor gets to confirm it cleaned up a cancelled capture."""
        self._closing_task: asyncio.Task[None] | None = None
        self.state_dir = state_dir
        self._live: dict[str, Live] = {}
        self._held_notices: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        """Notices from syncing shared files for file requests, by environment and session,
        for the session's next result there (D79)."""
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
                on_event=lambda environment_id, message: self._on_event(environment_id, message),
                on_closed=lambda environment_id, channel: self._on_closed(environment_id, channel),
                daemon_identity=lambda environment_id: self._daemon_binding(environment_id),
                incarnation_lost=lambda environment_id, previous: self._recovery.incarnation_lost(
                    environment_id, previous
                ),
                recycle=lambda environment_id: self._spawn(self._recovery.recycle(environment_id)),
                sync_package_gate=lambda environment_id, channel: self._admission.sync(
                    environment_id, channel
                ),
                hello=lambda environment_id, channel: self._hello(environment_id, channel),
                connected=lambda environment_id, channel: self._publication.reconcile(
                    environment_id, channel
                ),
            ),
            clock=clock,
            pool_id=pool_id,
            state_dir=state_dir,
        )
        self._admission = PackageAdmission(
            store,
            backend,
            self._usability,
            connect=lambda environment_id: self._environment(environment_id),
            spawn=lambda coroutine: self._spawn(coroutine),
            daemon_identity=lambda: self.daemon_identity,
        )
        self._kernels = KernelRegistry(
            store,
            self._connections,
            self._usability,
            self._admission,
            clock=clock,
            observation_timeout=lambda: self.timeouts.kernel_observation,
            daemon_identity=lambda: self.daemon_identity,
        )
        self._publication = Publication(
            store,
            blobs,
            self._connections,
            self._usability,
            clock=clock,
            # Read when used, as these settings are public and may change.
            max_artifact_size=lambda: self.max_artifact_size,
            ack_timeout=lambda: self.capture_ack_timeout,
        )
        self._recovery = DaemonRecovery(
            store,
            self._connections,
            self._usability,
            self._publication,
            RecoveryHooks(
                lives=lambda: self._live.values(),
                lose=lambda live, what: self._lose(live, what),
                spawn=lambda coroutine: self._spawn(coroutine),
                watch=lambda live, timeout: self._watch(live, timeout),
                watch_cancel=lambda live: self._watch_cancel(live),
                send_cancel=lambda live: self._send_cancel(live),
                connect=lambda environment_id: self._environment(environment_id),
            ),
            clock=clock,
            timeouts=lambda: self.timeouts,
            confirmed_daemon_loss=lambda: self.confirmed_daemon_loss,
            recycle_daemon=lambda: self.recycle_daemon,
        )
        self._streams_ended = False
        self.job_resources: Callable[[str], Resources] | None = None
        self.confirmed_daemon_loss: Callable[[str], Awaitable[bool]] | None = None
        self.daemon_identity: Callable[[str], tuple[str, str] | None] | None = None
        self.recycle_daemon: Callable[[str, str, str], Awaitable[None]] | None = None
        self.notices: Notices | None = None
        """Admission notices for an environment since a time; the environments set it."""
        self._noticed: dict[tuple[str, str], datetime] = {}

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
        """Recover known daemon executions without ever resubmitting a command."""
        daemon_enabled = getattr(getattr(self.backend, "settings", None), "daemon", False)
        endpoints = tuple(set(self.store.endpoints()) | set(self.store.pending_recycles()))
        if endpoints and not daemon_enabled:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                "sandbox daemon endpoints remain; explicitly release or recycle their "
                "sandboxes before disabling daemon transport; startup refused",
            )
        recovery: dict[str, list[Live]] = {key: [] for key in endpoints}
        for execution_id, execution in self.store.unfinished():
            if execution is None:
                self.store.mark_terminal(execution_id)
                continue
            last = max(execution.last_seq, self.store.last_seq(execution_id))
            try:
                saved = self.store.recovery(execution_id)
                request = REQUEST.validate_json(saved[0]) if saved is not None else None
            except (ValueError, sqlite3.Error):
                request = None
                saved = None
            live = self._track(
                execution.model_copy(update={"last_seq": last}),
                request.output_limit_bytes if request else DEFAULT_OUTPUT_LIMIT,
            )
            live.events, live.stored = self.store.event_usage(execution_id)
            if saved is not None and saved[1] and daemon_enabled:
                live.dispatched = True
                live.cancel_requested = saved[3] or execution.origin is not None
                recovery.setdefault(execution.context.environment_id, []).append(live)
            else:
                self._lose(live, "the worker that restarted without a recoverable daemon dispatch")
        for environment_id in recovery:
            await self._recovery.recover(environment_id, startup=True)

    async def release(self, environment_id: str) -> None:
        """Stop an environment's executor before its sandbox goes away.

        Attempts still running there end as the executor's channel closes.
        """
        await self.guard_package_deletion(environment_id)
        with self._usability.releasing_executor(environment_id):
            await self._release(environment_id)

    async def _release(self, environment_id: str) -> None:
        await self._publication.cancel(environment_id)
        jobs = self._running_jobs(environment_id)
        await self._cancel_jobs(jobs)
        await self._connections.release(environment_id)

    async def activity(self, environment_id: str) -> Activity | None:
        """What an environment is doing now; None when its executor cannot say."""
        return await self._connections.activity(
            environment_id, [job.execution_id for job in self._running_jobs(environment_id)]
        )

    async def release_if_idle(self, environment_id: str, idle_since: float) -> bool:
        """Stop an environment's executor if nothing ran in it or used it since ``idle_since``.

        Returns whether it did (or there was none), so that its sandbox may stop.
        """
        if self._running_jobs(environment_id) or self._publication.holds(environment_id):
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
        for live in list(self._live.values()):
            saved = self.store.recovery(live.execution.execution_id)
            recoverable = (
                getattr(getattr(self.backend, "settings", None), "daemon", False)
                and live.dispatched
                and saved is not None
                and bool(saved[1])
            )
            if live.execution.state not in TERMINAL_STATES and not recoverable:
                self._lose(live, "the worker")

    def _lose(self, live: Live, what: str) -> None:
        """End an attempt whose outcome is unknown, because ``what`` stopped during it."""
        message = f"{what} stopped during this attempt; its outcome is unknown"
        if live.execution.job is not None:
            message += (
                "; partial whole logs remain at "
                + live.execution.job.stdout_path
                + " and "
                + live.execution.job.stderr_path
            )
        try:
            if live.final is not None:
                # The outcome is known; only the shared files' sync after it was cut short.
                self._complete(live, [*live.notices, _UNSYNCED])
                return
            self._finish(
                live,
                ExecutionState.LOST,
                Result(error=ErrorInfo(kind=ErrorKind.INTERNAL, message=message)),
            )
        except Exception:
            _log.exception("could not report execution %s as lost", live.execution.execution_id)

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

    def _track(self, execution: Execution, limit: int = DEFAULT_OUTPUT_LIMIT) -> Live:
        live = Live(
            execution,
            execution.context.environment_id,
            asyncio.get_running_loop().create_future(),
            limit=limit,
        )
        self._live[execution.execution_id] = live
        return live

    def _save(self, live: Live, **changes: Any) -> None:
        live.execution = live.execution.model_copy(update=changes)
        self.store.update_execution(live.execution)

    def _changed(self, live: Live) -> None:
        changed, live.changed = live.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def _emit(self, live: Live, make: Callable[[int, datetime], Event]) -> None:
        seq = live.execution.last_seq + 1
        event = make(seq, self.clock.now())
        live.execution = live.execution.model_copy(update={"last_seq": seq})
        source = _SOURCE_EVENT.get()
        acknowledged = (
            source[1] if source is not None and source[0] == live.execution.execution_id else None
        )
        self.store.commit_event(live.execution, event, acknowledged)
        self._changed(live)

    def _state(self, live: Live, state: ExecutionState) -> None:
        execution_id = live.execution.execution_id
        self._emit(
            live,
            lambda seq, time: StateEvent(
                execution_id=execution_id, seq=seq, time=time, state=state
            ),
        )

    def _finish(self, live: Live, state: ExecutionState, result: Result) -> None:
        """End an execution: its state event, result event and record are stored together."""
        if live.execution.state in TERMINAL_STATES:
            return
        execution_id = live.execution.execution_id
        final, events = self._final(live.execution, state, result)
        source = _SOURCE_EVENT.get()
        acknowledged = live.final_source_seq
        if acknowledged is None and source is not None and source[0] == execution_id:
            acknowledged = source[1]
        self.store.finish(
            final,
            events,
            acknowledged,
            observation=live.observation,
            observation_gap=live.observation_gap,
        )
        live.execution = final
        self._changed(live)
        environment = self._connections.get(live.environment_id)
        if environment is not None:
            environment.running.discard(execution_id)
            environment.last_used = self.clock.monotonic()
        if live.watchdog is not None and live.watchdog is not asyncio.current_task():
            live.watchdog.cancel()
        if live.cancel_watchdog is not None and live.cancel_watchdog is not asyncio.current_task():
            live.cancel_watchdog.cancel()
        asyncio.get_running_loop().call_later(
            self.timeouts.retain_live, self._live.pop, execution_id, None
        )

    def _final(
        self, execution: Execution, state: ExecutionState, result: Result
    ) -> tuple[Execution, tuple[StateEvent, ResultEvent]]:
        """The terminal record and its last two events, checked before anything is stored.

        Every value must survive a round trip through JSON and fit in one event
        line; a result that does not is shrunk, and failing that replaced.
        """
        unstorable = Result(
            error=ErrorInfo(
                kind=ErrorKind.INTERNAL, message="the attempt's result could not be stored"
            )
        )
        candidates = (
            (state, result),
            (state, _shrunk(result)),
            (ExecutionState.FAILED, unstorable),
        )
        now = self.clock.now()
        seq = execution.last_seq
        for final_state, candidate in candidates:
            final = execution.model_copy(
                update={
                    "state": final_state,
                    "ended_at": now,
                    "result": candidate,
                    "last_seq": seq + 2,
                }
            )
            try:
                event = ResultEvent(
                    execution_id=execution.execution_id, seq=seq + 2, time=now, execution=final
                )
                encoded = _EVENT.dump_json(event)
                if len(encoded) > MAX_EVENT_BYTES:
                    raise ValueError(f"{len(encoded)} bytes")
                checked = _EVENT.validate_json(encoded)
            except ValueError as exc:  # PydanticSerializationError and ValidationError too
                _log.warning(
                    "execution %s: result not storable as reported (%s)",
                    execution.execution_id,
                    str(exc)[:300],
                )
                continue
            if not isinstance(checked, ResultEvent):
                continue
            state_event = StateEvent(
                execution_id=execution.execution_id, seq=seq + 1, time=now, state=final_state
            )
            return checked.execution, (state_event, checked)
        raise RuntimeError("the fallback result could not be stored")

    # Environments.

    async def _environment(self, environment_id: str) -> Connection:
        """The environment's connection, with its executor open."""
        return await self._connections.connect(environment_id)

    def _daemon_binding(self, environment_id: str) -> tuple[str, str] | None:
        return self.daemon_identity(environment_id) if self.daemon_identity else None

    def _hello(self, environment_id: str, channel: ExecutorChannel) -> None:
        """Note the package state a new executor reports."""
        self._observe(environment_id, channel.hello.get("environment"), "")
        self.store.kernel_starts.epoch(
            environment_id,
            channel.incarnation,
            channel.hello.get("package_epoch"),
            channel.hello.get("environment"),
            remember=True,
        )

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

    def _want_lockfile(self, environment_id: str) -> bool:
        """Ask the executor for the current lockfile if the current revision has none, as
        revision 0 has none at first; return whether it was asked. One request at a time
        for each environment, and nothing waits for it. Executions ask (_ask_lockfile),
        each within its budget: revision 0 gets its lockfile as the first one starts."""
        environment = self._connections.get(environment_id)
        channel = environment.channel if environment is not None else None
        if (
            channel is None
            or channel.closed
            or environment_id in self._fetching
            or not self.revisions.needs_lockfile(environment_id)
        ):
            return False
        self._fetching.add(environment_id)
        task = self._spawn(self._fetch_lockfile(environment_id, channel))
        task.add_done_callback(lambda _: self._fetching.discard(environment_id))
        return True

    async def _fetch_lockfile(self, environment_id: str, channel: ExecutorChannel) -> None:
        try:
            reply = await channel.request("lockfile", reply_timeout=self.timeouts.lockfile)
        except WorkerError as error:
            _log.info("environment %s: no lockfile from its executor: %s", environment_id, error)
            return
        if not self.revisions.needs_lockfile(environment_id):
            return  # a revision event brought one meanwhile
        prepared = await self._prepare(environment_id, reply.get("lockfile"))
        if prepared is None or not self.revisions.attach(
            environment_id, reply.get("fingerprint"), prepared
        ):
            _log.info("environment %s: the executor's lockfile was not kept", environment_id)

    async def _prepare(self, environment_id: str, raw: object) -> Prepared | None:
        """Check and store a lockfile from the executor, in a thread, one at a time for each
        environment: the event loop never waits for one (CP-18)."""
        if raw is None:
            return None
        lock = self._lockfile_locks.setdefault(environment_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.to_thread(self.revisions.prepare, raw)
            except OSError as exc:
                _log.warning("environment %s: cannot store a lockfile: %s", environment_id, exc)
                return None

    async def _on_closed(self, environment_id: str, channel: ExecutorChannel) -> None:
        self._connections.channels.discard(channel)
        self._admission.channel_closed(channel)
        if channel.daemon_token is not None and (self._usability.closing or not channel.detaching):
            self._recovery.bridge_lost(environment_id, channel)
            return
        # The executions sent to this executor end with it, whether or not its
        # environment's record still names it.
        for live in list(self._live.values()):
            if live.channel is channel and live.execution.state not in TERMINAL_STATES:
                self._lose(live, "the worker" if self._usability.closing else "the executor")
        self._connections.closed(environment_id, channel)

    # Submission and dispatch.

    async def submit(
        self, request: ExecutionRequest, *, origin: str | None = None
    ) -> tuple[Execution, bool]:
        """Return the execution for ``request.operation_id``, creating it if it is new.

        ``origin`` names the provisioning operation the execution is an install step of: a
        managed change its package commands make is recorded as that operation's.
        """
        if self._usability.closing:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the worker is closing")
        digest = execution_digest(request)
        found = self.store.find_operation(request.context.project_id, request.operation_id)
        if found is not None:
            execution_id, existing = found
            if not same_execution(existing, request):
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"operation {request.operation_id} was submitted with a different request",
                )
            return self.get(execution_id), False
        self._admission.check_origin(request, origin)
        if self._usability.fenced(request.context.environment_id):
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                "the environment is fenced until its sandbox executor is safely recycled",
            )
        job: JobDetails | None = None
        execution_id = new_id("exe")
        if isinstance(request, JobRequest):
            context = request.context
            if self._usability.winding_down(context):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "the job's session or environment is closing"
                )
            resources = (
                self.job_resources(context.environment_id) if self.job_resources else Resources()
            )
            maximum = min(
                604800.0,
                resources.job_wall_time_max.total_seconds()
                if resources.job_wall_time_max
                else 604800.0,
            )
            default = min(
                maximum,
                resources.job_wall_time_default.total_seconds()
                if resources.job_wall_time_default
                else 86400.0,
            )
            timeout = request.timeout_seconds if request.timeout_seconds is not None else default
            if timeout > maximum:
                raise WorkerError(
                    ErrorKind.POLICY_DENIED,
                    f"job time limit exceeds the policy maximum of {maximum:g} s",
                )
            running = self._running_jobs(context.environment_id)
            session = [
                live.execution
                for live in self._live.values()
                if live.execution.kind == "job"
                and live.execution.state not in TERMINAL_STATES
                and live.execution.context.project_id == context.project_id
                and live.execution.context.session_id == context.session_id
            ]
            if len(running) >= (resources.jobs_per_environment or 8) or len(session) >= (
                resources.jobs_per_session or 4
            ):
                ids = ", ".join(dict.fromkeys(job.execution_id for job in [*running, *session]))
                raise WorkerError(
                    ErrorKind.POLICY_DENIED, f"job concurrency limit reached; active jobs: {ids}"
                )
            request = request.model_copy(update={"timeout_seconds": timeout})
            job = JobDetails(
                purpose=request.purpose,
                expected_progress=request.expected_progress,
                wake_session=request.wake_session,
                timeout_seconds=timeout,
                stdout_path=f".jobs/{execution_id}/stdout.log",
                stderr_path=f".jobs/{execution_id}/stderr.log",
            )
        execution = Execution(
            execution_id=execution_id,
            attempt_id=new_id("att"),
            operation_id=request.operation_id,
            kind=request.kind,
            context=request.context,
            state=ExecutionState.QUEUED,
            submitted_at=self.clock.now(),
            origin=origin,
            job=job,
        )
        self.store.insert_execution(execution, digest)
        self.store.remember_request(execution_id, request.model_dump_json())
        live = self._track(execution, request.output_limit_bytes)
        self._state(live, ExecutionState.QUEUED)
        self.store.update_execution(live.execution)
        self._spawn(self._dispatch(live, request))
        return live.execution, True

    async def _dispatch(self, live: Live, request: ExecutionRequest) -> None:
        """Send the request to its executor; whatever goes wrong, the execution ends."""
        try:
            await self._send(live, request)
        except WorkerError as error:
            if (
                live.dispatched
                and not isinstance(error, ExecutorRefusalError)
                and live.channel is not None
                and live.channel.daemon_token is not None
            ):
                await live.channel.close()
            else:
                self._fail(live, error.info())
        except Exception as exc:
            _log.exception("dispatching execution %s failed", live.execution.execution_id)
            self._fail(
                live,
                ErrorInfo(
                    kind=ErrorKind.INTERNAL,
                    message=f"the worker could not dispatch this attempt ({type(exc).__name__})",
                ),
            )

    def _fail(self, live: Live, error: ErrorInfo) -> None:
        environment = self._connections.get(live.environment_id)
        if environment is not None:
            environment.running.discard(live.execution.execution_id)
            environment.last_used = self.clock.monotonic()
        self._finish(live, ExecutionState.FAILED, Result(error=error))

    async def _send(self, live: Live, request: ExecutionRequest) -> None:
        execution_id = live.execution.execution_id
        if live.cancel_requested or live.execution.state in TERMINAL_STATES:
            self._finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
            return
        environment = await self._environment(request.context.environment_id)
        if live.cancel_requested or live.execution.state in TERMINAL_STATES:
            self._finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
            return
        if isinstance(request, JobRequest) and "jobs" not in environment.features:
            raise WorkerError(
                ErrorKind.UNSUPPORTED, "this executor does not support background jobs"
            )
        if isinstance(request, PdfRequest) and "pdf" not in environment.features:
            raise WorkerError(
                ErrorKind.UNSUPPORTED, "this environment cannot read PDFs (it lacks pdf_oxide)"
            )
        if isinstance(request, CellRequest) and request.language not in environment.languages:
            raise WorkerError(
                ErrorKind.UNSUPPORTED, f"no {request.language} kernel in this environment"
            )
        self._save(live, location=self._located(request.context.environment_id, environment))
        fields: dict[str, Any] = {
            "execution": execution_id,
            "session": request.context.session_id,
            "timeout": time_limit(request),
            "limit": request.output_limit_bytes,
        }
        if isinstance(request, ShellRequest | JobRequest):
            fields.update(cwd=request.cwd)
            if request.argv is not None:
                fields["argv"] = list(request.argv)
            else:
                fields["script"] = request.script
        elif isinstance(request, PdfRequest):
            fields.update(
                request.model_dump(
                    mode="json",
                    include={"operation", "path", "pages", "query", "page", "region", "dpi"}
                    | {"include_artifacts", "out"},
                    exclude_none=True,
                )
            )
        else:
            fields.update(language=request.language, code=request.code)
        channel = environment.channel
        if channel is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
        environment.running.add(execution_id)
        if self.shared is not None:
            live.notices += await self._sync(self.shared.down, request.context, channel)
            if live.cancel_requested:  # while the shared files synced
                self._finish(
                    live,
                    ExecutionState.CANCELLED,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.CANCELLED, message="cancelled before it started"
                        ),
                        notices=tuple(live.notices),
                    ),
                )
                return
        self._admission.check_origin(request, live.execution.origin)
        if not await self._admission.sync(request.context.environment_id, channel):
            raise WorkerError(ErrorKind.UNAVAILABLE, PACKAGE_GATE_LEGACY)
        gate = self.store.package_gate(request.context.environment_id)
        self._admission.check_origin(request, live.execution.origin)
        if live.execution.origin is not None:
            fields.update(
                package_operation=live.execution.origin, package_generation=gate.generation
            )
        live.dispatched = True
        live.dispatched_at = self.clock.monotonic()
        live.channel = channel
        identity = self.daemon_identity(live.environment_id) if self.daemon_identity else None
        if channel.daemon_token is not None and (
            self.store.endpoint_incarnation(live.environment_id) != channel.incarnation
            or self.store.endpoint_binding(live.environment_id) != identity
        ):
            identity = None  # never combine this channel with a newer endpoint's identity
        self.store.observation_dispatch(
            live.execution, channel.incarnation, identity, daemon=channel.daemon_token is not None
        )
        live.watchdog = self._spawn(self._watch(live, time_limit(request)))
        await channel.request(request.kind, **fields)
        if channel.daemon_token is not None:
            self._recovery.poll(request.context.environment_id, channel)

    async def _watch(self, live: Live, timeout: float) -> None:
        await self.clock.sleep(max(0, timeout + self.timeouts.watchdog_grace))
        if live.execution.state in TERMINAL_STATES or live.final is not None:
            return
        _log.warning("execution %s overran its limit; cancelling it", live.execution.execution_id)
        with contextlib.suppress(WorkerError):
            await self.cancel(live.execution.execution_id)
        await self.clock.sleep(self.timeouts.lost_grace)
        self._finish(
            live,
            ExecutionState.LOST,
            Result(
                error=ErrorInfo(
                    kind=ErrorKind.INTERNAL, message="the executor never reported a result"
                )
            ),
        )

    async def _on_event(self, environment_id: str, message: Message) -> None:
        execution_id = message.get("execution")
        live = self._live.get(execution_id) if isinstance(execution_id, str) else None
        # Only the executor an execution was sent to may report on it.
        if (
            live is None
            or live.environment_id != environment_id
            or not live.dispatched
            or live.channel is None
            or live.final is not None  # it ended; only its sync is left
            or live.execution.state in TERMINAL_STATES
        ):
            return
        source = message.get("source_seq")
        if live.channel.daemon_token is not None:
            if message.get("_bridge") != live.channel.bridge_id:
                return
            saved = self.store.recovery(live.execution.execution_id)
            previous = saved[2] if saved else 0
            if not isinstance(source, int) or isinstance(source, bool) or not 0 < source < 2**62:
                return
            if source <= previous:
                return
            source_from = message.get("source_from")
            if source != previous + 1 and (
                not isinstance(source_from, int)
                or isinstance(source_from, bool)
                or source_from != previous + 1
            ):
                self._lose(live, "the executor with an invalid replay range")
                return
        token = _SOURCE_EVENT.set(
            (live.execution.execution_id, source)
            if isinstance(source, int)
            and not isinstance(source, bool)
            and live.channel.daemon_token
            else None
        )
        try:
            if message.get("event") == "revision":
                # Its lockfile is checked in a thread; this executor's next events wait.
                await self._revised(environment_id, live, message)
            else:
                self._apply(live, environment_id, message)
            if (
                isinstance(source, int)
                and message.get("event") != "end"
                and live.channel.daemon_token
            ):
                self.store.source_cursor(live.execution.execution_id, source)
        except Exception:
            _log.exception("execution %s: an event from the executor failed", execution_id)
            if message.get("event") == "end":  # the execution must end all the same
                self._finish(
                    live,
                    ExecutionState.FAILED,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.INTERNAL, message="the executor's result was unusable"
                        )
                    ),
                )
        finally:
            _SOURCE_EVENT.reset(token)

    def _apply(self, live: Live, environment_id: str, message: Message) -> None:
        execution_id = live.execution.execution_id
        kind = message.get("event")
        if kind == "state" and message.get("state") == "running":
            if live.execution.state is ExecutionState.QUEUED:
                update: dict[str, Any] = {
                    "state": ExecutionState.RUNNING,
                    "started_at": self.clock.now(),
                }
                # The revision as it started, which waiting for a package command can change.
                location = live.execution.location
                if location is not None and "environment" in message:
                    revision = self._observe(environment_id, message.get("environment"), "")
                    if revision is not None:
                        update["location"] = location.model_copy(
                            update={"environment_revision": str(revision)}
                        )
                event = StateEvent(
                    execution_id=execution_id,
                    seq=live.execution.last_seq + 1,
                    time=self.clock.now(),
                    state=ExecutionState.RUNNING,
                )

                def start() -> Execution:
                    kernel, kernel_state_lost = self._kernels.kernel(
                        environment_id,
                        live.execution.context.session_id,
                        message.get("kernel"),
                        False,
                        project_id=live.execution.context.project_id,
                        birth_execution=execution_id if not live.reattached else None,
                        fingerprint=message.get("environment"),
                        expected_channel=live.channel,
                    )
                    if kernel is not None and live.execution.kind == "cell":
                        update.update(
                            kernel=kernel,
                            kernel_state_lost=live.execution.kernel_state_lost or kernel_state_lost,
                        )
                    return live.execution.model_copy(update={**update, "last_seq": event.seq})

                source = _SOURCE_EVENT.get()
                acknowledged = (
                    source[1] if source is not None and source[0] == execution_id else None
                )
                live.execution = self.store.commit_kernel_start(start, event, acknowledged)
                self._changed(live)
                self._ask_lockfile(environment_id, live)
        elif kind == "stream":
            name = "stderr" if message.get("name") == "stderr" else "stdout"
            text = message.get("text")
            if not isinstance(text, str) or not text or not self._admit(live, _size(text)):
                return
            self._emit(
                live,
                lambda seq, time: StreamEvent(
                    execution_id=execution_id, seq=seq, time=time, name=name, text=text
                ),
            )
        elif kind == "output":
            try:
                output = RichOutput.model_validate(message.get("output"))
            except ValidationError:
                _log.warning("execution %s: malformed output dropped", execution_id)
                return
            if not self._admit(live, sum(_size(value) for value in output.data.values())):
                return
            self._emit(
                live,
                lambda seq, time: OutputEvent(
                    execution_id=execution_id, seq=seq, time=time, output=output
                ),
            )
        elif kind == "end":
            # Observation data is derived and independent of the research result. A
            # bad sidecar must never enter the result parser's FAILED fallback.
            live.observation, live.observation_gap = normalize_observation(
                message.get("output_observations")
            )
            if message.get("observation_omitted") == "oversized":
                live.observation, live.observation_gap = None, "oversized"
            try:
                state = ExecutionState(str(message.get("state")))
                if state not in TERMINAL_STATES:
                    raise ValueError(state)
                raw = message.get("result")
                fields = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
                result = self._result(environment_id, live, fields)
            except Exception as exc:  # the execution must end whatever the executor sent
                state = ExecutionState.FAILED
                result = Result(
                    error=ErrorInfo(
                        kind=ErrorKind.INTERNAL,
                        message=f"malformed result from the executor: {str(exc)[:500]}",
                    )
                )
            if live.overflowed and state is ExecutionState.CANCELLED:
                result = result.model_copy(
                    update={
                        "error": ErrorInfo(
                            kind=ErrorKind.CANCELLED,
                            message="cancelled by the worker: the executor sent more output "
                            "than the output limit allows",
                        )
                    }
                )
            if message.get("recovery_compacted") is True:
                live.notices.append(
                    "The executor retained a compact recovery result after journal pressure; "
                    "whole job stdout/stderr logs remain in the workspace."
                )
            source = _SOURCE_EVENT.get()
            live.final_source_seq = source[1] if source is not None else None
            live.final = (state, result)
            if live.cancel_watchdog is not None:
                live.cancel_watchdog.cancel()
            live.ended_at = self.clock.monotonic()
            if self.shared is None or live.channel is None:
                self._complete(live, live.notices)
            else:
                if live.watchdog is not None:
                    live.watchdog.cancel()  # the attempt is over; only the sync is left
                    live.watchdog = None
                self._spawn(self._end(live, live.channel))

    async def _end(self, live: Live, channel: ExecutorChannel) -> None:
        """Copy the shared files the attempt changed into the project's copy, then end it (D79).

        A later call in another environment of the project then finds them there.
        """
        notices = list(live.notices)
        if self.shared is not None:
            notices += await self._sync(self.shared.up, live.execution.context, channel)
        try:
            self._complete(live, notices)
        except Exception:
            # The attempt must end all the same; the watchdog no longer watches it.
            _log.exception(
                "execution %s: its result could not be stored", live.execution.execution_id
            )
            with contextlib.suppress(Exception):
                self._finish(
                    live,
                    ExecutionState.FAILED,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.INTERNAL,
                            message="the attempt's result could not be stored",
                        )
                    ),
                )

    def _complete(self, live: Live, notices: list[str]) -> None:
        """End the attempt with the outcome the executor reported, and the sync's notices."""
        if live.final is None:
            return
        state, result = live.final
        context = live.execution.context
        held = self._held_notices.pop((context.environment_id, context.session_id), [])
        changes: dict[str, Any] = {}
        if held or notices:
            changes["notices"] = (*result.notices, *held, *notices)
        if self.shared is not None and result.written_files:
            # Files the sync wrote during the call, unchanged since, are not the call's own.
            synced = self.shared.unchanged_writes(
                live.environment_id, result.written_files, live.dispatched_at, live.ended_at
            )
            if synced:
                changes["written_files"] = tuple(
                    path for path in result.written_files if path not in synced
                )
        self._finish(live, state, result.model_copy(update=changes) if changes else result)

    async def _sync_shared(
        self, context: ExecutionContext, path: str, channel: ExecutorChannel
    ) -> None:
        """Bring the shared files up to date before a file request that reads them (D79)."""
        if self.shared is None or not _in_shared(path):
            return
        self._tell(context, await self._sync(self.shared.down, context, channel))

    def _tell(self, context: ExecutionContext, notices: list[str]) -> None:
        """Pass on what syncing for a file request found (D79).

        The calls running in the environment get it with their results, since a change of
        theirs may be what it is about; the requesting session gets it with its next
        result there, unless one of those calls is its own.
        """
        if not notices:
            return
        told = False
        for live in self._live.values():
            running = live.execution.context
            if (
                live.environment_id == context.environment_id
                and running.project_id == context.project_id  # never another project's
                and live.dispatched
                and live.final is None
                and live.execution.state not in TERMINAL_STATES
            ):
                live.notices = _held([*live.notices, *notices])
                told = told or running.session_id == context.session_id
        if not told:
            key = (context.environment_id, context.session_id)
            held = self._held_notices.pop(key, [])
            self._held_notices[key] = _held([*held, *notices])
            while len(self._held_notices) > MAX_HELD_SESSIONS:
                self._held_notices.popitem(last=False)

    async def _sync(
        self,
        step: Callable[[str, str, str, ExecutorChannel], Awaitable[list[str]]],
        context: ExecutionContext,
        channel: ExecutorChannel,
    ) -> list[str]:
        """Run one step of the shared files' sync; its failure never fails the call (D79)."""
        try:
            return await asyncio.wait_for(
                step(context.project_id, context.environment_id, context.session_id, channel),
                self.timeouts.shared,
            )
        except Exception as exc:  # the call goes on with shared/ as it is
            _log.warning(
                "syncing the shared files of %s in %s failed: %s",
                context.project_id,
                context.environment_id,
                exc,
            )
            return [_UNSYNCED]

    def _admit(self, live: Live, size: int) -> bool:
        """Charge one stream or output event to the execution's budget; False once spent.

        Past the event count, events are only dropped; past the byte budget, which
        an honest executor never reaches, the attempt is also cancelled.
        """
        live.events += 1
        live.stored += size
        if live.stored > OUTPUT_BUDGET * live.limit + 65536:
            if not live.overflowed:
                live.overflowed = True
                _log.warning(
                    "execution %s: the executor sent more output than its limit; cancelling it",
                    live.execution.execution_id,
                )
                self._spawn(self._cancel_quietly(live))
            return False
        if live.events == MAX_EVENTS + 1:
            _log.warning(
                "execution %s: over %d events; dropping the rest",
                live.execution.execution_id,
                MAX_EVENTS,
            )
        return live.events <= MAX_EVENTS

    async def _cancel_quietly(self, live: Live) -> None:
        with contextlib.suppress(WorkerError):
            await self.cancel(live.execution.execution_id)

    def _result(self, environment_id: str, live: Live, raw: dict[str, Any]) -> Result:
        """The executor's result, checked and bounded by the attempt's output limit."""
        after = self._revision_after(environment_id, live, raw)
        kernel, lost = self._kernels.kernel(
            environment_id,
            live.execution.context.session_id,
            raw.get("kernel"),
            raw.get("state_lost") is True or live.execution.kernel_state_lost,
            project_id=live.execution.context.project_id,
            expected_channel=live.channel,
        )
        pinned = live.execution.kernel
        if (
            kernel is not None
            and pinned is not None
            and (kernel.kernel_id, kernel.language, kernel.generation)
            == (pinned.kernel_id, pinned.language, pinned.generation)
        ):
            revision = kernel.current_revision
            start_revision = pinned.start_revision
            if start_revision is not None and revision is not None and start_revision > revision:
                start_revision = None
            status = (
                "unknown"
                if start_revision is None or revision is None
                else ("current" if start_revision == revision else "predates")
            )
            kernel = kernel.model_copy(
                update={"start_revision": start_revision, "revision_status": status}
            )
        running = self._kernels.session_kernels(
            live.execution.context, raw.get("running_kernels"), expected_channel=live.channel
        )
        written = raw.get("written_files")
        paths = [
            path
            for path in (cast(list[object], written) if isinstance(written, list) else [])
            if isinstance(path, str)
            and path
            and len(path.encode("utf-8", "replace")) <= MAX_PATH_BYTES
        ]
        location = live.execution.location
        before = location.environment_revision if location is not None else None
        return Result(
            exit_code=integer(raw.get("exit_code")),
            signal=integer(raw.get("signal"), 1, 255),
            status=raw.get("status"),
            execution_count=integer(raw.get("execution_count"), 0),
            stdout=_stream(raw.get("stdout"), "stdout", live.limit),
            stderr=_stream(raw.get("stderr"), "stderr", live.limit),
            outputs=_outputs(raw.get("outputs"), live.limit),
            error=_error(raw.get("error")),
            kernel=kernel,
            running_kernels=running,
            state_lost=lost,
            elapsed_seconds=_seconds(raw.get("elapsed_seconds")),
            sandbox_started_at=epoch(raw.get("sandbox_started")),
            sandbox_ended_at=epoch(raw.get("sandbox_ended")),
            written_files=tuple(paths[:MAX_WRITTEN]),
            written_files_truncated=raw.get("written_files_truncated") is True
            or len(paths) > MAX_WRITTEN,
            environment_revision=after,
            overlapping_change=raw.get("environment_changing") is True
            or (before is not None and after is not None and before != after),
            notices=self._notices(environment_id, live),
        )

    def _notices(self, environment_id: str, live: Live) -> tuple[str, ...]:
        """Admission notices the session has not seen, each once (D69).

        A session's first result starts from its submission; later ones from the last
        notice it received, since decisions are stamped when they are recorded.
        """
        if self.notices is None:
            return ()
        key = (environment_id, live.execution.context.session_id)
        since = self._noticed.get(key, live.execution.submitted_at)
        try:
            fresh = self.notices(environment_id, since)
        except Exception:
            _log.exception("could not read the admission notices of %s", environment_id)
            return ()
        self._noticed[key] = max([since, *(at for at, _ in fresh)])
        return tuple(text for _, text in fresh)

    def _located(self, environment_id: str, environment: Connection) -> Location | None:
        """The environment's location, with its current revision."""
        location, record = environment.location, self.store.revision(environment_id)
        if location is None or record is None:
            return location
        return location.model_copy(update={"environment_revision": str(record.revision)})

    def _revision_after(self, environment_id: str, live: Live, raw: dict[str, Any]) -> str | None:
        """The environment's revision once the attempt's fingerprint is counted. A change
        seen only here is unmanaged, so it is no provisioning operation's, even when the
        execution is one's step."""
        revision = self._observe(environment_id, raw.get("environment"), live.execution.attempt_id)
        self._ask_lockfile(environment_id, live)
        return None if revision is None else str(revision)

    def _observe(self, environment_id: str, fingerprint: object, changed_by: str) -> int | None:
        """The environment's revision after the executor reported ``fingerprint`` (W3).

        The first fingerprint is revision 0, and each different one counts one up,
        recorded against the attempt that reported it (empty between executors). The
        executor reports each change in a ``revision`` event first, so a new fingerprint
        here is one it could not account for. The executor is untrusted: research code
        can only make its own environment's revisions count up.
        """
        return self.revisions.observe(environment_id, fingerprint, changed_by)

    async def _revised(self, environment_id: str, live: Live, message: Message) -> None:
        """A change to the environment's packages that the executor saw (CP-4, CP-16).

        A managed change names the attempt that ran its package commands, which must be
        one of this environment's; an unmanaged one, the attempt after which it was seen.
        The executor sends a lockfile with each attempt's last change. The worker keeps
        one for each attempt, and checks at most MAX_EXECUTION_LOCKFILES for each
        execution, those it asks for included. A revision left without one gets the
        current lockfile asked for, while it is current.
        """
        live.revisions += 1
        if live.revisions > MAX_REVISIONS:
            if live.revisions == MAX_REVISIONS + 1:
                _log.warning(
                    "execution %s: over %d revisions; dropping the rest",
                    live.execution.execution_id,
                    MAX_REVISIONS,
                )
            return
        managed = message.get("managed")
        cause = message.get("cause")
        attempt, origin = (
            self._attempt(environment_id, cause) if isinstance(cause, str) else ("", None)
        )
        prepared: Prepared | None = None
        raw = message.get("lockfile")
        if (
            raw is not None
            and attempt
            and attempt not in live.lockfile_attempts
            and live.lockfiles < MAX_EXECUTION_LOCKFILES
        ):
            live.lockfiles += 1
            live.lockfile_attempts.add(attempt)
            prepared = await self._prepare(environment_id, raw)
        commands, omitted = (
            package_commands(message.get("commands"), message.get("omitted_commands"))
            if managed is True
            else ((), 0)
        )
        self.revisions.observe(
            environment_id,
            message.get("fingerprint"),
            attempt,
            managed=managed if isinstance(managed, bool) else None,
            commands=commands,
            omitted=omitted,
            lockfile=prepared,
            # Only a change its own package commands made is the operation's.
            operation=origin if managed is True else None,
        )
        if (
            live.channel is not None
            and (connection := self._connections.get(environment_id)) is not None
            and connection.channel is live.channel
        ):
            self.store.kernel_starts.epoch(
                environment_id,
                live.channel.incarnation,
                message.get("package_epoch"),
                message.get("fingerprint"),
                remember=True,
            )
        self._ask_lockfile(environment_id, live)

    def _ask_lockfile(self, environment_id: str, live: Live) -> None:
        """Ask for the current lockfile on behalf of ``live``, within its budget: every
        lockfile checked counts, those an execution's start and end ask for too."""
        if live.lockfiles < MAX_EXECUTION_LOCKFILES and self._want_lockfile(environment_id):
            live.lockfiles += 1

    def _attempt(self, environment_id: str, execution_id: str) -> tuple[str, str | None]:
        """The current attempt of one of the environment's executions, and the provisioning
        operation it is a step of; empty for any other execution."""
        if not _EXECUTION.fullmatch(execution_id):
            return "", None
        live = self._live.get(execution_id)
        try:
            execution = live.execution if live is not None else self.store.execution(execution_id)
        except ValidationError:
            return "", None
        if execution is None or execution.context.environment_id != environment_id:
            return "", None
        return execution.attempt_id, execution.origin

    async def kernel_status(self, request: KernelStatusRequest) -> KernelListing:
        """Observe only the authenticated context's kernels; admission gates stay unchanged."""
        return await self._kernels.status(request)

    async def observe_kernels(self, request: KernelStatusRequest) -> KernelObservation:
        """Read an existing channel without keeping it alive or waiting on its lifecycle."""
        return await self._kernels.observe(request)

    # Queries and control.

    def get(self, execution_id: str, project_id: str | None = None) -> Execution:
        """The execution, which must belong to ``project_id`` when one is given."""
        live = self._live.get(execution_id)
        if live is not None:
            execution = live.execution
        else:
            try:
                found = self.store.execution(execution_id)
            except ValidationError as exc:
                raise WorkerError(
                    ErrorKind.INTERNAL, f"the stored record of {execution_id} is unreadable"
                ) from exc
            if found is None:
                raise WorkerError(ErrorKind.NOT_FOUND, f"no execution {execution_id}")
            execution = found
        if project_id is not None and execution.context.project_id != project_id:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no execution {execution_id}")
        return execution

    async def _cancel_jobs(self, jobs: list[Execution]) -> None:
        """Bound cancel acknowledgments and process completion together, including broken peers."""
        if not jobs:
            return

        async def cancel_all() -> None:
            await asyncio.gather(*(self.cancel(job.execution_id) for job in jobs))
            await asyncio.gather(*(self.wait(job.execution_id) for job in jobs))

        try:
            await asyncio.wait_for(cancel_all(), self.timeouts.cancel_grace)
        except (TimeoutError, WorkerError):
            channels: set[ExecutorChannel] = set()
            for job in jobs:
                live = self._live.get(job.execution_id)
                if live is not None and live.execution.state not in TERMINAL_STATES:
                    if live.channel is not None:
                        channels.add(live.channel)
                    self._lose(live, "the executor that did not confirm job cancellation")
            await asyncio.gather(*(channel.shutdown() for channel in channels))

    def _running_jobs(self, environment_id: str) -> list[Execution]:
        return [
            live.execution
            for live in self._live.values()
            if live.environment_id == environment_id
            and live.execution.kind == "job"
            and live.execution.state not in TERMINAL_STATES
        ]

    def jobs(
        self, context: ExecutionContext, limit: int = 100, after: str | None = None
    ) -> JobListing:
        """List only the requested session's jobs, including recently ended attempts."""
        if not 1 <= limit <= 500:
            raise WorkerError(
                ErrorKind.INVALID_REQUEST, "job listing limit must be between 1 and 500"
            )
        try:
            jobs = self.store.session_jobs(context, limit + 1, after)
        except LookupError as exc:
            raise WorkerError(
                ErrorKind.NOT_FOUND, "no job listing cursor for this session"
            ) from exc
        selected: list[Execution] = []
        total = 0
        for job in jobs[:limit]:
            size = len(job.model_dump_json().encode())
            if total + size > 1024 * 1024:
                break
            selected.append(job)
            total += size
        return JobListing(
            jobs=tuple(selected),
            truncated=len(jobs) > len(selected),
            next_after=selected[-1].execution_id if selected else after,
        )

    async def job_status(self, execution_id: str, project_id: str | None = None) -> JobStatus:
        """Read bounded live output without opening a new executor or waiting for the job."""
        execution = self.get(execution_id, project_id)
        if execution.kind != "job":
            raise WorkerError(ErrorKind.NOT_FOUND, f"no job {execution_id}")
        result = execution.result
        if result is not None:
            error = result.error
            if error is not None:
                error = ErrorInfo(kind=error.kind, message=_text(error.message, 4096) or "")
            compact = result.model_copy(
                update={
                    "stdout": None,
                    "stderr": None,
                    "outputs": (),
                    "written_files": (),
                    "notices": (),
                    "error": error,
                }
            )
            return JobStatus(
                execution=execution.model_copy(update={"result": compact}),
                stdout=_job_stream(result.stdout),
                stderr=_job_stream(result.stderr),
            )
        live = self._live.get(execution_id)
        channel = live.channel if live is not None else None
        if live is None or channel is None or channel.closed:
            return JobStatus(execution=execution)
        reply = await channel.request("job_status", execution=execution_id, reply_timeout=10)
        cpu = reply.get("cpu_seconds")
        cpu = (
            float(cpu)
            if isinstance(cpu, int | float) and not isinstance(cpu, bool) and 0 <= cpu <= 1e15
            else None
        )
        memory = integer(reply.get("memory_bytes"), 0, 2**62)
        execution = self.get(execution_id)
        if execution.result is not None:
            return await self.job_status(execution_id, project_id)
        return JobStatus(
            execution=execution,
            stdout=_stream(reply.get("stdout"), "stdout", min(live.limit, 16384)),
            stderr=_stream(reply.get("stderr"), "stderr", min(live.limit, 16384)),
            cpu_seconds=cpu,
            memory_bytes=memory,
        )

    async def events(
        self, execution_id: str, after: int, *, heartbeat: float | None = None
    ) -> AsyncGenerator[Event | None, None]:
        """Yield the events after ``after``, then new ones, ending with the result.

        While nothing happens, ``None`` comes every ``heartbeat`` seconds. The
        stream also ends when the execution is over, whatever ``after`` was, and
        without a result once ``end_streams`` was called.
        """
        self.get(execution_id)
        while True:
            live = self._live.get(execution_id)
            changed = live.changed if live is not None else None
            over = live is None or live.execution.state in TERMINAL_STATES or self._streams_ended
            while True:
                try:
                    page = self.store.events_after(execution_id, after, EVENT_PAGE)
                except ValidationError as exc:
                    raise WorkerError(
                        ErrorKind.INTERNAL, f"a stored event of {execution_id} is unreadable"
                    ) from exc
                for event in page:
                    yield event
                    after = event.seq
                    if isinstance(event, ResultEvent):
                        return
                if len(page) < EVENT_PAGE:
                    break
            if over or changed is None:
                return
            # Every reader shares the future: one that goes away must not cancel it.
            if heartbeat is None:
                await asyncio.shield(changed)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(changed), heartbeat)
            except TimeoutError:
                yield None

    def end_streams(self) -> None:
        """End every event stream, so that shutdown does not wait for its readers.

        Readers ask again with ``after``, from this worker once it runs again.
        """
        self._streams_ended = True
        for live in self._live.values():
            self._changed(live)

    async def wait(self, execution_id: str) -> Execution:
        """The execution once it is terminal."""
        while True:
            live = self._live.get(execution_id)
            if live is None:
                return self.get(execution_id)
            if live.execution.state in TERMINAL_STATES:
                return live.execution
            # Every waiter shares the future: one that goes away must not cancel it.
            await asyncio.shield(live.changed)

    async def cancel(self, execution_id: str, project_id: str | None = None) -> Execution:
        execution = self.get(execution_id, project_id)
        live = self._live.get(execution_id)
        if live is None or live.execution.state in TERMINAL_STATES or live.final is not None:
            return execution
        already_requested = live.cancel_requested
        self.store.cancel_intent(execution_id)
        live.cancel_requested = True
        if live.dispatched and not already_requested:
            if execution.kind == "job" or (live.channel is not None and live.channel.daemon_token):
                live.cancel_watchdog = self._spawn(self._watch_cancel(live))
            try:
                await self._send_cancel(live)
            except WorkerError:
                if live.execution.state not in TERMINAL_STATES:
                    raise
        elif not live.dispatched:
            self._finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
        return live.execution

    async def _watch_cancel(self, live: Live) -> None:
        """A direct job cancel has its own deadline, independent of the wall-time limit."""
        await self.clock.sleep(self.timeouts.cancel_grace)
        if live.execution.state in TERMINAL_STATES or live.final is not None:
            return
        channel = live.channel
        self._lose(live, "the executor that did not confirm job cancellation")
        if channel is not None:
            await channel.shutdown()

    async def _send_cancel(self, live: Live) -> None:
        if live.execution.state not in (ExecutionState.CANCELLING, *TERMINAL_STATES):
            live.execution = live.execution.model_copy(update={"state": ExecutionState.CANCELLING})
            self._state(live, ExecutionState.CANCELLING)
            self.store.update_execution(live.execution)
        # Only the executor it was sent to has it; never start another one to cancel.
        channel = live.channel
        if channel is None or channel.closed:
            return
        await channel.request("cancel", execution=live.execution.execution_id)

    async def restart_kernel(self, request: KernelRestartRequest) -> KernelInfo:
        return await self._kernels.restart(request)

    async def close_session(self, request: SessionCloseRequest) -> SessionClosed:
        """Cancel and reap jobs, then stop kernels. Starts racing the close are refused."""
        context = request.context
        self._kernels.invalidate(context)
        with self._usability.closing_session(context):
            jobs = [
                job
                for job in self._running_jobs(context.environment_id)
                if job.context.project_id == context.project_id
                and job.context.session_id == context.session_id
            ]
            await self._cancel_jobs(jobs)
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
            location=self._located(environment_id, environment) or environment.location,
            features=tuple(name for name in environment.features if name in FEATURES),
        )

    # Files. Replies are checked like events: a malformed one is an internal error.

    async def _read_range(
        self,
        channel: ExecutorChannel,
        request: ReadFileRequest,
        offset: int,
        length: int,
        *,
        hashed: bool,
    ) -> Range:
        reply = await channel.request(
            "read",
            session=request.context.session_id,
            path=request.path,
            offset=offset,
            length=length,
            hash=hashed,
        )
        try:
            encoded = reply["content_b64"]
            if not isinstance(encoded, str):
                raise TypeError("content_b64")
            data = base64.b64decode(encoded, validate=True)
            size, start = integer(reply.get("size"), 0, 2**62), integer(reply.get("offset"), 0)
            sha256 = reply.get("sha256")
            if size is None or start != offset or len(data) > length or offset + len(data) > size:
                raise ValueError("a range that was not asked for")
            if hashed and not (isinstance(sha256, str) and len(sha256) == 64):
                raise ValueError("no SHA-256")
        except (KeyError, TypeError, ValueError) as exc:  # binascii.Error is a ValueError
            raise WorkerError(
                ErrorKind.INTERNAL, f"the executor sent a malformed file reply ({exc})"
            ) from exc
        return Range(
            size=size,
            sha256=sha256 if hashed else None,
            modified=epoch(reply.get("modified")),
            offset=offset,
            data=data,
            content_b64=encoded,
            eof=reply.get("eof") is True,
        )

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        async with self._connections.using(request.context.environment_id) as channel:
            await self._sync_shared(request.context, request.path, channel)
            found = await self._read_range(
                channel, request, request.offset, request.length, hashed=True
            )
            return _checked(
                FileContent,
                path=request.path,
                sha256=found.sha256,
                size=found.size,
                modified_at=found.modified,
                offset=found.offset,
                content_b64=found.content_b64,
                eof=found.eof,
            )

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        context = request.context
        async with self._connections.using(context.environment_id) as channel:
            fields: dict[str, Any] = {
                "path": request.path,
                "content_b64": request.content_b64,
                "expected_sha256": request.expected_sha256,
                "create_only": request.create_only,
            }
            if self.shared is not None and _in_shared(request.path):
                # The expected SHA-256 is checked against the project's version (D79).
                notices: list[str] = []
                try:
                    reply = await self.shared.write(
                        context.project_id,
                        context.environment_id,
                        context.session_id,
                        channel,
                        fields,
                        self.timeouts.shared,
                        notices,
                    )
                finally:  # what the sync found is told even when the write is refused
                    self._tell(context, notices)
            else:
                reply = await channel.request("write", session=context.session_id, **fields)
            return _checked(WriteFileResult, sha256=reply.get("sha256"), size=reply.get("size"))

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        context = request.context
        async with self._connections.using(context.environment_id) as channel:
            await self._sync_shared(context, request.path, channel)
            reply = await channel.request("list", session=context.session_id, path=request.path)
            raw = reply.get("entries")
            every = cast(list[object], raw) if isinstance(raw, list) else []
            entries = every[:MAX_LIST]
            listed: list[FileEntry] = []
            for entry in entries:
                item = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
                listed.append(
                    _checked(
                        FileEntry,
                        name=item.get("name"),
                        type=item.get("type"),
                        size=item.get("size"),
                        modified_at=epoch(item.get("modified")),
                    )
                )
            return FileListing(
                path=request.path,
                entries=tuple(listed),
                truncated=reply.get("truncated") is True or len(entries) < len(every),
            )

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        """Publish a file of the environment as an artifact, within ``capture_timeout``."""
        return await self._publication.publish(request, self.capture_timeout)

    def artifact(self, artifact_id: str, project_id: str | None = None) -> ArtifactRef:
        return self._publication.artifact(artifact_id, project_id)


def _size(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def _held(notices: list[str]) -> list[str]:
    """The latest notices, within ``MAX_HELD_NOTICES`` and ``MAX_HELD_BYTES``."""
    kept: list[str] = []
    size = 0
    for notice in reversed(notices[-MAX_HELD_NOTICES:]):
        size += len(notice)
        if size > MAX_HELD_BYTES:
            break
        kept.append(notice)
    return kept[::-1]


def _in_shared(path: str) -> bool:
    """Whether a file request's path is in the project's shared area (D79)."""
    normal = posixpath.normpath(path)
    return normal == "shared" or normal.startswith("shared/")


def _checked[Model: BaseModel](model: type[Model], **fields: Any) -> Model:
    """A reply from the executor as a contract model; a malformed one is an internal error."""
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        raise WorkerError(
            ErrorKind.INTERNAL, f"the executor sent a malformed reply ({model.__name__})"
        ) from exc


def _job_stream(stream: StreamOutput | None) -> StreamOutput | None:
    if stream is None:
        return None
    text = _text(stream.text, 16384) or ""
    return stream.model_copy(
        update={"text": text, "truncated": stream.truncated or text != stream.text}
    )


def _stream(raw: object, name: str, limit: int) -> StreamOutput | None:
    """A stream's summary; its text keeps its head and tail within three times the limit."""
    if not isinstance(raw, dict):
        return None
    raw = cast(dict[str, Any], raw)
    text = _text(raw.get("text"), 3 * limit)
    total = integer(raw.get("total_bytes"), 0, 2**62)
    if text is None or total is None:
        return None
    truncated = raw.get("truncated") is True or text is not raw.get("text")
    return StreamOutput.model_validate(
        {"name": name, "text": text, "total_bytes": total, "truncated": truncated}
    )


def _outputs(raw: object, limit: int) -> tuple[RichOutput, ...]:
    """Rich outputs up to the limit; a note stands for those beyond it."""
    outputs: list[RichOutput] = []
    spent, omitted = 0, 0
    for item in cast(list[object], raw) if isinstance(raw, list) else []:
        try:
            output = RichOutput.model_validate(item)
        except ValidationError:
            continue
        spent += sum(_size(value) for value in output.data.values())
        if spent > limit + 65536:
            omitted += 1
            continue
        outputs.append(output)
    if omitted:
        note = f"[{omitted} outputs omitted: over the output limit]"
        outputs.append(RichOutput(type="display", data={"text/plain": note}))
    return tuple(outputs)


def _error(raw: object) -> ErrorInfo | None:
    if not isinstance(raw, dict):
        return None
    raw = cast(dict[str, Any], raw)
    lines = raw.get("traceback")
    traceback = cast(list[object], lines)[-MAX_TRACEBACK_LINES:] if isinstance(lines, list) else []
    return ErrorInfo.model_validate(
        {
            "kind": raw.get("kind"),
            "message": _text(raw.get("message"), MAX_ERROR_TEXT) or "",
            "name": _text(raw.get("name"), 256),
            "value": _text(raw.get("value"), MAX_ERROR_TEXT),
            "traceback": [_text(line, MAX_TRACEBACK_LINE) or "" for line in traceback],
        }
    )


def _shrunk(result: Result) -> Result:
    """The result without its bulk, for when it is too large to store as reported."""
    note = "[omitted: the result was too large to store]"

    def stream(output: StreamOutput | None) -> StreamOutput | None:
        return (
            None if output is None else output.model_copy(update={"text": note, "truncated": True})
        )

    error = result.error
    if error is not None:
        error = error.model_copy(
            update={"message": error.message[:1000], "value": None, "traceback": ()}
        )
    return result.model_copy(
        update={
            "stdout": stream(result.stdout),
            "stderr": stream(result.stderr),
            "outputs": (RichOutput(type="display", data={"text/plain": note}),)
            if result.outputs
            else (),
            "error": error,
            "written_files": (),
            "written_files_truncated": bool(result.written_files),
        }
    )
