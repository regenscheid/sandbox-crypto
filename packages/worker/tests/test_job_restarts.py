# SPDX-License-Identifier: MIT
"""A lost job that asked for ``restart`` is started again, as attempts of one execution."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from sandbox_science.contracts import (
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    JobAttempt,
    JobDetails,
    JobRequest,
    SessionCloseRequest,
)
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.contracts.restarts import RESTART_POLICY, LossCause, Refusal
from sandbox_science.contracts.worker import MAX_JOB_RESTARTS
from sandbox_science.testing import eventually
from sandbox_science.worker import sandbox_processes
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.job_restarts import REASONS, attempt_began, sent_attempts
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.recovery_fence import operator_scope
from sandbox_science.worker.store import Store
from sandbox_science.worker.timeouts import MANAGER_TIMEOUTS

from .harness import TEST_TIMEOUTS, Harness, running

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
    assert {"checkpoint", "attempts"} <= {file.path for file in lost.survived}
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
    assert f"not restarted: {REASONS[Refusal.RESTARTS_USED]}" in (result.result.error.message)
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
    assert REASONS[Refusal.SANDBOX_GONE] in result.result.error.message
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
async def test_restart_is_refused_on_the_stdio_transport(stdio: Harness) -> None:
    with pytest.raises(WorkerError, match="sandbox daemon transport") as refused:
        await stdio.manager.submit(request("stdio", RESUMING, restart=True))
    assert refused.value.kind is ErrorKind.UNSUPPORTED


@pytest.mark.asyncio
async def test_a_failure_or_time_limit_of_its_own_is_never_restarted(worker: Harness) -> None:
    manager = worker.manager
    failed, _ = await manager.submit(request("fails", "exit 3", restart=True))
    assert (await ended(worker, failed.execution_id)).state is ExecutionState.FAILED
    timed, _ = await manager.submit(request("slow", "sleep 30", restart=True, timeout_seconds=1))
    ended_timed = await ended(worker, timed.execution_id)
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


@pytest.mark.asyncio
async def test_a_restart_never_takes_the_operator_grant_of_the_request_that_opened_the_channel(
    worker: Harness,
) -> None:
    # An operator's request opens the executor: the channel's tasks copy its context, the
    # loss is noticed in them, and the restart is spawned from them.
    with operator_scope(True):
        await worker.manager.capabilities("env1")
    job = await lost_and_waiting(worker, "operator-opened")
    worker.store.recovery_fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=1))
    worker.view.release_quarantine("env1")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.result is not None
    assert result.result.error is not None
    assert "authority changed" in result.result.error.message
    assert attempts_file(worker) == "attempt 1\n"


@pytest.mark.asyncio
async def test_a_cancel_stored_before_a_crash_holds_for_a_waiting_restart(worker: Harness) -> None:
    job = await lost_and_waiting(worker, "cancelled-then-crash")
    worker.store.cancel_intent(job.execution_id)  # recorded, then the worker crashed
    await worker.restart()
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.CANCELLED
    await asyncio.sleep(0.5)
    assert attempts_file(worker) == "attempt 1\n"


def test_a_later_attempt_counts_its_time_limit_from_its_own_dispatch() -> None:
    base = restarted_record()
    assert base.job is not None
    dispatched = base.job.attempts[-1].lost_at + timedelta(hours=2)
    assert attempt_began(base, dispatched) == dispatched
    assert attempt_began(base, None) == base.job.attempts[-1].lost_at
    first = base.model_copy(update={"job": base.job.model_copy(update={"attempts": ()})})
    assert attempt_began(first, None) == base.submitted_at


def test_only_attempts_whose_command_was_sent_use_up_restarts() -> None:
    base = restarted_record()
    assert base.job is not None
    never = base.job.attempts[0].model_copy(update={"started_at": None, "sent": False})
    taken = never.model_copy(update={"sent": True})  # taken, though it never said it ran
    job = base.job.model_copy(update={"attempts": (never, never, taken)})
    assert sent_attempts(base.model_copy(update={"job": job})) == 1
    assert sent_attempts(base) == 1


@pytest.mark.asyncio
async def test_a_loss_the_same_daemon_may_still_run_is_never_restarted(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("same-daemon", restart=True))
    await running(worker.manager, job.execution_id)
    worker.view.lose(job.execution_id, "the executor with an invalid replay range")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.job is not None
    assert result.job.attempts == ()
    assert result.result is not None
    assert result.result.error is not None
    assert "not restarted: the daemon that ran it may still be running it" in (
        result.result.error.message
    )


@pytest.mark.asyncio
async def test_a_new_daemon_ends_what_a_lost_attempt_left_without_its_mark(
    worker: Harness,
) -> None:
    script = (
        'if [ "$SANDBOX_JOB_ATTEMPT" = 1 ]; then '
        "env -i /bin/sh -c 'echo $$ > orphan; exec sleep 300' & sleep 60; "
        "else printf second; fi"
    )
    job, _ = await worker.manager.submit(request("unmarked", script, restart=True))
    await running(worker.manager, job.execution_id)

    def orphan() -> int | None:
        path = worker.workspace / "orphan"
        text = path.read_text() if path.exists() else ""
        return int(text) if text.endswith("\n") else None

    pid = await eventually(orphan, timeout=10)
    kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.SUCCEEDED
    info = sandbox_processes.process_info(pid)
    assert info is None or info.zombie


@pytest.mark.asyncio
async def test_a_waiting_restart_never_runs_in_another_sandbox(worker: Harness) -> None:
    job = await lost_and_waiting(worker, "recreated")
    worker.view.set_lifecycle(daemon_identity=lambda _environment: ("other", "other-id"))
    worker.view.release_quarantine("env1")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.result is not None
    assert result.result.error is not None
    assert REASONS[Refusal.OTHER_SANDBOX] in result.result.error.message
    assert attempts_file(worker) == "attempt 1\n"


@pytest.mark.asyncio
async def test_a_stopped_environment_holds_a_waiting_restart(worker: Harness) -> None:
    stopped = {"env1": True}
    worker.view.set_lifecycle(environment_stopped=lambda environment: stopped[environment])
    job = await lost_and_waiting(worker, "held")
    worker.view.release_quarantine("env1")
    await asyncio.sleep(1.0)
    assert worker.manager.get(job.execution_id).state is ExecutionState.QUEUED
    stopped["env1"] = False  # another use started the environment
    await attempt_running(worker.manager, job.execution_id, 2)


@pytest.mark.asyncio
async def test_turning_restarts_off_refuses_new_ones_and_ends_waiting_ones(
    worker: Harness,
) -> None:
    job = await lost_and_waiting(worker, "turned-off")
    worker.reconfigure(job_restarts=False)
    await worker.restart()
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert result.result is not None
    assert result.result.error is not None
    assert "job restarts are turned off" in result.result.error.message
    with pytest.raises(WorkerError, match="turned off"):
        await worker.manager.submit(request("refused", restart=True))
    plain, _ = await worker.manager.submit(request("plain", "true"))
    assert (await ended(worker, plain.execution_id)).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_releasing_the_environment_cancels_a_waiting_restart(worker: Harness) -> None:
    job = await lost_and_waiting(worker, "released")
    await worker.manager.release("env1")
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.CANCELLED
    assert attempts_file(worker) == "attempt 1\n"


@pytest.mark.asyncio
async def test_the_bound_holds_across_worker_restarts(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("bounded-restarts", restart=True))
    for started in range(1, MAX_JOB_RESTARTS + 2):

        def runs(count: int = started) -> bool:
            execution = worker.manager.get(job.execution_id)
            return (
                attempts_file(worker).count("attempt") == count
                and execution.state is ExecutionState.RUNNING
            )

        await eventually(runs, timeout=20)
        kill_daemon(worker, job.execution_id)
        if started <= MAX_JOB_RESTARTS:
            await worker.restart()  # wherever the next attempt is: waiting or sent
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.LOST
    assert sent_attempts(result) == MAX_JOB_RESTARTS
    assert attempts_file(worker).count("attempt") == MAX_JOB_RESTARTS + 1


@pytest.mark.asyncio
async def test_each_attempt_has_its_own_output_budget(worker: Harness) -> None:
    # Each attempt streams up to its output limit; the second's output is kept beside the
    # first's in the one execution's events.
    script = (
        "head -c 60000 /dev/zero | tr '\\0' x; "
        'if [ "$SANDBOX_JOB_ATTEMPT" = 1 ]; then touch started-1; sleep 60; fi'
    )
    limit = 1024
    job, _ = await worker.manager.submit(
        request("budget", script, restart=True, output_limit_bytes=limit)
    )

    def streamed() -> int:
        events = worker.store.events_after(job.execution_id, 0)
        return sum(len(e.text) for e in events if e.type == "stream")

    await running(worker.manager, job.execution_id)
    await eventually((worker.workspace / "started-1").exists, timeout=10)
    # The daemon streams the head of the limit live; what it has not sent yet is lost with
    # it, so the first attempt's whole head is stored before the daemon goes.
    await eventually(lambda: streamed() == limit // 2, timeout=10, describe="the head stored")
    kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.SUCCEEDED
    events = worker.store.events_after(job.execution_id, 0)
    queued = [
        e.seq
        for e in events
        if e.type == "state" and getattr(e, "state", None) is (ExecutionState.QUEUED)
    ]
    assert len(queued) == 2
    first = sum(len(e.text) for e in events if e.type == "stream" and e.seq < queued[1])
    second = sum(len(e.text) for e in events if e.type == "stream" and e.seq > queued[1])
    assert first > 0
    assert second == first  # not dropped as past the first attempt's budget


def restarted_record() -> Execution:
    """A job's record with its first attempt lost and its second queued."""
    submitted = datetime(2026, 11, 27, 8, tzinfo=UTC)
    lost = JobAttempt(
        attempt=1,
        attempt_id="att-1",
        started_at=submitted + timedelta(minutes=1),
        lost_at=submitted + timedelta(hours=5),
        reason="the sandbox executor incarnation stopped during this attempt",
        stdout_path=".jobs/exe-1/stdout.log",
        stderr_path=".jobs/exe-1/stderr.log",
        sent=True,
    )
    return Execution(
        execution_id="exe-1",
        attempt_id="att-2",
        operation_id="op",
        kind="job",
        context=CONTEXT,
        state=ExecutionState.QUEUED,
        submitted_at=submitted,
        job=JobDetails(
            purpose="sweep",
            expected_progress="cells",
            timeout_seconds=3600,
            stdout_path=".jobs/exe-1/attempt-2/stdout.log",
            stderr_path=".jobs/exe-1/attempt-2/stderr.log",
            restart=True,
            attempt=2,
            attempts=(lost,),
        ),
    )


@pytest.mark.asyncio
async def test_a_lost_attempts_results_are_kept_with_their_digests(worker: Harness) -> None:
    # Attempt 1 writes its result and a checkpoint and is lost; attempt 2 resumes, skips
    # the result, rewrites one file and deletes another.
    script = (
        'if [ "$SANDBOX_JOB_ATTEMPT" = 1 ]; then '
        "printf r1 > result.txt; printf b1 > both.txt; printf x > removed.txt; "
        "touch checkpoint; sleep 60; "
        "else printf b2 > both.txt; rm removed.txt; fi"
    )
    job, _ = await worker.manager.submit(request("results", script, restart=True))
    await running(worker.manager, job.execution_id)
    await eventually((worker.workspace / "checkpoint").exists, timeout=10)
    kill_daemon(worker, job.execution_id)
    result = await ended(worker, job.execution_id)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.job is not None
    [lost] = result.job.attempts
    assert lost.sent
    assert lost.survived is not None
    found = {file.path: file for file in lost.survived}
    assert found["result.txt"].sha256 == hashlib.sha256(b"r1").hexdigest()
    assert found["both.txt"].sha256 == hashlib.sha256(b"b1").hexdigest()
    assert "removed.txt" in found
    assert result.result is not None
    assert "result.txt" not in result.result.written_files  # attempt 2 never wrote it
    assert "both.txt" in result.result.written_files
    assert "removed.txt" in result.result.deleted_files


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", list(LossCause))
async def test_every_loss_cause_does_what_the_policy_says(
    worker: Harness, cause: LossCause
) -> None:
    loss = RESTART_POLICY.loss(cause)
    # What the agent is told: the description puts each cause on the side the table does.
    restarted, kept = RESTART_POLICY.description().split("It is not started again when")
    assert (loss.happened in restarted) is loss.restarts
    assert (loss.happened in kept) is not loss.restarts
    job, _ = await worker.manager.submit(request(f"cause-{cause}", restart=True))
    await running(worker.manager, job.execution_id)
    worker.view.quarantine("env1")  # a restart waits, so nothing runs beside the attempt
    worker.view.lose(job.execution_id, "the test's", cause)
    execution = worker.manager.get(job.execution_id)
    assert execution.job is not None
    if loss.restarts:
        assert execution.state is ExecutionState.QUEUED
        assert execution.job.attempt == 2
        await worker.manager.cancel(job.execution_id)
    else:
        assert execution.state is ExecutionState.LOST
        assert execution.job.attempts == ()
        assert execution.result is not None
        assert execution.result.error is not None
        assert f"not restarted: {loss.why}" in execution.result.error.message
    worker.view.release_quarantine("env1")


def test_an_attempt_counts_once_an_executor_took_it_or_may_have(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    try:
        now = datetime.now(UTC)
        assert not store.attempt_sent("exe-1", 1)  # never dispatched
        store.attempt_dispatched("exe-1", 1, None, now)
        assert store.attempt_sent("exe-1", 1)  # sent, its answer unknown: it counts
        store.attempt_accepted("exe-1", 1, False)
        assert not store.attempt_sent("exe-1", 1)  # refused: its command never ran
        store.attempt_accepted("exe-1", 1, True)
        assert store.attempt_sent("exe-1", 1)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_is_refused_on_the_local_backend_unless_allowed(tmp_path: Path) -> None:
    async with await Harness.start(
        tmp_path, daemon=True, timeouts=TIMEOUTS, job_restarts_unisolated=False
    ) as strict:
        with pytest.raises(WorkerError) as refused:
            await strict.manager.submit(request("local", restart=True))
        assert str(refused.value.message) == REASONS[Refusal.LOCAL_BACKEND]


def test_a_noted_session_counts_only_while_its_leader_is_the_one_noted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; wait"],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    child = int(process.stdout.readline())
    try:
        leader = sandbox_processes.process_info(process.pid)
        assert leader is not None
        sandbox_processes.note_job_session(state, (process.pid, leader.start + 1))
        assert sandbox_processes.noted_job_sessions(state) == []  # another start time
        sandbox_processes.note_job_session(state, (process.pid, leader.start))
        assert {p.pid for p in sandbox_processes.noted_job_sessions(state)} >= {
            process.pid,
            child,
        }
        os.kill(process.pid, signal.SIGKILL)
        process.wait()
        # Its child lives on in the session, but a gone leader's number may be anyone's.
        assert sandbox_processes.noted_job_sessions(state) == []
        sandbox_processes.forget_job_session(state, (process.pid, leader.start))
        sandbox_processes.forget_job_session(state, (process.pid, leader.start + 1))
        assert not (state / sandbox_processes.JOB_SESSIONS).exists()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_a_job_that_ends_forgets_its_noted_session(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("forgotten", "true", restart=True))
    assert (await ended(worker, job.execution_id)).state is ExecutionState.SUCCEEDED
    noted = worker.workspace.parent.parent / ".executor" / sandbox_processes.JOB_SESSIONS
    assert not noted.exists() or noted.read_text().strip() == ""


def test_the_wait_the_policy_states_is_the_workers() -> None:
    assert RESTART_POLICY.wait_hours * 3600 == MANAGER_TIMEOUTS.restart_wait
    assert RESTART_POLICY.max_runs == MAX_JOB_RESTARTS + 1
