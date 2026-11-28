# SPDX-License-Identifier: MIT
"""When the worker starts a lost background job again: the one table of its restart rules.

A job started with ``restart`` says that its command is safe to run again and resumes from
its own checkpoints. Whether the worker starts it again depends on why its attempt was
lost (``LossCause``) and on the conditions after it (``Refusal``). The worker decides from
``RESTART_POLICY``; ``start_job``'s description, the job notices and every "not
restarted: …" reason are made from the same table, so what the agent is told is what
the worker does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .worker import MAX_JOB_ATTEMPTS, MAX_JOB_RESTARTS


class LossCause(StrEnum):
    """Why an attempt of a job ended with its outcome unknown."""

    SANDBOX_STOPPED = "sandbox_stopped"
    NEW_DAEMON = "new_daemon"
    RECORD_GONE = "record_gone"
    NEVER_SENT = "never_sent"
    SAME_DAEMON = "same_daemon"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    EXECUTOR_EXITED = "executor_exited"


class Refusal(StrEnum):
    """Why the worker does not start a lost job again, or ends one whose next attempt waits."""

    NOT_ASKED = "not_asked"
    TURNED_OFF = "turned_off"
    STDIO = "stdio"
    LOCAL_BACKEND = "local_backend"
    OLD_EXECUTOR = "old_executor"
    CANCELLED = "cancelled"
    RESTARTS_USED = "restarts_used"
    RECORD_FULL = "record_full"
    CLOSING = "closing"
    SANDBOX_GONE = "sandbox_gone"
    OTHER_SANDBOX = "other_sandbox"
    NOT_BACK = "not_back"
    AUTHORITY = "authority"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Loss:
    """One row: a cause, whether it restarts the job, and what the agent is told."""

    cause: LossCause
    restarts: bool
    happened: str
    """What happened, completing "a job is started again when …" or "… is not"."""
    why: str = ""
    """Why it is not started again, for a cause that does not restart it."""


@dataclass(frozen=True)
class RestartPolicy:
    """The worker's restart rules, which its decisions and its texts are made from."""

    losses: tuple[Loss, ...]
    refusals: dict[Refusal, str]
    """Each reason a job is not started again, as its error and notices say it."""
    max_runs: int
    """Times the job's command is sent to an executor that took it, at most."""
    max_attempts: int
    """Attempts the job's record keeps, those whose command was never taken among them."""
    wait_hours: int
    """How long a next attempt waits for its environment."""

    def loss(self, cause: LossCause) -> Loss:
        return next(row for row in self.losses if row.cause is cause)

    def restarts(self, cause: LossCause) -> bool:
        return self.loss(cause).restarts

    def description(self) -> str:
        """``start_job``'s ``restart``, said from the table."""
        restarted = "; ".join(row.happened for row in self.losses if row.restarts)
        kept = "; ".join(f"{row.happened} ({row.why})" for row in self.losses if not row.restarts)
        return (
            "Set true only when the command is safe to run again and resumes from its own "
            "checkpoints, files it writes in the workspace as it goes. The platform then "
            f"starts the same command again when its attempt is lost because {restarted}: "
            "with the same working directory, environment and time limit, in the same "
            "sandbox once that can be used again. It is not started again when "
            f"{kept}; when {self.refusals[Refusal.SANDBOX_GONE]}, or "
            f"{self.refusals[Refusal.OTHER_SANDBOX]}, since the files it would resume from "
            f"went with the sandbox; when its environment cannot be used within "
            f"{self.wait_hours} hours; and never after the command fails, times out or is "
            "cancelled. The command is sent to run at most "
            f"{self.max_runs} times in all. The time limit applies to each attempt. Each "
            "attempt has SANDBOX_JOB_ATTEMPT (1, then 2, ...) in its environment and logs of "
            "its own; job_status lists the attempts. A command without checkpoints starts "
            "again from the beginning."
        )

    def restarting(self, attempt: int, runs: int) -> str:
        """A lost attempt's notice: the attempt that follows and the runs left."""
        return (
            f"being restarted as attempt {attempt}, in the same sandbox once that can be "
            f"used; its command has been sent to run {runs} of at most {self.max_runs} "
            "times, and resumes from what its checkpoints left"
        )


_REFUSALS: Final[dict[Refusal, str]] = {
    Refusal.NOT_ASKED: "it was not started with restart, so its command is not run again",
    Refusal.TURNED_OFF: "job restarts are turned off on this worker (job_restarts = false)",
    Refusal.STDIO: (
        "restart needs the sandbox daemon transport, which makes sure a lost attempt's "
        "processes are gone before the next starts; this worker runs its executors over stdio"
    ),
    Refusal.LOCAL_BACKEND: (
        "restart is refused on the local backend: a process that leaves its session and "
        "clears its environment can outlive the daemon, so two attempts could run at once; "
        "a development worker allows it with job_restarts_unisolated = true"
    ),
    Refusal.OLD_EXECUTOR: (
        "this environment's executor predates job restarts; start the job without restart, "
        "or recycle the environment"
    ),
    Refusal.CANCELLED: "it was being cancelled",
    Refusal.RESTARTS_USED: f"its command was sent to run {MAX_JOB_RESTARTS + 1} times already",
    Refusal.RECORD_FULL: f"its record holds {MAX_JOB_ATTEMPTS} lost attempts already",
    Refusal.CLOSING: "its session or its environment's executor is closing",
    Refusal.SANDBOX_GONE: "its sandbox was deleted, so the worker gave its environment up",
    Refusal.OTHER_SANDBOX: (
        "its environment now has another sandbox than the one the lost attempt ran in"
    ),
    Refusal.NOT_BACK: "its environment could not be used again in time",
    Refusal.AUTHORITY: "its session's authority changed (a newer recovery epoch)",
    Refusal.UNREADABLE: "its stored request is unreadable",
}

RESTART_POLICY: Final = RestartPolicy(
    losses=(
        Loss(
            LossCause.SANDBOX_STOPPED,
            True,
            "its sandbox stopped (it runs again only if the same sandbox is started again)",
        ),
        Loss(
            LossCause.NEW_DAEMON,
            True,
            "a new daemon answered in its sandbox, which ended the old daemon's processes",
        ),
        Loss(LossCause.RECORD_GONE, True, "its daemon no longer holds it"),
        Loss(LossCause.NEVER_SENT, True, "a restarted worker found it was never sent"),
        Loss(
            LossCause.SAME_DAEMON,
            False,
            "the daemon that ran it misbehaved or could not be reconciled",
            "the daemon that ran it may still be running it, and a second attempt never "
            "runs beside the first",
        ),
        Loss(
            LossCause.CANCEL_UNCONFIRMED,
            False,
            "its cancellation was never confirmed",
            "it was being cancelled",
        ),
        Loss(
            LossCause.EXECUTOR_EXITED,
            False,
            "a stdio executor or its worker stopped",
            "a stdio executor's processes may outlive it",
        ),
    ),
    refusals=_REFUSALS,
    max_runs=MAX_JOB_RESTARTS + 1,
    max_attempts=MAX_JOB_ATTEMPTS,
    wait_hours=24,
)
"""The worker's restart rules (job restarts)."""
