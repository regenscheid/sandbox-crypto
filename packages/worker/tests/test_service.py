# SPDX-License-Identifier: MIT
"""The worker service on the local backend, where a test needs more than its API.

These tests reach into the service: they kill its executor, crash it, close it,
give it a stand-in kernelspec or a backend that overstates its tier. The
acceptance fixtures of docs/specs/tools.md, which any backend must pass through
the API alone, are in test_conformance.py.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import ErrorKind, Execution, ExecutionContext, ExecutionState
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend, LocalBackend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import Activity, ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CONTEXT = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]

pytestmark = pytest.mark.asyncio


class Worker:
    """A test client plus helpers that follow an execution to its end."""

    def __init__(self, client: TestClient[Any, Any], manager: ExecutionManager, root: Path) -> None:
        self.client = client
        self.manager = manager
        self.root = root

    async def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = await self.client.post(path, json=body, headers=AUTH)
        return response.status, await response.json()

    async def get(self, path: str) -> tuple[int, dict[str, Any]]:
        response = await self.client.get(path, headers=AUTH)
        return response.status, await response.json()

    async def submit(self, operation_id: str, **request: Any) -> dict[str, Any]:
        status, execution = await self.post(
            "/v1/executions", {"operation_id": operation_id, "context": CONTEXT, **request}
        )
        assert status in (200, 201), execution
        return execution

    async def events(self, execution_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        response = await self.client.get(
            f"/v1/executions/{execution_id}/events", params={"after": after}, headers=AUTH
        )
        assert response.status == 200
        async for line in response.content:
            if line.strip():
                yield json.loads(line)

    async def finish(self, execution_id: str, timeout: float = 120) -> dict[str, Any]:
        async def follow() -> dict[str, Any]:
            async for event in self.events(execution_id):
                if event["type"] == "result":
                    return event["execution"]
            raise AssertionError("the event stream ended without a result")

        return await asyncio.wait_for(follow(), timeout)

    async def run(self, operation_id: str, **request: Any) -> dict[str, Any]:
        execution = await self.submit(operation_id, **request)
        return await self.finish(execution["execution_id"])

    async def wait_running(self, execution_id: str, timeout: float = 60) -> None:
        async def follow() -> None:
            async for event in self.events(execution_id):
                if event["type"] == "state" and event["state"] == "running":
                    return
            raise AssertionError("the execution never ran")

        await asyncio.wait_for(follow(), timeout)


async def start_worker(
    aiohttp_client: ClientFactory,
    root: Path,
    python: str = sys.executable,
    backend: Backend | None = None,
) -> tuple[Worker, Store]:
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        backend or LocalBackend(LocalBackendSettings(python=python), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    return Worker(client, manager, root), store


@pytest_asyncio.fixture
async def worker(aiohttp_client: ClientFactory, tmp_path: Path) -> AsyncIterator[Worker]:
    started, store = await start_worker(aiohttp_client, tmp_path)
    yield started
    await started.manager.close()
    store.close()


async def test_an_executor_that_cannot_start_fails_the_attempt(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    broken, store = await start_worker(aiohttp_client, tmp_path, python="/nonexistent/python3")
    try:
        execution = await broken.run("op-x", kind="shell", script="echo hi")
        assert execution["state"] == "failed"
        assert execution["result"]["error"]["kind"] == "unavailable"
        assert execution["location"] is None
    finally:
        await broken.manager.close()
        store.close()


async def test_a_dead_executor_loses_its_attempts(worker: Worker) -> None:
    execution = await worker.submit("op-long", kind="shell", script="sleep 60")
    await worker.wait_running(execution["execution_id"])
    environment = worker.manager._environments["env1"]  # pyright: ignore[reportPrivateUsage]
    process = environment.channel._process  # pyright: ignore[reportOptionalMemberAccess,reportPrivateUsage]
    assert process is not None
    process.send_signal(signal.SIGKILL)
    lost = await worker.finish(execution["execution_id"])
    assert lost["state"] == "lost"
    # The next request starts a new executor.
    again = await worker.run("op-after", kind="shell", script="echo back")
    assert again["result"]["stdout"]["text"] == "back\n"


async def test_a_restarted_worker_reports_unfinished_attempts_as_lost(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
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
    second, store = await start_worker(aiohttp_client, tmp_path)
    try:
        lost = await second.finish("exe-crashed")
        assert lost["state"] == "lost"
        assert "restarted" in lost["result"]["error"]["message"]
        events = [event async for event in second.events("exe-crashed")]
        assert [event["type"] for event in events] == ["state", "result"]
    finally:
        await second.manager.close()
        store.close()


async def test_closing_the_worker_loses_running_attempts(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    first, store = await start_worker(aiohttp_client, tmp_path)
    execution = await first.submit("op-long", kind="shell", script="sleep 60")
    await first.wait_running(execution["execution_id"])
    await first.manager.close()
    try:
        record = store.execution(execution["execution_id"])
        assert record is not None
        assert record.state == "lost"
    finally:
        store.close()


async def test_a_sage_kernel_is_offered_when_its_kernelspec_exists(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    started, store = await start_worker(aiohttp_client, tmp_path / "state")
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
        }
        assert sage["result"]["outputs"][0]["data"]["text/plain"] == "'sage'"
        # Each language has its own kernel and state.
        assert python["result"]["outputs"][0]["data"]["text/plain"] == "'python'"
        status, kernel = await started.post(
            "/v1/kernels/restart", {"context": CONTEXT, "language": "sage"}
        )
        assert (status, kernel["language"], kernel["generation"]) == (200, "sage", 2)
    finally:
        await started.manager.close()
        store.close()


async def test_a_language_without_a_kernel_is_unsupported(worker: Worker) -> None:
    execution = await worker.run("op-sage", kind="cell", language="sage", code="1")
    assert execution["state"] == "failed"
    assert execution["result"]["error"]["kind"] == "unsupported"


async def test_a_reader_leaving_its_event_stream_does_not_stop_the_execution(
    worker: Worker,
) -> None:
    # The test server cancels the handler of a closed connection, as any aiohttp
    # server does when it shuts down. The execution and other readers carry on.
    execution = await worker.submit("op-leave", kind="shell", script="sleep 0.5; echo done")
    response = await worker.client.get(
        f"/v1/executions/{execution['execution_id']}/events", headers=AUTH
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


async def test_the_probe_lowers_an_overstated_tier(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    backend = OverstatedBackend(LocalBackendSettings(), tmp_path)
    started, store = await start_worker(aiohttp_client, tmp_path, backend=backend)
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
        await started.manager.close()
        store.close()


async def test_a_failed_probe_means_tier_0(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = ExecutorChannel.request

    async def refuse_probe(self: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "probe":
            raise WorkerError(ErrorKind.INVALID_REQUEST, "unknown operation 'probe'")
        return await request(self, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", refuse_probe)
    backend = OverstatedBackend(LocalBackendSettings(), tmp_path)
    started, store = await start_worker(aiohttp_client, tmp_path, backend=backend)
    try:
        execution = await started.run("op-where", kind="shell", script="echo still runs")
        assert execution["result"]["stdout"]["text"] == "still runs\n"
        location = execution["location"]
        assert (location["isolation_tier"], location["declared_isolation_tier"]) == (0, 3)
        evidence = location["isolation_evidence"]
        assert evidence["error"] == "unknown operation 'probe'"
        assert evidence["sandbox"] is None
    finally:
        await started.manager.close()
        store.close()


async def test_closing_a_session_stops_its_kernels_only(worker: Worker) -> None:
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


async def test_closing_a_session_starts_no_executor(worker: Worker) -> None:
    idle = {**CONTEXT, "environment_id": "env-idle"}
    status, closed = await worker.post("/v1/sessions/close", {"context": idle})
    assert (status, closed) == (200, {"kernels": []})
    assert "env-idle" not in worker.manager._environments  # pyright: ignore[reportPrivateUsage]


async def test_an_idle_kernel_is_stopped_and_its_state_reported_lost(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    root = tmp_path / "worker"
    manager = ExecutionManager(
        Store(root / "worker.sqlite3"),
        LocalBackend(LocalBackendSettings(), root, ["--kernel-idle", "0.5"]),
        Blobs(root / "artifacts"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    try:
        idle = Worker(await aiohttp_client(create_app(manager, TOKEN)), manager, root)
        await idle.run("op-i1", kind="cell", code="x = 1")
        await asyncio.sleep(1.5)
        after = await idle.run("op-i2", kind="cell", code="x")
        assert after["result"]["state_lost"] is True
        assert after["result"]["error"]["name"] == "NameError"
        assert after["result"]["kernel"]["generation"] == 2
    finally:
        await manager.close()


async def test_activity_reports_use_running_calls_and_kernel_cpu(worker: Worker) -> None:
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


async def test_cpu_used_by_a_kernel_thread_counts_as_activity(worker: Worker) -> None:
    spin = (
        "import threading, time\n"
        "def spin():\n"
        "    end = time.time() + 4\n"
        "    while time.time() < end:\n"
        "        pass\n"
        "threading.Thread(target=spin, daemon=True).start()"
    )
    await worker.run("op-t1", kind="cell", code=spin)
    first = await worker.manager.activity("env1")
    await asyncio.sleep(1.5)
    second = await worker.manager.activity("env1")
    assert first is not None
    assert second is not None
    assert first.cpu_seconds is not None
    assert second.cpu_seconds is not None
    assert second.cpu_seconds - first.cpu_seconds > 0.5
    assert second.running == 0


async def test_an_idle_executor_is_released_and_its_kernels_reported_lost(worker: Worker) -> None:
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
    assert "env1" not in manager._environments  # pyright: ignore[reportPrivateUsage]
    assert await manager.release_if_idle("env1", time.monotonic())  # nothing left to stop
    after = await worker.run("op-r3", kind="cell", code="x")
    assert after["result"]["state_lost"] is True
    assert after["result"]["error"]["name"] == "NameError"
