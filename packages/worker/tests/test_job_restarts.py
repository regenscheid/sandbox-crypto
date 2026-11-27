# SPDX-License-Identifier: MIT
"""A lost job that asked for ``restart`` is started again, as attempts of one execution."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    SessionCloseRequest,
)
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.contracts.worker import MAX_JOB_RESTARTS
from sandbox_science.testing import eventually
from sandbox_science.worker.manager import ExecutionManager

from .harness import TEST_TIMEOUTS, Harness, running, view

pytestmark = pytest.mark.slow

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
TIMEOUTS = replace(TEST_TIMEOUTS, restart_retry=0.05, restart_retry_max=0.2)

# Each attempt notes its number; the first leaves a checkpoint and waits to be lost, and an
# attempt that finds the checkpoint resumes from it and ends.
RESUMING = (
    'echo "attempt $SANDBOX_JOB_ATTEMPT" >> attempts; '
    "if [ -e checkpoint ]; then printf resumed; else touch checkpoint; sleep 60; fi"
)
# Never ends by itself: each attempt waits to be lost.
FOREVER = (
    'echo "attempt $SANDBOX_JOB_ATTEMPT" >> attempts; touch "started-$SANDBOX_JOB_ATTEMPT"; '
    "sleep 60"
)


def request(operation: str, script: str = FOREVER, **fields: Any) -> JobRequest:
    return JobRequest(
        operation_id=operation,
        context=CONTEXT,
        script=script,
        purpose="test resumable sweep",
        expected_progress="a checkpoint per cell",
        **fields,
    )


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True, timeouts=TIMEOUTS) as value:
        yield value


@pytest_asyncio.fixture
async def stdio(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, timeouts=TIMEOUTS) as value:
        yield value


async def attempt_running(manager: ExecutionManager, execution_id: str, attempt: int) -> None:
    """Until the job's ``attempt`` runs."""

    def runs() -> bool:
        execution = manager.get(execution_id)
        assert execution.job is not None
        return execution.job.attempt == attempt and execution.state is ExecutionState.RUNNING

    await eventually(runs, timeout=20)


def kill_daemon(worker: Harness, execution_id: str) -> None:
    """Lose the job's sandbox daemon, and the job's command with it."""
    channel = worker.view.live_channel(execution_id)
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)


async def ended(worker: Harness, execution_id: str) -> Execution:
    return await asyncio.wait_for(worker.manager.wait(execution_id), 30)


def attempts_file(worker: Harness) -> str:
    path = worker.workspace / "attempts"
    return path.read_text() if path.exists() else ""


def test_a_job_sandbox_job_cannot_ask_for_restart() -> None:
    with pytest.raises(ValidationError, match="never restarted"):
        request("sandboxed", restart=True, profile="large")


@pytest.mark.asyncio
async def test_a_lost_job_restarts_and_its_second_attempt_resumes_from_the_checkpoint(
    worker: Harness,
) -> None:
    job, _ = await worker.manager.submit(request("resume", RESUMING, restart=True))
    first = job.attempt_id
    await running(worker.manager, job.execution_id)
    await eventually(lambda: (worker.workspace / "checkpoint").exists(), timeout=10)
    kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.execution_id == job.execution_id  # one execution, two attempts
    assert result.attempt_id != first
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "resumed"
    details = result.job
    assert details is not None
    assert details.restart
    assert details.attempt == 2
    [lost] = details.attempts
    assert (lost.attempt, lost.attempt_id) == (1, first)
    assert "stopped during this attempt" in lost.reason
    assert lost.started_at is not None
    assert lost.survived is not None
    assert {"checkpoint", "attempts"} <= set(lost.survived)
    assert attempts_file(worker) == "attempt 1\nattempt 2\n"
    # Each attempt's whole logs stay readable, each in its own place.
    assert lost.stdout_path == f".jobs/{job.execution_id}/stdout.log"
    assert details.stdout_path == f".jobs/{job.execution_id}/attempt-2/stdout.log"
    assert (worker.workspace / details.stdout_path).read_text() == "resumed"
    assert (worker.workspace / lost.stdout_path).exists()
    # The written files are the last attempt's own.
    assert "checkpoint" not in result.result.written_files
    events = worker.store.events_after(job.execution_id, 0)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert sum(event.type == "result" for event in events) == 1


@pytest.mark.asyncio
async def test_a_lost_job_that_did_not_ask_ends_lost_and_never_runs_again(
    worker: Harness,
) -> None:
    job, _ = await worker.manager.submit(request("plain", RESUMING))
    await running(worker.manager, job.execution_id)
    await eventually(lambda: (worker.workspace / "checkpoint").exists(), timeout=10)
    kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.job is not None
    assert (result.job.attempt, result.job.attempts) == (1, ())
    assert result.result is not None
    assert result.result.error is not None
    assert "not restarted" not in result.result.error.message
    await asyncio.sleep(0.5)
    assert attempts_file(worker) == "attempt 1\n"


@pytest.mark.asyncio
async def test_restarts_stop_at_their_bound_and_the_job_ends_lost_with_every_attempt(
    worker: Harness,
) -> None:
    job, _ = await worker.manager.submit(request("bounded", restart=True))
    for attempt in range(1, MAX_JOB_RESTARTS + 2):
        await attempt_running(worker.manager, job.execution_id, attempt)
        started = worker.workspace / f"started-{attempt}"
        await eventually(started.exists, timeout=10)
        kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.job is not None
    assert result.job.attempt == MAX_JOB_RESTARTS + 1
    assert [a.attempt for a in result.job.attempts] == list(range(1, MAX_JOB_RESTARTS + 1))
    assert result.result is not None
    assert result.result.error is not None
    assert f"not restarted: its {MAX_JOB_RESTARTS} restarts are used up" in (
        result.result.error.message
    )
    expected = "".join(f"attempt {n}\n" for n in range(1, MAX_JOB_RESTARTS + 2))
    assert attempts_file(worker) == expected


async def lost_and_waiting(worker: Harness, operation: str) -> Execution:
    """A restartable job whose first attempt is lost while its environment cannot be
    used (quarantined), so that its second attempt waits."""
    job, _ = await worker.manager.submit(request(operation, restart=True))
    await running(worker.manager, job.execution_id)
    await eventually((worker.workspace / "started-1").exists, timeout=10)
    worker.view.quarantine("env1")
    kill_daemon(worker, job.execution_id)

    def waiting() -> bool:
        execution = worker.manager.get(job.execution_id)
        return execution.job is not None and execution.job.attempt == 2

    await eventually(waiting, timeout=20)
    queued = worker.manager.get(job.execution_id)
    assert queued.state is ExecutionState.QUEUED
    return queued


@pytest.mark.asyncio
async def test_cancelling_a_job_while_its_restart_waits_ends_it_and_nothing_runs(
    worker: Harness,
) -> None:
    job = await lost_and_waiting(worker, "cancelled")
    await worker.manager.cancel(job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.CANCELLED
    assert result.result is not None
    assert result.result.error is not None
    assert "cancel_job" in result.result.error.message
    worker.view.release_quarantine("env1")
    await asyncio.sleep(1.0)
    assert worker.manager.get(job.execution_id).state is ExecutionState.CANCELLED
    assert attempts_file(worker) == "attempt 1\n"


@pytest.mark.asyncio
async def test_closing_the_session_cancels_a_waiting_restart(worker: Harness) -> None:
    job = await lost_and_waiting(worker, "closed")
    worker.view.release_quarantine("env1")  # the close reaches the environment
    await worker.manager.close_session(SessionCloseRequest(context=CONTEXT))
    result = await ended(worker, job.execution_id)
    if result.state is ExecutionState.CANCELLED:
        assert result.result is not None
        assert result.result.error is not None
        assert "its session's close" in result.result.error.message
    else:  # the restart raced the close and ran first: the close cancelled it then
        assert result.state is ExecutionState.CANCELLED
    assert result.job is not None
    assert len(result.job.attempts) == 1


@pytest.mark.asyncio
async def test_a_given_up_environment_ends_the_waiting_restart_lost(worker: Harness) -> None:
    job = await lost_and_waiting(worker, "given-up")
    worker.manager.environment_given_up("env1")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.result is not None
    assert result.result.error is not None
    assert "given up" in result.result.error.message
    assert result.job is not None
    assert len(result.job.attempts) == 1


@pytest.mark.asyncio
async def test_a_restart_waiting_across_a_worker_restart_waits_on_without_a_new_loss(
    worker: Harness,
) -> None:
    job = await lost_and_waiting(worker, "across")
    await worker.restart()  # the new worker's environment is not quarantined
    await attempt_running(worker.manager, job.execution_id, 2)
    execution = worker.manager.get(job.execution_id)
    assert execution.job is not None
    assert len(execution.job.attempts) == 1
    await eventually((worker.workspace / "started-2").exists, timeout=10)
    assert attempts_file(worker) == "attempt 1\nattempt 2\n"


@pytest.mark.asyncio
async def test_a_worker_restart_without_a_daemon_restarts_the_job(stdio: Harness) -> None:
    manager = stdio.manager
    job, _ = await manager.submit(request("stdio", RESUMING, restart=True))
    await running(manager, job.execution_id)
    await eventually(lambda: (stdio.workspace / "checkpoint").exists(), timeout=10)
    await manager.close()  # the executor stops with the worker
    view(manager).reopen()  # as a fresh worker process starts
    await manager.start()
    result = await asyncio.wait_for(manager.wait(job.execution_id), 30)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.job is not None
    assert result.job.attempt == 2
    [lost] = result.job.attempts
    assert "restarted without a recoverable daemon dispatch" in lost.reason


@pytest.mark.asyncio
async def test_a_failure_or_time_limit_of_its_own_is_never_restarted(stdio: Harness) -> None:
    manager = stdio.manager
    failed, _ = await manager.submit(request("fails", "exit 3", restart=True))
    timed, _ = await manager.submit(request("slow", "sleep 30", restart=True, timeout_seconds=1))
    assert (await asyncio.wait_for(manager.wait(failed.execution_id), 30)).state is (
        ExecutionState.FAILED
    )
    ended_timed = await asyncio.wait_for(manager.wait(timed.execution_id), 30)
    assert ended_timed.state is ExecutionState.TIMED_OUT
    for execution in (manager.get(failed.execution_id), ended_timed):
        assert execution.job is not None
        assert execution.job.attempt == 1


@pytest.mark.asyncio
async def test_the_first_attempt_is_told_its_number(stdio: Harness) -> None:
    job, _ = await stdio.manager.submit(request("told", 'printf "$SANDBOX_JOB_ATTEMPT"'))
    result = await asyncio.wait_for(stdio.manager.wait(job.execution_id), 30)
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "1"


@pytest.mark.asyncio
async def test_a_newer_recovery_epoch_of_the_session_refuses_the_restart(
    worker: Harness,
) -> None:
    job = await lost_and_waiting(worker, "superseded")
    worker.store.recovery_fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=1))
    worker.view.release_quarantine("env1")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.result is not None
    assert result.result.error is not None
    assert "authority changed" in result.result.error.message
    assert attempts_file(worker) == "attempt 1\n"
