# SPDX-License-Identifier: MIT
"""Regression tests for the adversarial review of the worker (2026-10-11).

Each test names the finding it covers (C1, H1 to H4, M1 to M5, or a Low item)
and repeats its probe: through a real executor on the local backend, or through
a stand-in for an executor that research code has taken over
(hostile_executor.py). What an executor sends is untrusted: none of it may wedge
an execution, end the connection, leave an executor running or exhaust the
worker, and every execution ends in a state that is stored and replays.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from sandbox_science.contracts import (
    ArtifactRef,
    Execution,
    ExecutionContext,
    ExecutionState,
    StreamEvent,
)
from sandbox_science.testing import eventually
from sandbox_science.worker import manager as manager_module
from sandbox_science.worker.app import create_app
from sandbox_science.worker.backends import ClosedHandler, LocalBackend, OpenShellBackend
from sandbox_science.worker.channel import ChannelTimeouts, EventHandler, ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import LocalBackendSettings, OpenShellBackendSettings
from sandbox_science.worker.store import Store
from sandbox_science.worker.timeouts import ManagerTimeouts

from .harness import AUTH, CONTEXT, HOSTILE, TOKEN, Harness, HostileBackend

OTHER_SESSION = {**CONTEXT, "session_id": "s2"}

pytestmark = pytest.mark.asyncio


def _running(channel: ExecutorChannel) -> bool:
    process = channel._process
    return process is not None and process.returncode is None


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as started:
        yield started


def _gone(pid: int) -> bool:
    for _ in range(100):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                state = handle.read().rsplit(b")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == b"Z":
            return True
        time.sleep(0.05)
    return False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# C1: the worker always starts again.


async def test_c1_a_worker_starts_again_after_a_crash(tmp_path: Path) -> None:
    # A crash left the record's last_seq behind the stored events: the restart reused a
    # sequence number, failed on the events table's key, and so did every later start.
    now = datetime.now(UTC)
    store = Store(tmp_path / "worker.sqlite3")
    crashed = Execution(
        execution_id="exe-crashed",
        attempt_id="att-crashed",
        operation_id="op-crashed",
        kind="shell",
        context=ExecutionContext.model_validate(CONTEXT),
        state=ExecutionState.RUNNING,
        submitted_at=now,
        last_seq=2,
    )
    store.insert_execution(crashed, "digest")
    for seq in range(1, 8):
        text = f"line {seq}\n"
        store.add_event(
            StreamEvent(execution_id="exe-crashed", seq=seq, time=now, name="stdout", text=text)
        )
    # A record that does not validate any more must not stop the start either.
    store._db.execute(
        "INSERT INTO executions(execution_id,operation_id,request_digest,environment_id,"
        "session_id,terminal,record) VALUES ('exe-bad', 'op-bad', 'd', 'env1', 's1', 0, "
        "'{\"x\": 1}')"
    )
    store.close()
    started = await Harness.start(tmp_path)
    try:
        lost = await started.finish("exe-crashed")
        assert lost["state"] == "lost"
        assert lost["last_seq"] == 9
        events = [event async for event in started.events("exe-crashed")]
        assert [event["seq"] for event in events] == list(range(1, 10))
        assert events[-1]["type"] == "result"
        assert started.store.unfinished() == []  # the bad record is not retried at every start
        status, body = await started.get("/v1/executions/exe-bad")
        assert (status, body["error"]["kind"]) == (500, "internal")
        again = await started.run("op-after", kind="shell", script="echo back")
        assert again["result"]["stdout"]["text"] == "back\n"
    finally:
        await started.close()


async def test_c1_a_store_with_delegated_run_executions_is_refused(tmp_path: Path) -> None:
    # Delegated runs were removed; their old executions must not be read as ordinary ones.
    path = tmp_path / "worker.sqlite3"
    Store(path).close()
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO executions VALUES ('exe-run', 'op-run', 'd', 'env1', 's1', 0, '{}',"
            " 'run-v1')"
        )
    with pytest.raises(WorkerError, match="delegated-run executions"):
        Store(path)


def _served(tmp_path: Path) -> tuple[str, Callable[[str], Awaitable[subprocess.Popen[bytes]]]]:
    """The URL of a ``sandbox-science-worker serve`` process, and how to start one."""
    port = _free_port()
    (tmp_path / "worker.token").write_text(TOKEN + "\n")
    (tmp_path / "worker.toml").write_text(
        f'listen = "127.0.0.1:{port}"\ntoken_file = "worker.token"\nstate_dir = "state"\n'
        f'pool_id = "test"\n\n[local]\npython = "{sys.executable}"\n'
    )
    command = [
        str(Path(sys.executable).parent / "sandbox-science-worker"),
        "serve",
        "--settings",
        str(tmp_path / "worker.toml"),
    ]

    async def launch(name: str) -> subprocess.Popen[bytes]:
        log = (tmp_path / name).open("wb")
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(200):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
                return process
            assert process.poll() is None, (tmp_path / name).read_text()
            await asyncio.sleep(0.05)
        raise AssertionError("the worker did not start")

    return f"http://127.0.0.1:{port}", launch


async def test_c1_sigterm_stops_the_worker_cleanly_and_it_starts_again(tmp_path: Path) -> None:
    # SIGTERM (a systemd stop) ran no cleanup; with output stored, no start succeeded after.
    url, launch = _served(tmp_path)
    first = await launch("first.log")
    async with aiohttp.ClientSession(headers=AUTH) as http:
        script = "for i in 1 2 3 4 5; do echo line $i; sleep 0.1; done; sleep 60"
        body = {"operation_id": "op-term", "kind": "shell", "context": CONTEXT, "script": script}
        async with http.post(f"{url}/v1/executions", json=body) as response:
            execution_id = (await response.json())["execution_id"]
        printed = ""
        async with http.get(f"{url}/v1/executions/{execution_id}/events") as response:
            async for line in response.content:
                if line.strip() and json.loads(line)["type"] == "stream":
                    printed += json.loads(line)["text"]
                    if "line 5" in printed:
                        break
        first.send_signal(signal.SIGTERM)
        assert await asyncio.to_thread(first.wait, 30) == 0
        second = await launch("second.log")
        try:
            async with http.get(f"{url}/v1/executions/{execution_id}") as response:
                stored = await response.json()
            assert response.status == 200, stored
            assert stored["state"] == "lost"
            assert "stopped" in stored["result"]["error"]["message"]
            async with http.get(f"{url}/v1/executions/{execution_id}/events") as response:
                lines = [json.loads(line) async for line in response.content if line.strip()]
            assert [event["seq"] for event in lines] == list(range(1, len(lines) + 1))
            assert lines[-1]["type"] == "result"
        finally:
            second.terminate()
            await asyncio.to_thread(second.wait, 30)


async def test_sigterm_ends_open_event_streams_instead_of_waiting_for_them(
    tmp_path: Path,
) -> None:
    # A connected event stream held shutdown until aiohttp gave up on it after 60 s.
    url, launch = _served(tmp_path)
    worker = await launch("worker.log")
    try:
        async with aiohttp.ClientSession(headers=AUTH) as http:
            body = {
                "operation_id": "op-stream",
                "kind": "shell",
                "context": CONTEXT,
                "script": "echo started; sleep 60",
            }
            async with http.post(f"{url}/v1/executions", json=body) as response:
                execution_id = (await response.json())["execution_id"]
            async with http.get(f"{url}/v1/executions/{execution_id}/events") as response:
                async for line in response.content:
                    if line.strip() and "started" in json.loads(line).get("text", ""):
                        break
                worker.send_signal(signal.SIGTERM)
                # Far below the 60 s, and the 15 s heartbeat, the stream held it before.
                assert await asyncio.to_thread(worker.wait, 10) == 0
                rest = [json.loads(line) async for line in response.content if line.strip()]
        # The stream ended early, without a result: the client asks again with ``after``.
        assert all(event["type"] != "result" for event in rest)
    finally:
        if worker.poll() is None:
            worker.kill()
            await asyncio.to_thread(worker.wait, 30)


# H2: an execution always ends, in a state that is stored and replays.


@pytest.mark.parametrize("mode", ["inf", "big_float", "surrogate", "huge_numbers"])
async def test_h2_values_that_could_not_be_stored_are_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Later reads come from the store.
    timeouts = ManagerTimeouts(retain_live=0.01)
    started = await Harness.start(tmp_path, backend=HostileBackend(mode), timeouts=timeouts)
    try:
        execution = await started.run("op-x", kind="shell", script="true")
        assert execution["state"] == "succeeded"
        await eventually(lambda: execution["execution_id"] not in started.view.live_executions())
        status, stored = await started.get(f"/v1/executions/{execution['execution_id']}")
        assert status == 200
        result = Execution.model_validate(stored).result
        assert result is not None
        if mode == "surrogate":
            assert result.stdout is not None
            assert result.stdout.text == "� and �"
            assert result.written_files == ("�.txt",)
        if mode == "huge_numbers":
            assert (result.exit_code, result.signal, result.execution_count) == (None, None, None)
        replay = [event async for event in started.events(execution["execution_id"])]
        assert replay[-1]["type"] == "result"
        status, _ = await started.post("/v1/artifacts", {"context": CONTEXT, "path": "x.txt"})
        assert status == 201
    finally:
        await started.close()
    restarted = await Harness.start(tmp_path)
    await restarted.close()


async def test_h2_a_malformed_hello_fails_the_attempt_and_stops_its_executor(
    tmp_path: Path,
) -> None:
    backend = HostileBackend("hello_bad")
    started = await Harness.start(tmp_path, backend=backend)
    try:
        for number in range(2):
            execution = await started.run(f"op-{number}", kind="shell", script="true")
            assert execution["state"] == "failed"
            assert execution["result"]["error"]["kind"] == "unavailable"
        assert len(backend.channels) == 2
        assert not any(_running(channel) for channel in backend.channels)
    finally:
        await started.close()


async def test_h2_a_missing_openshell_cli_fails_the_attempt(tmp_path: Path) -> None:
    settings = OpenShellBackendSettings(
        cli=str(tmp_path / "missing" / "openshell"),
        isolation_tier=3,
        sched_shim=False,
        sandboxes={"env1": "research-1"},
    )
    started = await Harness.start(tmp_path, backend=OpenShellBackend(settings, tmp_path))
    try:
        execution = await started.run("op-os", kind="shell", script="true")
        assert execution["state"] == "failed"
        assert execution["result"]["error"]["kind"] == "unavailable"
        status, body = await started.get("/v1/capabilities?environment_id=env1")
        assert (status, body["error"]["kind"]) == (503, "unavailable")
    finally:
        await started.close()


async def test_h2_an_unexpected_failure_in_dispatch_ends_the_attempt(tmp_path: Path) -> None:
    class Broken(LocalBackend):
        async def open(
            self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
        ) -> ExecutorChannel:
            raise RuntimeError("something nobody expected")

    started = await Harness.start(tmp_path, backend=Broken(LocalBackendSettings(), tmp_path))
    try:
        execution = await started.run("op-x", kind="shell", script="true")
        assert execution["state"] == "failed"
        assert execution["result"]["error"]["kind"] == "internal"
    finally:
        await started.close()


async def test_h2_a_stream_that_fails_once_started_drops_the_connection(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The middleware used to write a second HTTP response into the open stream.
    execution = await worker.run("op-x", kind="shell", script="echo hi")

    def broken(execution_id: str, after: int, limit: int = -1) -> list[Any]:
        raise RuntimeError("the store failed")

    monkeypatch.setattr(worker.manager.store, "events_after", broken)
    server = worker.client.server
    reader, writer = await asyncio.open_connection(server.host, server.port)
    request = (
        f"GET /v1/executions/{execution['execution_id']}/events HTTP/1.1\r\n"
        f"Host: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    )
    writer.write(request.encode())
    received = await asyncio.wait_for(reader.read(), 10)  # until the worker closes it
    writer.close()
    assert received.startswith(b"HTTP/1.1 200")
    assert received.count(b"HTTP/1.1") == 1


async def test_h2_a_client_leaving_a_stream_is_not_an_error(
    worker: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    # Lab 5: a client that stopped reading made the worker log an error with a traceback.
    # aiohttp's test server cancels a handler whose client leaves, so this needs a
    # runner like the worker's own, where the next write fails instead.
    caplog.set_level(logging.DEBUG, logger="sandbox_science.worker")
    runner = web.AppRunner(create_app(worker.manager, TOKEN))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        script = "for i in 1 2 3 4 5 6; do echo $i; sleep 0.2; done"
        execution = await worker.submit("op-left", kind="shell", script=script)
        events = f"{url}/v1/executions/{execution['execution_id']}/events"
        async with aiohttp.ClientSession(headers=AUTH) as http, http.get(events) as response:
            async for line in response.content:
                if line.strip() and json.loads(line)["type"] == "stream":
                    break  # the client goes away
        finished = await worker.finish(execution["execution_id"])
        assert finished["state"] == "succeeded"
    finally:
        await runner.cleanup()
    assert "a client left the event stream" in caplog.text
    worker_errors = [
        record
        for record in caplog.records
        if record.name.startswith("sandbox_science.worker") and record.levelno >= logging.ERROR
    ]
    assert worker_errors == []


async def test_h2_quiet_streams_carry_heartbeats(tmp_path: Path) -> None:
    # The call ends only once the stream has carried three heartbeats.
    gate = tmp_path / "gate"
    script = f"until [ -e {gate} ]; do sleep 0.01; done"
    async with await Harness.start(tmp_path / "worker", heartbeat=0.05) as worker:
        execution = await worker.submit("op-quiet", kind="shell", script=script)
        lines: list[bytes] = []
        async with worker.client.get(
            f"/v1/executions/{execution['execution_id']}/events", headers=AUTH
        ) as response:
            while line := await response.content.readline():
                lines.append(line)
                if lines.count(b"\n") == 3:
                    gate.touch()
    assert lines.count(b"\n") >= 3
    assert json.loads(lines[-1])["type"] == "result"


# H3: one bad line no longer ends the connection, nor leaves an executor running.


async def test_h3_bad_lines_do_not_end_the_connection(tmp_path: Path) -> None:
    backend = HostileBackend("bad_lines")
    started = await Harness.start(tmp_path, backend=backend)
    try:
        for number in range(2):
            execution = await started.run(f"op-{number}", kind="shell", script="true")
            assert execution["state"] == "succeeded"
            assert execution["result"]["stdout"]["text"] == "after\n"
        (channel,) = backend.channels  # the same executor all along
        assert not channel.closed
    finally:
        await started.close()
    assert not _running(channel)


async def test_h3_a_huge_exception_fails_only_its_cell(worker: Harness) -> None:
    # Honest research code: the end line was over the limit, so the reader ended and
    # every running execution in the environment was lost.
    bystander = await worker.submit(
        "op-bystander", OTHER_SESSION, kind="shell", script="sleep 2; echo done"
    )
    cell = await worker.run("op-big", kind="cell", code='raise ValueError("x" * 25_000_000)')
    assert cell["state"] == "failed"
    error = cell["result"]["error"]
    assert (error["kind"], error["name"]) == ("exception", "ValueError")
    assert len(error["value"]) < 100_000
    assert len(error["message"]) < 100_000
    assert (await worker.finish(bystander["execution_id"]))["state"] == "succeeded"


async def test_h3_research_code_cannot_write_into_the_protocol(worker: Harness) -> None:
    # A shell call wrote into the executor's protocol pipe through /proc/$PPID/fd.
    if os.geteuid() == 0:
        pytest.skip("root may open any process's /proc files")
    bystander = await worker.submit(
        "op-bystander", OTHER_SESSION, kind="shell", script="sleep 2; echo done"
    )
    script = (
        "for fd in /proc/$PPID/fd/*; do\n"
        '  printf \'{"type":"reply","id":"x"}\\n\' > "$fd" 2>/dev/null && echo wrote\n'
        "done\n"
        "ls /proc/$PPID/fd >/dev/null 2>&1 || echo closed"
    )
    injected = await worker.run("op-inject", kind="shell", script=script)
    assert injected["result"]["stdout"]["text"] == "closed\n"
    assert (await worker.finish(bystander["execution_id"]))["state"] == "succeeded"


# H4: the worker bounds what executors send.


async def test_h4_output_beyond_the_budget_is_not_stored(tmp_path: Path) -> None:
    backend = HostileBackend("flood", env={**os.environ, "FLOOD_MIB": "16"})
    started = await Harness.start(tmp_path, backend=backend)
    try:
        execution = await started.run(
            "op-flood", kind="shell", script="true", output_limit_bytes=1024
        )
        events = [event async for event in started.events(execution["execution_id"])]
        stored = sum(len(event.get("text", "")) for event in events)
        assert stored < 128 * 1024
        assert ExecutionState.CANCELLING in [event.get("state") for event in events]
    finally:
        await started.close()
    assert (tmp_path / "worker.sqlite3").stat().st_size < 8 * 1024 * 1024


async def test_h4_the_number_of_events_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "MAX_EVENTS", 50)
    backend = HostileBackend("tinyflood", env={**os.environ, "FLOOD_N": "500"})
    started = await Harness.start(tmp_path, backend=backend)
    try:
        execution = await started.run("op-tiny", kind="shell", script="true")
        assert execution["state"] == "succeeded"
        events = [event async for event in started.events(execution["execution_id"])]
        assert sum(event["type"] == "stream" for event in events) == 50
    finally:
        await started.close()


@pytest.mark.parametrize("mode", ["sizelie", "badb64", "badoffset", "shrinks"])
async def test_h4_publication_checks_what_the_executor_sends(tmp_path: Path, mode: str) -> None:
    # The size of the first range was trusted: 32 MiB were stored under a 1 MiB limit.
    started = await Harness.start(
        tmp_path, backend=HostileBackend(mode), max_artifact_size=64 << 20
    )
    try:
        status, body = await started.post("/v1/artifacts", {"context": CONTEXT, "path": "a.bin"})
        assert status in (409, 500), body
        assert not (tmp_path / "artifacts" / "blobs").exists()
        assert list((tmp_path / "artifacts" / "tmp").iterdir()) == []
        if mode != "shrinks":  # each of its ranges is well formed on its own
            # M5: a malformed reply used to reach the harness as it was.
            status, body = await started.post("/v1/files/read", {"context": CONTEXT, "path": "a"})
            assert (status, body["error"]["kind"]) == (500, "internal")
    finally:
        await started.close()


async def test_h4_publication_stops_at_the_size_first_reported(tmp_path: Path) -> None:
    # An executor that never said eof made publication grow its temporary blob forever.
    started = await Harness.start(
        tmp_path, backend=HostileBackend("noeof"), max_artifact_size=64 << 20
    )
    try:
        status, artifact = await started.post(
            "/v1/artifacts", {"context": CONTEXT, "path": "a.bin"}
        )
        # Protected capture now rejects contradictory EOF metadata rather than
        # accepting a peer's size as a stopping hint. No bytes become an artifact.
        assert status == 500, artifact
        assert not (tmp_path / "artifacts" / "blobs").exists()
        assert list((tmp_path / "artifacts" / "tmp").iterdir()) == []
    finally:
        await started.close()


# M1, M2, M3: files, leftover processes and replay.


async def test_m1_a_fifo_does_not_stall_other_sessions(worker: Harness) -> None:
    await worker.run("op-fifo", kind="shell", script="mkfifo pipe")
    read = worker.post("/v1/files/read", {"context": CONTEXT, "path": "pipe"})
    status, body = await asyncio.wait_for(read, 10)
    assert status == 400
    assert "not a regular file" in body["error"]["message"]
    other = await worker.run("op-echo", OTHER_SESSION, kind="shell", script="echo hi")
    assert other["state"] == "succeeded"


async def test_m1_publication_hashes_the_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await Harness.start(tmp_path, max_artifact_size=64 << 20)
    try:
        await started.run("op-big", kind="shell", script="head -c 20000000 /dev/urandom > big")
        channel = started.view.channel("env1")
        reads: list[dict[str, Any]] = []
        request = channel.request

        async def recording(op: str, **fields: Any) -> Message:
            if op == "capture_read":
                reads.append(fields)
            return await request(op, **fields)

        monkeypatch.setattr(channel, "request", recording)
        status, artifact = await started.post("/v1/artifacts", {"context": CONTEXT, "path": "big"})
        assert status == 201, artifact
        assert artifact["size"] == 20000000
        assert [fields["hash"] for fields in reads] == [True, False, False]
    finally:
        await started.close()


async def test_m2_a_shell_call_leaves_no_processes_behind(worker: Harness) -> None:
    # setsid and job control (set -m) took processes out of the call's process group;
    # env -i with a double fork also drops the call's mark. The kernel and another
    # session's call must survive the cleanup.
    await worker.run("op-x", kind="cell", code="x = 41")
    other = await worker.submit("op-other", OTHER_SESSION, kind="shell", script="sleep 30")
    await worker.wait_running(other["execution_id"])
    script = (
        "setsid sleep 300 >/dev/null 2>&1 </dev/null & echo $! > pid-setsid\n"
        "set -m; sleep 301 >/dev/null 2>&1 </dev/null & echo $! > pid-jobs; set +m\n"
        "env -i PATH=/usr/bin:/bin sh -c "
        "'setsid sleep 302 >/dev/null 2>&1 </dev/null & echo $! > pid-scrubbed'\n"
        "sleep 0.5; echo started"
    )
    escaped = await worker.run("op-escape", kind="shell", script=script)
    assert escaped["state"] == "succeeded", escaped["result"]
    for name in ("pid-setsid", "pid-jobs", "pid-scrubbed"):
        assert _gone(int((worker.workspace / name).read_text())), name
    after = await worker.run("op-x-again", kind="cell", code="x")
    assert after["result"]["outputs"][0]["data"]["text/plain"] == "41"
    assert after["result"]["kernel"]["generation"] == 1
    status, _ = await worker.post(f"/v1/executions/{other['execution_id']}/cancel", {})
    assert status == 200
    assert (await worker.finish(other["execution_id"]))["state"] == "cancelled"


async def test_m3_replay_reads_the_store_a_page_at_a_time(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = "for i in $(seq 1 12); do echo $i; sleep 0.02; done"
    execution = await worker.run("op-many", kind="shell", script=script)
    monkeypatch.setattr(manager_module, "EVENT_PAGE", 2)
    limits: list[int] = []
    events_after = worker.manager.store.events_after

    def paged(execution_id: str, after: int, limit: int = -1) -> list[Any]:
        limits.append(limit)
        return events_after(execution_id, after, limit)

    monkeypatch.setattr(worker.manager.store, "events_after", paged)
    events = [event async for event in worker.events(execution["execution_id"])]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "result"
    assert set(limits) == {2}


# The Low items.


async def test_low_events_from_another_environment_are_ignored(tmp_path: Path) -> None:
    hostile_env: dict[str, str] = {}

    class Mixed(LocalBackend):
        async def open(
            self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
        ) -> ExecutorChannel:
            if environment_id == "env1":
                return await super().open(environment_id, on_event, on_closed)
            argv = [sys.executable, "-I", str(HOSTILE), "foreign"]
            channel = ExecutorChannel(argv, on_event, on_closed, env=hostile_env)
            await channel.start()
            return channel

    started = await Harness.start(tmp_path, backend=Mixed(LocalBackendSettings(), tmp_path))
    try:
        victim = await started.submit("op-victim", kind="shell", script="sleep 2; echo real")
        await started.wait_running(victim["execution_id"])
        hostile_env.update(os.environ, TARGET=victim["execution_id"])
        other = {**CONTEXT, "environment_id": "env2"}
        attacker = await started.run("op-attacker", other, kind="shell", script="true")
        assert attacker["state"] == "succeeded"
        final = await started.finish(victim["execution_id"])
        assert final["state"] == "succeeded"
        assert final["result"]["stdout"]["text"] == "real\n"
    finally:
        await started.close()


async def test_low_asking_for_events_after_the_last_ends_the_stream(worker: Harness) -> None:
    execution = await worker.run("op-x", kind="shell", script="true")

    async def collect() -> list[dict[str, Any]]:
        stream = worker.events(execution["execution_id"], after=execution["last_seq"])
        return [event async for event in stream]

    assert await asyncio.wait_for(collect(), 5) == []


async def test_low_environment_ids_must_be_identifiers(worker: Harness, tmp_path: Path) -> None:
    status, body = await worker.get("/v1/capabilities?environment_id=../../escaped")
    assert (status, body["error"]["kind"]) == (400, "invalid_request")
    assert not (tmp_path.parent / "escaped").exists()
    backend = LocalBackend(LocalBackendSettings(), tmp_path)

    async def ignore(*_: Any) -> None:
        return None

    with pytest.raises(WorkerError):
        await backend.open("../escaped", ignore, ignore)


async def test_low_a_long_session_id_runs_cells(worker: Harness) -> None:
    context = {**CONTEXT, "session_id": "s" * 120}
    cell = await worker.run("op-long", context, kind="cell", code="1 + 1")
    assert cell["state"] == "succeeded", cell["result"]
    assert len(cell["result"]["kernel"]["kernel_id"]) <= 128


async def test_low_media_types_are_checked_and_artifacts_served_as_downloads(
    worker: Harness,
) -> None:
    content = base64.b64encode(b"<script>alert(1)</script>").decode()
    await worker.post(
        "/v1/files/write", {"context": CONTEXT, "path": "page.txt", "content_b64": content}
    )
    publish = {"context": CONTEXT, "path": "page.txt"}
    status, body = await worker.post(
        "/v1/artifacts", {**publish, "media_type": "text/plain\r\nX-Injected: 1"}
    )
    assert (status, body["error"]["kind"]) == (400, "invalid_request")
    status, artifact = await worker.post("/v1/artifacts", {**publish, "media_type": "text/html"})
    assert status == 201
    response = await worker.client.get(
        f"/v1/artifacts/{artifact['artifact_id']}/content", headers=AUTH
    )
    assert response.headers["Content-Type"] == "text/html"
    assert response.headers["Content-Disposition"] == "attachment"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"].startswith("sandbox")
    # One stored before media types were checked is served as plain bytes.
    legacy = ArtifactRef.model_validate(
        {**artifact, "artifact_id": "art-legacy", "media_type": "text/plain\r\nX-Injected: 1"}
    )
    worker.manager.store.insert_artifact(legacy)
    response = await worker.client.get("/v1/artifacts/art-legacy/content", headers=AUTH)
    assert response.status == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert "X-Injected" not in response.headers


async def test_low_an_authorization_header_that_is_not_utf8_is_refused(worker: Harness) -> None:
    server = worker.client.server
    reader, writer = await asyncio.open_connection(server.host, server.port)
    writer.write(
        b"GET /v1/executions/x HTTP/1.1\r\nHost: x\r\n"
        b"Authorization: Bearer \xff\r\nConnection: close\r\n\r\n"
    )
    status = await asyncio.wait_for(reader.readline(), 10)
    writer.close()
    assert status.startswith(b"HTTP/1.1 401")


async def test_low_requests_can_name_the_project(worker: Harness) -> None:
    execution = await worker.run("op-x", kind="shell", script="echo x > x.txt")
    execution_id = execution["execution_id"]
    status, _ = await worker.get(f"/v1/executions/{execution_id}?project_id=p1")
    assert status == 200
    for path in (
        f"/v1/executions/{execution_id}?project_id=p2",
        f"/v1/executions/{execution_id}/events?project_id=p2",
    ):
        status, body = await worker.get(path)
        assert (status, body["error"]["kind"]) == (404, "not_found"), path
    status, _ = await worker.post(f"/v1/executions/{execution_id}/cancel?project_id=p2", {})
    assert status == 404
    status, artifact = await worker.post("/v1/artifacts", {"context": CONTEXT, "path": "x.txt"})
    assert status == 201
    for suffix in ("", "/content"):
        response = await worker.client.get(
            f"/v1/artifacts/{artifact['artifact_id']}{suffix}?project_id=p2", headers=AUTH
        )
        assert response.status == 404
    status, _ = await worker.get(f"/v1/executions/{execution_id}?project_id=..%2Fx")
    assert status == 400


async def test_an_executor_that_stops_reading_fails_its_requests_in_time(
    tmp_path: Path,
) -> None:
    # Research code can stop its executor; a request must not wait for it forever. Nor
    # does closing the worker wait the full grace for an executor that does not exit.
    timeouts = ChannelTimeouts(write=0.5, close_grace=0.5)
    started = await Harness.start(tmp_path, backend=HostileBackend("deaf", timeouts=timeouts))
    try:
        payload = base64.b64encode(b"x" * (4 << 20)).decode()
        began = time.monotonic()
        status, body = await started.post(
            "/v1/files/write", {"context": CONTEXT, "path": "big.bin", "content_b64": payload}
        )
        assert (status, body["error"]["kind"]) == (503, "unavailable")
        assert "stopped reading" in body["error"]["message"]
        assert time.monotonic() - began < 10
    finally:
        await started.close()


async def test_a_forged_cpu_time_is_no_activity(tmp_path: Path) -> None:
    started = await Harness.start(tmp_path, backend=HostileBackend("huge_cpu"))
    try:
        await started.manager.capabilities("env1")
        assert await started.manager.activity("env1") is None
    finally:
        await started.close()
