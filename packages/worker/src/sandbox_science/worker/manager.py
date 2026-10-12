# SPDX-License-Identifier: MIT
"""Executions: submission, dispatch to executors, events, cancellation and files.

docs/specs/tools.md defines the behavior. Every execution is persisted before
it is dispatched, and every event before it is delivered, so a client can
reconnect and replay, and a restarted worker reports unfinished attempts as
``lost`` instead of running them again. What an executor sends is untrusted:
it is checked and bounded before it changes any state, and every execution
reaches a terminal state whatever the executor does.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import math
import mimetypes
import posixpath
import re
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    ArtifactRef,
    Capabilities,
    CellRequest,
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
    KernelRestartRequest,
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
    MAX_READ_BYTES,
    PROTOCOL_VERSION,
)
from sandbox_science.policy.model import Resources

from .artifacts import Blobs
from .backends import Backend
from .channel import ExecutorChannel, Message
from .errors import WorkerError
from .isolation import probe
from .revisions import Prepared, Revisions, package_commands
from .shared_sync import SharedSync
from .store import KernelRecord, Store

_log = logging.getLogger(__name__)
_EVENT = TypeAdapter[Event](Event)

SHARED_TIMEOUT = 120.0
"""The longest one step of the shared files' sync may take around a call (D79)."""
MAX_HELD_NOTICES = 20
"""Sync notices held for one call's or one session's next result."""
MAX_HELD_BYTES = 16 * 1024
MAX_HELD_SESSIONS = 256
_UNSYNCED = (
    "shared/ could not be synced with the project's other environments just now; "
    "it is synced again at the next call"
)
WATCHDOG_GRACE = 30.0
"""Seconds past an execution's own limit before the worker cancels it itself."""
CANCEL_GRACE = 30.0
"""Seconds to confirm job cancellation before abandoning its executor connection."""
LOST_GRACE = 60.0
"""Seconds after that cancel before the worker gives the attempt up as lost."""
RETAIN_LIVE = 600.0
"""Seconds a finished execution stays in memory for clients still streaming it."""
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
MAX_HOSTNAME = 255
MAX_REVISIONS = 64
"""``revision`` events one execution may report; later ones are dropped."""
MAX_EXECUTION_LOCKFILES = 4
"""Lockfiles one execution's ``revision`` events may bring or have asked for: one for each
attempt whose package commands made a change. A revision left without one gets the
current lockfile asked for instead, while it is current."""
LOCKFILE_TIMEOUT = 120.0
"""Seconds the executor gets to send the current revision's lockfile."""
_EXECUTION = re.compile(r"exe-[0-9a-f]{24}")


def _now() -> datetime:
    return datetime.now(UTC)


def _integer(value: object, low: int = -(2**31), high: int = 2**31 - 1) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def _epoch(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC)
    return None


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


@dataclass
class _Live:
    execution: Execution
    environment_id: str
    changed: asyncio.Future[None]
    limit: int = DEFAULT_OUTPUT_LIMIT
    cancel_requested: bool = False
    dispatched: bool = False
    watchdog: asyncio.Task[None] | None = None
    cancel_watchdog: asyncio.Task[None] | None = None
    stored: int = 0
    """Bytes of stream text and rich output stored as events."""
    events: int = 0
    overflowed: bool = False
    channel: ExecutorChannel | None = None
    """The executor the execution was sent to: only it may cancel or lose it."""
    notices: list[str] = field(default_factory=list[str])
    """What syncing the project's shared files told before the execution ran (D79)."""
    dispatched_at: float = 0.0
    """When the request went to the executor, by the monotonic clock."""
    final: tuple[ExecutionState, Result] | None = None
    """The executor's outcome, held while the shared files sync after it (D79)."""
    ended_at: float = 0.0
    """When the executor reported the outcome, by the monotonic clock."""
    revisions: int = 0
    """``revision`` events it carried."""
    lockfiles: int = 0
    """Lockfiles its ``revision`` events brought or had asked for."""
    lockfile_attempts: set[str] = field(default_factory=set[str])
    """The attempts whose lockfiles its ``revision`` events brought."""


@dataclass
class _Environment:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    channel: ExecutorChannel | None = None
    location: Location | None = None
    languages: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    running: set[str] = field(default_factory=set[str])
    last_used: float = field(default_factory=time.monotonic)
    """When the worker last asked anything of the environment, by the monotonic clock."""
    requests: int = 0
    """Requests other than executions in flight there: files, publication, kernels."""


Notices = Callable[[str, datetime], list[tuple[datetime, str]]]


@dataclass(frozen=True)
class Activity:
    """What an environment is doing, as the worker sees it, for idle suspension (W3)."""

    last_used: float | None
    """When the worker last asked anything of it (monotonic clock); None if never since it
    started."""
    running: int
    cpu_seconds: float | None
    """CPU time used below its executor so far; None without an executor, when no research
    code runs there."""
    kernels: int
    executor: str | None = None
    """The executor's incarnation, so that CPU samples of different executors are not
    compared."""


@dataclass(frozen=True)
class _Range:
    """A range of a file as the executor read it; ``sha256`` only when it was asked for."""

    size: int
    sha256: str | None
    modified: datetime | None
    offset: int
    data: bytes
    content_b64: str
    eof: bool


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
    ) -> None:
        self.store = store
        self.backend = backend
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
        # The isolation probe's sentinel goes here, among the worker's own files.
        self.state_dir = state_dir
        self._live: dict[str, _Live] = {}
        self._held_notices: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        """Notices from syncing shared files for file requests, by environment and session,
        for the session's next result there (D79)."""
        self._environments: dict[str, _Environment] = {}
        self._channels: set[ExecutorChannel] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self.job_resources: Callable[[str], Resources] | None = None
        self._closing_sessions: dict[tuple[str, str, str], int] = {}
        self._released_environments: dict[str, int] = {}
        self.notices: Notices | None = None
        """Admission notices for an environment since a time; the environments set it."""
        self._noticed: dict[tuple[str, str], datetime] = {}

    async def start(self) -> None:
        """Report attempts a previous run left unfinished as lost.

        One record that cannot be finished is logged and skipped: it never
        stops the others, or the worker.
        """
        for execution_id, execution in self.store.unfinished():
            try:
                if execution is None:
                    self.store.mark_terminal(execution_id)
                    continue
                # Events are stored as they happen, the record only now and then.
                last = max(execution.last_seq, self.store.last_seq(execution_id))
                live = self._track(execution.model_copy(update={"last_seq": last}))
                self._finish(
                    live,
                    ExecutionState.LOST,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.INTERNAL,
                            message="the worker restarted during this attempt; "
                            "its outcome is unknown",
                        )
                    ),
                )
            except Exception:
                _log.exception("could not report execution %s as lost", execution_id)
                self._live.pop(execution_id, None)

    async def release(self, environment_id: str) -> None:
        """Stop an environment's executor before its sandbox goes away.

        Attempts still running there end as the executor's channel closes.
        """
        self._released_environments[environment_id] = (
            self._released_environments.get(environment_id, 0) + 1
        )
        try:
            await self._release(environment_id)
        finally:
            if self._released_environments[environment_id] == 1:
                self._released_environments.pop(environment_id)
            else:
                self._released_environments[environment_id] -= 1

    async def _release(self, environment_id: str) -> None:
        jobs = self._running_jobs(environment_id)
        await self._cancel_jobs(jobs)
        environment = self._environments.get(environment_id)
        if environment is None:
            return
        async with environment.lock:
            await self._drop(environment_id, environment)

    async def _drop(self, environment_id: str, environment: _Environment) -> None:
        """Forget an environment's record, then close its executor. Hold its lock.

        The record goes first, so that a request arriving meanwhile starts with a
        new one instead of opening an executor nobody tracks.
        """
        if self._environments.get(environment_id) is environment:
            self._environments.pop(environment_id)
        channel = environment.channel
        if channel is not None:
            self._channels.discard(channel)
            await channel.close()

    async def activity(self, environment_id: str) -> Activity | None:
        """What an environment is doing now; None when its executor cannot say."""
        environment = self._environments.get(environment_id)
        if environment is None:
            return Activity(
                last_used=None,
                running=len(self._running_jobs(environment_id)),
                cpu_seconds=None,
                kernels=0,
            )
        busy = len(environment.running) + environment.requests
        busy += sum(
            1
            for job in self._running_jobs(environment_id)
            if job.execution_id not in environment.running
        )
        channel = environment.channel
        if channel is None or channel.closed:
            return Activity(environment.last_used, busy, None, 0)
        try:
            reply = await channel.request("usage", reply_timeout=10)
        except WorkerError:
            return None
        raw, kernels = reply.get("cpu_seconds"), reply.get("kernels")
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int | float)
            or not isinstance(kernels, int)
        ):
            return None
        try:
            cpu = float(raw)  # an executor research code took over may send 10**400
        except OverflowError:
            return None
        if not math.isfinite(cpu) or cpu < 0:
            return None
        return Activity(environment.last_used, busy, cpu, kernels, channel.incarnation)

    async def release_if_idle(self, environment_id: str, idle_since: float) -> bool:
        """Stop an environment's executor if nothing ran in it or used it since ``idle_since``.

        Returns whether it did (or there was none), so that its sandbox may stop.
        """
        if self._running_jobs(environment_id):
            return False
        environment = self._environments.get(environment_id)
        if environment is None:
            return True
        async with environment.lock:
            if environment.running or environment.requests or environment.last_used > idle_since:
                return False
            await self._drop(environment_id, environment)
        return True

    async def close(self) -> None:
        """Stop every executor. Attempts still running are reported lost."""
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*(channel.close() for channel in self._channels))
        for live in list(self._live.values()):
            if live.execution.state not in TERMINAL_STATES:
                self._lose(live, "the worker")

    def _lose(self, live: _Live, what: str) -> None:
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
        task.add_done_callback(self._tasks.discard)
        return task

    def _track(self, execution: Execution, limit: int = DEFAULT_OUTPUT_LIMIT) -> _Live:
        live = _Live(
            execution,
            execution.context.environment_id,
            asyncio.get_running_loop().create_future(),
            limit,
        )
        self._live[execution.execution_id] = live
        return live

    def _save(self, live: _Live, **changes: Any) -> None:
        live.execution = live.execution.model_copy(update=changes)
        self.store.update_execution(live.execution)

    def _changed(self, live: _Live) -> None:
        changed, live.changed = live.changed, asyncio.get_running_loop().create_future()
        changed.set_result(None)

    def _emit(self, live: _Live, make: Callable[[int, datetime], Event]) -> None:
        seq = live.execution.last_seq + 1
        event = make(seq, _now())
        self.store.add_event(event)
        live.execution = live.execution.model_copy(update={"last_seq": seq})
        self._changed(live)

    def _state(self, live: _Live, state: ExecutionState) -> None:
        execution_id = live.execution.execution_id
        self._emit(
            live,
            lambda seq, time: StateEvent(
                execution_id=execution_id, seq=seq, time=time, state=state
            ),
        )

    def _finish(self, live: _Live, state: ExecutionState, result: Result) -> None:
        """End an execution: its state event, result event and record are stored together."""
        if live.execution.state in TERMINAL_STATES:
            return
        execution_id = live.execution.execution_id
        final, events = self._final(live.execution, state, result)
        self.store.finish(final, events)
        live.execution = final
        self._changed(live)
        environment = self._environments.get(live.environment_id)
        if environment is not None:
            environment.running.discard(execution_id)
            environment.last_used = time.monotonic()
        if live.watchdog is not None and live.watchdog is not asyncio.current_task():
            live.watchdog.cancel()
        if live.cancel_watchdog is not None and live.cancel_watchdog is not asyncio.current_task():
            live.cancel_watchdog.cancel()
        asyncio.get_running_loop().call_later(RETAIN_LIVE, self._live.pop, execution_id, None)

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
        now = _now()
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

    async def _environment(self, environment_id: str) -> _Environment:
        while True:
            environment = self._environments.setdefault(environment_id, _Environment())
            # Marked used before anything waits, so that suspension leaves it alone.
            environment.last_used = time.monotonic()
            if environment.channel is None or environment.channel.closed:
                # Outside the lock: preparing may start a stopped sandbox under the
                # environments' own lock, which suspension holds while it takes this one.
                await self.backend.prepare(environment_id)
            async with environment.lock:
                if self._environments.get(environment_id) is not environment:
                    continue  # released meanwhile, by a suspension or a deletion: start over
                await self._open(environment_id, environment)
            return environment

    async def _open(self, environment_id: str, environment: _Environment) -> None:
        """Start the environment's executor unless one runs. Hold its lock."""
        if environment.channel is None or environment.channel.closed:
            channel = await self.backend.open(
                environment_id,
                partial(self._on_event, environment_id),
                partial(self._on_closed, environment_id),
            )
            self._channels.add(channel)
            try:
                hostname, languages, features = self._describe(environment_id, channel.hello)
                self._observe(environment_id, channel.hello.get("environment"), "")
                declared = self.backend.isolation_tier
                isolation = await probe(channel, declared, self.state_dir)
            except Exception:
                await channel.close()  # never leave an executor running unused
                raise
            if isolation.tier < declared:
                _log.warning(
                    "environment %s: isolation tier %d declared, %d in effect: %s",
                    environment_id,
                    declared,
                    isolation.tier,
                    "; ".join(isolation.evidence.findings),
                )
            environment.location = Location(
                pool_id=self.pool_id,
                backend=self.backend.name,
                sandbox_id=self.backend.sandbox_id(environment_id),
                hostname=hostname,
                isolation_tier=isolation.tier,
                declared_isolation_tier=declared,
                isolation_evidence=isolation.evidence,
            )
            environment.languages = languages
            environment.features = features
            environment.channel = channel

    async def forget_revisions(self, environment_id: str) -> None:
        """Forget a deleted environment's revisions, and delete the lockfiles only they
        named: environments that are gone keep nothing in the store."""
        self.revisions.forget(environment_id)

    def _want_lockfile(self, environment_id: str) -> bool:
        """Ask the executor for the current lockfile if the current revision has none, as
        revision 0 has none at first; return whether it was asked. One request at a time
        for each environment, and nothing waits for it. Executions ask (_ask_lockfile),
        each within its budget: revision 0 gets its lockfile as the first one starts."""
        environment = self._environments.get(environment_id)
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
            reply = await channel.request("lockfile", reply_timeout=LOCKFILE_TIMEOUT)
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

    def _describe(
        self, environment_id: str, hello: Message
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """The host name, kernel languages and features from an executor's ``hello``, checked."""
        where, languages = hello.get("location"), hello.get("languages", [])
        if not isinstance(where, dict) or not isinstance(languages, list):
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor sent a malformed hello")
        hostname = cast(dict[str, Any], where).get("hostname")
        if not isinstance(hostname, str):
            hostname = ""
        hostname = "".join(char for char in hostname[:MAX_HOSTNAME] if char.isprintable())
        if hello.get("not_dumpable") is False:
            _log.info(
                "the executor for %s stays dumpable, so research code in that environment can "
                "reach its file descriptors through /proc (the sandbox refuses sockets to a "
                "process that is not dumpable)",
                environment_id,
            )
        if hello.get("subreaper") is False:
            _log.warning(
                "the executor for %s cannot adopt orphaned processes; a shell call's processes "
                "that leave its session and clear their environment can outlive it",
                environment_id,
            )
        names = (name for name in cast(list[object], languages) if isinstance(name, str))
        raw: object = hello.get("features", [])
        offered = cast(list[object], raw) if isinstance(raw, list) else []
        features = tuple(dict.fromkeys(f for f in offered if isinstance(f, str) and f in FEATURES))
        return (
            hostname or "unknown",
            tuple(dict.fromkeys(n for n in names if n in LANGUAGES)),
            features,
        )

    @contextlib.asynccontextmanager
    async def _using(self, environment_id: str) -> AsyncGenerator[ExecutorChannel]:
        """The environment's executor, counted as in use until the block ends."""
        environment = await self._environment(environment_id)
        channel = environment.channel
        if channel is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"environment {environment_id} has no executor"
            )
        environment.requests += 1
        try:
            yield channel
        finally:
            environment.requests -= 1
            environment.last_used = time.monotonic()

    async def _on_closed(self, environment_id: str, channel: ExecutorChannel) -> None:
        # The executions sent to this executor end with it, whether or not its
        # environment's record still names it.
        for live in list(self._live.values()):
            if live.channel is channel and live.execution.state not in TERMINAL_STATES:
                self._lose(live, "the worker" if self._closing else "the executor")
        environment = self._environments.get(environment_id)
        if environment is not None and environment.channel is channel:
            environment.channel = None

    # Submission and dispatch.

    async def submit(
        self, request: ExecutionRequest, *, origin: str | None = None
    ) -> tuple[Execution, bool]:
        """Return the execution for ``request.operation_id``, creating it if it is new.

        ``origin`` names the provisioning operation the execution is an install step of: a
        managed change its package commands make is recorded as that operation's.
        """
        if self._closing:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the worker is closing")
        encoded = request.model_dump_json()
        if isinstance(request, JobRequest):
            # Omitting a timeout selects policy defaults; explicitly requesting 24h does
            # not. Keep the original semantic request stable across policy changes.
            encoded += ":explicit_timeout=" + str("timeout_seconds" in request.model_fields_set)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        found = self.store.find_operation(request.context.project_id, request.operation_id)
        if found is not None:
            execution_id, existing = found
            if existing != digest:
                raise WorkerError(
                    ErrorKind.CONFLICT,
                    f"operation {request.operation_id} was submitted with a different request",
                )
            return self.get(execution_id), False
        job: JobDetails | None = None
        execution_id = new_id("exe")
        if isinstance(request, JobRequest):
            context = request.context
            if (
                context.environment_id in self._released_environments
                or (context.project_id, context.environment_id, context.session_id)
                in self._closing_sessions
            ):
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
            timeout = (
                request.timeout_seconds
                if "timeout_seconds" in request.model_fields_set
                else default
            )
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
            submitted_at=_now(),
            origin=origin,
            job=job,
        )
        self.store.insert_execution(execution, digest)
        live = self._track(execution, request.output_limit_bytes)
        self._state(live, ExecutionState.QUEUED)
        self.store.update_execution(live.execution)
        self._spawn(self._dispatch(live, request))
        return live.execution, True

    async def _dispatch(self, live: _Live, request: ExecutionRequest) -> None:
        """Send the request to its executor; whatever goes wrong, the execution ends."""
        try:
            await self._send(live, request)
        except WorkerError as error:
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

    def _fail(self, live: _Live, error: ErrorInfo) -> None:
        environment = self._environments.get(live.environment_id)
        if environment is not None:
            environment.running.discard(live.execution.execution_id)
            environment.last_used = time.monotonic()
        self._finish(live, ExecutionState.FAILED, Result(error=error))

    async def _send(self, live: _Live, request: ExecutionRequest) -> None:
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
            "timeout": request.timeout_seconds,
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
        live.dispatched = True
        live.dispatched_at = time.monotonic()
        live.channel = channel
        await channel.request(request.kind, **fields)
        live.watchdog = self._spawn(self._watch(live, request.timeout_seconds))

    async def _watch(self, live: _Live, timeout: float) -> None:
        await asyncio.sleep(timeout + WATCHDOG_GRACE)
        if live.execution.state in TERMINAL_STATES:
            return
        _log.warning("execution %s overran its limit; cancelling it", live.execution.execution_id)
        with contextlib.suppress(WorkerError):
            await self._send_cancel(live)
        await asyncio.sleep(LOST_GRACE)
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
            or live.final is not None  # it ended; only its sync is left
            or live.execution.state in TERMINAL_STATES
        ):
            return
        try:
            if message.get("event") == "revision":
                # Its lockfile is checked in a thread; this executor's next events wait.
                await self._revised(environment_id, live, message)
            else:
                self._apply(live, environment_id, message)
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

    def _apply(self, live: _Live, environment_id: str, message: Message) -> None:
        execution_id = live.execution.execution_id
        kind = message.get("event")
        if kind == "state" and message.get("state") == "running":
            if live.execution.state is ExecutionState.QUEUED:
                update: dict[str, Any] = {"state": ExecutionState.RUNNING, "started_at": _now()}
                # The revision as it started, which waiting for a package command can change.
                location = live.execution.location
                if location is not None and "environment" in message:
                    revision = self._observe(environment_id, message.get("environment"), "")
                    if revision is not None:
                        update["location"] = location.model_copy(
                            update={"environment_revision": str(revision)}
                        )
                live.execution = live.execution.model_copy(update=update)
                self._state(live, ExecutionState.RUNNING)
                self.store.update_execution(live.execution)
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
            live.final = (state, result)
            if live.cancel_watchdog is not None:
                live.cancel_watchdog.cancel()
            live.ended_at = time.monotonic()
            if self.shared is None or live.channel is None:
                self._complete(live, live.notices)
            else:
                if live.watchdog is not None:
                    live.watchdog.cancel()  # the attempt is over; only the sync is left
                    live.watchdog = None
                self._spawn(self._end(live, live.channel))

    async def _end(self, live: _Live, channel: ExecutorChannel) -> None:
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

    def _complete(self, live: _Live, notices: list[str]) -> None:
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
                SHARED_TIMEOUT,
            )
        except Exception as exc:  # the call goes on with shared/ as it is
            _log.warning(
                "syncing the shared files of %s in %s failed: %s",
                context.project_id,
                context.environment_id,
                exc,
            )
            return [_UNSYNCED]

    def _admit(self, live: _Live, size: int) -> bool:
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

    async def _cancel_quietly(self, live: _Live) -> None:
        with contextlib.suppress(WorkerError):
            await self._send_cancel(live)

    def _result(self, environment_id: str, live: _Live, raw: dict[str, Any]) -> Result:
        """The executor's result, checked and bounded by the attempt's output limit."""
        kernel, lost = self._kernel(
            environment_id,
            live.execution.context.session_id,
            raw.get("kernel"),
            raw.get("state_lost") is True,
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
        after = self._revision_after(environment_id, live, raw)
        return Result(
            exit_code=_integer(raw.get("exit_code")),
            signal=_integer(raw.get("signal"), 1, 255),
            status=raw.get("status"),
            execution_count=_integer(raw.get("execution_count"), 0),
            stdout=_stream(raw.get("stdout"), "stdout", live.limit),
            stderr=_stream(raw.get("stderr"), "stderr", live.limit),
            outputs=_outputs(raw.get("outputs"), live.limit),
            error=_error(raw.get("error")),
            kernel=kernel,
            state_lost=lost,
            elapsed_seconds=_seconds(raw.get("elapsed_seconds")),
            sandbox_started_at=_epoch(raw.get("sandbox_started")),
            sandbox_ended_at=_epoch(raw.get("sandbox_ended")),
            written_files=tuple(paths[:MAX_WRITTEN]),
            written_files_truncated=raw.get("written_files_truncated") is True
            or len(paths) > MAX_WRITTEN,
            environment_revision=after,
            overlapping_change=raw.get("environment_changing") is True
            or (before is not None and after is not None and before != after),
            notices=self._notices(environment_id, live),
        )

    def _notices(self, environment_id: str, live: _Live) -> tuple[str, ...]:
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

    def _located(self, environment_id: str, environment: _Environment) -> Location | None:
        """The environment's location, with its current revision."""
        location, record = environment.location, self.store.revision(environment_id)
        if location is None or record is None:
            return location
        return location.model_copy(update={"environment_revision": str(record.revision)})

    def _revision_after(self, environment_id: str, live: _Live, raw: dict[str, Any]) -> str | None:
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

    async def _revised(self, environment_id: str, live: _Live, message: Message) -> None:
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
        self._ask_lockfile(environment_id, live)

    def _ask_lockfile(self, environment_id: str, live: _Live) -> None:
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

    def _kernel(
        self, environment_id: str, session_id: str, raw: object, state_lost: bool
    ) -> tuple[KernelInfo | None, bool]:
        """Map the executor's kernel generation onto one that never goes back."""
        if not isinstance(raw, dict):
            return None, state_lost
        raw = cast(dict[str, Any], raw)
        language = raw.get("language", "python")
        generation = _integer(raw.get("generation", 1), 1)
        if language not in LANGUAGES or generation is None:
            return None, state_lost
        environment = self._environments.get(environment_id)
        channel = environment.channel if environment is not None else None
        incarnation = channel.incarnation if channel is not None else "unknown"
        record = self.store.kernel(environment_id, session_id, language)
        if record is None:
            base = 0
        elif record.incarnation != incarnation:
            # A new executor: its kernels start over, and earlier state is gone.
            base, state_lost = record.last, state_lost or record.last > 0
        else:
            base = record.base
        self.store.save_kernel(
            environment_id, session_id, language, KernelRecord(incarnation, base, base + generation)
        )
        info = {
            "kernel_id": raw.get("kernel_id"),
            "language": language,
            "generation": base + generation,
        }
        try:
            return KernelInfo.model_validate(info), state_lost
        except ValidationError:
            # Not an identifier the worker can report: name the kernel after its session.
            digest = hashlib.sha256(f"{session_id}\0{language}".encode()).hexdigest()[:24]
            return KernelInfo.model_validate({**info, "kernel_id": f"krn-{digest}"}), state_lost

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
            await asyncio.wait_for(cancel_all(), CANCEL_GRACE)
        except (TimeoutError, WorkerError):
            channels: set[ExecutorChannel] = set()
            for job in jobs:
                live = self._live.get(job.execution_id)
                if live is not None and live.execution.state not in TERMINAL_STATES:
                    if live.channel is not None:
                        channels.add(live.channel)
                    self._lose(live, "the executor that did not confirm job cancellation")
            await asyncio.gather(*(channel.close() for channel in channels))

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
        memory = _integer(reply.get("memory_bytes"), 0, 2**62)
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
        stream also ends when the execution is over, whatever ``after`` was.
        """
        self.get(execution_id)
        while True:
            live = self._live.get(execution_id)
            changed = live.changed if live is not None else None
            over = live is None or live.execution.state in TERMINAL_STATES
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
        if live is None or live.execution.state in TERMINAL_STATES:
            return execution
        already_requested = live.cancel_requested
        live.cancel_requested = True
        if live.dispatched and not already_requested:
            if execution.kind == "job":
                live.cancel_watchdog = self._spawn(self._watch_cancel(live))
            try:
                await self._send_cancel(live)
            except WorkerError:
                if live.execution.state not in TERMINAL_STATES:
                    raise
        elif not live.dispatched and execution.kind == "job":
            self._finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
        return live.execution

    async def _watch_cancel(self, live: _Live) -> None:
        """A direct job cancel has its own deadline, independent of the wall-time limit."""
        await asyncio.sleep(CANCEL_GRACE)
        if live.execution.state in TERMINAL_STATES or live.final is not None:
            return
        channel = live.channel
        self._lose(live, "the executor that did not confirm job cancellation")
        if channel is not None:
            await channel.close()

    async def _send_cancel(self, live: _Live) -> None:
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
        context = request.context
        async with self._using(context.environment_id) as channel:
            reply = await channel.request(
                "restart", session=context.session_id, language=request.language, reply_timeout=180
            )
            kernel, _ = self._kernel(
                context.environment_id, context.session_id, reply.get("kernel"), True
            )
            if kernel is None:
                raise WorkerError(ErrorKind.INTERNAL, "the executor did not describe the kernel")
            return kernel

    async def close_session(self, request: SessionCloseRequest) -> SessionClosed:
        """Cancel and reap jobs, then stop kernels. Starts racing the close are refused."""
        context = request.context
        key = (context.project_id, context.environment_id, context.session_id)
        self._closing_sessions[key] = self._closing_sessions.get(key, 0) + 1
        try:
            jobs = [
                job
                for job in self._running_jobs(context.environment_id)
                if job.context.project_id == context.project_id
                and job.context.session_id == context.session_id
            ]
            await self._cancel_jobs(jobs)
            return await self._close_session(request)
        finally:
            if self._closing_sessions[key] == 1:
                self._closing_sessions.pop(key)
            else:
                self._closing_sessions[key] -= 1

    async def _close_session(self, request: SessionCloseRequest) -> SessionClosed:
        context = request.context
        environment = self._environments.get(context.environment_id)
        channel = environment.channel if environment is not None else None
        if environment is None or channel is None or channel.closed:
            return SessionClosed()
        environment.requests += 1
        try:
            reply = await channel.request(
                "close_session", session=context.session_id, reply_timeout=180
            )
        finally:
            environment.requests -= 1
            environment.last_used = time.monotonic()
        raw: object = reply.get("kernels")
        stopped: list[KernelInfo] = []
        for item in cast(list[object], raw) if isinstance(raw, list) else []:
            kernel, _ = self._kernel(context.environment_id, context.session_id, item, False)
            if kernel is not None:
                stopped.append(kernel)
        return SessionClosed(kernels=tuple(stopped))

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
    ) -> _Range:
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
            size, start = _integer(reply.get("size"), 0, 2**62), _integer(reply.get("offset"), 0)
            sha256 = reply.get("sha256")
            if size is None or start != offset or len(data) > length or offset + len(data) > size:
                raise ValueError("a range that was not asked for")
            if hashed and not (isinstance(sha256, str) and len(sha256) == 64):
                raise ValueError("no SHA-256")
        except (KeyError, TypeError, ValueError) as exc:  # binascii.Error is a ValueError
            raise WorkerError(
                ErrorKind.INTERNAL, f"the executor sent a malformed file reply ({exc})"
            ) from exc
        return _Range(
            size=size,
            sha256=sha256 if hashed else None,
            modified=_epoch(reply.get("modified")),
            offset=offset,
            data=data,
            content_b64=encoded,
            eof=reply.get("eof") is True,
        )

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        async with self._using(request.context.environment_id) as channel:
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
        async with self._using(context.environment_id) as channel:
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
                        SHARED_TIMEOUT,
                        notices,
                    )
                finally:  # what the sync found is told even when the write is refused
                    self._tell(context, notices)
            else:
                reply = await channel.request("write", session=context.session_id, **fields)
            return _checked(WriteFileResult, sha256=reply.get("sha256"), size=reply.get("size"))

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        context = request.context
        async with self._using(context.environment_id) as channel:
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
                        modified_at=_epoch(item.get("modified")),
                    )
                )
            return FileListing(
                path=request.path,
                entries=tuple(listed),
                truncated=reply.get("truncated") is True or len(entries) < len(every),
            )

    async def publish(self, request: PublishRequest) -> ArtifactRef:
        """Copy a workspace file into the artifact store, checking it did not change meanwhile.

        The executor hashes the whole file once, with the first range. The
        ranges must then add up to exactly the size it reported, within the
        limit, and the stored bytes must have the hash it reported.
        """
        context = request.context
        async with self._using(context.environment_id) as channel:
            reading = ReadFileRequest(context=context, path=request.path)
            changed = WorkerError(
                ErrorKind.CONFLICT, f"{request.path} changed while it was published"
            )
            writer = self.blobs.writer()
            try:
                found = await self._read_range(channel, reading, 0, MAX_READ_BYTES, hashed=True)
                size, sha256 = found.size, found.sha256
                if size > self.max_artifact_size:
                    limit = self.max_artifact_size
                    raise WorkerError(
                        ErrorKind.INVALID_REQUEST,
                        f"{request.path} is {size} bytes, over the {limit}-byte limit",
                    )
                offset = 0
                while True:
                    writer.write(found.data)
                    offset += len(found.data)
                    if offset == size:
                        break
                    # Short of the end, a range shorter than asked for means the file shrank.
                    if len(found.data) < MAX_READ_BYTES or found.eof:
                        raise changed
                    found = await self._read_range(
                        channel, reading, offset, MAX_READ_BYTES, hashed=False
                    )
                    if found.size != size:
                        raise changed
                if sha256 is None or writer.size != size or not writer.commit(sha256):
                    raise changed
            except BaseException:
                writer.discard()
                raise
            media_type = (
                request.media_type
                or mimetypes.guess_type(request.path)[0]
                or "application/octet-stream"
            )
            artifact = ArtifactRef(
                artifact_id=new_id("art"),
                project_id=context.project_id,
                sha256=sha256,
                size=size,
                media_type=media_type,
                source_path=request.path,
                produced_by=self._producer(
                    context.environment_id, context.session_id, request.path
                ),
                created_at=_now(),
            )
            self.store.insert_artifact(artifact)
            return artifact

    def _producer(self, environment_id: str, session_id: str, path: str) -> str | None:
        """The last finished attempt that wrote ``path``, as the executor observed it.

        A session path names a file of that session only, so only its attempts
        count; any session's attempts count for the shared area.
        """
        shared = path == "shared" or path.startswith("shared/")
        executions = (
            self.store.environment_executions(environment_id)
            if shared
            else self.store.session_executions(environment_id, session_id)
        )
        writers = [
            execution
            for execution in executions
            if execution.result is not None
            and execution.ended_at is not None
            and path in execution.result.written_files
        ]
        if not writers:
            return None
        latest = max(writers, key=lambda execution: execution.ended_at or execution.submitted_at)
        return latest.attempt_id

    def artifact(self, artifact_id: str, project_id: str | None = None) -> ArtifactRef:
        try:
            artifact = self.store.artifact(artifact_id)
        except ValidationError as exc:
            raise WorkerError(
                ErrorKind.INTERNAL, f"the stored record of {artifact_id} is unreadable"
            ) from exc
        if artifact is None or (project_id is not None and artifact.project_id != project_id):
            raise WorkerError(ErrorKind.NOT_FOUND, f"no artifact {artifact_id}")
        return artifact


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
    total = _integer(raw.get("total_bytes"), 0, 2**62)
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
