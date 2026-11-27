# SPDX-License-Identifier: MIT
"""How a background job ends when the platform loses it, walked against a real worker.

Each row of ``ENDINGS`` is one cause of loss, with the job started with ``restart`` or
not. A worker on the local backend's sandbox daemon (or its stdio transport, where the
row says so) runs the job; the test brings about the cause through the worker's own
seams (the daemon killed or stopped, its identity changed, the worker restarted) and
asserts, at the far end:

- the job's final state and lost attempts, against what the agent's own tool texts
  promise (``start_job``'s description and its ``restart`` parameter, O/jobs.py): a job
  started with ``restart`` whose sandbox, executor or worker stops while it runs is
  started again; one started without it ends lost and is not run again (the claims are
  read from the texts, ``claimed``);
- that a restarted job's written files include every attempt's (V3);
- that every loss ends through the same ending, whose error names the logs left and,
  for a job that asked for restart, why it was not restarted (V9);
- what the orchestrator tells the agent of the ending (its job notice's restart text),
  against what happened.

A row that fails today is ``xfail(strict=True)`` with the audit's identifier and with
``raises`` naming the property that fails. Jobs on a profile (job sandboxes) have no
in-process backend: ``PROFILE_ROWS`` says which cells are walked elsewhere or could not
be written.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

import pytest
import pytest_asyncio
from pydantic import ValidationError

from sandbox_science.contracts import Execution, ExecutionContext, ExecutionState, JobRequest
from sandbox_science.orchestrator.jobs import JOB_SPECS, RESTART
from sandbox_science.orchestrator.jobs import _not_restarted as not_restarted
from sandbox_science.testing import eventually
from sandbox_science.worker.errors import WorkerError

from .harness import TEST_TIMEOUTS, Harness, running

pytestmark = pytest.mark.slow

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
TIMEOUTS = replace(
    TEST_TIMEOUTS,
    restart_retry=0.05,
    restart_retry_max=0.2,
    watchdog_grace=0.2,
    lost_grace=0.2,
    cancel_grace=10.0,
)
"""Short waits for a restart and for the watchdog. A cancel the daemon does not confirm
is given longer than the watchdog's own last wait, so that an executor that stops
answering meets the watchdog's ending (W/dispatcher.py:549-559), as one behind a long
gateway outage does."""
SCRIPT: Final = (
    'echo "attempt $SANDBOX_JOB_ATTEMPT" >> attempts; '
    'echo "$SANDBOX_JOB_ATTEMPT" > "wrote-$SANDBOX_JOB_ATTEMPT"; '
    "if [ -e checkpoint ]; then printf resumed; else touch checkpoint; "
    "while [ ! -e stop ]; do sleep 0.05; done; fi"
)
"""Each attempt notes itself and writes a file of its own; the first leaves a checkpoint
and runs until the test writes ``stop``, and an attempt that finds the checkpoint
resumes from it and ends."""

# --- The claims, read from the agent's tool texts --------------------------------------

Part = Literal["sandbox", "executor", "worker"]


def claimed(part: Part, restart: bool) -> Literal["restarted", "lost"]:
    """What the agent is told becomes of a job when ``part`` stops while it runs: from
    ``start_job``'s description and its ``restart`` parameter's."""
    (start,) = [spec for spec in JOB_SPECS if spec.name == "start_job"]
    promised = re.search(r"its (sandbox), (executor) or (worker) stops while it runs", RESTART)
    assert promised is not None, RESTART
    if restart and part in promised.groups():
        return "restarted"
    assert f"If the sandbox, its executor or the {part}" in start.description or (
        "the sandbox, its executor or the worker is lost" in start.description
    )
    return "lost"


# --- The table ------------------------------------------------------------------------

Cause = Literal[
    "executor_incarnation",
    "sandbox_replaced",
    "executor_silent",
    "worker_stdio",
    "worker_daemon",
    "may_still_run",
]


class Violation(AssertionError):  # noqa: N818 - each names the property that fails
    """A property the job's ending must have does not hold."""


class NotAsClaimed(Violation):
    """The job ended otherwise than the agent's tool texts say it does."""


class FilesMissed(Violation):
    """A restarted job's written files leave out an earlier attempt's."""


class NotOneEnding(Violation):
    """The loss did not go through the ending the other losses take."""


@dataclass(frozen=True)
class Known:
    audit: str
    raises: type[Violation]
    reason: str


@dataclass(frozen=True)
class Ending:
    cause: Cause
    part: Part | None
    """What stopped, in the texts' words; ``None`` when nothing the texts name did."""
    restart: bool
    known: Known | None = None


SILENT: Final = Known(
    "V9",
    NotOneEnding,
    "the watchdog finishes the job lost itself, without the log paths or a restart",
)
ENDINGS: Final = (
    Ending("executor_incarnation", "executor", False),
    Ending(
        "executor_incarnation",
        "executor",
        True,
        Known("V3", FilesMissed, "a restarted job's written files are its last attempt's"),
    ),
    Ending("sandbox_replaced", "sandbox", False),
    Ending(
        "sandbox_replaced",
        "sandbox",
        True,
        Known(
            "V4",
            NotAsClaimed,
            "restart is promised for a lost sandbox; a replaced one ends the job lost",
        ),
    ),
    Ending("executor_silent", "executor", False, SILENT),
    Ending(
        "executor_silent",
        "executor",
        True,
        Known("V9", NotAsClaimed, "an executor that stops answering is never restarted"),
    ),
    Ending("worker_stdio", "worker", False),
    Ending(
        "worker_stdio",
        "worker",
        True,
        Known(
            "V12",
            NotAsClaimed,
            "restart is offered whatever the transport, and refused at the call on stdio",
        ),
    ),
    Ending("worker_daemon", None, False),
    Ending("worker_daemon", None, True),
    Ending("may_still_run", None, True),
)
"""``executor_incarnation``: the sandbox daemon dies and a new one starts (W/recovery.py
loses the attempt, gone). ``sandbox_replaced``: it dies and the environment's sandbox is
another when the job could start again (a recreated VM). ``executor_silent``: the daemon
stops answering past the job's limit (a gateway outage; the watchdog). ``worker_stdio``:
the worker restarts and its stdio executor goes with it. ``worker_daemon``: the worker
restarts while the daemon keeps the job (the texts: jobs outlive it). ``may_still_run``:
the worker loses its record of an attempt the same daemon may still run."""

PROFILE_ROWS: Final = {
    ("any", True): "refused by the request's contract (test_a_profile_job_cannot_restart)",
    ("sandbox_evicted", False): "walked over a real worker process with a fake OpenShell in "
    "test_job_sandboxes_process.py; no in-process job sandbox backend exists for this table",
}


def _param(ending: Ending) -> object:
    marks = (
        [
            pytest.mark.xfail(
                strict=True,
                raises=ending.known.raises,
                reason=f"{ending.known.audit}: {ending.known.reason}",
            )
        ]
        if ending.known is not None
        else []
    )
    name = f"{ending.cause}-{'restart' if ending.restart else 'plain'}"
    return pytest.param(ending, id=name, marks=marks)


# --- The walk -------------------------------------------------------------------------


def request(operation: str, *, restart: bool, timeout: float | None = None) -> JobRequest:
    return JobRequest(
        operation_id=operation,
        context=CONTEXT,
        script=SCRIPT,
        purpose="a resumable sweep",
        expected_progress="a checkpoint per cell",
        restart=restart,
        timeout_seconds=timeout,
    )


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True, timeouts=TIMEOUTS) as value:
        yield value


@pytest_asyncio.fixture
async def stdio(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, timeouts=TIMEOUTS) as value:
        yield value


def daemon_pid(worker: Harness, execution_id: str) -> int:
    """The process of the sandbox daemon that runs the job: this test's own."""
    return int(worker.view.live_channel(execution_id).hello["location"]["pid"])


def stop(worker: Harness) -> None:
    (worker.workspace / "stop").touch()


async def ended(worker: Harness, execution_id: str) -> Execution:
    return await asyncio.wait_for(worker.manager.wait(execution_id), 30)


async def bring_about(worker: Harness, ending: Ending, execution_id: str) -> Execution:
    """Cause the row's loss and return the job once it has ended."""
    match ending.cause:
        case "executor_incarnation":
            os.kill(daemon_pid(worker, execution_id), signal.SIGKILL)
        case "sandbox_replaced":
            worker.view.quarantine("env1")  # the job's next attempt waits for its sandbox
            os.kill(daemon_pid(worker, execution_id), signal.SIGKILL)
            if ending.restart:

                def waiting() -> bool:
                    job = worker.manager.get(execution_id).job
                    return job is not None and job.attempt == 2

                await eventually(waiting, timeout=20)
            worker.view.set_lifecycle(daemon_identity=lambda _environment: ("other", "other-id"))
            worker.view.release_quarantine("env1")
        case "executor_silent":
            # The daemon stops answering and its bridge goes, as behind a gateway outage:
            # the cancel at the job's limit reaches no executor, and none reports a result.
            pid = daemon_pid(worker, execution_id)
            os.kill(pid, signal.SIGSTOP)
            await worker.view.live_channel(execution_id).close()
            try:
                return await ended(worker, execution_id)
            finally:
                os.kill(pid, signal.SIGCONT)
                stop(worker)
        case "worker_stdio" | "worker_daemon":
            await worker.restart()
            if ending.cause == "worker_daemon":
                stop(worker)
        case "may_still_run":
            worker.view.lose(execution_id, "the executor with an invalid replay range")
    try:
        return await ended(worker, execution_id)
    finally:
        stop(worker)


@pytest.mark.parametrize("ending", [_param(ending) for ending in ENDINGS])
async def test_a_lost_job_ends_as_its_tool_texts_say(
    ending: Ending, daemon: Harness, stdio: Harness
) -> None:
    worker = stdio if ending.cause == "worker_stdio" else daemon
    limit = 2.0 if ending.cause == "executor_silent" else None
    try:
        job, _ = await worker.manager.submit(
            request(ending.cause, restart=ending.restart, timeout=limit)
        )
    except WorkerError as refused:
        if ending.part is not None and claimed(ending.part, ending.restart) == "restarted":
            raise NotAsClaimed(f"refused at the call: {refused.message}") from refused
        raise
    await running(worker.manager, job.execution_id)
    await eventually((worker.workspace / "checkpoint").exists, timeout=10)
    result = await bring_about(worker, ending, job.execution_id)
    details = result.job
    assert details is not None
    assert result.result is not None
    error = result.result.error.message if result.result.error is not None else ""

    # The ending against what the texts promise.
    expected = "survived" if ending.part is None else claimed(ending.part, ending.restart)
    if ending.cause == "may_still_run":
        expected = "lost"  # nothing the texts name stopped: the daemon may still run it
    if expected == "restarted":
        if result.state is not ExecutionState.SUCCEEDED or not details.attempts:
            raise NotAsClaimed(f"{result.state}, {len(details.attempts)} lost: {error}")
        assert result.result.stdout is not None
        assert result.result.stdout.text == "resumed"
    elif expected == "lost":
        if result.state is not ExecutionState.LOST:
            raise NotAsClaimed(f"{result.state}: {error}")
        assert details.attempts == ()
        assert (worker.workspace / "attempts").read_text() == "attempt 1\n"  # not run again
    else:
        assert result.state is ExecutionState.SUCCEEDED, (result.state, error)
        assert details.attempts == ()

    # One ending for every loss: its logs, and why a job that asked was not restarted.
    if result.state is ExecutionState.LOST:
        if "partial whole logs remain at" not in error:
            raise NotOneEnding(error)
        if details.restart and "not restarted" not in error:
            raise NotOneEnding(error)
        # What the agent's notice says of it (O/jobs.py, _not_restarted).
        told = str(not_restarted(result)["restart"])
        if details.restart:
            assert "the error says why" in told, told
        else:
            assert "not started with restart" in told, told

    # A restarted job's written files are every attempt's.
    if details.attempts:
        written = set(result.result.written_files)
        if not {"checkpoint", "wrote-1"} <= written:
            raise FilesMissed(f"written: {sorted(written)}")


def test_a_profile_job_cannot_restart() -> None:
    """A job sandbox's job is never restarted, and the request says so at once."""
    with pytest.raises(ValidationError, match="never restarted"):
        JobRequest(
            operation_id="profiled",
            context=CONTEXT,
            script="true",
            purpose="p",
            expected_progress="e",
            restart=True,
            profile="large",
        )


def test_the_table_covers_every_cause_with_and_without_restart() -> None:
    causes = {e.cause for e in ENDINGS}
    for cause in causes - {"may_still_run"}:
        assert {e.restart for e in ENDINGS if e.cause == cause} == {False, True}, cause
