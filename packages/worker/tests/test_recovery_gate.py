# SPDX-License-Identifier: MIT
"""Sticky recovery admission, exact hold ownership and actual launch ordering."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import (
    ExecutionContext,
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
    ShellRequest,
)
from sandbox_science.worker import executor, sandbox_processes
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.package_gate import PackageGate
from sandbox_science.worker.store import Store

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def operation(
    identity: str = "prv-first", state: ProvisionState = ProvisionState.QUEUED
) -> ProvisionOperation:
    return ProvisionOperation(
        provision_id=identity,
        request=ProvisionRequest(
            operation_id=identity,
            context=CONTEXT,
            conda=("example",),
            purpose="test fence",
        ),
        state=state,
        submitted_at=datetime.now(UTC),
    )


def test_install_hold_and_operation_are_one_transaction(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    record = operation()
    try:
        store.insert_provision(record, "digest")
        store._db.execute(
            "CREATE TRIGGER fail_hold BEFORE INSERT ON package_execution_gates"
            " BEGIN SELECT RAISE(ABORT,'synthetic SQL failure'); END"
        )
        with pytest.raises(Exception, match="synthetic SQL failure"):
            store.update_provision(record.model_copy(update={"state": ProvisionState.INSTALLING}))
        assert store.provision(record.provision_id) == record
        assert store.package_gate("env1") == PackageGate()
        store._db.execute("DROP TRIGGER fail_hold")
        store.update_provision(record.model_copy(update={"state": ProvisionState.INSTALLING}))
        assert store.package_gate("env1") == PackageGate(1, "hold", record.provision_id)
    finally:
        store.close()


def test_ready_empty_fields_binding_and_sticky_legacy_status(tmp_path: Path) -> None:
    path = tmp_path / "worker.db"
    store = Store(path)
    store.bind_package_gate("env1", "immutable-original")
    assert store.package_gate("env1") == PackageGate()
    store._db.execute("INSERT INTO package_status VALUES ('env1','needs_recovery','prv-first',3)")
    store.close()
    store = Store(path)
    try:
        assert store.package_gate("env1") == PackageGate(1, "needs_recovery", "prv-first", 3)
        store.set_package_status("env1", "needs_recovery", "prv-later", 5)
        assert store.package_gate("env1") == PackageGate(1, "needs_recovery", "prv-first", 3)
        with pytest.raises(WorkerError, match="binding changed"):
            store.bind_package_gate("env1", "different-sandbox")
        store.bind_package_gate("env1", "immutable-original")
    finally:
        store.close()


def test_old_operation_finish_cannot_clear_new_hold_or_recovery(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    first, second = operation(), operation("prv-second")
    try:
        for record in (first, second):
            store.insert_provision(record, record.provision_id)
        store.update_provision(second.model_copy(update={"state": ProvisionState.INSTALLING}))
        store.update_provision(first.model_copy(update={"state": ProvisionState.FINISHED}))
        assert store.package_gate("env1").operation == second.provision_id
        store.set_package_status("env1", "needs_recovery", second.provision_id, 0)
        gate = store.package_gate("env1")
        store.update_provision(second.model_copy(update={"state": ProvisionState.FINISHED}))
        assert store.package_gate("env1") == gate
    finally:
        store.close()


def test_existing_ready_gate_reconciles_unfinished_legacy_install_before_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.db"
    store = Store(path)
    store.bind_package_gate("env1", "original")
    # A canonical older writer can insert this operation without the new save hook.
    store.insert_provision(operation(state=ProvisionState.INSTALLING), "legacy")
    store.close()
    store = Store(path)
    try:
        assert store.package_gate("env1") == PackageGate(1, "hold", "prv-first")
        store.bind_package_gate("env1", "original")
        with pytest.raises(WorkerError, match="binding changed"):
            store.bind_package_gate("env1", "replacement")
    finally:
        store.close()


@pytest.mark.parametrize("field", ["operation", "mode"])
def test_a_corrupt_gate_scalar_is_refused_when_written(tmp_path: Path, field: str) -> None:
    # Changed by C-08: the gate's values are checked when they are written (decision 3),
    # so a corrupt one never reaches a read, which takes the row as it is.
    store = Store(tmp_path / "worker.db")
    try:
        store.bind_package_gate("env1", "binding")
        before = store.package_gate("env1")
        query = (
            "UPDATE package_execution_gates SET operation=?,mode='hold'"
            if field == "operation"
            else "UPDATE package_execution_gates SET mode=?"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            store._db.execute(query, ("x" * 20_000_000,))
        assert store.package_gate("env1") == before
        store.bind_package_gate("env1", "binding")
    finally:
        store.close()


def test_legacy_endpoint_proof_is_bounded_and_abandon_does_not_clear_hold(tmp_path: Path) -> None:
    store = Store(tmp_path / "worker.db")
    try:
        store.set_package_status("env1", "needs_recovery", "prv-first", 1)
        store.remember_endpoint("env1", "incarnation", ("x" * 20_000_000, "id"))
        tracemalloc.start()
        try:
            with pytest.raises(WorkerError, match="invalid retained executor binding"):
                store.bind_package_gate("env1", repr(("name", "id")))
            assert tracemalloc.get_traced_memory()[1] < 100_000
        finally:
            tracemalloc.stop()
        store.forget_revisions("env1")
        assert store.package_gate("env1").mode == "needs_recovery"
        assert store.package_status("env1") == ("needs_recovery", "prv-first", 1)
        unfinished = operation()
        store.insert_provision(unfinished, "digest")
        store._db.execute("UPDATE provisions SET record='unreadable'")
        with pytest.raises(WorkerError, match="not durably finished"):
            store.forget_package_gate("env1", binding=None)
        assert store.package_gate("env1").mode == "needs_recovery"
        store.update_provision(unfinished.model_copy(update={"state": ProvisionState.FINISHED}))
        store.remember_endpoint("env1", "incarnation", ("original-name", "original-id"))
        store.forget_package_gate("env1", binding=repr(("original-name", "original-id")))
        assert store.package_gate("env1") == PackageGate()
    finally:
        store.close()


@pytest.mark.parametrize("flag", [0, 2, "corrupt", 1])
def test_gate_clear_requires_exact_finished_flag_and_canonical_state(
    tmp_path: Path, flag: object
) -> None:
    store = Store(tmp_path / "worker.db")
    try:
        store.insert_provision(operation(state=ProvisionState.INSTALLING), "digest")
        store.set_package_status("env1", "needs_recovery", "prv-first", 1)
        store._db.execute("UPDATE provisions SET finished=?", (flag,))
        assert not store.provisioning_finished("env1")
        with pytest.raises(WorkerError, match="not durably finished"):
            store.forget_package_gate("env1", binding=None)
        assert store.package_gate("env1").mode == "needs_recovery"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ready_original_binding_requires_confirmed_delete_before_reuse(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "worker.db")
    manager = ExecutionManager.__new__(ExecutionManager)
    manager.store = store
    try:
        store.bind_package_gate("env1", repr(("original-name", "original-id")))
        assert store.package_gate("env1").mode == "ready"
        with pytest.raises(WorkerError, match="requires confirmed sandbox deletion"):
            await manager.guard_package_reuse("env1")
        store.forget_package_gate("env1", binding=repr(("original-name", "original-id")))
        await manager.guard_package_reuse("env1")
    finally:
        store.close()


@pytest.mark.parametrize("proof", ["missing", "wrong", "bound", "endpoint", "ready"])
def test_deletion_requires_original_proof_at_both_boundaries(tmp_path: Path, proof: str) -> None:
    store = Store(tmp_path / "worker.db")
    binding = repr(("original-name", "original-id"))
    try:
        if proof in {"wrong", "bound"}:
            store.bind_package_gate("env1", "another-binding" if proof == "wrong" else binding)
        elif proof == "endpoint":
            store.remember_endpoint("env1", "incarnation", ("original-name", "original-id"))
        if proof != "ready":
            store.set_package_status("env1", "needs_recovery", "prv-first", 1)
        if proof in {"missing", "wrong"}:
            before = store.package_gate("env1")
            for action in (
                lambda: store.guard_package_deletion("env1", binding),
                lambda: store.forget_package_gate("env1", binding=binding),
            ):
                with pytest.raises(WorkerError):
                    action()
                assert store.package_gate("env1") == before
            return
        store.guard_package_deletion("env1", binding)
        # The endpoint may disappear during actual executor shutdown. The exact
        # proof retained before mutation must still support atomic final cleanup.
        store.forget_endpoint("env1")
        assert store.package_gate_has_binding("env1")
        with pytest.raises(WorkerError, match="binding changed"):
            store.forget_package_gate("env1", binding=repr(("replacement", "new-id")))
        store.forget_package_gate("env1", binding=binding)
        assert store.package_gate("env1") == PackageGate()
        store.forget_package_gate("env1", binding=None)  # ready/empty is compatible
    finally:
        store.close()


def test_a_large_original_binding_is_refused_when_written(tmp_path: Path) -> None:
    # Changed by C-08: the binding's bound is checked when it is written (decision 3).
    store = Store(tmp_path / "worker.db")
    try:
        store.bind_package_gate("env1", "original")
        store.set_package_status("env1", "needs_recovery", "prv-first", 1)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            store._db.execute("UPDATE package_execution_gates SET binding=?", ("x" * 20_000_000,))
        with pytest.raises(WorkerError, match="binding changed"):
            store.guard_package_deletion("env1", "replacement")
        assert store.package_gate("env1").mode == "needs_recovery"
    finally:
        store.close()


class Guest:
    def __init__(self, root: Path) -> None:
        self.messages: list[dict[str, Any]] = []
        self.executor = executor.Executor(root, self.send)

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def gate(self, generation: int, mode: str, operation: str = "", **extra: Any) -> None:
        await self.executor.op_package_gate(
            {
                "id": generation,
                "incarnation": self.executor.admission.incarnation,
                "generation": generation,
                "mode": mode,
                "operation": operation,
                "revision": None,
                **extra,
            }
        )


@pytest.mark.asyncio
async def test_guest_starts_closed_and_exact_exemption_does_not_unlock_ordinary(
    tmp_path: Path,
) -> None:
    guest = Guest(tmp_path)
    ordinary = executor.Running("ordinary", "shell")
    with pytest.raises(executor._BusyError):
        async with guest.executor.admission.launch(ordinary, 0.01):
            pytest.fail("bootstrap gate opened itself")
    await guest.gate(1, "hold", "prv-first")
    exempt = executor.Running(
        "install", "shell", package_operation="prv-first", package_generation=1
    )
    async with guest.executor.admission.launch(exempt, 1):
        pass
    with pytest.raises(executor._BusyError):
        async with guest.executor.admission.launch(executor.Running("other", "shell"), 0.01):
            pytest.fail("an installer exemption unlocked ordinary work")
    await guest.gate(2, "hold", "prv-second")
    with pytest.raises(executor._BusyError):
        async with guest.executor.admission.launch(exempt, 0.01):
            pytest.fail("old exemption crossed a new generation")
    await guest.gate(3, "needs_recovery", "prv-second")
    with pytest.raises(executor.ToolError, match="needs_recovery"):
        async with guest.executor.admission.launch(exempt, 1):
            pytest.fail("recovery admitted a stale installer")


@pytest.mark.asyncio
async def test_hold_ack_waits_for_actual_launch_and_double_cancel_drain(tmp_path: Path) -> None:
    guest = Guest(tmp_path)
    await guest.gate(0, "ready")
    entered, release = asyncio.Event(), asyncio.Event()
    running = executor.Running("shell", "shell")

    async def actual() -> int:
        entered.set()
        await release.wait()
        return 42

    async def launch() -> int:
        async with guest.executor.admission.launch(running, 10):
            return await guest.executor.own_launch(running, actual())

    host = asyncio.create_task(launch())
    await entered.wait()
    hold = asyncio.create_task(guest.gate(1, "hold", "prv-first"))
    host.cancel()
    await asyncio.sleep(0)
    host.cancel()
    await asyncio.sleep(0)
    assert not host.done()
    assert not hold.done()
    release.set()
    assert await host == 42
    await hold
    assert running.cancel_requested
    assert guest.executor.admission.mode == "hold"


@pytest.mark.asyncio
async def test_stale_controller_waiting_on_launch_lock_cannot_release_hold(tmp_path: Path) -> None:
    guest = Guest(tmp_path)
    await guest.gate(1, "hold", "prv-first")
    attached = True
    await guest.executor.admission.launch_lock.acquire()
    release = asyncio.create_task(guest.gate(2, "ready", _admission_guard=lambda: attached))
    await asyncio.sleep(0)
    attached = False
    guest.executor.admission.launch_lock.release()
    with pytest.raises(executor.ToolError, match="detached"):
        await release
    assert guest.executor.admission.mode == "hold"
    assert guest.executor.admission.generation == 1


@pytest.mark.asyncio
async def test_snapshot_waiter_cannot_spawn_after_recovery_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guest = Guest(tmp_path)
    await guest.gate(0, "ready")
    entered, release = threading.Event(), threading.Event()
    original = guest.executor.snapshot

    def snapshot(session: str) -> Any:
        entered.set()
        assert release.wait(5)
        return original(session)

    monkeypatch.setattr(guest.executor, "snapshot", snapshot)
    try:
        await guest.executor.op_shell(
            {
                "id": 1,
                "session": "s1",
                "execution": "blocked",
                "argv": ["/bin/sh", "-c", "echo wrong > marker"],
            }
        )
        assert await asyncio.to_thread(entered.wait, 5)
        await guest.gate(1, "hold", "prv-first")
        await guest.gate(2, "needs_recovery", "prv-first")
        release.set()
        await asyncio.wait_for(asyncio.gather(*guest.executor.lifecycle.tasks), 5)
        ended = [item for item in guest.messages if item.get("event") == "end"][-1]
        assert ended["state"] == "failed"
        assert ended["result"]["error"]["kind"] == "unavailable"
        assert not (tmp_path / "sessions/s1/marker").exists()
    finally:
        release.set()
        await guest.executor.shutdown()


@pytest.mark.asyncio
async def test_gate_wait_uses_original_timeout_budget(tmp_path: Path) -> None:
    guest = Guest(tmp_path)
    await guest.gate(1, "hold", "prv-first")
    running = executor.Running("waiting", "shell")

    async def launch() -> float:
        async with guest.executor.admission.launch(running, 1):
            return running.waited

    pending = asyncio.create_task(launch())
    await asyncio.sleep(0.1)
    await guest.gate(2, "ready")
    assert 0.09 <= await pending < 1
    with pytest.raises(executor.ToolError, match="stale"):
        await guest.gate(1, "ready")
    with pytest.raises(executor.ToolError, match="invalid"):
        await guest.gate(3, "ready", incarnation="foreign")


def authorize(store: Store, request: ShellRequest, origin: str) -> None:
    store.authorize_provision_step(
        origin,
        request.operation_id,
        # The protocol 1 digest, of the fields protocol 1 had.
        hashlib.sha256(request.model_dump_json(exclude={"bindings"}).encode()).hexdigest(),
    )


@pytest.mark.asyncio
async def test_restart_launch_is_owned_by_shutdown_and_cannot_outlive_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = Guest(tmp_path)
    await guest.gate(0, "ready")
    entered, release = asyncio.Event(), asyncio.Event()

    class Kernel:
        manager: object | None = None
        generation = 0
        stops = 0
        bindings: tuple[str, ...] = ()
        binding_inventory = None
        control = asyncio.Lock()
        retired = False

        async def ensure_started(self, launcher: Any) -> None:
            async def work() -> None:
                entered.set()
                await release.wait()

            await launcher(work)
            self.manager = object()

        async def stop(self) -> None:
            self.stops += 1
            self.manager = None

        def info(self) -> dict[str, Any]:
            return {}

    kernel = Kernel()
    guest.executor.lifecycle.kernels[("s1", "", "python")] = kernel  # pyright: ignore
    monkeypatch.setattr(guest.executor, "kernel", lambda *_: kernel)
    monkeypatch.setattr(sandbox_processes, "end_descendants", lambda: None)
    restart = asyncio.create_task(guest.executor.op_restart({"id": 1, "session": "s1"}))
    await entered.wait()
    stop = asyncio.create_task(guest.executor.shutdown())
    await asyncio.sleep(0)
    stop.cancel()
    await asyncio.sleep(0)
    stop.cancel()
    assert not stop.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(stop, restart), 5)
    assert kernel.manager is None
    assert kernel.stops == 1
    with pytest.raises(executor.ToolError, match="closing"):
        async with guest.executor.admission.launch(executor.Running("late", "shell"), 1):
            pytest.fail("a launch escaped shutdown")


@pytest.mark.asyncio
async def test_restart_rechecks_original_controller_after_kernel_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = Guest(tmp_path)
    await guest.gate(0, "ready")
    entered, release = asyncio.Event(), asyncio.Event()
    attached = True

    class Kernel:
        manager: object | None = object()
        control = asyncio.Lock()
        retired = False
        state_lost = False
        starts = 0

        async def stop(self) -> None:
            entered.set()
            await release.wait()
            self.manager = None

        async def ensure_started(self, launcher: Any) -> None:
            async def work() -> None:
                self.starts += 1

            await launcher(work)

        def info(self) -> dict[str, Any]:
            return {}

    kernel = Kernel()
    monkeypatch.setattr(guest.executor, "kernel", lambda *_: kernel)
    restart = asyncio.create_task(
        guest.executor.op_restart(
            {
                "id": 1,
                "session": "s1",
                "_admission_guard": lambda: attached,
            }
        )
    )
    await entered.wait()
    attached = False
    release.set()
    with pytest.raises(executor.ToolError, match="detached"):
        await restart
    assert kernel.starts == 0
