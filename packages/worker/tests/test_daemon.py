# SPDX-License-Identifier: MIT
"""Sandbox-lifetime recovery never replays commands or trusts wider sandbox limits."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    CellRequest,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
    ReadFileRequest,
    ShellRequest,
)
from sandbox_science.testing import eventually
from sandbox_science.worker import daemon, executor
from sandbox_science.worker.backends import ClosedHandler, LocalBackend, daemon_token
from sandbox_science.worker.channel import EventHandler, ExecutorChannel
from sandbox_science.worker.store import Store

from .harness import Harness, running

pytestmark = pytest.mark.slow

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


def gated(then: str, gate: str = "go") -> str:
    """A job script that runs ``then`` once the test creates ``gate`` in the session."""
    return f"until [ -e {gate} ]; do sleep 0.01; done; {then}"


def open_gate(worker: Harness, gate: str = "go") -> None:
    (worker.workspace / gate).touch()


def internal_request(worker: Harness, script: str = "sleep 60") -> ShellRequest:
    """A positive saved operation+exact step, as registered only by the provisioner."""
    record = ProvisionOperation(
        provision_id="provision-test",
        request=ProvisionRequest(
            operation_id="internal",
            context=CONTEXT,
            conda=("example",),
            purpose="test recovery",
        ),
        state=ProvisionState.QUEUED,
        submitted_at=datetime.now(UTC),
    )
    worker.store.insert_provision(record, "test")
    worker.store.update_provision(record.model_copy(update={"state": ProvisionState.INSTALLING}))
    result = ShellRequest(operation_id="provision-test.install", context=CONTEXT, script=script)
    worker.store.authorize_provision_step(
        record.provision_id,
        result.operation_id,
        hashlib.sha256(result.model_dump_json().encode()).hexdigest(),
    )
    return result


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True) as value:
        yield value


@pytest.mark.asyncio
async def test_worker_restart_preserves_identity_events_logs_and_never_replays(
    worker: Harness,
) -> None:
    job, _ = await worker.manager.submit(
        request("restart", "printf run >> marker; printf before; " + gated("printf after"))
    )
    await running(worker.manager, job.execution_id)
    channel = worker.view.live_channel(job.execution_id)
    incarnation = channel.incarnation
    await worker.restart()
    open_gate(worker)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.attempt_id == job.attempt_id
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "beforeafter"
    recovered_channel = worker.view.channel("env1")
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
async def test_bridge_failure_recovers_without_losing_or_restarting_job(worker: Harness) -> None:
    job, _ = await worker.manager.submit(
        request("bridge", "printf before; " + gated("printf after"))
    )
    await running(worker.manager, job.execution_id)
    channel = worker.view.live_channel(job.execution_id)
    assert channel._process is not None
    channel._process.kill()
    open_gate(worker)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.result is not None
    assert result.result.stdout is not None
    assert result.result.stdout.text == "beforeafter"
    recovered_channel = worker.view.channel("env1")
    assert recovered_channel.incarnation == channel.incarnation


@pytest.mark.asyncio
async def test_connected_final_result_preserves_approved_output(worker: Harness) -> None:
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
async def test_definite_refusal_is_immediately_terminal(worker: Harness) -> None:
    call, _ = await worker.manager.submit(
        ShellRequest(operation_id="badcwd", context=CONTEXT, script="true", cwd="missing")
    )
    result = await asyncio.wait_for(worker.manager.wait(call.execution_id), 10)
    assert result.state is ExecutionState.FAILED
    assert result.result is not None
    assert result.result.error is not None
    assert result.result.error.kind == "invalid_request"


@pytest.mark.asyncio
async def test_cancel_persisted_while_disconnected_is_recovered(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("cancel"))
    await running(worker.manager, job.execution_id)
    worker.store.cancel_intent(job.execution_id)
    await worker.restart()
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    assert result.state is ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_kernel_globals_survive_worker_restart(worker: Harness) -> None:
    first, _ = await worker.manager.submit(
        CellRequest(operation_id="kernel1", context=CONTEXT, language="python", code="value = 1729")
    )
    ended = await asyncio.wait_for(worker.manager.wait(first.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.result is not None
    kernel = ended.result.kernel
    assert kernel is not None
    assert kernel.start_revision == 0
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
async def test_unknown_control_requests_are_cancelled_during_recovery(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("known", gated("printf kept")))
    await running(worker.manager, job.execution_id)
    channel = worker.view.live_channel(job.execution_id)
    unknown = "exe-" + "f" * 24
    await channel.request("shell", execution=unknown, session="s1", script="sleep 60", timeout=100)
    await worker.restart()
    channel = worker.view.channel("env1")
    assert unknown not in (await channel.request("active"))["executions"]
    open_gate(worker)
    assert (
        await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    ).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_withdrawn_provisioning_execution_is_cancelled_on_restart(worker: Harness) -> None:
    job, _ = await worker.manager.submit(internal_request(worker), origin="provision-test")
    await running(worker.manager, job.execution_id)
    await worker.restart()
    assert (
        await asyncio.wait_for(worker.manager.wait(job.execution_id), 10)
    ).state is ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_corrupt_recovery_row_does_not_block_other_jobs(worker: Harness) -> None:
    bad, _ = await worker.manager.submit(request("bad"))
    good, _ = await worker.manager.submit(request("good", gated("echo good")))
    await running(worker.manager, good.execution_id)
    worker.store._db.execute(
        "UPDATE execution_recovery SET request = 'invalid' WHERE execution_id = ?",
        (bad.execution_id,),
    )
    await worker.restart()
    assert worker.manager.get(bad.execution_id).state is ExecutionState.LOST
    open_gate(worker)
    assert (
        await asyncio.wait_for(worker.manager.wait(good.execution_id), 10)
    ).state is ExecutionState.SUCCEEDED
    channel = worker.view.channel("env1")
    assert bad.execution_id not in (await channel.request("active"))["executions"]


@pytest.mark.asyncio
async def test_daemon_incarnation_loss_reaps_escaped_children(worker: Harness) -> None:
    job, _ = await worker.manager.submit(
        request("incarnation", "setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > child; wait")
    )
    await running(worker.manager, job.execution_id)
    data = await worker.manager.read_file(ReadFileRequest(context=CONTEXT, path="child"))
    child = int(base64.b64decode(data.content_b64))
    channel = worker.view.live_channel(job.execution_id)
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.LOST
    await eventually(lambda: (info := executor.process_info(child)) is None or info.zombie)


@pytest.mark.asyncio
async def test_openshell_replacement_waits_for_one_durable_lifecycle_fence(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker.errors import WorkerError

    monkeypatch.setattr(worker.backend, "name", "openshell")
    job, _ = await worker.manager.submit(request("fenced-incarnation"))
    await running(worker.manager, job.execution_id)
    channel = worker.view.live_channel(job.execution_id)
    old = channel.incarnation
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def recycle(environment: str, sandbox: str, sandbox_id: str) -> None:
        assert (sandbox, sandbox_id) == ("synthetic-sandbox", "synthetic-id")
        calls.append(environment)
        entered.set()
        await proceed.wait()

    worker.view.set_lifecycle(recycle_daemon=recycle)
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert worker.manager.get(job.execution_id).state is ExecutionState.LOST
        assert worker.store.recycle_needed("env1")
        assert worker.store.endpoint_incarnation("env1") == old
        with pytest.raises(WorkerError, match="fenced until"):
            await worker.manager.submit(request("must-wait"))
        with pytest.raises(WorkerError, match="fenced until"):
            await worker.manager.submit(
                ShellRequest(operation_id="call-must-wait", context=CONTEXT, script="true")
            )
        waiting = [asyncio.create_task(worker.manager.capabilities("env1")) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in waiting)
        assert calls == ["env1"]
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*waiting), 10)
        assert not worker.store.recycle_needed("env1")
        assert worker.store.endpoint_incarnation("env1") != old
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_openshell_replacement_fences_endpoint_without_unfinished_jobs(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker.backend, "name", "openshell")
    await worker.manager.capabilities("env1")
    channel = worker.view.channel("env1")
    old = channel.incarnation
    await worker.manager.close()
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)
    worker.manager = worker.new_manager()
    calls: list[str] = []

    async def recycle(environment: str, sandbox: str, sandbox_id: str) -> None:
        assert (sandbox, sandbox_id) == ("synthetic-sandbox", "synthetic-id")
        assert worker.store.recycle_needed(environment)
        assert worker.store.endpoint_incarnation(environment) == old
        calls.append(environment)

    worker.view.set_lifecycle(recycle_daemon=recycle)
    await asyncio.wait_for(worker.manager.start(), 10)
    assert calls == ["env1"]
    assert not worker.store.recycle_needed("env1")
    assert worker.store.endpoint_incarnation("env1") != old


@pytest.mark.asyncio
async def test_recycle_intent_survives_failed_fence_and_worker_restart(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.contracts import ErrorKind
    from sandbox_science.worker.errors import WorkerError

    monkeypatch.setattr(worker.backend, "name", "openshell")
    await worker.manager.capabilities("env1")
    channel = worker.view.channel("env1")
    old, pid = channel.incarnation, channel.hello["location"]["pid"]
    await worker.manager.close()
    # Crash after durable intent, before the stop command or any replacement open.
    worker.store.require_recycle("env1", old)
    worker.manager = worker.new_manager()

    async def failed(_environment: str, _sandbox: str, _sandbox_id: str) -> None:
        raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic stop failure")

    worker.view.set_lifecycle(recycle_daemon=failed)
    with pytest.raises(WorkerError, match="startup refused"):
        await worker.manager.start()
    assert worker.store.recycle_needed("env1")
    assert worker.store.endpoint_incarnation("env1") == old
    assert worker.view.channel_or_none("env1") is None
    await worker.manager.close()
    worker.store.close()
    worker.store = Store(worker.store_path)
    worker.manager = worker.new_manager()

    async def succeeded(environment: str, _sandbox: str, _sandbox_id: str) -> None:
        assert worker.store.recycle_needed(environment)
        os.kill(pid, signal.SIGKILL)  # local fixture's stand-in for a confirmed VM stop
        await eventually(lambda: (info := executor.process_info(pid)) is None or info.zombie)

    worker.view.set_lifecycle(recycle_daemon=succeeded)
    await asyncio.wait_for(worker.manager.start(), 10)
    assert not worker.store.recycle_needed("env1")
    assert worker.store.endpoint_incarnation("env1") != old


@pytest.mark.asyncio
async def test_transport_or_authentication_failure_never_recycles_sandbox(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.contracts import ErrorKind
    from sandbox_science.worker.errors import WorkerError

    monkeypatch.setattr(worker.backend, "name", "openshell")
    await worker.manager.capabilities("env1")
    old = worker.store.endpoint_incarnation("env1")
    await worker.manager.close()
    worker.manager = worker.new_manager()

    async def forbidden(_environment: str, _sandbox: str, _sandbox_id: str) -> None:
        raise AssertionError("transport errors cannot justify recycling")

    async def unavailable(*_args: Any, **_kwargs: Any) -> ExecutorChannel:
        raise WorkerError(ErrorKind.UNAVAILABLE, "synthetic authentication/transport failure")

    worker.view.set_lifecycle(recycle_daemon=forbidden)
    with monkeypatch.context() as patch:
        patch.setattr(worker.backend, "open", unavailable)
        with pytest.raises(WorkerError, match="startup refused"):
            await worker.manager.start()
    assert not worker.store.recycle_needed("env1")
    assert worker.store.endpoint_incarnation("env1") == old
    await worker.manager.close()
    worker.manager = worker.new_manager()
    await worker.manager.start()


@pytest.mark.asyncio
async def test_unmanaged_replacement_requires_operator_lifecycle_cleanup(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker.errors import WorkerError

    monkeypatch.setattr(worker.backend, "name", "openshell")
    await worker.manager.capabilities("env1")
    old = worker.store.endpoint_incarnation("env1")
    assert old is not None
    await worker.manager.close()
    worker.store.require_recycle("env1", old)
    worker.manager = worker.new_manager()
    try:
        with pytest.raises(WorkerError, match="replacement refused until the sandbox is recycled"):
            await worker.manager.capabilities("env1")
        assert worker.store.recycle_needed("env1")
        assert worker.store.endpoint_incarnation("env1") == old
        assert worker.view.channel_or_none("env1") is None
    finally:
        # Fixture-only cleanup: reconnect the known local daemon to reap its processes.
        worker.store.finish_recycle("env1")
        await worker.manager.close()
        worker.manager = worker.new_manager()
        await worker.manager.start()


@pytest.mark.asyncio
async def test_legacy_endpoint_requires_cleanup_if_incarnation_changed_before_binding_known(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker.errors import WorkerError

    monkeypatch.setattr(worker.backend, "name", "openshell")
    await worker.manager.capabilities("env1")
    channel = worker.view.channel("env1")
    old = channel.incarnation
    worker.store.remember_endpoint("env1", old)  # previous schema has no binding
    await worker.manager.close()
    os.kill(channel.hello["location"]["pid"], signal.SIGKILL)
    worker.manager = worker.new_manager()

    async def forbidden(_environment: str, _sandbox: str, _sandbox_id: str) -> None:
        raise AssertionError("unknown original identity cannot recycle the current VM")

    worker.view.set_lifecycle(recycle_daemon=forbidden)
    try:
        with pytest.raises(WorkerError, match="identity was not recorded"):
            await worker.manager.capabilities("env1")
        assert worker.store.recycle_needed("env1")
        assert worker.store.recycle_binding("env1") is None
    finally:
        worker.store.finish_recycle("env1")  # fixture cleanup after deliberate corrupt history
        await worker.manager.close()
        worker.manager = worker.new_manager()
        await worker.manager.start()


@pytest.mark.asyncio
async def test_same_incarnation_backfills_legacy_endpoint_binding(worker: Harness) -> None:
    await worker.manager.capabilities("env1")
    old = worker.store.endpoint_incarnation("env1")
    assert old is not None
    worker.store.remember_endpoint("env1", old)
    assert worker.store.endpoint_binding("env1") is None
    await worker.restart()
    assert worker.store.endpoint_incarnation("env1") == old
    assert worker.store.endpoint_binding("env1") == ("synthetic-sandbox", "synthetic-id")


@pytest.mark.asyncio
async def test_output_charge_survives_restart(worker: Harness) -> None:
    job, _ = await worker.manager.submit(
        request("budget", "printf before; " + gated("printf after"))
    )
    await running(worker.manager, job.execution_id)
    await eventually(lambda: worker.store.event_usage(job.execution_id)[1] > 0)
    previous = worker.store.event_usage(job.execution_id)
    await worker.restart()
    open_gate(worker)
    live = worker.view.live(job.execution_id)
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
async def test_cancel_during_pending_final_sync_cannot_acknowledge_end(worker: Harness) -> None:
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
    worker.view.set_shared(cast(Any, shared))
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
async def test_corrupt_only_execution_still_quiesces_its_daemon(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("sole-corrupt"))
    await running(worker.manager, job.execution_id)
    worker.store._db.execute(
        "UPDATE execution_recovery SET request = 'invalid' WHERE execution_id = ?",
        (job.execution_id,),
    )
    await worker.restart()
    assert worker.manager.get(job.execution_id).state is ExecutionState.LOST
    channel = worker.view.channel("env1")
    assert (await channel.request("active"))["executions"] == []


@pytest.mark.asyncio
async def test_provisioning_recovery_waits_for_termination_before_startup(worker: Harness) -> None:
    job, _ = await worker.manager.submit(
        internal_request(
            worker,
            f"{sys.executable} -I -c 'import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            'print("ready",flush=True); time.sleep(60)\'',
        ),
        origin="provision-test",
    )
    await running(worker.manager, job.execution_id)
    await worker.restart()
    assert worker.manager.get(job.execution_id).state is ExecutionState.CANCELLED
    channel = worker.view.channel("env1")
    assert (await channel.request("active"))["executions"] == []


@pytest.mark.asyncio
async def test_offline_rollover_preserves_full_log_and_same_attempt(worker: Harness) -> None:
    job, _ = await worker.manager.submit(
        request(
            "noisy",
            gated(f'{sys.executable} -I -c \'print("z"*3000000,end="")\'; touch printed'),
            output_limit_bytes=2_000_000,
        )
    )
    await running(worker.manager, job.execution_id)
    await worker.manager.close()
    open_gate(worker)
    await eventually(lambda: (worker.workspace / "printed").exists())
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
async def test_stale_bridge_event_cannot_finish_reconnected_attempt(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("fenced", gated("printf actual")))
    await running(worker.manager, job.execution_id)
    worker.view.live_channel(job.execution_id)
    await worker.view.deliver(
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
    open_gate(worker)
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
async def test_reconnect_keeps_existing_watchdog_and_replaces_old_poller(worker: Harness) -> None:
    job, _ = await worker.manager.submit(request("watchdog", gated("printf done")))
    await running(worker.manager, job.execution_id)
    live = worker.view.live(job.execution_id)
    watchdog = live.watchdog
    channel = live.channel
    assert channel is not None
    # A live recovery must never add another grace period or replace its deadline.
    await worker.view.recover("env1")
    assert live.watchdog is watchdog
    stale = asyncio.create_task(asyncio.Event().wait())
    worker.view.poller("env1")[1].cancel()
    worker.view.put_poller(
        "env1",
        ExecutorChannel([], lambda _m: asyncio.sleep(0), lambda _c: asyncio.sleep(0)),
        stale,
    )
    worker.view.poll("env1", channel)
    assert worker.view.poller("env1")[0] is channel
    await asyncio.sleep(0)
    assert stale.cancelled()
    open_gate(worker)
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
    worker: Harness,
) -> None:
    job, _ = await worker.manager.submit(request("known-lost"))
    await running(worker.manager, job.execution_id)
    await worker.manager.close()
    worker.manager = worker.new_manager()

    async def confirmed(_environment: str) -> bool:
        return True

    worker.view.set_lifecycle(confirmed_daemon_loss=confirmed)
    await worker.manager.start()
    assert worker.manager.get(job.execution_id).state is ExecutionState.LOST
    assert "env1" not in worker.store.endpoints()


@pytest.mark.asyncio
async def test_unreachable_live_endpoint_refuses_startup_without_claiming_loss(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
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
    worker: Harness, known_job: bool
) -> None:
    from sandbox_science.worker.errors import WorkerError

    job = None
    if known_job:
        job, _ = await worker.manager.submit(request("transport-change"))
        await running(worker.manager, job.execution_id)
    else:
        await worker.manager.capabilities("env1")
    channel = worker.view.channel("env1")
    unknown = "exe-" + "e" * 24
    await channel.request("shell", execution=unknown, session="s1", script="sleep 60", timeout=100)
    await worker.manager.close()
    backend = worker.backend
    assert isinstance(backend, LocalBackend)
    settings = backend.settings
    backend.settings = settings.model_copy(update={"daemon": False})
    worker.manager = worker.new_manager()
    try:
        with pytest.raises(WorkerError, match="before disabling daemon transport; startup refused"):
            await worker.manager.start()
        assert "env1" in worker.store.endpoints()
        if job is not None:
            assert worker.manager.get(job.execution_id).state is ExecutionState.RUNNING
    finally:
        backend.settings = settings
        await worker.manager.close()
        worker.manager = worker.new_manager()
        await worker.manager.start()
    channel = worker.view.channel("env1")
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


def record_channels(worker: Harness, monkeypatch: pytest.MonkeyPatch) -> list[ExecutorChannel]:
    """The executor channels the backend opens from now on, in order."""
    opened: list[ExecutorChannel] = []
    original = worker.backend.open

    async def open_and_record(
        environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        channel = await original(environment_id, on_event, on_closed)
        opened.append(channel)
        return channel

    monkeypatch.setattr(worker.backend, "open", open_and_record)
    return opened


class Records(logging.Handler):
    """The worker's error records; ``error`` is set by the first."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []
        self.error = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.error.set()

    def failures(self) -> list[str]:
        return [str(record.exc_info[1]) for record in self.records if record.exc_info]


@pytest.fixture
def manager_errors() -> Iterator[Records]:
    records = Records()
    # The manager logs its own failures; daemon recovery logs under its own module.
    loggers = [
        logging.getLogger("sandbox_science.worker.manager"),
        logging.getLogger("sandbox_science.worker.recovery"),
    ]
    for logger in loggers:
        logger.addHandler(records)
    try:
        yield records
    finally:
        for logger in loggers:
            logger.removeHandler(records)


@pytest.mark.asyncio
async def test_execution_dispatched_during_a_recovery_is_recovered_too(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = record_channels(worker, monkeypatch)
    first, _ = await worker.manager.submit(request("first", gated("printf first")))
    await running(worker.manager, first.execution_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def held_loss_check(_environment: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return False

    worker.view.set_lifecycle(confirmed_daemon_loss=held_loss_check)
    await opened[-1].close()  # the bridge is lost: a recovery starts and waits
    await asyncio.wait_for(entered.wait(), 10)
    second, _ = await worker.manager.submit(request("second", gated("printf second")))
    await running(worker.manager, second.execution_id)
    await opened[-1].close()  # the second job's bridge is lost while the recovery runs
    release.set()
    open_gate(worker)
    for job, text in ((first, "first"), (second, "second")):
        result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
        assert result.state is ExecutionState.SUCCEEDED
        assert result.result is not None
        assert result.result.stdout is not None
        assert result.result.stdout.text == text


@pytest.mark.asyncio
async def test_poller_survives_an_unexpected_error_and_logs_it(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, manager_errors: Records
) -> None:
    job, _ = await worker.manager.submit(request("poll-error", gated("printf done")))
    await running(worker.manager, job.execution_id)
    original = worker.store.recovery
    failed = False

    def fail_once(execution_id: str) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("synthetic database failure")
        return original(execution_id)

    monkeypatch.setattr(worker.store, "recovery", fail_once)
    await eventually(lambda: failed)  # the poller met the failure while the job ran
    open_gate(worker)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.SUCCEEDED
    assert "synthetic database failure" in manager_errors.failures()


@pytest.mark.asyncio
async def test_unexpected_recovery_error_is_logged_and_recovery_goes_on(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, manager_errors: Records
) -> None:
    opened = record_channels(worker, monkeypatch)
    job, _ = await worker.manager.submit(request("recovery-error", gated("printf done")))
    await running(worker.manager, job.execution_id)
    calls = 0

    async def failing_loss_check(_environment: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic recovery failure")
        return False

    worker.view.set_lifecycle(confirmed_daemon_loss=failing_loss_check)
    await opened[-1].close()
    await eventually(lambda: calls >= 2)  # recovery went on after the failure
    open_gate(worker)
    result = await asyncio.wait_for(worker.manager.wait(job.execution_id), 15)
    assert result.state is ExecutionState.SUCCEEDED
    assert "synthetic recovery failure" in manager_errors.failures()


@pytest.mark.asyncio
async def test_failed_background_task_is_logged(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, manager_errors: Records
) -> None:
    await worker.manager.capabilities("env1")

    def broken_gate(_environment: str) -> Any:
        raise sqlite3.OperationalError("synthetic gate failure")

    monkeypatch.setattr(worker.store, "package_gate", broken_gate)
    worker.manager.refresh_package_gate("env1")
    await asyncio.wait_for(manager_errors.error.wait(), 10)
    assert "synthetic gate failure" in manager_errors.failures()
    monkeypatch.undo()
