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
attempt is admitted under the stored context's authority, which a newer recovery epoch of
the session refuses as it refuses any command of the old one.

A restart never follows a cancellation, the command's own end (a non-zero exit, a time
limit) or a job sandbox's job, and stops at ``MAX_JOB_RESTARTS``. A session close or an
environment release cancels a waiting attempt as any queued job. An environment the worker
gives up, because its sandbox is gone, ends its waiting attempts ``lost``: the workspace they
would resume from went with the sandbox. An environment that does not come back within
``ManagerTimeouts.restart_wait`` ends them too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from typing import Any

from sandbox_science.contracts import (
    TERMINAL_STATES,
    Clock,
    ErrorInfo,
    ErrorKind,
    ExecutionRequest,
    ExecutionState,
    JobAttempt,
    JobRequest,
    Result,
    new_id,
)
from sandbox_science.contracts.worker import MAX_JOB_RESTARTS

from .connections import Connection, EnvironmentConnections
from .errors import WorkerError
from .journal import ExecutionJournal
from .live import REQUEST, Live
from .recovery_fence import current_admission
from .store import Store
from .timeouts import ManagerTimeouts
from .usability import Usability

_log = logging.getLogger(__name__)


def attempt_logs(execution_id: str, attempt: int) -> tuple[str, str]:
    """An attempt's whole logs: the first attempt's in ``.jobs/<ID>/``, a later one's in a
    directory of its own there, so that every attempt's stay readable."""
    directory = (
        f".jobs/{execution_id}" if attempt == 1 else f".jobs/{execution_id}/attempt-{attempt}"
    )
    return f"{directory}/stdout.log", f"{directory}/stderr.log"


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
        timeouts: ManagerTimeouts,
        connect: Callable[[str], Awaitable[Connection]],
        spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]],
        dispatch: Callable[[Live, ExecutionRequest], Coroutine[Any, Any, None]],
    ) -> None:
        self.store = store
        self.journal = journal
        self.connections = connections
        self.usability = usability
        self.clock = clock
        self.timeouts = timeouts
        self.connect = connect
        self.spawn = spawn
        self.dispatch = dispatch
        """Sends an attempt to its executor: the dispatcher's way, which ends it whatever
        goes wrong."""

    def refusal(self, live: Live) -> str | None:
        """Why the worker does not start a job that asked for ``restart`` again now that it
        lost it; ``None`` when it does. Not asked for a job that did not ask."""
        job = live.execution.job
        if job is None or not job.restart:
            return "it did not ask for restart"
        if live.cancel_requested:
            return "it was being cancelled"
        if len(job.attempts) >= MAX_JOB_RESTARTS:
            return f"its {MAX_JOB_RESTARTS} restarts are used up"
        if self.usability.winding_down(live.execution.context):
            return "its session or its environment's executor is closing"
        return None

    def take(self, live: Live, what: str) -> bool:
        """Start a lost job again, if it asked for it: whether the worker took it.

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
            or self.refusal(live) is not None
        ):
            return False
        if self.usability.closing:
            return True
        self._next_attempt(live, what)
        self.spawn(self.start(live))
        return True

    def _next_attempt(self, live: Live, what: str) -> None:
        """Record the lost attempt and queue the job's next one, in one stored change."""
        execution = live.execution
        job = execution.job
        if job is None:  # checked by ``take``
            return
        now = self.clock.now()
        live.ended_at = self.clock.monotonic()
        # The attempts still running beside it may need to know that it ran.
        self.journal.activity.ended(live)
        live.ended_at = 0.0
        lost = JobAttempt(
            attempt=job.attempt,
            attempt_id=execution.attempt_id,
            started_at=execution.started_at,
            lost_at=now,
            reason=f"{what} stopped during this attempt"[:1024],
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

    def pending(self, live: Live, saved: tuple[str, str, int, bool] | None) -> bool:
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
            and not saved[3]
        )

    async def start(self, live: Live) -> None:
        """Dispatch the job's next attempt once its environment can be used, waiting with a
        capped backoff; end it ``lost`` if the environment does not come back in time."""
        job = live.execution.job
        if job is None or not job.attempts:
            return
        deadline = job.attempts[-1].lost_at + timedelta(seconds=self.timeouts.restart_wait)
        delay = self.timeouts.restart_retry
        context = live.execution.context
        while True:
            if live.execution.state in TERMINAL_STATES or live.dispatched or self.usability.closing:
                return
            reason = None
            if self.usability.usable(context):
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
                why = f" ({reason[:300]})" if reason else ""
                self.end(
                    live,
                    "its environment could not be used again within "
                    f"{self.timeouts.restart_wait:g} s{why}",
                )
                return
            await self.clock.sleep(delay)
            delay = min(2 * delay, self.timeouts.restart_retry_max)
        if live.execution.state in TERMINAL_STATES or live.dispatched:
            return
        saved = self.store.recovery(live.execution.execution_id)
        try:
            request = REQUEST.validate_json(saved[0]) if saved is not None else None
        except ValueError:
            request = None
        if not isinstance(request, JobRequest):
            self.end(live, "its stored request is unreadable")
            return
        try:
            # The original start's authority, from its stored context: a newer recovery
            # epoch of the session refuses the restart, as it would a new command.
            with self.store.recovery_fences.bind(context):
                live.admission = current_admission()
                await self.dispatch(live, request)
        except WorkerError as error:
            self.end(live, f"its session's authority changed ({error.message[:300]})")

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
                self.end(live, "its environment was given up, as its sandbox is gone")

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
