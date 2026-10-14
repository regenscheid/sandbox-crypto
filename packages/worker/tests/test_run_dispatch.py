# SPDX-License-Identifier: MIT
"""Internal child routing with fake channels/kernels only; no host research kernel."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import tracemalloc
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import (
    CellRequest,
    ExecutionContext,
    ExecutionState,
    KernelInfo,
    KernelStatusRequest,
)
from sandbox_science.contracts.run_cells import RunCellRequest, run_cell_bytes
from sandbox_science.contracts.run_kernels import (
    RunKernelBinding,
    RunKernelScope,
    run_kernel_binding_sha256,
)
from sandbox_science.worker import executor
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager, _Environment
from sandbox_science.worker.run_kernels import RunKernelBindings
from sandbox_science.worker.store import Store

# Only isolated synthetic owners inspect private lifecycle state.
# pyright: reportPrivateUsage=false
INC = "inc-" + "a" * 24
VM = ("ss-synthetic", "vm-synthetic")


def binding(child: str = "child") -> RunKernelBinding:
    return RunKernelBinding(
        scope=RunKernelScope(
            project_id="p",
            session_id="s",
            environment_id="e",
            parent_run_id="parent",
            delegation_id="dlg-" + child,
            child_run_id=child,
        ),
        sandbox=VM[0],
        sandbox_id=VM[1],
        incarnation=INC,
    )


def request(operation: str = "cell", child: str = "child") -> RunCellRequest:
    return RunCellRequest(
        operation_id=operation,
        context=ExecutionContext(project_id="p", session_id="s", environment_id="e"),
        binding=binding(child),
        code="synthetic = 1",
    )


class FakeChannel:
    incarnation = INC
    daemon_token = "synthetic-not-connected"
    closed = False
    detaching = False

    def __init__(self) -> None:
        self.hello = {"run_kernel_protocol": 1, "kernel_revision_protocol": 1}
        self.calls: list[tuple[str, Message]] = []
        self.reply: Message = {"kernels": []}
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def request(self, op: str, **fields: object) -> Message:
        self.calls.append((op, fields))
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        return self.reply

    async def detach(self) -> None:
        self.detaching = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def manager(tmp_path: Path) -> Generator[ExecutionManager]:
    store = Store(tmp_path / "worker.sqlite3")
    value = ExecutionManager(
        store,
        cast(Backend, object()),
        Blobs(tmp_path / "blobs"),
        pool_id="synthetic",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    value._environments["e"] = _Environment(
        channel=cast(ExecutorChannel, FakeChannel()), languages=("python", "sage"), last_used=123
    )
    value.daemon_identity = lambda _: VM
    store.remember_endpoint("e", INC, VM)
    RunKernelBindings(store).enroll(binding(), "enroll", initialize=True)
    value.run_dispatcher.initialize()
    try:
        yield value
    finally:
        for task in value._tasks:
            task.cancel()
        value._blob_pool.shutdown(wait=True, cancel_futures=True)
        store.close()


def channel(manager: ExecutionManager) -> FakeChannel:
    return cast(FakeChannel, manager._environments["e"].channel)


async def accepted(
    manager: ExecutionManager, monkeypatch: pytest.MonkeyPatch, operation: str = "cell"
):
    async def no_send(*_: object) -> None:
        return None

    monkeypatch.setattr(manager, "_dispatch", no_send)
    result, created = await manager.run_dispatcher.submit(request(operation), admitted=lambda: True)
    assert created
    await asyncio.sleep(0)
    return result


@pytest.mark.asyncio
async def test_selection_request_attempt_digest_atomically_retained_and_replay(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    stored = manager.store.run_kernel_state.request(first.execution)
    assert stored == request()
    assert (
        manager.store.recovery(first.execution.execution_id)[0]
        == run_cell_bytes(request()).decode()
    )
    assert manager.store.kernel_scope(first.execution.execution_id) == "run-v1"
    assert (
        manager.store.run_operation("p", "cell")[1]
        == hashlib.sha256(run_cell_bytes(request())).hexdigest()
    )
    replay, created = await manager.run_dispatcher.submit(request(), admitted=lambda: True)
    assert not created
    assert replay == first
    with pytest.raises(WorkerError):
        await manager.run_dispatcher.submit(
            request().model_copy(update={"code": "different"}), admitted=lambda: True
        )
    assert channel(manager).calls == []


@pytest.mark.asyncio
async def test_internal_send_exact_wire_and_no_open(manager, monkeypatch):
    first = await accepted(manager, monkeypatch)

    async def forbidden(*_: object):
        pytest.fail("child dispatch must not open or reconnect")

    async def gate(*_: object):
        return True

    monkeypatch.setattr(manager, "_environment", forbidden)
    monkeypatch.setattr(manager, "_sync_package_gate", gate)
    monkeypatch.setattr(manager, "_poll", lambda *_: None)
    await manager._send(manager._live[first.execution.execution_id], request())
    op, fields = channel(manager).calls[-1]
    assert op == "run_cell"
    assert fields["run_binding"] == binding().model_dump(mode="json")
    assert fields["run_binding_sha256"] == run_kernel_binding_sha256(binding())
    assert manager.store.recovery(first.execution.execution_id)[1] == INC
    assert manager.store.kernel("e", "s", "python") is None


@pytest.mark.parametrize(
    "bad", ["legacy\0run-v1", "run-v1\0bad", "run-v1-more", "", b"run-v1", None]
)
@pytest.mark.asyncio
async def test_exact_marker_refuses_nul_type_missing_and_oversize(manager, monkeypatch, bad):
    first = await accepted(manager, monkeypatch)
    if bad is None:
        manager.store._db.execute(
            "DELETE FROM executions WHERE execution_id=?", (first.execution.execution_id,)
        )
        assert manager.store.kernel_scope(first.execution.execution_id) is None
    else:
        manager.store._db.execute("UPDATE executions SET kernel_scope=?", (bad,))
        with pytest.raises(WorkerError):
            manager.store.kernel_scope(first.execution.execution_id)
    with pytest.raises(WorkerError):
        manager.run_dispatcher.get(
            binding(), first.execution.execution_id, first.execution.attempt_id
        )
    assert channel(manager).calls == []


@pytest.mark.asyncio
async def test_missing_selection_cannot_fall_back_to_main_cancel(manager, monkeypatch):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    live.channel = cast(ExecutorChannel, channel(manager))
    manager.store._db.execute("DROP TRIGGER run_cell_selections_no_delete")
    manager.store._db.execute("DELETE FROM run_cell_selections")
    with pytest.raises(WorkerError):
        await manager._send_cancel(live)
    assert channel(manager).calls == []


@pytest.mark.parametrize("column", ["project", "binding", "operation", "action", "request_sha"])
def test_control_all_retained_keys_bounded_before_fetch(manager, column):
    state = manager.store.run_kernel_state
    state.begin_control(binding(), "restart", "restart", "python")
    manager.store._db.execute("DROP TRIGGER run_controls_identity")
    manager.store._db.execute(f"UPDATE run_kernel_controls SET {column}=?", ("x" * 2_000_000,))
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError):
            state.budget()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100_000


@pytest.mark.parametrize(
    ("column", "value"), [("project", "foreign"), ("action", "close"), ("request_sha", "b" * 64)]
)
def test_control_replay_binds_canonical_columns(manager, column, value):
    state = manager.store.run_kernel_state
    state.begin_control(binding(), "restart", "restart", "python")
    manager.store._db.execute("DROP TRIGGER run_controls_identity")
    manager.store._db.execute(f"UPDATE run_kernel_controls SET {column}=?", (value,))
    with pytest.raises(WorkerError):
        state.begin_control(binding(), "restart", "restart", "python")


@pytest.mark.asyncio
async def test_pending_control_never_resends_and_closed_child_stays_closed(manager):
    state = manager.store.run_kernel_state
    original, new = state.begin_control(binding(), "close", "close", None)
    assert new
    replay = await manager.run_dispatcher.close(binding(), "close", admitted=lambda: True)
    assert replay == original
    assert channel(manager).calls == []
    with pytest.raises(WorkerError):
        await manager.run_dispatcher.restart(binding(), "later", "python", admitted=lambda: True)


@pytest.mark.asyncio
async def test_actual_control_owner_drain_survives_repeated_cancel(manager):
    ch = channel(manager)
    ch.block = asyncio.Event()
    task = asyncio.create_task(
        manager.run_dispatcher.close(binding(), "close", admitted=lambda: True)
    )
    await ch.entered.wait()
    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    await asyncio.sleep(0)
    assert not task.done()
    assert manager._environments["e"].requests == 1
    assert len(manager.run_dispatcher._controls) == 1
    ch.block.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert error.value.args == ("first",)
    assert manager._environments["e"].requests == 0
    assert manager.run_dispatcher._controls == {}
    receipt, _ = manager.store.run_kernel_state.begin_control(binding(), "close", "close", None)
    assert receipt.state == "done"
    assert len(ch.calls) == 1


@pytest.mark.asyncio
async def test_passive_run_observer_shares_lane_and_does_not_touch(manager):
    env = manager._environments["e"]
    ch = channel(manager)
    ch.block = asyncio.Event()
    before = list(manager.store._db.iterdump())
    task = asyncio.create_task(manager.run_dispatcher.observe(binding()))
    await ch.entered.wait()
    with pytest.raises(WorkerError):
        await manager.observe_kernels(KernelStatusRequest(context=request().context))
    assert (env.requests, env.last_used) == (0, 123)
    ch.block.set()
    assert (await task).kernels == ()
    assert list(manager.store._db.iterdump()) == before
    assert ch.calls[0][0] == "run_kernels"


def test_legacy_cell_bytes_and_kernel_id_are_unchanged(tmp_path):
    value = CellRequest(operation_id="legacy", context=request().context, code="x=1")
    assert value.model_dump_json() == (
        '{"kind":"cell","operation_id":"legacy","context":{"project_i'
        'd":"p","session_id":"s","environment_id":"e","policy_revisio'
        'n":null,"actor":"agent"},"language":"python","code":"x=1","t'
        'imeout_seconds":120.0,"output_limit_bytes":1048576}'
    )
    assert executor.Kernel("s", "python", tmp_path, {}).kernel_id == "krn-s-python"
    a = executor.Kernel("s", "python", tmp_path, {}, run_binding=binding().model_dump(mode="json"))
    b = executor.Kernel(
        "s", "python", tmp_path, {}, run_binding=binding("other").model_dump(mode="json")
    )
    assert len({a.kernel_id, b.kernel_id, "krn-s-python"}) == 3


def raw_kernel(tmp_path: Path, execution_id: str = "synthetic") -> Message:
    kernel = executor.Kernel(
        "s",
        "python",
        tmp_path,
        {},
        generation=1,
        birth_id="birth-" + "a" * 24,
        start_environment="a" * 32,
        start_execution=execution_id,
        start_package_epoch=1,
        run_binding=binding().model_dump(mode="json"),
    )
    return kernel.info()


@pytest.mark.asyncio
async def test_running_transaction_pins_original_birth_and_readonly_status_cannot_freeze_unknown(
    manager, monkeypatch, tmp_path
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    ch = channel(manager)
    live.channel = cast(ExecutorChannel, ch)
    store = manager.store
    store._db.execute("INSERT INTO environment_revisions VALUES('e',0,?,'')", ("a" * 32,))
    store.kernel_starts.epoch("e", INC, 1, "a" * 32, remember=True)
    ch.reply = {"kernels": [raw_kernel(tmp_path, first.execution.execution_id)]}
    before = list(store._db.iterdump())
    unknown = await manager.run_dispatcher.observe(binding())
    assert unknown.kernels[0].lineage_status == "unknown"
    assert list(store._db.iterdump()) == before
    manager._apply(
        live,
        "e",
        {
            "event": "state",
            "state": "running",
            "environment": "a" * 32,
            "kernel": ch.reply["kernels"][0],
        },
    )
    assert live.execution.kernel is not None
    assert live.execution.kernel.start_revision == 0
    assert store.run_kernel_state.birth(binding(), "python").start_revision == 0
    assert store.kernel("e", "s", "python") is None
    assert store.kernel_starts.retained("e", "s", "python")[0] is None
    observed = await manager.run_dispatcher.observe(binding())
    assert observed.kernels[0].lineage_status == "proved"
    store._db.execute("UPDATE environment_revisions SET revision=1,fingerprint=?", ("b" * 32,))
    observed = await manager.run_dispatcher.observe(binding())
    assert observed.kernels[0].kernel.revision_status == "predates"
    assert observed.kernels[0].kernel.start_revision == 0


@pytest.mark.asyncio
async def test_running_failure_rolls_back_state_then_same_event_replays(
    manager, monkeypatch, tmp_path
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    live.channel = cast(ExecutorChannel, channel(manager))
    message = {
        "event": "state",
        "state": "running",
        "kernel": raw_kernel(tmp_path, first.execution.execution_id),
    }
    manager.store._db.execute(
        "CREATE TRIGGER fail_birth BEFORE INSERT ON run_kernel_births BEGIN S"
        "ELECT RAISE(ABORT,'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        manager._apply(live, "e", message)
    assert live.execution.state == ExecutionState.QUEUED
    assert manager.store.run_kernel_state.birth(binding(), "python") is None
    manager.store._db.execute("DROP TRIGGER fail_birth")
    manager._apply(live, "e", message)
    assert live.execution.state == ExecutionState.RUNNING
    assert manager.store.run_kernel_state.birth(binding(), "python") is not None


@pytest.mark.asyncio
async def test_derived_child_birth_damage_cannot_rollback_terminal_result(
    manager, monkeypatch, tmp_path
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    live.channel = cast(ExecutorChannel, channel(manager))
    raw = raw_kernel(tmp_path, first.execution.execution_id)
    manager._apply(live, "e", {"event": "state", "state": "running", "kernel": raw})
    manager.store._db.execute("UPDATE run_kernel_births SET record='corrupt'")
    manager._apply(
        live,
        "e",
        {
            "event": "end",
            "state": "succeeded",
            "result": {"kernel": raw, "stdout": {"text": "healthy", "total_bytes": 7}},
        },
    )
    assert live.execution.state == ExecutionState.SUCCEEDED
    assert live.execution.result is not None
    assert live.execution.result.stdout is not None
    assert live.execution.result.stdout.text == "healthy"
    assert live.execution.result.kernel is None


@pytest.mark.asyncio
async def test_recovery_original_selection_without_resubmit_and_replacement_refuses(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    manager.store.dispatch_incarnation(first.execution.execution_id, INC)
    manager._live.clear()

    class Settings:
        daemon = True

    class SyntheticBackend:
        settings = Settings()

    manager.backend = cast(Backend, SyntheticBackend())
    recovered = []

    async def reconcile(environment, lives, *, startup=False):
        del environment, startup
        recovered.extend(lives)

    monkeypatch.setattr(manager, "_recover", reconcile)
    await manager.start()
    assert len(recovered) == 1
    live = recovered[0]
    assert live.is_run
    assert live.run_request == request()
    assert live.execution.attempt_id == first.execution.attempt_id
    assert channel(manager).calls == []
    manager.daemon_identity = lambda _: ("replacement", VM[1])
    with pytest.raises(WorkerError):
        manager.run_dispatcher.proof(binding(), recovery=True, allow_closed=True)


def wire(child: str = "child") -> Message:
    return {
        "session": "s",
        "run_binding": binding(child).model_dump(mode="json"),
        "run_binding_sha256": run_kernel_binding_sha256(binding(child)),
        "language": "python",
        "_admission_guard": lambda: True,
    }


@pytest.mark.asyncio
async def test_guest_queued_restart_cannot_launch_after_child_close(tmp_path):
    messages = []

    async def send(message):
        messages.append(message)

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    guest.run_selection(wire(), remember=True)
    kernel = guest.run_kernel(binding().model_dump(mode="json"), "python")
    launched = []

    async def no_real_kernel(launcher=None):
        assert launcher is not None

        async def work():
            launched.append(True)

        await launcher(work)

    kernel.ensure_started = no_real_kernel
    await kernel.control.acquire()
    restart = asyncio.create_task(guest.op_run_restart(wire()))
    await asyncio.sleep(0)
    close = asyncio.create_task(guest.op_run_close(wire()))
    await asyncio.sleep(0)
    assert ("p", "child") in guest.closed_runs
    kernel.control.release()
    with pytest.raises(executor.ToolError):
        await restart
    await close
    assert launched == []
    assert guest.launch_controls == set()


@pytest.mark.asyncio
async def test_guest_child_close_preserves_parent_sibling_and_cancel_exact_binding(tmp_path):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    guest.run_selection(wire(), remember=True)
    guest.run_selection(wire("sibling"), remember=True)
    own = guest.run_kernel(binding().model_dump(mode="json"), "python")
    sibling = guest.run_kernel(binding("sibling").model_dump(mode="json"), "python")
    parent = guest.kernel("s", "python")
    stopped = []
    for name, kernel in (("own", own), ("sibling", sibling), ("parent", parent)):
        kernel.manager = object()

        async def stop(name=name, kernel=kernel):
            stopped.append(name)
            kernel.manager = None

        kernel.stop = stop
    running = executor.Running("exe-" + "a" * 24, "cell", kernel=sibling)
    guest.running[running.execution] = running
    with pytest.raises(executor.ToolError):
        await guest.op_run_cancel({**wire(), "execution": running.execution})
    await guest.op_run_close(wire())
    assert stopped == ["own"]
    assert sibling.manager is not None
    assert parent.manager is not None
    assert not running.cancel_requested
    await guest.op_close_session({"session": "s"})
    assert set(stopped) == {"own", "sibling", "parent"}
    assert ("p", "sibling") in guest.closed_runs


@pytest.mark.parametrize(
    "mutation",
    [{"run_binding": None}, {"run_binding_sha256": "b" * 64}, {"_admission_guard": None}],
)
@pytest.mark.asyncio
async def test_guest_missing_invalid_selection_refuses_without_main_fallback(tmp_path, mutation):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation = INC
    with pytest.raises(executor.ToolError):
        await guest.op_run_cell(
            {
                **wire(),
                "execution": "exe-" + "a" * 24,
                "code": "synthetic",
                "timeout": 1,
                "limit": 1024,
                **mutation,
            }
        )
    assert guest.kernels == {}
    assert guest.run_kernels == {}
    assert guest.running == {}


def test_original_generation_birth_and_control_result_language_cannot_be_substituted(manager):
    state = manager.store.run_kernel_state
    intent, _ = state.begin_control(binding(), "restart", "restart", "python")
    with pytest.raises(WorkerError), manager.store._transaction():
        state.finish_control(
            intent.request, KernelInfo(kernel_id="synthetic", language="sage", generation=1)
        )
    manager.store._db.execute("DROP TRIGGER run_controls_once")
    manager.store._db.execute(
        "UPDATE run_kernel_controls SET state='done',result=?",
        (KernelInfo(kernel_id="synthetic", language="sage", generation=1).model_dump_json(),),
    )
    with pytest.raises(WorkerError):
        state.begin_control(binding(), "restart", "restart", "python")


def test_legacy_migration_only_when_no_dispatch_namespace_and_missing_marker_refuses(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    owner = Store(path)
    RunKernelBindings(owner).enroll(binding(), "enroll", initialize=True)
    owner._db.execute("ALTER TABLE executions DROP COLUMN kernel_scope")
    owner.close()
    owner = Store(path)
    owner.run_kernel_state.initialize()
    owner._db.execute("ALTER TABLE executions DROP COLUMN kernel_scope")
    owner.close()
    with pytest.raises(WorkerError):
        Store(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", "\0" * 400_000),
        ("generation", True),
        ("birth_id", None),
        ("run_binding_sha256", {}),
        ("kernel_id", {}),
    ],
)
def test_raw_kernel_scalars_refuse_before_aggregate_encoding(manager, tmp_path, field, value):
    raw = raw_kernel(tmp_path)
    raw[field] = value
    tracemalloc.start()
    try:
        with pytest.raises(WorkerError):
            manager.run_dispatcher.map_kernel(
                binding(), raw, cast(ExecutorChannel, channel(manager))
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100_000


@pytest.mark.parametrize("suffix", ["\0corrupt", "x"])
@pytest.mark.asyncio
async def test_original_request_digest_and_dispatch_incarnation_never_nul_normalized(
    manager, monkeypatch, suffix
):
    first = await accepted(manager, monkeypatch)
    db = manager.store._db
    digest = hashlib.sha256(run_cell_bytes(request())).hexdigest()
    db.execute("UPDATE executions SET request_digest=?", (digest + suffix,))
    for read in (
        lambda: manager.store.run_operation("p", "cell"),
        lambda: manager.store.run_kernel_state.request(first.execution),
    ):
        with pytest.raises(WorkerError):
            read()
    db.execute("UPDATE executions SET request_digest=?", (digest,))
    db.execute("UPDATE execution_recovery SET incarnation=?", ("\0corrupt",))
    with pytest.raises(WorkerError):
        manager.store.recovery(first.execution.execution_id)


@pytest.mark.asyncio
async def test_exact_legacy_marker_with_retained_child_header_refuses(manager, monkeypatch):
    first = await accepted(manager, monkeypatch)
    manager.store._db.execute("UPDATE executions SET kernel_scope='legacy'")
    with pytest.raises(WorkerError):
        manager.store.kernel_scope(first.execution.execution_id)
    with pytest.raises(WorkerError):
        manager.store.recovery(first.execution.execution_id)


def test_control_original_child_id_action_language_and_historical_replay(manager, tmp_path):
    state = manager.store.run_kernel_state
    raw = raw_kernel(tmp_path)
    kernel = KernelInfo(kernel_id=raw["kernel_id"], language="python", generation=1)
    intent, _ = state.begin_control(binding(), "restart", "restart", "python")
    with manager.store._transaction():
        done = state.finish_control(intent.request, kernel)
    state.begin_control(binding(), "close", "close", None)
    manager.daemon_identity = None
    assert (
        asyncio.run(
            manager.run_dispatcher.restart(binding(), "restart", "python", admitted=lambda: True)
        )
        == done
    )
    assert channel(manager).calls == []
    state2 = state.lookup_control(binding(), "restart", "restart", "python")
    assert state2 == done
    for action, language in (("restart", None), ("close", "python")):
        with pytest.raises(ValueError, match="child control language"):
            state.begin_control(binding(), "invalid", action, language)
    manager.store._db.execute("DROP TRIGGER run_controls_once")
    manager.store._db.execute(
        "UPDATE run_kernel_controls SET result=? WHERE operation='restart'",
        (kernel.model_copy(update={"kernel_id": "krn-s-python"}).model_dump_json(),),
    )
    with pytest.raises(WorkerError):
        state.lookup_control(binding(), "restart", "restart", "python")


@pytest.mark.asyncio
async def test_retained_generation_birth_disagreement_is_held(manager, monkeypatch, tmp_path):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    live.channel = cast(ExecutorChannel, channel(manager))
    raw = raw_kernel(tmp_path, first.execution.execution_id)
    manager._apply(live, "e", {"event": "state", "state": "running", "kernel": raw})
    manager.store._db.execute("UPDATE run_kernel_generations SET last=5")
    for remember in (False, True):
        before = list(manager.store._db.iterdump())
        with pytest.raises(WorkerError), manager.store._transaction():
            manager.run_dispatcher.map_kernel(binding(), raw, live.channel, remember=remember)
        assert list(manager.store._db.iterdump()) == before


@pytest.mark.asyncio
async def test_guest_close_drains_admitted_cell_handler_waiting_for_acceptance_reply(
    tmp_path, monkeypatch
):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    entered, release = asyncio.Event(), asyncio.Event()
    original = guest.reply

    async def blocked_reply(request, **payload):
        if request.get("id") == 1:
            entered.set()
            await release.wait()
        await original(request, **payload)

    monkeypatch.setattr(guest, "reply", blocked_reply)
    submit = asyncio.create_task(
        guest.op_run_cell(
            {
                **wire(),
                "id": 1,
                "execution": "exe-" + "a" * 24,
                "code": "synthetic",
                "timeout": 1,
                "limit": 1024,
            }
        )
    )
    await entered.wait()
    close = asyncio.create_task(guest.op_run_close({**wire(), "id": 2}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not close.done()
    assert guest.run_handlers
    release.set()
    await submit
    await close
    assert guest.run_handlers == {}
    assert guest.running == {}
    assert all(kernel.manager is None for kernel in guest.run_kernels.values())


@pytest.mark.asyncio
async def test_cancel_never_certifies_current_channel_then_uses_old_open_channel(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    old = FakeChannel()
    live.channel = cast(ExecutorChannel, old)
    with pytest.raises(WorkerError):
        await manager._send_cancel(live)
    assert old.calls == []
    assert channel(manager).calls == []


@pytest.mark.parametrize("column", ["project", "environment_id", "session_id", "operation_id"])
@pytest.mark.asyncio
async def test_selected_canonical_scope_columns_cannot_disagree_or_nul_normalize(
    manager, monkeypatch, column
):
    first = await accepted(manager, monkeypatch)
    db = manager.store._db
    if column == "project":
        db.execute("DROP TRIGGER run_cell_selections_no_update")
        db.execute("UPDATE run_cell_selections SET project=?", ("p\0bad",))
    else:
        original = {"environment_id": "e", "session_id": "s", "operation_id": "p/cell"}[column]
        db.execute(f"UPDATE executions SET {column}=?", (original + "\0bad",))
    with pytest.raises(WorkerError):
        manager.store.run_kernel_state.request(first.execution)
    assert channel(manager).calls == []


@pytest.mark.asyncio
async def test_public_cancel_refuses_child_without_effect_but_legacy_cancel_is_unchanged(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    before = list(manager.store._db.iterdump())
    with pytest.raises(WorkerError) as error:
        await manager.cancel(first.execution.execution_id, "p")
    assert error.value.kind.value == "unsupported"
    assert list(manager.store._db.iterdump()) == before
    assert not manager._live[first.execution.execution_id].cancel_requested
    assert channel(manager).calls == []
    legacy, _ = await manager.submit(
        CellRequest(operation_id="legacy-cancel", context=request().context, code="synthetic")
    )
    cancelled = await manager.cancel(legacy.execution_id, "p")
    assert cancelled.state == ExecutionState.CANCELLED
    assert channel(manager).calls == []
    selected = await manager.run_dispatcher.cancel(
        binding(), first.execution.execution_id, first.execution.attempt_id, admitted=lambda: True
    )
    assert selected.execution.state == ExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_same_incarnation_actual_recovery_replays_only_and_new_incarnation_loses(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    manager.store.dispatch_incarnation(first.execution.execution_id, INC)
    ch = channel(manager)

    async def existing(_):
        return manager._environments["e"]

    async def fake_request(op, **fields):
        ch.calls.append((op, fields))
        return {"executions": [first.execution.execution_id]} if op == "active" else {"found": True}

    monkeypatch.setattr(manager, "_environment", existing)
    monkeypatch.setattr(ch, "request", fake_request)
    monkeypatch.setattr(manager, "_poll", lambda *_: None)
    await manager._recover("e", [live])
    assert live.reattached
    assert live.channel is ch
    assert [op for op, _ in ch.calls] == ["active", "replay"]
    assert live.execution.attempt_id == first.execution.attempt_id
    ch.incarnation = "inc-" + "b" * 24
    await manager._recover("e", [live])
    assert live.execution.state == ExecutionState.LOST
    assert all(op != "run_cell" for op, _ in ch.calls)


@pytest.mark.asyncio
async def test_manager_shutdown_drains_admitted_control_before_channel_detach(manager):
    ch = channel(manager)
    manager._channels.add(cast(ExecutorChannel, ch))
    ch.block = asyncio.Event()
    control = asyncio.create_task(
        manager.run_dispatcher.close(binding(), "close", admitted=lambda: True)
    )
    await ch.entered.wait()
    stop = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not stop.done()
    assert not ch.detaching
    assert manager._environments["e"].requests == 1
    ch.block.set()
    with pytest.raises(WorkerError):
        await control
    await stop
    assert ch.detaching
    assert manager._environments["e"].requests == 0
    receipt, _ = manager.store.run_kernel_state.begin_control(binding(), "close", "close", None)
    assert receipt.state == "pending"


@pytest.mark.asyncio
async def test_public_submit_cannot_adopt_selected_attempt_even_corrupt_digest_matches_legacy(
    manager, monkeypatch
):
    first = await accepted(manager, monkeypatch)
    legacy = CellRequest(operation_id="cell", context=request().context, code="synthetic")
    manager.store._db.execute(
        "UPDATE executions SET request_digest=?",
        (hashlib.sha256(legacy.model_dump_json().encode()).hexdigest(),),
    )
    with pytest.raises(WorkerError):
        await manager.submit(legacy)
    assert manager._live[first.execution.execution_id].is_run
    assert channel(manager).calls == []


@pytest.mark.asyncio
async def test_guest_lost_acceptance_reply_retains_cancelled_attempt_until_owned_cleanup(
    tmp_path, monkeypatch
):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    original = guest.reply

    async def failed_reply(request, **payload):
        if request.get("id") == 1:
            raise OSError("synthetic acceptance reply failure")
        await original(request, **payload)

    monkeypatch.setattr(guest, "reply", failed_reply)
    with pytest.raises(OSError, match="synthetic acceptance"):
        await guest.op_run_cell(
            {
                **wire(),
                "id": 1,
                "execution": "exe-" + "a" * 24,
                "code": "synthetic",
                "timeout": 1,
                "limit": 1024,
            }
        )
    await guest.op_run_close({**wire(), "id": 2})
    assert guest.running == {}
    assert guest.run_handlers == {}
    assert all(kernel.manager is None for kernel in guest.run_kernels.values())


@pytest.mark.parametrize(
    ("field", "value"),
    [("operation_id", "x" * 1000), ("recorded_at", "x" * 1000), ("extra", "undeclared")],
)
def test_constructed_control_metadata_cannot_bypass_preflight(manager, field, value):
    state = manager.store.run_kernel_state
    receipt, _ = state.begin_control(binding(), "close", "close", None)
    before = list(manager.store._db.iterdump())
    with pytest.raises(WorkerError), manager.store._transaction():
        state.finish_control(receipt.request.model_copy(update={field: value}), None)
    assert list(manager.store._db.iterdump()) == before


@pytest.mark.asyncio
async def test_quota_failure_rolls_back_attempt_and_close_intent_atomically(manager, monkeypatch):
    from sandbox_science.worker import run_kernel_state

    before = list(manager.store._db.iterdump())
    monkeypatch.setattr(run_kernel_state, "MAX_RUN_ATTEMPTS", 0)
    with pytest.raises(WorkerError):
        await manager.run_dispatcher.submit(request(), admitted=lambda: True)
    assert list(manager.store._db.iterdump()) == before
    monkeypatch.setattr(run_kernel_state, "MAX_RUN_CONTROLS", 0)
    with pytest.raises(WorkerError):
        manager.store.run_kernel_state.begin_control(binding(), "close", "close", None)
    assert list(manager.store._db.iterdump()) == before
    assert channel(manager).calls == []


@pytest.mark.parametrize("whole_session", [False, True])
@pytest.mark.asyncio
async def test_guest_shutdown_drains_close_owner_after_kernel_manager_cleared(
    tmp_path, monkeypatch, whole_session
):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    guest.run_selection(wire(), remember=True)
    kernel = guest.run_kernel(binding().model_dump(mode="json"), "python")
    kernel.manager = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_stop():
        kernel.manager = None
        entered.set()
        await release.wait()

    monkeypatch.setattr(kernel, "stop", blocked_stop)
    # Absolutely no process sweep or actual kernel work is permitted in this fixture.
    monkeypatch.setattr(executor, "end_descendants", lambda: None)
    close = asyncio.create_task(
        guest.op_close_session({"session": "s"}) if whole_session else guest.op_run_close(wire())
    )
    await entered.wait()
    shutdown = asyncio.create_task(guest.shutdown())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not close.done()
    close.cancel("again")
    shutdown.cancel("again")
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await close
    await shutdown
    assert guest.launch_controls == set()
    assert kernel.manager is None


@pytest.mark.asyncio
async def test_damaged_derived_schema_cannot_turn_healthy_terminal_child_into_failure(
    manager, monkeypatch, tmp_path
):
    first = await accepted(manager, monkeypatch)
    live = manager._live[first.execution.execution_id]
    live.channel = cast(ExecutorChannel, channel(manager))
    raw = raw_kernel(tmp_path, first.execution.execution_id)
    manager._apply(live, "e", {"event": "state", "state": "running", "kernel": raw})
    manager.store._db.execute("ALTER TABLE run_dispatch_meta RENAME COLUMN version TO damaged")
    manager._apply(
        live,
        "e",
        {
            "event": "end",
            "state": "succeeded",
            "result": {"kernel": raw, "stdout": {"text": "healthy", "total_bytes": 7}},
        },
    )
    assert live.execution.state == ExecutionState.SUCCEEDED
    assert live.execution.result is not None
    assert live.execution.result.stdout is not None
    assert live.execution.result.stdout.text == "healthy"
    assert live.execution.result.kernel is None


@pytest.mark.asyncio
async def test_duplicate_canonical_execution_json_refuses_selected_read(manager, monkeypatch):
    first = await accepted(manager, monkeypatch)
    raw = first.execution.model_dump_json()
    raw = raw[:-1] + ',"execution_id":"' + first.execution.execution_id + '"}'
    manager.store._db.execute("UPDATE executions SET record=?", (raw,))
    with pytest.raises(WorkerError):
        manager.run_dispatcher.get(
            binding(), first.execution.execution_id, first.execution.attempt_id
        )


@pytest.mark.asyncio
async def test_guest_shutdown_owns_admitted_handler_and_late_cancelled_attempt_task(
    tmp_path, monkeypatch
):
    async def send(_):
        return None

    guest = executor.Executor(tmp_path, send)
    guest.run_incarnation, guest.package_mode = INC, "ready"
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_reply(*_, **__):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(guest, "reply", blocked_reply)
    monkeypatch.setattr(executor, "end_descendants", lambda: None)
    handler = asyncio.create_task(
        guest.op_run_cell(
            {
                **wire(),
                "execution": "exe-" + "a" * 24,
                "code": "synthetic",
                "timeout": 1,
                "limit": 1024,
            }
        )
    )
    await entered.wait()
    shutdown = asyncio.create_task(guest.shutdown())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert guest.run_handlers
    shutdown.cancel("again")
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await handler
    await shutdown
    assert guest.run_handlers == {}
    assert guest.running == {}
    assert guest.launch_controls == set()
    assert all(kernel.manager is None for kernel in guest.run_kernels.values())


@pytest.mark.parametrize("kind", ["control", "kernel"])
def test_copied_control_or_kernel_field_count_refuses_before_key_iteration_or_sets(
    manager, monkeypatch, kind
):
    from sandbox_science.worker import run_kernel_state

    if kind == "control":
        receipt, _ = manager.store.run_kernel_state.begin_control(binding(), "close", "close", None)
        value = receipt.request.model_copy(update={f"extra-{i}": "x" for i in range(2048)})
    else:
        value = KernelInfo(kernel_id="synthetic", language="python", generation=1).model_copy(
            update={f"extra-{i}": "x" for i in range(2048)}
        )

    def forbidden(*_, **__):
        pytest.fail("unexpected aggregate key iteration/encoding before field-count refusal")

    monkeypatch.setattr(run_kernel_state, "set", forbidden, raising=False)
    monkeypatch.setattr(run_kernel_state, "any", forbidden, raising=False)
    if kind == "control":
        with pytest.raises(WorkerError):
            run_kernel_state.checked_control(cast(run_kernel_state.RunControl, value))
    else:
        with pytest.raises(WorkerError):
            run_kernel_state.checked_info(cast(KernelInfo, value))
