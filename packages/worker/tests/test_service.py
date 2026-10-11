# SPDX-License-Identifier: MIT
"""The worker API against the acceptance fixtures of docs/specs/tools.md.

A worker service on the local backend (a real executor and real kernels) runs
behind an aiohttp test client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import Execution, ExecutionContext, ExecutionState
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
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
    aiohttp_client: ClientFactory, root: Path, python: str = sys.executable
) -> tuple[Worker, Store]:
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=python), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
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


async def test_shell_basics_and_location(worker: Worker) -> None:
    execution = await worker.run(
        "op-shell", kind="shell", script="echo hello; echo err >&2; exit 3"
    )
    Execution.model_validate(execution)
    assert execution["state"] == "failed"
    result = execution["result"]
    assert result["exit_code"] == 3
    assert result["stdout"]["text"] == "hello\n"
    assert result["stderr"]["text"] == "err\n"
    location = execution["location"]
    assert location["backend"] == "local"
    assert location["isolation_tier"] == 0
    assert location["pool_id"] == "test-pool"
    assert location["hostname"] == socket.gethostname()


async def test_argv_runs_without_a_shell(worker: Worker) -> None:
    execution = await worker.run("op-argv", kind="shell", argv=["printf", "%s|", "$HOME", "a b"])
    assert execution["state"] == "succeeded"
    assert execution["result"]["stdout"]["text"] == "$HOME|a b|"


async def test_stateful_cells(worker: Worker) -> None:
    first = await worker.run("op-c1", kind="cell", code="x = 41")
    second = await worker.run("op-c2", kind="cell", code="x + 1")
    assert first["state"] == second["state"] == "succeeded"
    assert second["result"]["outputs"] == [
        {"type": "result", "data": {"text/plain": "42"}, "execution_count": 2}
    ]
    assert first["result"]["kernel"] == second["result"]["kernel"]
    assert second["result"]["kernel"]["generation"] == 1
    assert second["result"]["state_lost"] is False


async def test_cell_exceptions_and_latex_outputs(worker: Worker) -> None:
    failed = await worker.run("op-err", kind="cell", code="1/0")
    assert failed["state"] == "failed"
    error = failed["result"]["error"]
    assert (error["kind"], error["name"]) == ("exception", "ZeroDivisionError")
    assert any("ZeroDivisionError" in line for line in error["traceback"])
    latex = await worker.run(
        "op-latex",
        kind="cell",
        code="from IPython.display import Math, display\ndisplay(Math(r'\\frac{1}{2}'))",
    )
    (output,) = latex["result"]["outputs"]
    assert output["type"] == "display"
    assert output["data"]["text/latex"] == "$\\displaystyle \\frac{1}{2}$"


async def test_publication(worker: Worker) -> None:
    shell = await worker.run("op-write", kind="shell", script="printf 'a,b\\n1,2\\n' > out.csv")
    status, artifact = await worker.post("/v1/artifacts", {"context": CONTEXT, "path": "out.csv"})
    assert status == 201, artifact
    assert artifact["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert artifact["media_type"] == "text/csv"
    assert artifact["size"] == 8
    assert artifact["produced_by"] == shell["attempt_id"]
    response = await worker.client.get(
        f"/v1/artifacts/{artifact['artifact_id']}/content", headers=AUTH
    )
    assert await response.read() == b"a,b\n1,2\n"
    assert response.headers["X-Content-SHA256"] == artifact["sha256"]


async def test_interrupting_a_cell_keeps_its_state(worker: Worker) -> None:
    await worker.run("op-x", kind="cell", code="x = 41")
    sleeping = await worker.submit("op-sleep", kind="cell", code="import time\ntime.sleep(60)")
    await worker.wait_running(sleeping["execution_id"])
    await asyncio.sleep(0.5)
    started = asyncio.get_running_loop().time()
    status, _ = await worker.post(f"/v1/executions/{sleeping['execution_id']}/cancel", {})
    assert status == 200
    cancelled = await worker.finish(sleeping["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert asyncio.get_running_loop().time() - started < 5
    after = await worker.run("op-x-again", kind="cell", code="x")
    assert after["result"]["outputs"][0]["data"]["text/plain"] == "41"
    assert after["result"]["state_lost"] is False


async def test_cancelling_a_shell_call_stops_its_processes(worker: Worker, tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    execution = await worker.submit(
        "op-cancel",
        kind="shell",
        script=f"sleep 60 & echo $! > {pid_file}; sleep 60",
    )
    await worker.wait_running(execution["execution_id"])
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.1)
    await worker.post(f"/v1/executions/{execution['execution_id']}/cancel", {})
    cancelled = await worker.finish(execution["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["result"]["error"]["kind"] == "cancelled"
    background = int(pid_file.read_text())
    try:
        os.kill(background, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("the background process survived")


async def test_reconnecting_replays_each_event_once(worker: Worker) -> None:
    code = "import time\nfor i in range(1, 11):\n    print(i, flush=True)\n    time.sleep(0.3)"
    execution = await worker.submit("op-stream", kind="cell", code=code)
    execution_id = execution["execution_id"]
    seen: list[dict[str, Any]] = []
    streams = 0
    async for event in worker.events(execution_id):
        seen.append(event)
        if event["type"] == "stream":
            streams += 1
            if streams == 3:
                break  # the client goes away
    rest = [event async for event in worker.events(execution_id, after=seen[-1]["seq"])]
    sequence = [event["seq"] for event in seen + rest]
    assert sequence == list(range(1, len(sequence) + 1))
    assert sum(event["type"] == "result" for event in seen + rest) == 1
    printed = "".join(event["text"] for event in seen + rest if event["type"] == "stream")
    assert printed.split() == [str(i) for i in range(1, 11)]
    final = rest[-1]["execution"]
    assert final["state"] == "succeeded"
    assert final["last_seq"] == rest[-1]["seq"]


async def test_idempotent_submission(worker: Worker) -> None:
    first = await worker.submit("op-same", kind="shell", script="echo once")
    again = await worker.submit("op-same", kind="shell", script="echo once")
    assert again["execution_id"] == first["execution_id"]
    status, body = await worker.post(
        "/v1/executions",
        {"operation_id": "op-same", "context": CONTEXT, "kind": "shell", "script": "echo twice"},
    )
    assert status == 409
    assert body["error"]["kind"] == "conflict"
    finished = await worker.finish(first["execution_id"])
    assert finished["result"]["stdout"]["text"] == "once\n"


async def test_restart_loses_state_and_says_so(worker: Worker) -> None:
    await worker.run("op-r1", kind="cell", code="x = 41")
    status, kernel = await worker.post(
        "/v1/kernels/restart", {"context": CONTEXT, "language": "python"}
    )
    assert status == 200, kernel
    assert kernel["generation"] == 2
    after = await worker.run("op-r2", kind="cell", code="x")
    assert after["result"]["error"]["name"] == "NameError"
    assert after["result"]["kernel"]["generation"] == 2
    assert after["result"]["state_lost"] is True


async def test_kernel_death_is_reported(worker: Worker) -> None:
    await worker.run("op-d1", kind="cell", code="x = 41")
    died = await worker.run("op-d2", kind="cell", code="import os\nos._exit(1)")
    assert died["state"] == "failed"
    assert died["result"]["error"]["kind"] == "kernel_died"
    after = await worker.run("op-d3", kind="cell", code="1 + 1")
    assert after["result"]["kernel"]["generation"] == 2
    assert after["result"]["state_lost"] is True


async def test_output_bounds(worker: Worker) -> None:
    execution = await worker.run(
        "op-big",
        kind="shell",
        script="head -c 5242880 /dev/zero | tr '\\0' a",
        output_limit_bytes=65536,
    )
    stdout = execution["result"]["stdout"]
    assert stdout["truncated"] is True
    assert stdout["total_bytes"] == 5242880
    assert "bytes omitted" in stdout["text"]
    assert len(stdout["text"]) < 70000


async def test_timeouts(worker: Worker) -> None:
    shell = await worker.run("op-t1", kind="shell", script="sleep 30", timeout_seconds=1)
    assert shell["state"] == "timed_out", shell
    cell = await worker.run(
        "op-t2", kind="cell", code="import time\ntime.sleep(30)", timeout_seconds=1
    )
    assert cell["state"] == "timed_out"
    assert cell["result"]["error"]["kind"] == "timed_out"


async def test_queued_cells_can_be_cancelled(worker: Worker) -> None:
    first = await worker.submit("op-q1", kind="cell", code="import time\ntime.sleep(30)")
    await worker.wait_running(first["execution_id"])
    second = await worker.submit("op-q2", kind="cell", code="'second'")
    await asyncio.sleep(0.3)
    await worker.post(f"/v1/executions/{second['execution_id']}/cancel", {})
    cancelled = await worker.finish(second["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["started_at"] is None
    await worker.post(f"/v1/executions/{first['execution_id']}/cancel", {})
    assert (await worker.finish(first["execution_id"]))["state"] == "cancelled"


async def test_file_round_trip_and_preconditions(worker: Worker) -> None:
    content = base64.b64encode(b"hello").decode()
    status, written = await worker.post(
        "/v1/files/write", {"context": CONTEXT, "path": "notes/a.txt", "content_b64": content}
    )
    assert status == 200, written
    status, read = await worker.post("/v1/files/read", {"context": CONTEXT, "path": "notes/a.txt"})
    assert base64.b64decode(read["content_b64"]) == b"hello"
    assert read["sha256"] == written["sha256"] == hashlib.sha256(b"hello").hexdigest()
    status, listing = await worker.post("/v1/files/list", {"context": CONTEXT, "path": "notes"})
    assert [entry["name"] for entry in listing["entries"]] == ["a.txt"]
    shared = {"context": CONTEXT, "path": "shared/data.txt", "content_b64": content}
    status, body = await worker.post("/v1/files/write", shared)
    assert status == 400
    assert "expected_sha256 or create_only" in body["error"]["message"]
    status, _ = await worker.post("/v1/files/write", {**shared, "create_only": True})
    assert status == 200
    status, body = await worker.post("/v1/files/write", {**shared, "expected_sha256": "0" * 64})
    assert status == 409
    assert body["error"]["kind"] == "conflict"


async def test_confinement(worker: Worker) -> None:
    await worker.run("op-link", kind="shell", script="ln -s /etc/passwd link")
    for path in ("../x", "/etc/passwd", "a/../b", "link"):
        status, body = await worker.post("/v1/files/read", {"context": CONTEXT, "path": path})
        assert status == 400, (path, body)
        assert body["error"]["kind"] == "invalid_request"


async def test_requests_need_the_token(worker: Worker) -> None:
    response = await worker.client.get("/v1/executions/exe-1")
    assert response.status == 401
    response = await worker.client.get(
        "/v1/executions/exe-1", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status == 401


async def test_unknown_executions_are_not_found(worker: Worker) -> None:
    status, body = await worker.get("/v1/executions/exe-missing")
    assert status == 404
    assert body["error"]["kind"] == "not_found"


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


async def test_capabilities(worker: Worker) -> None:
    status, body = await worker.get("/v1/capabilities?environment_id=env1")
    assert status == 200
    assert body["languages"] == ["python"]
    assert body["location"]["backend"] == "local"
