# SPDX-License-Identifier: MIT
"""The dispatcher, for the execution manager: submission, executor events and cancellation.

An execution is stored before it is dispatched, and admitted only once: by its
operation ID, past the package gate and the job limits of the environment's
policy. ``Dispatcher`` sends it to the environment's executor, with the shared
files synced before and after (D79), watches its deadline, and applies what the
executor sends back. What an executor sends is untrusted: each event is checked
and bounded before it changes anything, and every execution reaches a terminal
state whatever the executor does. Cancellation goes only to the executor an
execution was sent to; one that does not confirm it in time is lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    CellRequest,
    Clock,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionRequest,
    ExecutionState,
    JobDetails,
    JobRequest,
    OutputEvent,
    PdfRequest,
    Result,
    RichOutput,
    ShellRequest,
    StateEvent,
    StreamEvent,
    new_id,
)
from sandbox_science.contracts.worker import DEFAULT_OUTPUT_LIMIT, MAX_PATH_BYTES
from sandbox_science.policy.model import Resources

from .channel import ExecutorChannel, Message
from .connections import Connection, EnvironmentConnections
from .errors import ExecutorRefusalError, WorkerError
from .files import UNSYNCED, Files
from .identity import execution_digest, same_execution
from .journal import SOURCE_EVENT, ExecutionJournal
from .kernels import KernelRegistry, require_run_kernels, require_skill_bindings
from .lifecycle import EnvironmentLifecycle
from .live import REQUEST, Live, time_limit
from .output_observations import normalize as normalize_observation
from .package_admission import LEGACY as PACKAGE_GATE_LEGACY
from .package_admission import PackageAdmission
from .package_changes import PackageChanges
from .recovery import DaemonRecovery
from .replies import epoch, error, integer, outputs, seconds, size, stream
from .shared_sync import SharedSync
from .store import Store
from .timeouts import ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)

OUTPUT_BUDGET = 4
"""Stream text and rich output an execution may store as events, in multiples of its
output limit (UTF-8 bytes). An honest executor stays under three: half the limit of each
stream, which can triple when invalid bytes become U+FFFD, plus the limit of outputs."""
MAX_EVENTS = 100_000
"""Stream and output events stored for one execution; later ones are dropped."""
MAX_WRITTEN = 1000


class Dispatcher:
    """Executions from submission to their end, through each environment's executor."""

    def __init__(
        self,
        store: Store,
        connections: EnvironmentConnections,
        usability: Usability,
        journal: ExecutionJournal,
        admission: PackageAdmission,
        kernels: KernelRegistry,
        recovery: DaemonRecovery,
        files: Files,
        packages: PackageChanges,
        *,
        clock: Clock,
        connect: Callable[[str], Awaitable[Connection]],
        spawn: Callable[[Any], asyncio.Task[Any]],
        timeouts: ManagerTimeouts,
        shared: SharedSync | None,
        lifecycle: EnvironmentLifecycle,
    ) -> None:
        self.store = store
        self.connections = connections
        self.usability = usability
        self.journal = journal
        self.admission = admission
        self.kernels = kernels
        self.recovery = recovery
        self.files = files
        self.packages = packages
        self.clock = clock
        self.connect = connect
        """How a request connects to an environment's executor: the manager's way."""
        self.spawn = spawn
        """Starts a background task that the manager owns and cancels when it closes."""
        self.timeouts = timeouts
        self.shared = shared
        """Keeps the project's shared files in step across its environments (D79)."""
        self.lifecycle = lifecycle
        """Admission notices, job limits and bindings, where the worker manages sandboxes."""
        self._noticed: dict[tuple[str, str], datetime] = {}
        """The time of the last admission notice each session got, by environment."""

    async def submit(
        self, request: ExecutionRequest, *, origin: str | None = None
    ) -> tuple[Execution, bool]:
        """Return the execution for ``request.operation_id``, creating it if it is new.

        ``origin`` names the provisioning operation the execution is an install step of: a
        managed change its package commands make is recorded as that operation's.
        """
        if self.usability.closing:
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
            return self.journal.get(execution_id), False
        self.admission.check_origin(request, origin)
        if self.usability.fenced(request.context.environment_id):
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                "the environment is fenced until its sandbox executor is safely recycled",
            )
        job: JobDetails | None = None
        execution_id = new_id("exe")
        if isinstance(request, JobRequest):
            context = request.context
            if self.usability.winding_down(context):
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "the job's session or environment is closing"
                )
            resources = (
                job_resources(context.environment_id)
                if (job_resources := self.lifecycle.job_resources)
                else Resources()
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
            running = self.journal.running_jobs(context.environment_id)
            session = [
                live.execution
                for live in self.journal.live.values()
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
        live = self.journal.track(execution, request.output_limit_bytes)
        self.journal.state(live, ExecutionState.QUEUED)
        self.store.update_execution(live.execution)
        self.spawn(self._dispatch(live, request))
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
        environment = self.connections.get(live.environment_id)
        if environment is not None:
            environment.running.discard(live.execution.execution_id)
            environment.last_used = self.clock.monotonic()
        self.journal.finish(live, ExecutionState.FAILED, Result(error=error))

    async def _send(self, live: Live, request: ExecutionRequest) -> None:
        execution_id = live.execution.execution_id
        if live.cancel_requested or live.execution.state in TERMINAL_STATES:
            self.journal.finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
            return
        environment = await self.connect(request.context.environment_id)
        if live.cancel_requested or live.execution.state in TERMINAL_STATES:
            self.journal.finish(
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
        self.journal.save(
            live, location=self.packages.located(request.context.environment_id, environment)
        )
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
            if request.run is not None:
                fields["run"] = request.run
        if not isinstance(request, PdfRequest) and request.bindings:
            fields["bindings"] = dict(request.bindings)  # the skills' (SK-16)
        channel = environment.channel
        if channel is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
        if "run" in fields:
            require_run_kernels(channel)
        if "bindings" in fields:
            require_skill_bindings(channel)
        environment.running.add(execution_id)
        shared = self.shared
        if shared is not None:
            live.notices += await self.files.sync(shared.down, request.context, channel)
            if live.cancel_requested:  # while the shared files synced
                self.journal.finish(
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
        self.admission.check_origin(request, live.execution.origin)
        if not await self.admission.sync(request.context.environment_id, channel):
            raise WorkerError(ErrorKind.UNAVAILABLE, PACKAGE_GATE_LEGACY)
        gate = self.store.package_gate(request.context.environment_id)
        self.admission.check_origin(request, live.execution.origin)
        if live.execution.origin is not None:
            fields.update(
                package_operation=live.execution.origin, package_generation=gate.generation
            )
        if live.cancel_requested or live.execution.state in TERMINAL_STATES:
            # Cancelled while the package gate was acknowledged: the executor never gets it.
            self.journal.finish(
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
        live.dispatched_at = self.clock.monotonic()
        live.channel = channel
        daemon_identity = self.lifecycle.daemon_identity
        identity = daemon_identity(live.environment_id) if daemon_identity else None
        if channel.daemon_token is not None and (
            self.store.endpoint_incarnation(live.environment_id) != channel.incarnation
            or self.store.endpoint_binding(live.environment_id) != identity
        ):
            identity = None  # never combine this channel with a newer endpoint's identity
        self.store.observation_dispatch(
            live.execution, channel.incarnation, identity, daemon=channel.daemon_token is not None
        )
        live.watchdog = self.spawn(self.watch(live, time_limit(request)))
        await channel.request(request.kind, **fields)
        if channel.daemon_token is not None:
            self.recovery.poll(request.context.environment_id, channel)

    async def watch(self, live: Live, timeout: float) -> None:
        await self.clock.sleep(max(0, timeout + self.timeouts.watchdog_grace))
        if live.execution.state in TERMINAL_STATES or live.final is not None:
            return
        _log.warning("execution %s overran its limit; cancelling it", live.execution.execution_id)
        with contextlib.suppress(WorkerError):
            await self.cancel(live.execution.execution_id)
        await self.clock.sleep(self.timeouts.lost_grace)
        self.journal.finish(
            live,
            ExecutionState.LOST,
            Result(
                error=ErrorInfo(
                    kind=ErrorKind.INTERNAL, message="the executor never reported a result"
                )
            ),
        )

    async def on_event(self, environment_id: str, message: Message) -> None:
        execution_id = message.get("execution")
        live = self.journal.live.get(execution_id) if isinstance(execution_id, str) else None
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
                if message.get("pushed") is True:
                    # An event before it failed here: a replay brings both again.
                    self.recovery.replay_now(live.channel)
                    return
                self.lose(live, "the executor with an invalid replay range")
                return
        token = SOURCE_EVENT.set(
            (live.execution.execution_id, source)
            if isinstance(source, int)
            and not isinstance(source, bool)
            and live.channel.daemon_token
            else None
        )
        try:
            if message.get("event") == "revision":
                # Its lockfile is checked in a thread; this executor's next events wait.
                await self.packages.revised(environment_id, live, message)
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
                self.journal.finish(
                    live,
                    ExecutionState.FAILED,
                    Result(
                        error=ErrorInfo(
                            kind=ErrorKind.INTERNAL, message="the executor's result was unusable"
                        )
                    ),
                )
        finally:
            SOURCE_EVENT.reset(token)

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
                    revision = self.packages.observe(environment_id, message.get("environment"), "")
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
                    kernel, kernel_state_lost = self.kernels.kernel(
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

                source = SOURCE_EVENT.get()
                acknowledged = (
                    source[1] if source is not None and source[0] == execution_id else None
                )
                live.execution = self.store.commit_kernel_start(start, event, acknowledged)
                self.journal.changed(live)
                self.packages.ask_lockfile(environment_id, live)
        elif kind == "stream":
            name = "stderr" if message.get("name") == "stderr" else "stdout"
            text = message.get("text")
            if not isinstance(text, str) or not text or not self._admit(live, size(text)):
                return
            self.journal.emit(
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
            if not self._admit(live, sum(size(value) for value in output.data.values())):
                return
            self.journal.emit(
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
            raw = message.get("result")
            if isinstance(raw, dict) and cast(dict[str, Any], raw).get("bindings_kept") is True:
                live.notices.append(
                    "this kernel holds skill bindings other than this run's, those it started "
                    "with; restart it to have the run's"
                )
            if message.get("recovery_compacted") is True:
                live.notices.append(
                    "The executor retained a compact recovery result after journal pressure; "
                    "whole job stdout/stderr logs remain in the workspace."
                )
            source = SOURCE_EVENT.get()
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
                self.spawn(self._end(live, live.channel))

    async def _end(self, live: Live, channel: ExecutorChannel) -> None:
        """Copy the shared files the attempt changed into the project's copy, then end it (D79).

        A later call in another environment of the project then finds them there.
        """
        notices = list(live.notices)
        shared = self.shared
        if shared is not None:
            notices += await self.files.sync(shared.up, live.execution.context, channel)
        try:
            self._complete(live, notices)
        except Exception:
            # The attempt must end all the same; the watchdog no longer watches it.
            _log.exception(
                "execution %s: its result could not be stored", live.execution.execution_id
            )
            with contextlib.suppress(Exception):
                self.journal.finish(
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
        """End the attempt with the outcome the executor reported, and the sync's notices.

        An attempt that already ended, such as one lost while its sync ran, takes nothing:
        the notices held for its session stay for the session's next result.
        """
        if live.final is None or live.execution.state in TERMINAL_STATES:
            return
        state, result = live.final
        context = live.execution.context
        held = self.files.take_held(context)
        changes: dict[str, Any] = {}
        if held or notices:
            changes["notices"] = (*result.notices, *held, *notices)
        shared = self.shared
        if shared is not None and result.written_files:
            # Files the sync wrote during the call, unchanged since, are not the call's own.
            synced = shared.unchanged_writes(
                live.environment_id, result.written_files, live.dispatched_at, live.ended_at
            )
            if synced:
                changes["written_files"] = tuple(
                    path for path in result.written_files if path not in synced
                )
        self.journal.finish(live, state, result.model_copy(update=changes) if changes else result)

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
                self.spawn(self._cancel_quietly(live))
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
        after = self.packages.revision_after(environment_id, live, raw)
        kernel, lost = self.kernels.kernel(
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
        running = self.kernels.session_kernels(
            live.execution.context, raw.get("running_kernels"), expected_channel=live.channel
        )
        paths, gone = _paths(raw.get("written_files")), _paths(raw.get("deleted_files"))
        location = live.execution.location
        before = location.environment_revision if location is not None else None
        return Result(
            exit_code=integer(raw.get("exit_code")),
            signal=integer(raw.get("signal"), 1, 255),
            status=raw.get("status"),
            execution_count=integer(raw.get("execution_count"), 0),
            stdout=stream(raw.get("stdout"), "stdout", live.limit),
            stderr=stream(raw.get("stderr"), "stderr", live.limit),
            outputs=outputs(raw.get("outputs"), live.limit),
            error=error(raw.get("error")),
            kernel=kernel,
            running_kernels=running,
            state_lost=lost,
            elapsed_seconds=seconds(raw.get("elapsed_seconds")),
            sandbox_started_at=epoch(raw.get("sandbox_started")),
            sandbox_ended_at=epoch(raw.get("sandbox_ended")),
            written_files=tuple(paths[:MAX_WRITTEN]),
            written_files_truncated=raw.get("written_files_truncated") is True
            or len(paths) > MAX_WRITTEN,
            deleted_files=tuple(gone[:MAX_WRITTEN]),
            deleted_files_truncated=raw.get("deleted_files_truncated") is not False
            or len(gone) > MAX_WRITTEN,
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
        notices = self.lifecycle.notices
        if notices is None:
            return ()
        key = (environment_id, live.execution.context.session_id)
        since = self._noticed.get(key, live.execution.submitted_at)
        try:
            fresh = notices(environment_id, since)
        except Exception:
            _log.exception("could not read the admission notices of %s", environment_id)
            return ()
        self._noticed[key] = max([since, *(at for at, _ in fresh)])
        return tuple(text for _, text in fresh)

    async def cancel(self, execution_id: str, project_id: str | None = None) -> Execution:
        execution = self.journal.get(execution_id, project_id)
        live = self.journal.live.get(execution_id)
        if live is None or live.execution.state in TERMINAL_STATES or live.final is not None:
            return execution
        already_requested = live.cancel_requested
        self.store.cancel_intent(execution_id)
        live.cancel_requested = True
        if live.dispatched and not already_requested:
            if execution.kind == "job" or (live.channel is not None and live.channel.daemon_token):
                live.cancel_watchdog = self.spawn(self.watch_cancel(live))
            try:
                await self.send_cancel(live)
            except WorkerError:
                if live.execution.state not in TERMINAL_STATES:
                    raise
        elif not live.dispatched:
            self.journal.finish(
                live,
                ExecutionState.CANCELLED,
                Result(
                    error=ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled before it started")
                ),
            )
        return live.execution

    async def watch_cancel(self, live: Live) -> None:
        """A direct job cancel has its own deadline, independent of the wall-time limit."""
        await self.clock.sleep(self.timeouts.cancel_grace)
        if live.execution.state in TERMINAL_STATES or live.final is not None:
            return
        channel = live.channel
        self.lose(live, "the executor that did not confirm job cancellation")
        if channel is not None:
            await channel.shutdown()

    async def send_cancel(self, live: Live) -> None:
        if live.execution.state not in (ExecutionState.CANCELLING, *TERMINAL_STATES):
            live.execution = live.execution.model_copy(update={"state": ExecutionState.CANCELLING})
            self.journal.state(live, ExecutionState.CANCELLING)
            self.store.update_execution(live.execution)
        # Only the executor it was sent to has it; never start another one to cancel.
        channel = live.channel
        if channel is None or channel.closed:
            return
        await channel.request("cancel", execution=live.execution.execution_id)

    async def cancel_jobs(self, jobs: list[Execution]) -> None:
        """Bound cancel acknowledgments and process completion together, including broken peers."""
        if not jobs:
            return

        async def cancel_all() -> None:
            await asyncio.gather(*(self.cancel(job.execution_id) for job in jobs))
            await asyncio.gather(*(self.journal.wait(job.execution_id) for job in jobs))

        try:
            await asyncio.wait_for(cancel_all(), self.timeouts.cancel_grace)
        except (TimeoutError, WorkerError):
            channels: set[ExecutorChannel] = set()
            for job in jobs:
                live = self.journal.live.get(job.execution_id)
                if live is not None and live.execution.state not in TERMINAL_STATES:
                    if live.channel is not None:
                        channels.add(live.channel)
                    self.lose(live, "the executor that did not confirm job cancellation")
            await asyncio.gather(*(channel.shutdown() for channel in channels))

    def lose(self, live: Live, what: str) -> None:
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
                self._complete(live, [*live.notices, UNSYNCED])
                return
            self.journal.finish(
                live,
                ExecutionState.LOST,
                Result(error=ErrorInfo(kind=ErrorKind.INTERNAL, message=message)),
            )
        except Exception:
            _log.exception("could not report execution %s as lost", live.execution.execution_id)

    async def restore(self, *, daemon_enabled: bool) -> None:
        """Load the executions a previous worker left unfinished, and reattach those its
        daemons still run; the others are lost. Nothing is ever resubmitted."""
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
            live = self.journal.track(
                execution.model_copy(update={"last_seq": last}),
                request.output_limit_bytes if request else DEFAULT_OUTPUT_LIMIT,
            )
            live.events, live.stored = self.store.event_usage(execution_id)
            if saved is not None and saved[1] and daemon_enabled:
                live.dispatched = True
                live.cancel_requested = saved[3] or execution.origin is not None
                recovery.setdefault(execution.context.environment_id, []).append(live)
            else:
                self.lose(live, "the worker that restarted without a recoverable daemon dispatch")
        for environment_id in recovery:
            await self.recovery.recover(environment_id, startup=True)


def _paths(raw: object) -> list[str]:
    """The paths of an executor's file list that a result can carry."""
    return [
        path
        for path in (cast(list[object], raw) if isinstance(raw, list) else [])
        if isinstance(path, str) and path and len(path.encode("utf-8", "replace")) <= MAX_PATH_BYTES
    ]
