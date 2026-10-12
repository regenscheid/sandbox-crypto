# SPDX-License-Identifier: MIT
"""Sandbox-lifetime recovery never replays commands or trusts wider sandbox limits."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    CellRequest,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    ReadFileRequest,
    ShellRequest,
)
from sandbox_science.worker import daemon, executor
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend, daemon_token
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def request(operation: str, script: str = "sleep 60", **fields: Any) -> JobRequest:
    return JobRequest(
        operation_id=operation,
        context=CONTEXT,
        script=script,
        purpose="test recoverable search",
        expected_progress="eventual completion",
        **fields,
    )


class Worker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = Store(root / "worker.sqlite")
        self.backend = LocalBackend(LocalBackendSettings(daemon=True), root)
        self.manager = self.new_manager()

    def new_manager(self) -> ExecutionManager:
        return ExecutionManager(
            self.store,
            self.backend,
            Blobs(self.root / "blobs"),
            pool_id="test",
            max_artifact_size=1 << 20,
            state_dir=self.root,
        )

    async def restart(self) -> None:
        await self.manager.close()
        self.store.close()
        self.store = Store(self.root / "worker.sqlite")
        self.manager = self.new_manager()
        await self.manager.start()

    async def cleanup(self) -> None:
        # close() deliberately detaches. Explicit lifecycle shutdown is mandatory even
        # when an assertion failed before the current manager obtained its channel.
        channel: ExecutorChannel | None = None
        try:
            async with asyncio.timeout(35):
                await self.manager.capabilities("env1")
                channel = self.manager._environments["env1"].channel
                await self.manager.release("env1")
        finally:
            # Even a failed assertion that disabled polling cannot strand a daemon.
            if channel is not None:
                await asyncio.shield(channel.shutdown())
            await self.manager.close()
            self.store.close()


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Worker]:
    value = Worker(tmp_path)
    await value.manager.start()
    try:
        yield value
    finally:
        await value.cleanup()


async def running(manager: ExecutionManager, execution_id: str) -> None:
    async with asyncio.timeout(15):
        async for event in manager.events(execution_id, 0):
            if getattr(event, "state", None) is ExecutionState.RUNNING:
                return
    raise AssertionError("execution never ran")


@pytest.mark.asyncio
async def test_worker_restart_preserves_identity_events_logs_and_never_replays(
    worker: Worker,
) -> None:
    job, _ = await worker.manager.submit(
        request("restart", "printf run >> marker; printf before; sleep 1; printf after")
    )
    await running(worker.manager, job.execution_id)
    channel = worker.manager._live[job.execution_id].channel
    assert channel is not None
    incarnation = channel.incarnation
    await worker.restart()
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.attempt_id == job.attempt_id
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "beforeafter"
    recovered_channel = worker.manager._environments["env1"].channel
    assert recovered_channel is not None
    assert recovered_channel.incarnation == incarnation
    marker = await worker.manager.read_file(ReadFileRequest(context=CONTEXT, path="marker"))
    assert base64.b64decode(marker.content_b64) == b"run"
    events = worker.store.events_after(job.execution_id, 0)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert sum(event.type == "result" for event in events) == 1
    saved = worker.store.recovery(job.execution_id)
    assert saved is not None
    assert saved[2] > 0


@pytest.mark.asyncio
async def test_bridge_failure_recovers_without_losing_or_restarting_job(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("bridge", "printf before; sleep 1; printf after"))
    await running(worker.manager, job.execution_id)
    channel = worker.manager._live[job.execution_id].channel
    assert channel is not None
    assert channel._process is not None
    channel._process.kill()
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "beforeafter"
    recovered_channel = worker.manager._environments["env1"].channel
    assert recovered_channel is not None
    assert recovered_channel.incarnation == channel.incarnation


@pytest.mark.asyncio
async def test_connected_final_result_preserves_approved_output(worker: Worker) -> None:
    job, _ = await worker.manager.submit(
        request("output", f'{sys.executable} -I -c \'print("x"*600000,end="")\'')
    )
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "x" * 600000
    assert not result.result.stdout.truncated


@pytest.mark.asyncio
async def test_definite_refusal_is_immediately_terminal(worker: Worker) -> None:
    call, _ = await worker.manager.submit(
        ShellRequest(operation_id="badcwd", context=CONTEXT, script="true", cwd="missing")
    )
    result = await asyncio.wait_for(worker.manager.wait(call.execution_id), 10)
    assert result.state is ExecutionState.FAILED
    assert result.result is not None
    assert result.result.error is not None
    assert result.result.error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_cancel_persisted_while_disconnected_is_recovered(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("cancel"))
    await running(worker.manager, job.execution_id)
    worker.store.cancel_intent(job.execution_id)
    await worker.restart()
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert result.state is ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_kernel_globals_survive_worker_restart(worker: Worker) -> None:
    first, _ = await worker.manager.submit(
        CellRequest(operation_id="kernel1", context=CONTEXT, language="python", code="value = 1729")
    )
    ended = await asyncio.wait_for(worker.manager.wait(first.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.result is not None
    kernel = ended.result.kernel
    await worker.restart()
    second, _ = await worker.manager.submit(
        CellRequest(operation_id="kernel2", context=CONTEXT, language="python", code="print(value)")
    )
    ended = await asyncio.wait_for(worker.manager.wait(second.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.result is not None
    assert ended.result.stdout is not None
    assert ended.result.stdout.text == "1729\n"
    assert ended.result.kernel == kernel


@pytest.mark.asyncio
async def test_unknown_control_requests_are_cancelled_during_recovery(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("known", "sleep 1; printf kept"))
    await running(worker.manager, job.execution_id)
    channel = worker.manager._live[job.execution_id].channel
    assert channel is not None
    unknown = "exe-" + "f" * 24
    await channel.request("shell", execution=unknown, session="s1", script="sleep 60", timeout=100)
    await worker.restart()
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    assert unknown not in (await channel.request("active"))["executions"]
    assert (
        await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    ).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_withdrawn_provisioning_execution_is_cancelled_on_restart(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("provisioning"), origin="provision-test")
    await running(worker.manager, job.execution_id)
    await worker.restart()
    assert (
        await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    ).state is ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_corrupt_recovery_row_does_not_block_other_jobs(worker: Worker) -> None:
    bad, _ = await worker.manager.submit(request("bad"))
    good, _ = await worker.manager.submit(request("good", "sleep 1; echo good"))
    await running(worker.manager, good.execution_id)
    worker.store._db.execute(
        "UPDATE execution_recovery SET request = 'invalid' WHERE execution_id = ?",
        (bad.execution_id,),
    )
    await worker.restart()
    assert worker.manager.get(bad.execution_id).state is ExecutionState.LOST
    assert (
        await asyncio.wait_for(worker.manager.wait(good.execution_id), 10)
    ).state is ExecutionState.SUCCEEDED
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    assert bad.execution_id not in (await channel.request("active"))["executions"]


@pytest.mark.asyncio
async def test_daemon_incarnation_loss_reaps_escaped_children(worker: Worker) -> None:
    job, _ = await worker.manager.submit(
        request("incarnation", "setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > child; wait")
    )
    await running(worker.manager, job.execution_id)
    data = await worker.manager.read_file(ReadFileRequest(context=CONTEXT, path="child"))
    child = int(base64.b64decode(data.content_b64))
    channel = worker.manager._live[job.execution_id].channel
    assert channel is not None
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.LOST
    async with asyncio.timeout(5):
        while (info := executor.process_info(child)) is not None and not info.zombie:
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_output_charge_survives_restart(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("budget", "printf before; sleep 2; printf after"))
    await running(worker.manager, job.execution_id)
    async with asyncio.timeout(5):
        while worker.store.event_usage(job.execution_id)[1] == 0:
            await asyncio.sleep(0.02)
    previous = worker.store.event_usage(job.execution_id)
    await worker.restart()
    live = worker.manager._live[job.execution_id]
    assert live.events >= previous[0]
    assert live.stored >= previous[1]
    assert (
        await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    ).state is ExecutionState.SUCCEEDED


@pytest_asyncio.fixture
async def server(tmp_path: Path) -> AsyncIterator[daemon.Daemon]:
    instance = daemon.Daemon(tmp_path, "a" * 64)
    listener = await asyncio.start_unix_server(
        instance.client, daemon.address(instance.token, instance.root)
    )
    try:
        yield instance
    finally:
        listener.close()
        await listener.wait_closed()
        if instance.writer is not None:
            instance.writer.close()
            await instance.writer.wait_closed()
        await instance.executor.shutdown()
        await asyncio.gather(*list(instance.executor.tasks), return_exceptions=True)


async def raw_request(writer: asyncio.StreamWriter, number: int, **fields: Any) -> None:
    writer.write(executor.encode_line({"id": number, **fields}))
    await writer.drain()


@pytest.mark.asyncio
async def test_synchronous_admission_caps_buffered_burst(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "MAX_ACTIVE", 2)
    reader, writer = await daemon.connect(server.token, server.root)
    for number in range(6):
        await raw_request(
            writer,
            number,
            op="shell",
            execution=f"exe-{number:024x}",
            session="s1",
            script="sleep 60",
            timeout=100,
        )
    replies = [json.loads(await asyncio.wait_for(reader.readline(), 3)) for _ in range(6)]
    assert sum(reply.get("ok") is True for reply in replies) == 2
    assert sum(not item.ended for item in server.history.values()) == 2
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_rejected_admissions_are_bounded_and_replay_terminal_outcome(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "MAX_RECORDS", 4)
    reader, writer = await daemon.connect(server.token, server.root)
    for number in range(6):
        await raw_request(
            writer,
            number,
            op="shell",
            execution=f"exe-{number:024x}",
            session="s1",
            cwd="missing",
            script="true",
        )
    replies = [json.loads(await asyncio.wait_for(reader.readline(), 3)) for _ in range(6)]
    assert all(reply.get("ok") is False for reply in replies)
    assert len(server.history) <= 4
    assert all(item.ended for item in server.history.values())
    await raw_request(writer, 10, op="replay", execution=f"exe-{5:024x}", after=0)
    terminal = json.loads(await reader.readline())
    assert terminal["event"] == "end"
    assert terminal["state"] == "failed"
    assert terminal["result"]["error"]["kind"] == "invalid_request"
    assert json.loads(await reader.readline())["ended"] is True
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_rejected_reply_lost_at_detach_still_has_terminal_record(
    server: daemon.Daemon,
) -> None:
    _, writer = await daemon.connect(server.token, server.root)
    execution = "exe-" + "b" * 24
    await raw_request(
        writer, 1, op="shell", execution=execution, session="s1", cwd="missing", script="true"
    )
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    reader, writer = await daemon.connect(server.token, server.root)
    await raw_request(writer, 2, op="replay", execution=execution, after=0)
    assert json.loads(await asyncio.wait_for(reader.readline(), 3))["event"] == "end"
    assert json.loads(await reader.readline())["ended"] is True
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_single_active_bridge_and_mutual_authentication(server: daemon.Daemon) -> None:
    _, writer = await daemon.connect(server.token, server.root)
    with pytest.raises(ConnectionError, match="active"):
        await daemon.connect(server.token, server.root)
    # Wrong tokens address a different endpoint and cannot attach to the live daemon.
    with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
        await daemon.connect("b" * 64, server.root)
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_global_trim_selects_other_active_streams(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "JOURNAL_BYTES", 300)
    for number in range(2):
        execution = f"exe-{number:024x}"
        server.history[execution] = daemon.History()
        await server.send(
            {"type": "event", "execution": execution, "event": "state", "state": "running"}
        )
        if number == 0:
            await server.send(
                {"type": "event", "execution": execution, "event": "stream", "text": "x" * 200}
            )
    assert server.bytes <= daemon.JOURNAL_BYTES
    assert all(
        any(b'"state"' in data for data in item.events.values()) for item in server.history.values()
    )


@pytest.mark.asyncio
async def test_rollover_pins_same_attempt_and_reports_explicit_omission(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "EXECUTION_BYTES", 300)
    monkeypatch.setattr(daemon, "JOURNAL_BYTES", 1500)
    execution = "exe-" + "c" * 24
    server.history[execution] = daemon.History()
    await server.send(
        {"type": "event", "execution": execution, "event": "state", "state": "running"}
    )
    for _ in range(20):
        await server.send(
            {"type": "event", "execution": execution, "event": "stream", "text": "x" * 200}
        )
    await server.send(
        {
            "type": "event",
            "execution": execution,
            "event": "end",
            "state": "succeeded",
            "result": {"exit_code": 0},
        }
    )
    reader, writer = await daemon.connect(server.token, server.root)
    await raw_request(writer, 1, op="replay", execution=execution, after=0)
    messages = []
    while True:
        message = json.loads(await reader.readline())
        messages.append(message)
        if message.get("type") == "reply":
            break
    assert messages[0]["source_seq"] == 1
    assert any("omitted while disconnected" in message.get("text", "") for message in messages)
    assert any(message.get("event") == "end" for message in messages)
    assert messages[-1]["found"] is True
    assert messages[-1]["ended"] is True
    assert server.bytes <= daemon.JOURNAL_BYTES
    writer.close()
    await writer.wait_closed()


def test_daemon_token_is_stable_private_and_rejects_symlink(tmp_path: Path) -> None:
    token = daemon_token(tmp_path, "env1")
    assert daemon_token(tmp_path, "env1") == token
    assert len(token) == 64
    path = tmp_path / "daemon-tokens" / "env1"
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()
    path.symlink_to(tmp_path / "other")
    with pytest.raises(OSError, match="Too many levels"):
        daemon_token(tmp_path, "env1")


@pytest.mark.asyncio
async def test_cancel_during_pending_final_sync_cannot_acknowledge_end(worker: Worker) -> None:
    from typing import cast

    class Shared:
        entered: asyncio.Event = asyncio.Event()
        release: asyncio.Event = asyncio.Event()

        async def down(self, *_args: Any) -> list[str]:
            return []

        async def up(self, *_args: Any) -> list[str]:
            self.entered.set()
            await self.release.wait()
            return []

        def unchanged_writes(self, *_args: Any) -> set[str]:
            return set()

    shared = Shared()
    worker.manager.shared = cast(Any, shared)
    job, _ = await worker.manager.submit(request("final-sync", "printf final"))
    await asyncio.wait_for(shared.entered.wait(), 10)
    saved = worker.store.recovery(job.execution_id)
    assert saved is not None
    before = saved[2]
    await worker.manager.cancel(job.execution_id)
    saved_after = worker.store.recovery(job.execution_id)
    assert saved_after is not None
    assert saved_after[2] == before
    # The end is not durable yet. A fresh worker must replay it and complete sync.
    await worker.restart()
    ended = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.result is not None
    assert ended.result.stdout is not None
    assert ended.result.stdout.text == "final"
    assert (
        sum(event.type == "result" for event in worker.store.events_after(job.execution_id, 0)) == 1
    )


@pytest.mark.asyncio
async def test_corrupt_only_execution_still_quiesces_its_daemon(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("sole-corrupt"))
    await running(worker.manager, job.execution_id)
    worker.store._db.execute(
        "UPDATE execution_recovery SET request = 'invalid' WHERE execution_id = ?",
        (job.execution_id,),
    )
    await worker.restart()
    assert worker.manager.get(job.execution_id).state is ExecutionState.LOST
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    assert (await channel.request("active"))["executions"] == []


@pytest.mark.asyncio
async def test_provisioning_recovery_waits_for_termination_before_startup(worker: Worker) -> None:
    job, _ = await worker.manager.submit(
        request(
            "withdrawn",
            f"{sys.executable} -I -c 'import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            'print("ready",flush=True); time.sleep(60)\'',
        ),
        origin="provision-test",
    )
    await running(worker.manager, job.execution_id)
    await worker.restart()
    assert worker.manager.get(job.execution_id).state is ExecutionState.CANCELLED
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    assert (await channel.request("active"))["executions"] == []


@pytest.mark.asyncio
async def test_offline_rollover_preserves_full_log_and_same_attempt(worker: Worker) -> None:
    job, _ = await worker.manager.submit(
        request(
            "noisy",
            f'sleep .3; {sys.executable} -I -c \'print("z"*3000000,end="")\'',
            output_limit_bytes=2_000_000,
        )
    )
    await running(worker.manager, job.execution_id)
    await worker.manager.close()
    await asyncio.sleep(0.8)
    worker.manager = worker.new_manager()
    await worker.manager.start()
    ended = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.attempt_id == job.attempt_id
    assert job.job is not None
    log = await worker.manager.read_file(
        ReadFileRequest(context=CONTEXT, path=job.job.stdout_path, length=4_000_000)
    )
    assert base64.b64decode(log.content_b64) == b"z" * 3_000_000
    assert any(
        event.type == "stream" and "omitted while disconnected" in event.text
        for event in worker.store.events_after(job.execution_id, 0)
    )


@pytest.mark.asyncio
async def test_stale_bridge_event_cannot_finish_reconnected_attempt(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("fenced", "sleep .4; printf actual"))
    await running(worker.manager, job.execution_id)
    channel = worker.manager._live[job.execution_id].channel
    assert channel is not None
    await worker.manager._on_event(
        "env1",
        {
            "execution": job.execution_id,
            "event": "end",
            "state": "succeeded",
            "result": {"stdout": {"text": "forged"}},
            "source_seq": 999,
            "source_from": 1,
            "_bridge": "bridge-old",
        },
    )
    ended = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert ended.result is not None
    assert ended.result.stdout is not None
    assert ended.result.stdout.text == "actual"


@pytest.mark.asyncio
async def test_pinned_state_and_compact_end_obey_aggregate_budget(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "JOURNAL_BYTES", 150_000)
    for number in range(3):
        execution = f"exe-{number:024x}"
        server.history[execution] = daemon.History()
        for _ in range(20):
            await server.send(
                {
                    "type": "event",
                    "execution": execution,
                    "event": "state",
                    "state": "running",
                    "environment": "a" * 1_000_000,
                }
            )
        await server.send(
            {
                "type": "event",
                "execution": execution,
                "event": "end",
                "state": "succeeded",
                "result": {"stdout": {"text": "x" * 600_000}},
            }
        )
    assert server.bytes <= daemon.JOURNAL_BYTES
    assert len(server.history) == 3
    assert all(item.ended for item in server.history.values())
    assert any(
        b'"recovery_compacted":true' in data
        for item in server.history.values()
        for data in item.events.values()
    )


@pytest.mark.asyncio
async def test_wrong_client_proof_is_rejected_on_real_endpoint(server: daemon.Daemon) -> None:
    reader, writer = await asyncio.open_unix_connection(daemon.address(server.token, server.root))
    writer.write(b"a" * 64 + b"\n")
    await writer.drain()
    greeting = json.loads(await reader.readline())
    assert greeting["incarnation"] == server.incarnation
    writer.write(b"0" * 64 + b"\n")
    await writer.drain()
    assert await asyncio.wait_for(reader.readline(), 3) == b""
    assert server.writer is None
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_wrong_server_proof_is_rejected(tmp_path: Path) -> None:
    closed = asyncio.Event()

    async def fake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(
                executor.encode_line(
                    {"incarnation": "inc-" + "a" * 24, "challenge": "a" * 64, "proof": "0" * 64}
                )
            )
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            closed.set()

    listener = await asyncio.start_unix_server(fake, daemon.address("a" * 64, tmp_path))
    try:
        with pytest.raises(ConnectionError, match="authentication"):
            await daemon.connect("a" * 64, tmp_path)
        await asyncio.wait_for(closed.wait(), 3)
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_reconnect_keeps_existing_watchdog_and_replaces_old_poller(worker: Worker) -> None:
    job, _ = await worker.manager.submit(request("watchdog", "sleep 1; printf done"))
    await running(worker.manager, job.execution_id)
    live = worker.manager._live[job.execution_id]
    watchdog = live.watchdog
    channel = live.channel
    assert channel is not None
    # A live recovery must never add another grace period or replace its deadline.
    await worker.manager._recover("env1", [live])
    assert live.watchdog is watchdog
    stale = asyncio.create_task(asyncio.Event().wait())
    worker.manager._pollers["env1"][1].cancel()
    worker.manager._pollers["env1"] = (
        ExecutorChannel([], lambda _m: asyncio.sleep(0), lambda _c: asyncio.sleep(0)),
        stale,
    )
    worker.manager._poll("env1", channel)
    assert worker.manager._pollers["env1"][0] is channel
    await asyncio.sleep(0)
    assert stale.cancelled()
    ended = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert ended.state is ExecutionState.SUCCEEDED


def test_event_and_source_cursor_rollback_together(tmp_path: Path) -> None:
    import sqlite3
    from datetime import UTC, datetime

    from sandbox_science.contracts import Execution, StateEvent

    store = Store(tmp_path / "atomic.sqlite")
    try:
        execution = Execution(
            execution_id="exe-" + "a" * 24,
            attempt_id="att-" + "a" * 24,
            operation_id="atomic",
            context=CONTEXT,
            kind="job",
            state=ExecutionState.QUEUED,
            submitted_at=datetime.now(UTC),
        )
        store.insert_execution(execution, "digest")
        store.remember_request(execution.execution_id, request("atomic").model_dump_json())
        store._db.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON executions BEGIN "
            "SELECT RAISE(ABORT, 'injected persistence failure'); END"
        )
        event = StateEvent(
            execution_id=execution.execution_id,
            seq=1,
            time=datetime.now(UTC),
            state=ExecutionState.RUNNING,
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected persistence"):
            store.commit_event(execution.model_copy(update={"last_seq": 1}), event, 1)
        assert store.events_after(execution.execution_id, 0) == []
        saved = store.recovery(execution.execution_id)
        assert saved is not None
        assert saved[2] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_confirmed_sandbox_loss_forgets_endpoint_without_blocking_startup(
    worker: Worker,
) -> None:
    job, _ = await worker.manager.submit(request("known-lost"))
    await running(worker.manager, job.execution_id)
    await worker.manager.close()
    worker.manager = worker.new_manager()

    async def confirmed(_environment: str) -> bool:
        return True

    worker.manager.confirmed_daemon_loss = confirmed
    await worker.manager.start()
    assert worker.manager.get(job.execution_id).state is ExecutionState.LOST
    assert "env1" not in worker.store.endpoints()


@pytest.mark.asyncio
async def test_unreachable_live_endpoint_refuses_startup_without_claiming_loss(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.contracts import ErrorKind
    from sandbox_science.worker.errors import WorkerError

    job, _ = await worker.manager.submit(request("unreachable"))
    await running(worker.manager, job.execution_id)
    await worker.manager.close()
    worker.manager = worker.new_manager()

    async def unavailable(_environment: str) -> None:
        raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic gateway unavailable")

    monkeypatch.setattr(worker.backend, "prepare", unavailable)
    with pytest.raises(WorkerError, match="startup refused"):
        await worker.manager.start()
    assert worker.manager.get(job.execution_id).state is ExecutionState.RUNNING
    assert "env1" in worker.store.endpoints()
    monkeypatch.undo()
    await worker.manager.close()
    worker.manager = worker.new_manager()
    await worker.manager.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("known_job", [False, True])
async def test_disabling_daemon_transport_with_live_endpoints_refuses_startup(
    worker: Worker, known_job: bool
) -> None:
    from sandbox_science.worker.errors import WorkerError

    job = None
    if known_job:
        job, _ = await worker.manager.submit(request("transport-change"))
        await running(worker.manager, job.execution_id)
    else:
        await worker.manager.capabilities("env1")
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    unknown = "exe-" + "e" * 24
    await channel.request("shell", execution=unknown, session="s1", script="sleep 60", timeout=100)
    await worker.manager.close()
    settings = worker.backend.settings
    worker.backend.settings = settings.model_copy(update={"daemon": False})
    worker.manager = worker.new_manager()
    try:
        with pytest.raises(WorkerError, match="before disabling daemon transport; startup refused"):
            await worker.manager.start()
        assert "env1" in worker.store.endpoints()
        if job is not None:
            assert worker.manager.get(job.execution_id).state is ExecutionState.RUNNING
    finally:
        worker.backend.settings = settings
        await worker.manager.close()
        worker.manager = worker.new_manager()
        await worker.manager.start()
    channel = worker.manager._environments["env1"].channel
    assert channel is not None
    assert unknown not in (await channel.request("active"))["executions"]


@pytest.mark.asyncio
async def test_replaced_controller_cannot_dispatch_already_buffered_shutdown(
    tmp_path: Path,
) -> None:
    from typing import cast

    class Writer:
        def __init__(self) -> None:
            self.closing = False
            self.messages: list[dict[str, Any]] = []
            self.authenticated = asyncio.Event()

        def write(self, data: bytes) -> None:
            message = json.loads(data)
            self.messages.append(message)
            if message.get("ok") is True:
                self.authenticated.set()

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closing = True

        def is_closing(self) -> bool:
            return self.closing

    class Reader:
        def __init__(self, writer: Writer, token: str, gate: asyncio.Event) -> None:
            self.writer, self.token, self.gate = writer, token, gate
            self.auth_reads = 0
            self.reads = 0
            self.nonce = "1" * 64

        async def readexactly(self, _size: int) -> bytes:
            import hashlib
            import hmac

            self.auth_reads += 1
            if self.auth_reads == 1:
                return (self.nonce + "\n").encode()
            challenge = self.writer.messages[-1]["challenge"]
            proof = hmac.new(
                self.token.encode(), ("client:" + challenge + self.nonce).encode(), hashlib.sha256
            ).hexdigest()
            return (proof + "\n").encode()

        async def readline(self) -> bytes:
            self.reads += 1
            if self.reads == 1:
                await self.gate.wait()
                return executor.encode_line({"id": 1, "op": "daemon_shutdown"})
            await asyncio.Future[None]()
            return b""

    instance = daemon.Daemon(tmp_path, "a" * 64)
    old_writer, new_writer = Writer(), Writer()
    old_gate, new_gate = asyncio.Event(), asyncio.Event()
    old_reader = Reader(old_writer, instance.token, old_gate)
    new_reader = Reader(new_writer, instance.token, new_gate)
    old_task = asyncio.create_task(instance.client(cast(Any, old_reader), cast(Any, old_writer)))
    new_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(old_writer.authenticated.wait(), 3)
        old_writer.close()  # A background reply's drain failure closed only this side.
        new_task = asyncio.create_task(
            instance.client(cast(Any, new_reader), cast(Any, new_writer))
        )
        await asyncio.wait_for(new_writer.authenticated.wait(), 3)
        assert instance.writer is new_writer
        old_gate.set()  # Deliver the old reader's buffered result after replacement.
        await asyncio.wait_for(old_task, 3)
        assert not instance.stop.is_set()
        assert not instance.executor.closing
    finally:
        old_task.cancel()
        if new_task is not None:
            new_task.cancel()
        await asyncio.gather(old_task, *([new_task] if new_task else []), return_exceptions=True)
        await instance.executor.shutdown()


@pytest.mark.asyncio
async def test_shutdown_exits_even_when_acknowledgement_is_lost(
    server: daemon.Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, writer = await daemon.connect(server.token, server.root)
    original = server.write

    async def fail_ack(target: asyncio.StreamWriter, message: daemon.Message | bytes) -> None:
        if isinstance(message, dict) and message.get("id") == 91:
            raise ConnectionError("synthetic shutdown acknowledgement disconnect")
        await original(target, message)

    monkeypatch.setattr(server, "write", fail_ack)
    await raw_request(writer, 91, op="daemon_shutdown")
    await asyncio.wait_for(server.stop.wait(), 3)
    assert server.executor.closing
    assert await asyncio.wait_for(reader.readline(), 3) == b""
    writer.close()
    await writer.wait_closed()
