# SPDX-License-Identifier: MIT
"""Restarts of lost jobs that asked for them (``JobRequest.restart``).

A job in the session environment whose executor, sandbox or worker stops while it runs
ends ``lost``: its outcome is unknown, and the worker never submits a command again on its
own account. A job started with ``restart`` says that its command is safe to run again and
resumes from its own checkpoints, files in the workspace. ``JobRestarts`` then records the
lost attempt in the job's record (``JobDetails.attempts``: its number, why it was lost, its
logs), puts the same execution back in the queue as its next attempt, and dispatches it once
the environment can be used again: the request the worker stored when the job was accepted,
so the same command, working directory, bindings, write area and time limit, under the
session's context. It is the worker's own act, as recovery is, and needs no client: it runs
whether or not a run of the session is active. Nothing secret is stored for it: the
attempt is admitted under the stored context's authority, never an operator's and never
that of the request whose task noticed the loss, so a newer recovery epoch of the session
refuses it as it refuses any command of the old one.

Which losses restart a job, and every reason one does not, are ``RESTART_POLICY``'s
(``contracts/restarts.py``), which the agent's texts are made from too. Never two attempts
at once: a job restarts only for a loss the table names as one whose attempt is known to
have stopped (its sandbox stopped, a new daemon incarnation, which reaps the old one's
processes, or a daemon no longer holding it), and only where a new incarnation ends every
process of the old: the stdio transport and, unless ``job_restarts_unisolated`` allows it,
the local backend refuse ``restart``.

A restart never follows a cancellation, the command's own end (a non-zero exit, a time
limit) or a job sandbox's job. An attempt counts against ``MAX_JOB_RESTARTS`` once its
command was sent to an executor that took it, or may have; the record keeps at most
``MAX_JOB_ATTEMPTS``. A session close or an environment release cancels a waiting attempt
as any queued job. A waiting attempt runs only in the sandbox the lost one ran in: an
environment the worker gives up, or creates again with another sandbox, ends it ``lost``.
A stopped (suspended) environment holds it until something else starts the environment.
An environment that does not come back within ``ManagerTimeouts.restart_wait`` ends it
``lost``, and so does the worker setting ``job_restarts = false``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, Final

from sandbox_science.contracts import (
    TERMINAL_STATES,
    Clock,
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionRequest,
    ExecutionState,
    JobAttempt,
    JobRequest,
    Result,
    new_id,
)
from sandbox_science.contracts.restarts import RESTART_POLICY, LossCause, Refusal

from .connections import Connection, EnvironmentConnections
from .errors import WorkerError
from .journal import SOURCE_EVENT, ExecutionJournal
from .lifecycle import EnvironmentLifecycle
from .live import REQUEST, Live
from .recovery_fence import admitted, current_admission, operator_scope
from .store import Store
from .timeouts import ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)

REASONS: Final = RESTART_POLICY.refusals
"""Every reason a job is not started again, as the agent is told it."""
OLD_EXECUTOR: Final = REASONS[Refusal.OLD_EXECUTOR]


def attempt_logs(execution_id: str, attempt: int) -> tuple[str, str]:
    """An attempt's whole logs: the first attempt's in ``.jobs/<ID>/``, a later one's in a
    directory of its own there, so that every attempt's stay readable."""
    directory = (
        f".jobs/{execution_id}" if attempt == 1 else f".jobs/{execution_id}/attempt-{attempt}"
    )
    return f"{directory}/stdout.log", f"{directory}/stderr.log"


def sent_attempts(execution: Execution) -> int:
    """How many of a job's lost attempts had their command sent to an executor that took
    it, or may have: what restarts use."""
    job = execution.job
    return sum(attempt.sent for attempt in job.attempts) if job else 0


def attempt_began(execution: Execution, dispatched: datetime | None) -> datetime:
    """When an attempt's time limit began, as far as the worker recorded it: its start, or
    for a later attempt that has not reported one its dispatch (``dispatched``), or failing
    that the loss before it; the job's submission for its first."""
    if execution.started_at is not None:
        return execution.started_at
    job = execution.job
    if job is not None and job.attempts:
        return dispatched or job.attempts[-1].lost_at
    return execution.submitted_at


class JobRestarts:
    """The worker's restarts of lost jobs, for the dispatcher."""

    def __init__(
        self,
        store: Store,
        journal: ExecutionJournal,
        connections: EnvironmentConnections,
        usability: Usability,
        *,
        clock: Clock,
        timeouts: Callable[[], ManagerTimeouts],
        lifecycle: Callable[[], EnvironmentLifecycle],
        connect: Callable[[str], Awaitable[Connection]],
        spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]],
        dispatch: Callable[[Live, ExecutionRequest], Coroutine[Any, Any, None]],
        daemon: bool,
        enabled: bool,
        contained: bool,
    ) -> None:
        self.store = store
        self.journal = journal
        self.connections = connections
        self.usability = usability
        self.clock = clock
        self.timeouts = timeouts
        self.lifecycle = lifecycle
        self.connect = connect
        self.spawn = spawn
        self.dispatch = dispatch
        """Sends an attempt to its executor: the dispatcher's way, which ends it whatever
        goes wrong."""
        self.daemon = daemon
        """Whether executors run under the sandbox daemon, which restarts need."""
        self.enabled = enabled
        """The worker setting ``job_restarts``."""
        self.contained = contained
        """Whether a new daemon is sure to end a lost attempt's processes: where the
        platform recycles the sandbox's VM, or where the operator allowed it on the local
        backend for development (``job_restarts_unisolated``)."""

    def admission_refusal(self, request: JobRequest, hello: dict[str, Any] | None) -> str | None:
        """Why ``start_job`` with ``restart`` is refused at once; ``None`` to accept it.
        ``hello`` is the environment's open executor's, if one is open."""
        if not request.restart:
            return None
        for refused, applies in (
            (Refusal.TURNED_OFF, not self.enabled),
            (Refusal.STDIO, not self.daemon),
            (Refusal.LOCAL_BACKEND, not self.contained),
            (Refusal.OLD_EXECUTOR, hello is not None and hello.get("job_attempts") != 1),
        ):
            if applies:
                return REASONS[refused]
        return None

    def refusal(self, live: Live, cause: LossCause | None) -> str | None:
        """Why the worker does not start a job that asked for ``restart`` again now that it
        lost it for ``cause``; ``None`` when it does (``RESTART_POLICY``)."""
        execution = live.execution
        job = execution.job
        if job is None or not job.restart:
            return REASONS[Refusal.NOT_ASKED]
        for refused, applies in (
            (Refusal.TURNED_OFF, not self.enabled),
            (Refusal.STDIO, not self.daemon),
            (Refusal.LOCAL_BACKEND, not self.contained),
            (Refusal.CANCELLED, live.cancel_requested),
        ):
            if applies:
                return REASONS[refused]
        if cause is None:
            return RESTART_POLICY.loss(LossCause.SAME_DAEMON).why
        loss = RESTART_POLICY.loss(cause)
        if not loss.restarts:
            return loss.why
        runs = sent_attempts(execution) + self.sent(live)
        for refused, applies in (
            (Refusal.RESTARTS_USED, runs >= RESTART_POLICY.max_runs),
            (Refusal.RECORD_FULL, len(job.attempts) >= RESTART_POLICY.max_attempts),
            (Refusal.CLOSING, self.usability.winding_down(execution.context)),
        ):
            if applies:
                return REASONS[refused]
        return None

    def sent(self, live: Live) -> bool:
        """Whether the current attempt's command was sent to an executor that took it."""
        execution = live.execution
        job = execution.job
        return execution.started_at is not None or (
            job is not None and self.store.attempt_sent(execution.execution_id, job.attempt)
        )

    def take(self, live: Live, what: str, cause: LossCause | None) -> bool:
        """Start a lost job again, if it asked for it and may: whether the worker took it.

        ``what`` stopped during the attempt. A worker that is closing leaves the job
        unfinished: the next worker's startup finds it lost and restarts it then.
        """
        execution = live.execution
        job = execution.job
        if (
            job is None
            or not job.restart
            or job.sandbox is not None
            or live.final is not None  # its outcome is known; only its sync was cut short
            or execution.state in TERMINAL_STATES
            or self.refusal(live, cause) is not None
        ):
            return False
        if self.usability.closing:
            return True
        token = SOURCE_EVENT.set(None)  # the queued state is no executor event's
        try:
            self._next_attempt(live, what, cause)
        finally:
            SOURCE_EVENT.reset(token)
        self.spawn(self.start(live))
        return True

    def _next_attempt(self, live: Live, what: str, cause: LossCause | None) -> None:
        """Record the lost attempt and queue the job's next one, in one stored change. Its
        reason is the restart table's for ``cause`` (``RESTART_POLICY``), the one source
        of that wording, then what stopped."""
        execution = live.execution
        job = execution.job
        if job is None:  # checked by ``take``
            return
        now = self.clock.now()
        live.ended_at = self.clock.monotonic()
        # The attempts still running beside it may need to know that it ran, and its own
        # files, which the next attempt finds, are sorted against what ran beside it.
        self.journal.activity.ended(live)
        live.lost_window = self.journal.activity.window(live) if live.dispatched else None
        live.ended_at = 0.0
        reason = f"{what} stopped during this attempt"
        if cause is not None:
            reason = f"{RESTART_POLICY.loss(cause).happened}: {reason}"
        lost = JobAttempt(
            attempt=job.attempt,
            attempt_id=execution.attempt_id,
            sent=self.sent(live),
            started_at=execution.started_at,
            lost_at=now,
            reason=reason[:1024],
            stdout_path=job.stdout_path,
            stderr_path=job.stderr_path,
        )
        number = job.attempt + 1
        stdout, stderr = attempt_logs(execution.execution_id, number)
        details = job.model_copy(
            update={
                "attempt": number,
                "attempts": (*job.attempts, lost),
                "stdout_path": stdout,
                "stderr_path": stderr,
            }
        )
        current = asyncio.current_task()
        for task in (live.watchdog, live.cancel_watchdog):
            if task is not None and task is not current:
                task.cancel()
        environment = self.connections.get(live.environment_id)
        if environment is not None:
            environment.running.discard(execution.execution_id)
        live.watchdog = live.cancel_watchdog = None
        live.dispatched = False
        live.channel = None
        live.running_at = None
        live.time_limit = None
        live.final = None
        live.final_source_seq = None
        live.reattached = False
        live.restored = False
        live.observation, live.observation_gap = None, "missing"
        live.notices = []
        live.execution = execution.model_copy(
            update={
                "attempt_id": new_id("att"),
                "state": ExecutionState.QUEUED,
                "started_at": None,
                "job": details,
            }
        )
        # A new daemon numbers the next attempt's events from one again.
        self.store.restart_recovery(execution.execution_id)
        self.journal.state(live, ExecutionState.QUEUED)
        _log.warning(
            "job %s: attempt %d lost (%s); attempt %d waits for its environment",
            execution.execution_id,
            job.attempt,
            what,
            number,
        )

    def held(self, live: Live) -> bool:
        """Whether ``live`` is a restarted job's attempt that waits for its environment and
        was never dispatched: a closing worker leaves it for the next one."""
        job = live.execution.job
        return (
            job is not None
            and job.restart
            and bool(job.attempts)
            and not live.dispatched
            and live.execution.state not in TERMINAL_STATES
        )

    def waiting(self, live: Live, saved: tuple[str, str, int, bool] | None) -> bool:
        """Whether a job a restarted worker found unfinished is an attempt that waited for
        its environment and was never sent to a daemon, which ``start`` takes up again."""
        job = live.execution.job
        return (
            job is not None
            and job.restart
            and job.attempt > 1
            and live.execution.state is ExecutionState.QUEUED
            and saved is not None
            and not saved[1]  # no daemon incarnation: never dispatched
        )

    async def start(self, live: Live) -> None:
        """Dispatch the job's next attempt once its environment can be used.

        The task may have been made by a request's task, an operator's among them, whose
        context it copied: it drops that request's operator grant, admission and executor
        event, so that only the job's own stored authority admits the attempt.
        """
        token = SOURCE_EVENT.set(None)
        try:
            with operator_scope(False), admitted(None):
                await self._start(live)
        finally:
            SOURCE_EVENT.reset(token)

    async def _start(self, live: Live) -> None:
        """Wait with a capped backoff for the environment, in the lost attempt's sandbox;
        end the job ``lost`` if it cannot run there in time."""
        job = live.execution.job
        if job is None or not job.attempts:
            return
        execution_id = live.execution.execution_id
        timeouts = self.timeouts()
        deadline = job.attempts[-1].lost_at + timedelta(seconds=timeouts.restart_wait)
        delay = timeouts.restart_retry
        context = live.execution.context
        recorded = self.store.attempt_dispatch(execution_id, job.attempts[-1].attempt)
        sandbox = recorded[0] if recorded is not None else None
        while True:
            if live.execution.state in TERMINAL_STATES or live.dispatched or self.usability.closing:
                return
            if not self.enabled or not self.daemon:
                self.end(live, REASONS[Refusal.TURNED_OFF if not self.enabled else Refusal.STDIO])
                return
            lifecycle = self.lifecycle()
            identity = lifecycle.daemon_identity
            now_bound = identity(live.environment_id) if identity is not None else None
            if sandbox is not None and now_bound is not None and now_bound != sandbox:
                self.end(live, REASONS[Refusal.OTHER_SANDBOX])
                return
            reason = None
            stopped = lifecycle.environment_stopped
            if stopped is not None and stopped(live.environment_id):
                reason = "its environment is stopped; the next use of it starts it"
            elif self.usability.usable(context):
                try:
                    await self.connect(live.environment_id)
                    break
                except WorkerError as error:
                    if error.kind is not ErrorKind.UNAVAILABLE:
                        self.end(live, f"its environment cannot be used ({error.message[:300]})")
                        return
                    reason = error.message
            if live.execution.state in TERMINAL_STATES:
                return  # cancelled, or its environment given up, meanwhile
            if self.clock.now() >= deadline:
                why = f": {reason[:300]}" if reason else ""
                self.end(live, f"{REASONS[Refusal.NOT_BACK]} ({timeouts.restart_wait:g} s){why}")
                return
            await self.clock.sleep(delay)
            delay = min(2 * delay, timeouts.restart_retry_max)
        if live.execution.state in TERMINAL_STATES or live.dispatched:
            return
        saved = self.store.recovery(execution_id)
        try:
            request = REQUEST.validate_json(saved[0]) if saved is not None else None
        except ValueError:
            request = None
        if not isinstance(request, JobRequest):
            self.end(live, REASONS[Refusal.UNREADABLE])
            return
        try:
            # The original start's authority, from its stored context: a newer recovery
            # epoch of the session refuses the restart, as it would a new command.
            with self.store.recovery_fences.bind(context):
                live.admission = current_admission()
                await self.dispatch(live, request)
        except WorkerError as error:
            self.end(live, f"{REASONS[Refusal.AUTHORITY]}: {error.message[:300]}")

    def given_up(self, environment_id: str) -> None:
        """The worker gave up the environment, as its sandbox went: its jobs' attempts that
        wait for it end ``lost``, since the workspace they would resume from went too."""
        for live in list(self.journal.live.values()):
            job = live.execution.job
            if (
                live.environment_id == environment_id
                and job is not None
                and job.attempts
                and not live.dispatched
                and live.execution.state not in TERMINAL_STATES
            ):
                self.end(live, REASONS[Refusal.SANDBOX_GONE])

    def end(self, live: Live, why: str) -> None:
        """End a job whose next attempt cannot run: ``lost``, with every attempt listed."""
        job = live.execution.job
        attempts = len(job.attempts) if job is not None else 0
        message = (
            f"the job was lost {attempts} time(s) and not restarted again: {why}; the whole "
            f"logs of each attempt remain under .jobs/{live.execution.execution_id}/"
        )
        self.journal.finish(
            live,
            ExecutionState.LOST,
            Result(error=ErrorInfo(kind=ErrorKind.INTERNAL, message=message[:4000])),
        )
