# SPDX-License-Identifier: MIT
"""The worker service on the local backend, where a test needs more than its API.

These tests reach into the service: they kill its executor, crash it, close it,
give it a stand-in kernelspec or a backend that overstates its tier. The
acceptance fixtures of docs/specs/tools.md, which any backend must pass through
the API alone, are in test_conformance.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
)
from sandbox_science.testing import FakeClock, eventually
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.connections import Activity
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .harness import CONTEXT, Harness

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as started:
        yield started


async def test_an_executor_that_cannot_start_fails_the_attempt(tmp_path: Path) -> None:
    broken = await Harness.start(tmp_path, python="/nonexistent/python3")
    try:
        execution = await broken.run("op-x", kind="shell", script="echo hi")
        assert execution["state"] == "failed"
        assert execution["result"]["error"]["kind"] == "unavailable"
        assert execution["location"] is None
    finally:
        await broken.close()


async def test_a_dead_executor_loses_its_attempts(worker: Harness) -> None:
    # The command outlives a killed executor, so it ends once its executor is gone.
    script = "while kill -0 $PPID 2>/dev/null; do sleep 0.1; done"
    execution = await worker.submit("op-long", kind="shell", script=script)
    await worker.wait_running(execution["execution_id"])
    worker.view.executor_process().send_signal(signal.SIGKILL)
    lost = await worker.finish(execution["execution_id"])
    assert lost["state"] == "lost"
    # The next request starts a new executor.
    again = await worker.run("op-after", kind="shell", script="echo back")
    assert again["result"]["stdout"]["text"] == "back\n"


async def test_a_restarted_worker_reports_unfinished_attempts_as_lost(tmp_path: Path) -> None:
    # A crash leaves an attempt running in the store; the next worker must not
    # run it again, nor leave it hanging.
    store = Store(tmp_path / "worker.sqlite3")
    crashed = Execution(
        execution_id="exe-crashed",
        attempt_id="att-crashed",
        operation_id="op-crashed",
        kind="shell",
        context=ExecutionContext.model_validate(CONTEXT),
        state=ExecutionState.RUNNING,
        submitted_at=datetime.now(UTC),
    )
    store.insert_execution(crashed, "digest")
    store.close()
    second = await Harness.start(tmp_path)
    try:
        lost = await second.finish("exe-crashed")
        assert lost["state"] == "lost"
        assert "restarted" in lost["result"]["error"]["message"]
        events = [event async for event in second.events("exe-crashed")]
        assert [event["type"] for event in events] == ["state", "result"]
    finally:
        await second.close()


async def test_closing_the_worker_loses_running_attempts(tmp_path: Path) -> None:
    first = await Harness.start(tmp_path)
    execution = await first.submit("op-long", kind="shell", script="sleep 60")
    await first.wait_running(execution["execution_id"])
    await first.manager.close()
    try:
        record = first.store.execution(execution["execution_id"])
        assert record is not None
        assert record.state == "lost"
    finally:
        await first.close()


async def test_a_sage_kernel_is_offered_when_its_kernelspec_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stand-in "sagemath" kernelspec that starts an ordinary Python kernel tests
    # the plumbing; real SageMath runs in the babbage lab.
    spec = tmp_path / "jupyter" / "kernels" / "sagemath"
    spec.mkdir(parents=True)
    (spec / "kernel.json").write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                "display_name": "stand-in for SageMath",
                "language": "sage",
            }
        )
    )
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jupyter"))
    started = await Harness.start(tmp_path / "state")
    try:
        _, capabilities = await started.get("/v1/capabilities?environment_id=env1")
        assert capabilities["languages"] == ["python", "sage"]
        await started.run("op-p1", kind="cell", code="x = 'python'")
        sage = await started.run("op-s1", kind="cell", language="sage", code="x = 'sage'\nx")
        python = await started.run("op-p2", kind="cell", code="x")
        assert sage["result"]["kernel"] == {
            "kernel_id": "krn-s1-sage",
            "language": "sage",
            "generation": 1,
            "run": None,
            "start_revision": 0,
            "current_revision": 0,
            "revision_status": "current",
        }
        assert sage["result"]["outputs"][0]["data"]["text/plain"] == "'sage'"
        # Each language has its own kernel and state.
        assert python["result"]["outputs"][0]["data"]["text/plain"] == "'python'"
        status, kernel = await started.post(
            "/v1/kernels/restart", {"context": CONTEXT, "language": "sage"}
        )
        assert (status, kernel["language"], kernel["generation"]) == (200, "sage", 2)
    finally:
        await started.close()


async def test_a_language_without_a_kernel_is_unsupported(worker: Harness) -> None:
    execution = await worker.run("op-sage", kind="cell", language="sage", code="1")
    assert execution["state"] == "failed"
    assert execution["result"]["error"]["kind"] == "unsupported"


async def test_a_reader_leaving_its_event_stream_does_not_stop_the_execution(
    worker: Harness,
) -> None:
    # The test server cancels the handler of a closed connection, as any aiohttp
    # server does when it shuts down. The execution and other readers carry on.
    execution = await worker.submit("op-leave", kind="shell", script="sleep 0.5; echo done")
    response = await worker.client.get(
        f"/v1/executions/{execution['execution_id']}/events", headers=worker.headers
    )
    assert json.loads(await response.content.readline())["type"] == "state"
    response.close()
    finished = await worker.finish(execution["execution_id"], timeout=30)
    assert finished["state"] == "succeeded"
    assert finished["result"]["stdout"]["text"] == "done\n"


class OverstatedBackend(LocalBackend):
    """The local backend, declaring a tier it does not give."""

    @property
    def isolation_tier(self) -> int:
        return 3


async def test_the_probe_lowers_an_overstated_tier(tmp_path: Path) -> None:
    backend = OverstatedBackend(LocalBackendSettings(), tmp_path)
    started = await Harness.start(tmp_path, backend=backend)
    try:
        execution = await started.run("op-where", kind="shell", script="true")
        location = execution["location"]
        assert (location["isolation_tier"], location["declared_isolation_tier"]) == (0, 3)
        evidence = location["isolation_evidence"]
        assert (evidence["kernel"], evidence["sentinel"]) == ("shared", "visible")
        assert "the sandbox runs on the worker host's kernel (same boot ID)" in evidence["findings"]
        _, capabilities = await started.get("/v1/capabilities?environment_id=env1")
        assert capabilities["location"] == location
        # The sentinel is gone once the executor has looked for it.
        assert list((tmp_path / "probe").iterdir()) == []
    finally:
        await started.close()


async def test_a_failed_probe_means_tier_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = ExecutorChannel.request

    async def refuse_probe(self: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "probe":
            raise WorkerError(ErrorKind.INVALID_REQUEST, "unknown operation 'probe'")
        return await request(self, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", refuse_probe)
    backend = OverstatedBackend(LocalBackendSettings(), tmp_path)
    started = await Harness.start(tmp_path, backend=backend)
    try:
        execution = await started.run("op-where", kind="shell", script="echo still runs")
        assert execution["result"]["stdout"]["text"] == "still runs\n"
        location = execution["location"]
        assert (location["isolation_tier"], location["declared_isolation_tier"]) == (0, 3)
        evidence = location["isolation_evidence"]
        assert evidence["error"] == "unknown operation 'probe'"
        assert evidence["sandbox"] is None
    finally:
        await started.close()


async def test_closing_a_session_stops_its_kernels_only(worker: Harness) -> None:
    other = {**CONTEXT, "session_id": "s2"}

    async def run_in(context: dict[str, str], operation_id: str, code: str) -> dict[str, Any]:
        body = {"operation_id": operation_id, "context": context, "kind": "cell", "code": code}
        status, execution = await worker.post("/v1/executions", body)
        assert status == 201, execution
        return await worker.finish(execution["execution_id"])

    await worker.run("op-a1", kind="cell", code="x = 'kept'")
    await run_in(other, "op-b1", "y = 'other'")
    sleeping = await worker.submit("op-a2", kind="cell", code="import time\ntime.sleep(60)")
    await worker.wait_running(sleeping["execution_id"])
    status, closed = await worker.post("/v1/sessions/close", {"context": CONTEXT})
    assert status == 200, closed
    assert [kernel["language"] for kernel in closed["kernels"]] == ["python"]
    generation = closed["kernels"][0]["generation"]
    ended = await worker.finish(sleeping["execution_id"])
    assert ended["state"] == "cancelled"
    assert ended["result"]["error"]["message"] == "the session was closed"
    after = await worker.run("op-a3", kind="cell", code="x")
    assert after["result"]["error"]["name"] == "NameError"
    assert after["result"]["state_lost"] is True
    assert after["result"]["kernel"]["generation"] == generation + 1
    kept = await run_in(other, "op-b2", "y")
    assert kept["result"]["outputs"][0]["data"]["text/plain"] == "'other'"


async def test_closing_a_session_starts_no_executor(worker: Harness) -> None:
    idle = {**CONTEXT, "environment_id": "env-idle"}
    status, closed = await worker.post("/v1/sessions/close", {"context": idle})
    assert (status, closed) == (200, {"kernels": []})
    assert "env-idle" not in worker.view.connected()


async def test_an_idle_kernel_is_stopped_and_its_state_reported_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The option reaches the executor; the reaper itself runs on a fake clock in
    # test_executor.py. The executor says on stderr when it stopped the kernel.
    caplog.set_level(logging.DEBUG, logger="sandbox_science.worker.channel")
    async with await Harness.start(
        tmp_path / "worker", executor_args=("--kernel-idle", "0.5")
    ) as idle:
        await idle.run("op-i1", kind="cell", code="x = 1")
        await eventually(
            lambda: any("stopped kernel" in record.getMessage() for record in caplog.records),
            describe="the idle kernel stopped",
        )
        after = await idle.run("op-i2", kind="cell", code="x")
        assert after["result"]["state_lost"] is True
        assert after["result"]["error"]["name"] == "NameError"
        assert after["result"]["kernel"]["generation"] == 2


async def test_activity_reports_use_running_calls_and_kernel_cpu(worker: Harness) -> None:
    manager = worker.manager
    assert await manager.activity("env1") == Activity(None, 0, None, 0)
    await worker.run("op-u1", kind="cell", code="x = sum(i * i for i in range(3_000_000))")
    idle = await manager.activity("env1")
    assert idle is not None
    assert (idle.running, idle.kernels) == (0, 1)
    assert idle.cpu_seconds is not None
    assert idle.cpu_seconds > 0.05
    assert idle.last_used is not None
    assert idle.last_used <= time.monotonic()
    busy = await worker.submit("op-u2", kind="cell", code="import time\ntime.sleep(60)")
    await worker.wait_running(busy["execution_id"])
    running = await manager.activity("env1")
    assert running is not None
    assert running.running == 1
    status, _ = await worker.post(f"/v1/executions/{busy['execution_id']}/cancel", {})
    assert status == 200
    await worker.finish(busy["execution_id"])


async def test_cpu_used_by_a_kernel_thread_counts_as_activity(worker: Harness) -> None:
    # The thread spins until the test is done with it.
    spin = (
        "import os, threading\n"
        "def spin():\n"
        "    while not os.path.exists('stop-spinning'):\n"
        "        pass\n"
        "threading.Thread(target=spin, daemon=True).start()"
    )
    await worker.run("op-t1", kind="cell", code=spin)
    first = await worker.manager.activity("env1")
    assert first is not None
    assert first.cpu_seconds is not None
    start = first.cpu_seconds

    async def busy() -> Activity | None:
        second = await worker.manager.activity("env1")
        assert second is not None
        assert second.cpu_seconds is not None
        return second if second.cpu_seconds - start > 0.5 else None

    try:
        second = await eventually(busy, describe="the thread's CPU time counted")
        assert second.running == 0
    finally:
        (worker.workspace / "stop-spinning").touch()


async def test_an_idle_executor_is_released_and_its_kernels_reported_lost(worker: Harness) -> None:
    manager = worker.manager
    await worker.run("op-r1", kind="cell", code="x = 1")
    # Used after this time: kept.
    assert not await manager.release_if_idle("env1", time.monotonic() - 60)
    busy = await worker.submit("op-r2", kind="cell", code="import time\ntime.sleep(60)")
    await worker.wait_running(busy["execution_id"])
    # Running: kept, whatever the time.
    assert not await manager.release_if_idle("env1", time.monotonic() + 60)
    status, _ = await worker.post(f"/v1/executions/{busy['execution_id']}/cancel", {})
    assert status == 200
    await worker.finish(busy["execution_id"])
    assert await manager.release_if_idle("env1", time.monotonic())
    assert "env1" not in worker.view.connected()
    assert await manager.release_if_idle("env1", time.monotonic())  # nothing left to stop
    after = await worker.run("op-r3", kind="cell", code="x")
    assert after["kernel_state_lost"] is True
    assert after["result"]["state_lost"] is True
    assert after["result"]["error"]["name"] == "NameError"


@pytest.mark.parametrize("route", ["/v1/kernels/status", "/v1/kernels/observe"])
async def test_status_before_start_and_failed_birth_commit_do_not_consume_kernel_identity(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    await worker.run("cp12-old", kind="cell", code="previous = 1729")
    manager = worker.manager
    before = manager.store.kernel("env1", "s1", "python")
    assert before is not None
    await manager.release("env1")
    observed: asyncio.Future[Message] = asyncio.get_running_loop().create_future()

    async def delayed_state(
        apply: Callable[[str, Message], Awaitable[None]], environment: str, message: Message
    ) -> None:
        if message.get("event") == "state" and message.get("kernel") is not None:
            if not observed.done():
                observed.set_result(message)
            return  # A source event delayed before the worker applies it.
        await apply(environment, message)

    on_event = worker.view.wrap_on_event(monkeypatch, delayed_state)
    execution = await worker.submit(
        "cp12-new",
        kind="cell",
        code="import time\nfrom pathlib import Path\n"
        "while not Path('cp12-release').exists(): time.sleep(.01)\nprint(42)",
    )
    message = await asyncio.wait_for(observed, 30)
    status, listing = await worker.post(route, {"context": CONTEXT})
    assert status == 200, listing
    kernel = listing["kernels"][0]
    assert kernel["start_revision"] is None
    assert manager.store.kernel("env1", "s1", "python") == before

    observe = manager.store.kernel_starts.observe

    def broken_observe(*args: Any, **kwargs: Any):
        observe(*args, **kwargs)
        raise sqlite3.OperationalError("one failed birth transaction")

    with monkeypatch.context() as failing:
        failing.setattr(manager.store.kernel_starts, "observe", broken_observe)
        await on_event("env1", message)
    saved = manager.store.execution(execution["execution_id"])
    assert saved is not None
    assert saved.state == ExecutionState.QUEUED
    assert manager.get(saved.execution_id).state == ExecutionState.QUEUED
    assert manager.store.kernel("env1", "s1", "python") == before
    await on_event("env1", message)  # Same-process replay retries the whole birth commit.
    saved = manager.store.execution(execution["execution_id"])
    assert saved is not None
    assert saved.kernel_state_lost
    assert saved.kernel is not None
    assert (saved.kernel.generation, saved.kernel.start_revision) == (2, 0)
    (worker.root / "environments/env1/project/sessions/s1/cp12-release").touch()
    done = await worker.finish(saved.execution_id)
    assert done["kernel_state_lost"] is True
    assert done["result"]["state_lost"] is True
    assert done["result"]["kernel"]["start_revision"] == 0


async def test_damaged_kernel_generation_refuses_status_but_preserves_shell_completion(
    worker: Harness,
) -> None:
    await worker.run("cp12-seed", kind="cell", code="value = 1729")
    worker.manager.store._db.execute("UPDATE kernels SET incarnation=?", ("x" * 20_000_000,))
    status, _ = await worker.post("/v1/kernels/status", {"context": CONTEXT})
    assert status == 503
    done = await worker.run("cp12-healthy", kind="shell", script="echo healthy")
    assert done["state"] == "succeeded"
    assert done["result"]["stdout"]["text"] == "healthy\n"
    assert done["result"]["running_kernels"] == []


@pytest.mark.parametrize(
    "kernel",
    [
        {},
        {"kernel_id": "k", "language": "python", "generation": True},
        {"kernel_id": "k", "language": "python", "generation": 1.0},
        {"kernel_id": "k", "language": "python", "generation": 2**63},
        {"kernel_id": "../invalid", "language": "python", "generation": 1},
        {"kernel_id": "k", "generation": 1},
    ],
)
async def test_protocol_one_kernel_status_refuses_missing_or_malformed_identity(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, kernel: dict[str, object]
) -> None:
    await worker.run("cp12-open", kind="shell", script="true")
    request = ExecutorChannel.request

    async def malformed(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            return {"kernels": [kernel]}
        return await request(channel, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", malformed)
    status, _ = await worker.post("/v1/kernels/status", {"context": CONTEXT})
    assert status == 503


async def test_package_changes_count_as_environment_revisions(tmp_path: Path) -> None:
    # A throwaway interpreter, so that the "install" touches nothing but the test's files.
    venv = tmp_path / "venv"
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = str(venv / "bin" / "python")
    root = tmp_path / "worker"
    started = await Harness.start(root, python=python)
    try:
        first = await started.run("op-v1", kind="shell", script="true")
        assert first["location"]["environment_revision"] == "0"
        assert first["result"]["environment_revision"] == "0"
        install = (
            'mkdir "$(python -c \'import sysconfig; print(sysconfig.get_paths()["purelib"])\')'
            '/fake_package-1.0.dist-info"'
        )
        installed = await started.run("op-v2", kind="shell", script=install)
        assert installed["state"] == "succeeded", installed["result"]
        assert installed["location"]["environment_revision"] == "0"
        assert installed["result"]["environment_revision"] == "1"
        after = await started.run("op-v3", kind="shell", script="true")
        assert after["location"]["environment_revision"] == "1"
        assert after["result"]["environment_revision"] == "1"
        record = started.store.revision("env1")
        assert record is not None
        assert record.changed_by == installed["attempt_id"]
    finally:
        await started.close()
    # A new worker, and a new executor, see the same packages: the revision stays.
    again = await Harness.start(root, python=python)
    try:
        later = await again.run("op-v4", kind="shell", script="true")
        assert later["result"]["environment_revision"] == "1"
    finally:
        await again.close()


async def test_a_session_sees_each_admission_notice_once(worker: Harness) -> None:
    recorded: list[tuple[datetime, str]] = []

    def notices(environment_id: str, since: datetime) -> list[tuple[datetime, str]]:
        return [(at, text) for at, text in recorded if at > since]

    worker.view.set_lifecycle(notices=notices)
    first = await worker.run("op-n1", kind="shell", script="true")
    assert first["result"]["notices"] == []
    recorded.append((datetime.now(UTC), "a.example.org was admitted on demand"))
    second = await worker.run("op-n2", kind="shell", script="true")
    assert second["result"]["notices"] == ["a.example.org was admitted on demand"]
    third = await worker.run("op-n3", kind="shell", script="true")
    assert third["result"]["notices"] == []
    # Another session starts from its own first submission: an older admission is not news.
    other = {**CONTEXT, "session_id": "s2"}
    status, execution = await worker.post(
        "/v1/executions",
        {"operation_id": "op-n4", "context": other, "kind": "shell", "script": "true"},
    )
    assert status == 201, execution
    fourth = await worker.finish(execution["execution_id"])
    assert fourth["result"]["notices"] == []


async def test_operation_ids_are_kept_per_project(worker: Harness) -> None:
    first = await worker.run("op-shared", kind="shell", script="echo one")
    other = {**CONTEXT, "project_id": "p2", "environment_id": "env2"}
    request = {"operation_id": "op-shared", "kind": "shell", "script": "echo two"}
    status, second = await worker.post("/v1/executions", {**request, "context": other})
    assert status == 201, second
    assert second["execution_id"] != first["execution_id"]
    # A record stored before operations were kept per project has the bare operation ID:
    # its own project still finds it, and other projects do not meet it.
    worker.manager.store._db.execute(
        "UPDATE executions SET operation_id = 'op-shared' WHERE execution_id = ?",
        (first["execution_id"],),
    )
    again = await worker.submit("op-shared", kind="shell", script="echo one")
    assert again["execution_id"] == first["execution_id"]
    third = {**CONTEXT, "project_id": "p3", "environment_id": "env3"}
    status, execution = await worker.post("/v1/executions", {**request, "context": third})
    assert status == 201, execution
    assert execution["execution_id"] not in (first["execution_id"], second["execution_id"])


# Shared project data across a project's environments (D79). On the local backend each
# environment has a directory of its own, as each OpenShell environment has a sandbox.


@pytest_asyncio.fixture
async def synced(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, shared_bytes=64) as started:
        yield started


def _in(environment: str, session: str) -> dict[str, str]:
    return {"project_id": "p1", "session_id": session, "environment_id": environment}


async def _shell(
    worker: Harness, operation: str, where: dict[str, str], script: str
) -> dict[str, Any]:
    status, execution = await worker.post(
        "/v1/executions",
        {"operation_id": operation, "context": where, "kind": "shell", "script": script},
    )
    assert status == 201, execution
    return await worker.finish(execution["execution_id"])


async def test_shared_files_reach_the_projects_other_environments(synced: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    await _shell(synced, "op-w", a, 'printf "1,2\\n" > "$PROJECT_SHARED/results.csv"')
    seen = await _shell(synced, "op-r", c, 'cat "$PROJECT_SHARED/results.csv"')
    assert seen["result"]["stdout"]["text"] == "1,2\n"
    # A deletion goes the same way.
    await _shell(synced, "op-d", a, 'rm "$PROJECT_SHARED/results.csv"')
    gone = await _shell(synced, "op-g", c, 'ls "$PROJECT_SHARED"; echo end')
    assert gone["result"]["stdout"]["text"] == "end\n"
    # The files API sees the project's copy too: a write in one environment, a read in another.
    status, written = await synced.post(
        "/v1/files/write",
        {"context": a, "path": "shared/notes.md", "content_b64": "bm90ZXMK", "create_only": True},
    )
    assert status == 200, written
    status, read = await synced.post("/v1/files/read", {"context": c, "path": "shared/notes.md"})
    assert (status, read["content_b64"]) == (200, "bm90ZXMK")


async def test_concurrent_changes_keep_a_conflict_copy(synced: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    await _shell(synced, "op-1", a, 'echo a > "$PROJECT_SHARED/notes.md"')
    await _shell(synced, "op-2", c, 'cat "$PROJECT_SHARED/notes.md"')
    # env2 changes the file while env1 changes it and finishes first.
    status, slow = await synced.post(
        "/v1/executions",
        {
            "operation_id": "op-3",
            "context": c,
            "kind": "shell",
            "script": 'sleep 2; echo c > "$PROJECT_SHARED/notes.md"',
        },
    )
    assert status == 201
    await _shell(synced, "op-4", a, 'echo b > "$PROJECT_SHARED/notes.md"')
    late = await synced.finish(slow["execution_id"])
    [notice] = [n for n in late["result"]["notices"] if "conflict" in n]
    assert "shared/notes.conflict-env2.md" in notice
    after = await _shell(
        synced,
        "op-5",
        c,
        'cat "$PROJECT_SHARED/notes.md" "$PROJECT_SHARED/notes.conflict-env2.md"',
    )
    assert after["result"]["stdout"]["text"] == "b\nc\n"
    elsewhere = await _shell(synced, "op-6", a, 'cat "$PROJECT_SHARED/notes.conflict-env2.md"')
    assert elsewhere["result"]["stdout"]["text"] == "c\n"


async def test_files_over_the_limit_stay_where_they_are(synced: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    wrote = await _shell(synced, "op-1", a, 'head -c 100 /dev/zero > "$PROJECT_SHARED/big.bin"')
    [notice] = wrote["result"]["notices"]
    assert "shared/big.bin" in notice
    assert "publish it as an artifact" in notice
    absent = await _shell(synced, "op-2", c, 'ls "$PROJECT_SHARED"; echo end')
    assert absent["result"]["stdout"]["text"] == "end\n"
    again = await _shell(synced, "op-3", a, "true")
    assert again["result"]["notices"] == []  # told once


async def test_the_projects_shared_files_have_a_total_limit(tmp_path: Path) -> None:
    capped = await Harness.start(tmp_path, shared_bytes=100, shared_total=150)
    try:
        a, c = _in("env1", "s1"), _in("env2", "s3")
        first = await _shell(capped, "op-1", a, 'head -c 100 /dev/zero > "$PROJECT_SHARED/a.bin"')
        assert first["result"]["notices"] == []
        second = await _shell(capped, "op-2", a, 'head -c 90 /dev/zero > "$PROJECT_SHARED/b.bin"')
        [notice] = second["result"]["notices"]
        assert "their limit" in notice
        listing = await _shell(capped, "op-3", c, 'cd "$PROJECT_SHARED" && ls')
        assert listing["result"]["stdout"]["text"] == "a.bin\n"
    finally:
        await capped.close()


async def test_file_writes_in_shared_expect_the_projects_version(synced: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    status, first = await synced.post(
        "/v1/files/write",
        {"context": a, "path": "shared/x.txt", "content_b64": "djAK", "create_only": True},
    )
    assert status == 200
    await _shell(synced, "op-1", c, "true")  # env2 has v0

    def body(where: dict[str, str], text: bytes) -> dict[str, Any]:
        return {
            "context": where,
            "path": "shared/x.txt",
            "content_b64": base64.b64encode(text).decode(),
            "expected_sha256": first["sha256"],
        }

    status, _ = await synced.post("/v1/files/write", body(a, b"from env1\n"))
    assert status == 200
    status, refused = await synced.post("/v1/files/write", body(c, b"from env2\n"))
    assert (status, refused["error"]["kind"]) == (409, "conflict")


async def test_a_sandbox_made_again_under_its_id_deletes_nothing(synced: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    await _shell(synced, "op-1", a, 'echo a > "$PROJECT_SHARED/a.txt"')
    await _shell(synced, "op-2", c, "true")
    await synced.manager.release("env2")
    shutil.rmtree(synced.root / "environments" / "env2")
    again = await _shell(synced, "op-3", c, 'cat "$PROJECT_SHARED/a.txt"')
    assert again["result"]["stdout"]["text"] == "a\n"
    kept = await _shell(synced, "op-4", a, 'cat "$PROJECT_SHARED/a.txt"')
    assert kept["result"]["stdout"]["text"] == "a\n"


async def test_a_call_cancelled_while_shared_files_sync_never_runs(
    synced: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = synced.manager.shared
    assert sync is not None
    gate, entered = asyncio.Event(), asyncio.Event()
    down = sync.down

    async def held(*args: Any) -> list[str]:
        entered.set()
        await gate.wait()
        return await down(*args)

    monkeypatch.setattr(sync, "down", held)
    status, execution = await synced.post(
        "/v1/executions",
        {
            "operation_id": "op-c",
            "context": _in("env1", "s1"),
            "kind": "shell",
            "script": 'echo ran > "$PROJECT_SHARED/ran"',
        },
    )
    assert status == 201
    await asyncio.wait_for(entered.wait(), 60)
    status, _ = await synced.post(f"/v1/executions/{execution['execution_id']}/cancel", {})
    assert status == 200
    gate.set()
    done = await synced.finish(execution["execution_id"])
    assert done["state"] == "cancelled"
    assert not (synced.root / "environments" / "env1" / "project" / "shared" / "ran").exists()


async def test_an_attempts_result_outlives_a_sync_cut_short(
    synced: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = synced.manager.shared
    assert sync is not None
    syncing = asyncio.Event()

    async def stuck(*args: Any) -> list[str]:
        syncing.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(sync, "up", stuck)
    status, execution = await synced.post(
        "/v1/executions",
        {
            "operation_id": "op-1",
            "context": _in("env1", "s1"),
            "kind": "shell",
            "script": "echo hi",
        },
    )
    assert status == 201
    execution_id = execution["execution_id"]
    await asyncio.wait_for(syncing.wait(), 60)
    # A second end from the executor is ignored.
    await synced.view.deliver(
        "env1", {"execution": execution_id, "event": "end", "state": "failed", "result": {}}
    )
    # The executor goes away during the sync: the attempt keeps the result it reported.
    synced.view.executor_process().send_signal(signal.SIGKILL)
    done = await synced.finish(execution_id)
    assert done["state"] == "succeeded"
    assert done["result"]["stdout"]["text"] == "hi\n"
    assert any("could not be synced" in notice for notice in done["result"]["notices"])


@pytest.mark.parametrize("fake", [False, True], ids=["system-clock", "fake-clock"])
async def test_files_the_sync_brings_are_not_a_calls_own(tmp_path: Path, fake: bool) -> None:
    # The sync and the manager compare times: both read the manager's clock, whatever it is.
    clock: Clock = FakeClock() if fake else SYSTEM_CLOCK
    gate = tmp_path / "gate"
    async with await Harness.start(tmp_path / "worker", shared_bytes=64, clock=clock) as synced:
        status, long = await synced.post(
            "/v1/executions",
            {
                "operation_id": "op-l",
                "context": _in("env2", "s2"),
                "kind": "shell",
                "script": f"until [ -e {gate} ]; do sleep 0.01; done",
            },
        )
        assert status == 201
        await synced.wait_running(long["execution_id"])
        await _shell(synced, "op-1", _in("env1", "s1"), 'echo r > "$PROJECT_SHARED/r.txt"')
        await _shell(synced, "op-2", _in("env2", "s3"), "true")  # brings r.txt in while s2 runs
        gate.touch()
        done = await synced.finish(long["execution_id"])
        assert done["result"]["written_files"] == []


async def test_a_conflict_found_for_a_file_request_reaches_the_running_call(
    synced: Harness,
) -> None:
    a, b = _in("env1", "s1"), _in("env2", "s2")
    await _shell(synced, "op-1", a, 'echo a > "$PROJECT_SHARED/notes.md"')
    await _shell(synced, "op-2", b, "true")  # env2 has it
    status, long = await synced.post(
        "/v1/executions",
        {
            "operation_id": "op-3",
            "context": b,
            "kind": "shell",
            "script": 'echo b > "$PROJECT_SHARED/notes.md"; until [ -f go ]; do sleep 0.05; done',
        },
    )
    assert status == 201
    await synced.wait_running(long["execution_id"])
    await _shell(synced, "op-4", a, 'echo a2 > "$PROJECT_SHARED/notes.md"')
    # Another session lists shared/ in env2 while the call runs: the sync finds the conflict.
    status, _ = await synced.post(
        "/v1/files/list", {"context": _in("env2", "s3"), "path": "shared"}
    )
    assert status == 200
    status, _ = await synced.post(
        "/v1/files/write", {"context": b, "path": "go", "content_b64": "", "create_only": True}
    )
    assert status == 200
    done = await synced.finish(long["execution_id"])
    [notice] = [n for n in done["result"]["notices"] if "conflict" in n]
    assert "shared/notes.conflict-env2.md" in notice


async def test_a_calls_own_write_after_the_sync_wrote_the_file_is_its_own(synced: Harness) -> None:
    b = _in("env2", "s2")
    status, long = await synced.post(
        "/v1/executions",
        {
            "operation_id": "op-l",
            "context": b,
            "kind": "shell",
            "script": 'until [ -f go ]; do sleep 0.05; done; echo mine > "$PROJECT_SHARED/r.txt"',
        },
    )
    assert status == 201
    await synced.wait_running(long["execution_id"])
    await _shell(synced, "op-1", _in("env1", "s1"), 'echo r > "$PROJECT_SHARED/r.txt"')
    await _shell(synced, "op-2", _in("env2", "s3"), "true")  # brings r.txt in while s2 runs
    status, _ = await synced.post(
        "/v1/files/write", {"context": b, "path": "go", "content_b64": "", "create_only": True}
    )
    assert status == 200
    done = await synced.finish(long["execution_id"])
    assert "shared/r.txt" in done["result"]["written_files"]


async def test_a_result_that_cannot_be_stored_still_ends_the_attempt(
    synced: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> None:
        raise RuntimeError("test: the store is full")

    synced.view.replace_completion(monkeypatch, broken)
    done = await _shell(synced, "op-1", _in("env1", "s1"), "echo hi")
    assert done["state"] == "failed"
    assert done["result"]["error"]["message"] == "the attempt's result could not be stored"


async def test_held_notices_are_bounded_by_size() -> None:
    from sandbox_science.worker.files import MAX_HELD_BYTES, MAX_HELD_NOTICES
    from sandbox_science.worker.files import latest as _held

    many = [f"notice {n}" for n in range(MAX_HELD_NOTICES * 2)]
    assert _held(many) == many[-MAX_HELD_NOTICES:]
    large = ["x" * (MAX_HELD_BYTES // 3)] * 5
    assert sum(len(notice) for notice in _held(large)) <= MAX_HELD_BYTES
    assert _held(large) == large[-3:]
