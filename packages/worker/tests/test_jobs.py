# SPDX-License-Identifier: MIT
"""D83 jobs use the execution lifecycle while bounded output and whole logs stay separate."""

from __future__ import annotations

import asyncio
import base64
import errno
import os
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import (
    ExecutionContext,
    ExecutionState,
    JobRequest,
    ReadFileRequest,
    SessionCloseRequest,
    ShellRequest,
)
from sandbox_science.policy.model import Resources
from sandbox_science.worker import executor
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


@pytest_asyncio.fixture
async def jobs(tmp_path: Path) -> AsyncIterator[ExecutionManager]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), tmp_path),
        Blobs(tmp_path / "blobs"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=tmp_path,
    )
    await manager.start()
    yield manager
    await manager.close()
    store.close()


def request(operation: str, script: str = "sleep 60", **fields: Any) -> JobRequest:
    return JobRequest(
        operation_id=operation,
        context=CONTEXT,
        script=script,
        purpose="test parameter search",
        expected_progress="periodic progress",
        **fields,
    )


async def running(manager: ExecutionManager, execution_id: str) -> None:
    async with asyncio.timeout(15):
        async for event in manager.events(execution_id, 0):
            if getattr(event, "state", None) is ExecutionState.RUNNING:
                return
    raise AssertionError("job never ran")


@pytest.mark.asyncio
async def test_job_runs_alongside_call_with_full_logs_and_inline_tail(
    jobs: ExecutionManager,
) -> None:
    job, created = await jobs.submit(
        request(
            "logs",
            "printf start; sleep 0.5; python -c 'print(\"x\"*5000)'; "
            "printf end; printf warning >&2",
            output_limit_bytes=1024,
        )
    )
    assert created
    assert job.job is not None
    assert job.job.timeout_seconds == 86400
    await running(jobs, job.execution_id)
    live = await jobs.job_status(job.execution_id)
    assert live.stdout is not None
    assert "start" in live.stdout.text
    assert live.cpu_seconds is not None
    assert live.memory_bytes is not None
    call, _ = await jobs.submit(
        ShellRequest(operation_id="parallel", context=CONTEXT, script="echo foreground")
    )
    assert (await jobs.wait(call.execution_id)).state is ExecutionState.SUCCEEDED
    ended = await jobs.wait(job.execution_id)
    assert ended.result is not None
    assert ended.result.stdout is not None
    assert ended.result.stdout.truncated
    assert ended.result.stdout.text.endswith("end")
    data = await jobs.read_file(ReadFileRequest(context=CONTEXT, path=job.job.stdout_path))
    content = base64.b64decode(data.content_b64)
    assert content == b"start" + b"x" * 5000 + b"\nend"
    assert job.job.stdout_path in ended.result.written_files
    assert (await jobs.job_status(job.execution_id)).stdout == ended.result.stdout
    listed = jobs.jobs(CONTEXT)
    assert [item.execution_id for item in listed.jobs] == [job.execution_id]
    assert jobs.jobs(CONTEXT.model_copy(update={"project_id": "other"})).jobs == ()


@pytest.mark.asyncio
async def test_queued_jobs_reserve_capacity_before_dispatch_and_retry(
    jobs: ExecutionManager,
) -> None:
    submitted = [await jobs.submit(request(f"slot-{i}")) for i in range(4)]
    again, created = await jobs.submit(request("slot-0"))
    assert not created
    assert again.execution_id == submitted[0][0].execution_id
    with pytest.raises(WorkerError, match=r"concurrency limit.*active jobs"):
        await jobs.submit(request("overflow"))
    with pytest.raises(WorkerError, match="different request"):
        await jobs.submit(request("slot-0", "echo changed"))
    activity = await jobs.activity("env1")
    assert activity is not None
    assert activity.running >= 4
    assert not await jobs.release_if_idle("env1", float("inf"))
    await jobs.cancel(submitted[0][0].execution_id)
    replacement, _ = await jobs.submit(request("replacement"))
    assert replacement.state is ExecutionState.QUEUED
    await jobs.close_session(SessionCloseRequest(context=CONTEXT))
    for job, _ in submitted:
        assert (await jobs.wait(job.execution_id)).state is ExecutionState.CANCELLED
    assert (await jobs.wait(replacement.execution_id)).state is ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_environment_limit_includes_other_sessions(jobs: ExecutionManager) -> None:
    for number in range(8):
        await jobs.submit(
            request(f"env-{number}").model_copy(
                update={"context": CONTEXT.model_copy(update={"session_id": f"s{number}"})}
            )
        )
    with pytest.raises(WorkerError, match="concurrency limit"):
        await jobs.submit(request("overflow-env"))
    await jobs.release("env1")
    assert all(
        job.state is ExecutionState.CANCELLED for job in jobs.store.environment_executions("env1")
    )
    fresh, _ = await jobs.submit(request("after-release", "true"))
    assert (await jobs.wait(fresh.execution_id)).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_job_timeout_uses_own_policy_and_keeps_output(jobs: ExecutionManager) -> None:
    jobs.job_resources = lambda _: Resources(
        wall_time=timedelta(seconds=1),
        job_wall_time_default=timedelta(seconds=2),
        job_wall_time_max=timedelta(seconds=3),
        jobs_per_session=1,
    )
    job, _ = await jobs.submit(request("timeout", "printf started; sleep 60", timeout_seconds=0.1))
    ended = await jobs.wait(job.execution_id)
    assert ended.state is ExecutionState.TIMED_OUT
    assert ended.result is not None
    assert ended.result.stdout is not None
    assert ended.result.stdout.text == "started"
    with pytest.raises(WorkerError, match="policy maximum"):
        await jobs.submit(request("too-long", timeout_seconds=4))
    default, _ = await jobs.submit(request("default", "true"))
    assert default.job is not None
    assert default.job.timeout_seconds == 2
    await jobs.wait(default.execution_id)
    jobs.job_resources = lambda _: Resources(job_wall_time_max=timedelta(seconds=1))
    clamped, _ = await jobs.submit(request("clamped", "true"))
    assert clamped.job is not None
    assert clamped.job.timeout_seconds == 1
    await jobs.wait(clamped.execution_id)


@pytest.mark.asyncio
async def test_close_cancels_jobs_but_foreground_calls_stay(jobs: ExecutionManager) -> None:
    job, _ = await jobs.submit(request("close-job", "printf running; sleep 60"))
    await running(jobs, job.execution_id)
    call, _ = await jobs.submit(
        ShellRequest(operation_id="stay", context=CONTEXT, script="sleep 0.2; echo stayed")
    )
    await jobs.close_session(SessionCloseRequest(context=CONTEXT))
    assert (await jobs.wait(job.execution_id)).state is ExecutionState.CANCELLED
    assert (await jobs.wait(call.execution_id)).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_restart_reports_lost_and_never_replays(
    jobs: ExecutionManager, tmp_path: Path
) -> None:
    job, _ = await jobs.submit(request("lost", "printf once; sleep 60"))
    await running(jobs, job.execution_id)
    await jobs.close()
    jobs._closing = False  # a fresh worker has this value
    await jobs.start()
    ended = jobs.get(job.execution_id)
    assert ended.state is ExecutionState.LOST
    assert ended.job == job.job
    same, created = await jobs.submit(request("lost", "printf once; sleep 60"))
    assert same.execution_id == job.execution_id
    assert not created


@pytest.mark.asyncio
async def test_jobs_http_queries_validate_scope_and_limits(
    jobs: ExecutionManager, aiohttp_client: Any
) -> None:
    client: TestClient[Any, Any] = await aiohttp_client(create_app(jobs, "test-secret-jobs"))
    headers = {"Authorization": "Bearer test-secret-jobs"}
    body = request("api", "echo api").model_dump(mode="json")
    response = await client.post("/v1/executions", json=body, headers=headers)
    assert response.status == 201
    job = await response.json()
    await jobs.wait(job["execution_id"])
    query = dict(CONTEXT.model_dump(exclude_none=True), limit="1")
    response = await client.get("/v1/jobs", params=query, headers=headers)
    assert response.status == 200
    assert len((await response.json())["jobs"]) == 1
    response = await client.get("/v1/jobs", params={**query, "limit": "0"}, headers=headers)
    assert response.status == 400
    response = await client.get(
        f"/v1/jobs/{job['execution_id']}", params={"project_id": "other"}, headers=headers
    )
    assert response.status == 404
    response = await client.get(f"/v1/jobs/{job['execution_id']}", headers=headers)
    assert response.status == 200
    assert (await response.json())["stdout"]["text"] == "api\n"
    call, _ = await jobs.submit(
        ShellRequest(operation_id="non-job", context=CONTEXT, script="true")
    )
    response = await client.get(f"/v1/jobs/{call.execution_id}", headers=headers)
    assert response.status == 404


def test_job_logs_refuse_symlink_and_preexisting_job_paths(tmp_path: Path) -> None:
    async def send(_: dict[str, Any]) -> None:
        return None

    instance = executor.Executor(tmp_path, send)
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (session / ".jobs").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, executor.ToolError)):
        instance.job_logs("s1", "exe-" + "a" * 24)
    assert list(outside.iterdir()) == []
    (session / ".jobs").unlink()
    (session / ".jobs").mkdir()
    (session / ".jobs" / ("exe-" + "a" * 24)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError):
        instance.job_logs("s1", "exe-" + "a" * 24)


@pytest.mark.asyncio
async def test_disk_write_failure_ends_job_and_preserves_bounded_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    instance = executor.Executor(tmp_path, send)

    def full(_fd: int, _data: bytes) -> None:
        raise OSError(errno.ENOSPC, "synthetic full job disk")

    monkeypatch.setattr(executor, "write_log", full)
    execution = "exe-" + "b" * 24
    await instance.op_shell(
        {
            "op": "job",
            "execution": execution,
            "session": "s1",
            "script": "printf before-full; sleep 60",
            "limit": 1024,
        }
    )
    running_job = instance.running[execution]
    assert running_job.task is not None
    await asyncio.wait_for(running_job.task, 15)
    end = next(event for event in events if event.get("event") == "end")
    assert end["state"] == "failed"
    assert end["result"]["stdout"]["text"] == "before-full"
    assert "whole logs may be incomplete" in end["result"]["error"]["message"]
    assert running_job.process is not None
    assert running_job.process.returncode is not None
    await instance.shutdown()


@pytest.mark.asyncio
async def test_session_quota_spans_environments_and_cursor_is_scoped(
    jobs: ExecutionManager,
) -> None:
    jobs.job_resources = lambda _: Resources(jobs_per_session=1)
    first, _ = await jobs.submit(request("first"))
    other_environment = request("second").model_copy(
        update={"context": CONTEXT.model_copy(update={"environment_id": "env2"})}
    )
    with pytest.raises(WorkerError, match="concurrency limit"):
        await jobs.submit(other_environment)
    await jobs.cancel(first.execution_id)
    await jobs.wait(first.execution_id)
    second, _ = await jobs.submit(request("second", "true"))
    await jobs.wait(second.execution_id)
    page = jobs.jobs(CONTEXT, limit=1)
    assert page.truncated
    assert page.next_after == first.execution_id
    next_page = jobs.jobs(CONTEXT, limit=1, after=page.next_after)
    assert not next_page.truncated
    assert [item.execution_id for item in next_page.jobs] == [second.execution_id]
    assert not jobs.jobs(CONTEXT, after=next_page.next_after).jobs
    with pytest.raises(WorkerError, match="cursor"):
        jobs.jobs(CONTEXT.model_copy(update={"session_id": "another"}), after=first.execution_id)


@pytest.mark.asyncio
async def test_omitted_timeout_is_distinct_and_idempotent_across_policy_changes(
    jobs: ExecutionManager,
) -> None:
    jobs.job_resources = lambda _: Resources(
        job_wall_time_default=timedelta(seconds=10), job_wall_time_max=timedelta(seconds=60)
    )
    original = request("policy-default", "true")
    first, _ = await jobs.submit(original)
    await jobs.wait(first.execution_id)
    with pytest.raises(WorkerError, match="different request"):
        await jobs.submit(request("policy-default", "true", timeout_seconds=86400))
    jobs.job_resources = lambda _: Resources(job_wall_time_default=timedelta(seconds=20))
    same, created = await jobs.submit(original)
    assert not created
    assert same.job is not None
    assert same.job.timeout_seconds == 10


@pytest.mark.asyncio
async def test_overlapping_closes_keep_submission_barrier(
    jobs: ExecutionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Queue[asyncio.Event]()

    async def blocked(_request: SessionCloseRequest) -> Any:
        release = asyncio.Event()
        await entered.put(release)
        await release.wait()
        from sandbox_science.contracts import SessionClosed

        return SessionClosed()

    monkeypatch.setattr(jobs, "_close_session", blocked)
    first = asyncio.create_task(jobs.close_session(SessionCloseRequest(context=CONTEXT)))
    release_first = await entered.get()
    second = asyncio.create_task(jobs.close_session(SessionCloseRequest(context=CONTEXT)))
    release_second = await entered.get()
    release_first.set()
    await first
    with pytest.raises(WorkerError, match="closing"):
        await jobs.submit(request("during-close", "true"))
    release_second.set()
    await second
    accepted, _ = await jobs.submit(request("after-close", "true"))
    assert (await jobs.wait(accepted.execution_id)).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_cancelling_pump_waits_for_actual_log_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    started, unblock = threading.Event(), threading.Event()

    def slow_write(fd: int, data: bytes) -> None:
        started.set()
        assert unblock.wait(5)
        original(fd, data)

    original = executor.write_log
    monkeypatch.setattr(executor, "write_log", slow_write)

    async def send(_: dict[str, Any]) -> None:
        return None

    instance = executor.Executor(tmp_path, send)
    descriptor = os.open(tmp_path / "log", os.O_CREAT | os.O_WRONLY, 0o600)
    stream = asyncio.StreamReader()
    stream.feed_data(b"recorded")
    stream.feed_eof()
    pump = asyncio.create_task(
        instance.pump("exe", stream, executor.Capture("stdout", 1024), descriptor)
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pump.cancel()
        await asyncio.sleep(0)
        assert not pump.done()
        unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await pump
        assert (tmp_path / "log").read_bytes() == b"recorded"
    finally:
        unblock.set()
        os.close(descriptor)
        await instance.shutdown()


@pytest.mark.asyncio
async def test_unacknowledged_cancel_cannot_hang_session_close(
    jobs: ExecutionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker import manager as manager_module

    job, _ = await jobs.submit(request("stuck-cancel", "printf alive; sleep 60"))
    await running(jobs, job.execution_id)
    live = jobs._live[job.execution_id]
    assert live.channel is not None
    channel = live.channel
    original = channel.request

    async def refuse_cancel(op: str, **fields: Any) -> Any:
        if op == "cancel":
            await asyncio.Event().wait()
        return await original(op, **fields)

    monkeypatch.setattr(channel, "request", refuse_cancel)
    monkeypatch.setattr(manager_module, "CANCEL_GRACE", 0.05)
    await asyncio.wait_for(jobs.close_session(SessionCloseRequest(context=CONTEXT)), 10)
    assert jobs.get(job.execution_id).state is ExecutionState.LOST
    assert channel.closed


@pytest.mark.asyncio
async def test_completed_job_summary_and_list_never_duplicate_large_output(
    jobs: ExecutionManager,
) -> None:
    job, _ = await jobs.submit(request("big-output", "python -c 'print(\"a\"*600000)'"))
    await jobs.wait(job.execution_id)
    status = await jobs.job_status(job.execution_id)
    assert len(status.model_dump_json().encode()) < 100000
    assert status.stdout is not None
    assert status.stdout.total_bytes == 600001
    assert status.stdout.truncated
    assert status.execution.result is not None
    assert status.execution.result.stdout is None
    assert len(jobs.jobs(CONTEXT).model_dump_json().encode()) < 10000


@pytest.mark.asyncio
async def test_direct_cancel_has_completion_deadline(
    jobs: ExecutionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker import manager as manager_module

    job, _ = await jobs.submit(request("ack-without-end", "sleep 60"))
    await running(jobs, job.execution_id)
    live = jobs._live[job.execution_id]
    assert live.channel is not None
    channel = live.channel
    original = channel.request

    async def acknowledge_cancel(op: str, **fields: Any) -> Any:
        if op == "cancel":
            return {"found": True}
        return await original(op, **fields)

    monkeypatch.setattr(channel, "request", acknowledge_cancel)
    monkeypatch.setattr(manager_module, "CANCEL_GRACE", 0.05)
    execution = await jobs.cancel(job.execution_id)
    assert execution.state is ExecutionState.CANCELLING
    final = await asyncio.wait_for(jobs.wait(job.execution_id), 10)
    assert final.state is ExecutionState.LOST
    assert live.cancel_watchdog is not None
    await live.cancel_watchdog
    assert channel.closed
