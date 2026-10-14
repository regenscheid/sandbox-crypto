# SPDX-License-Identifier: MIT
"""Kernels per run (plans-and-delegation spec, PD-37): what the worker refuses, and what
the executor does when a run ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    ExecutionContext,
    KernelRestartRequest,
    SessionCloseRequest,
)
from sandbox_science.worker import executor, schema
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.store import KernelRecord, Store

from .harness import CONTEXT, Harness, view

pytestmark = pytest.mark.asyncio
SESSION = ExecutionContext.model_validate(CONTEXT)


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as harness:
        yield harness


@pytest.mark.slow
async def test_an_executor_from_before_run_kernels_is_never_asked_for_one(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reattached daemon keeps its old executor, which would put a run's cell in the
    session's kernel and close the whole session for a run's close."""
    await worker.run("seed", kind="cell", code="kept = 1")
    channel = view(worker.manager).connection("env1").channel
    assert channel is not None
    assert channel.hello["run_kernels"] == 1
    monkeypatch.delitem(channel.hello, "run_kernels")
    with pytest.raises(WorkerError) as closing:
        await worker.manager.close_session(SessionCloseRequest(context=SESSION, run="r1"))
    with pytest.raises(WorkerError) as restarting:
        await worker.manager.restart_kernel(KernelRestartRequest(context=SESSION, run="r1"))
    cell = await worker.run("late", kind="cell", code="kept", run="r1")
    assert closing.value.kind == restarting.value.kind == "unsupported"
    assert cell["result"]["error"]["kind"] == "unsupported"
    session = await worker.run("session", kind="cell", code="kept")
    assert session["result"]["outputs"][0]["data"]["text/plain"] == "1"  # untouched


class Recorder:
    """An in-process executor whose replies are kept."""

    def __init__(self, root: Path) -> None:
        self.messages: list[dict[str, Any]] = []
        self.executor = executor.Executor(root, self.send)

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        await self.executor.handle(request)
        return self.messages[-1]


class Live:
    """A kernel manager that is always alive, for kernels the tests never start."""

    async def shutdown_kernel(self, now: bool = False) -> None:
        return None

    async def is_alive(self) -> bool:
        return True


def live(recorder: Recorder, run: str | None, generation: int = 1) -> executor.Kernel:
    kernel = recorder.executor.kernel("s1", "python", run)
    kernel.generation, kernel.manager = generation, Live()
    return kernel


async def test_the_cap_counts_runs_with_live_kernels_only(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path)
    recorder.executor.kernel("s1", "python", "never-started")  # no kernel, no cell
    for number in range(executor.MAX_RUN_KERNELS):
        live(recorder, f"r{number}")
    with pytest.raises(executor.ToolError, match="must end first"):
        recorder.executor.kernel("s1", "python", "one-too-many")
    reply = await recorder.ask({"id": 1, "op": "close_session", "session": "s1", "run": "r0"})
    assert reply["ok"] is True
    recorder.executor.kernel("s1", "python", "one-too-many")


async def test_a_session_close_ends_its_runs_and_frees_their_places(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path)
    session = live(recorder, None)
    for number in range(executor.MAX_RUN_KERNELS):
        live(recorder, f"r{number}")
    reply = await recorder.ask({"id": 1, "op": "close_session", "session": "s1"})
    assert reply["ok"] is True
    assert len(reply["kernels"]) == 1 + executor.MAX_RUN_KERNELS
    assert list(recorder.executor.kernels.values()) == [session]  # its generation kept
    recorder.executor.kernel("s1", "python", "a-new-run")
    with pytest.raises(executor.ToolError, match="has ended"):
        recorder.executor.kernel("s1", "python", "r0")


async def test_a_cell_for_an_ended_run_is_refused(tmp_path: Path) -> None:
    """Not a new kernel at generation 0 that does not say its state is gone."""
    recorder = Recorder(tmp_path)
    live(recorder, "r1", generation=3)
    await recorder.ask({"id": 1, "op": "close_session", "session": "s1", "run": "r1"})
    with pytest.raises(executor.ToolError, match="r1 has ended"):
        recorder.executor.kernel("s1", "python", "r1")
    reply = await recorder.ask({"id": 2, "op": "restart", "session": "s1", "run": "r1"})
    assert reply["error"]["kind"] == "conflict"


async def test_a_cell_racing_its_runs_close_never_starts_the_kernel_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(tmp_path)
    kernel = live(recorder, "r1")
    stopping, release = asyncio.Event(), asyncio.Event()
    started: list[executor.Kernel] = []

    async def slow_stop(self: executor.Kernel) -> None:
        stopping.set()
        await release.wait()
        self.manager = None

    async def start(self: executor.Kernel, launcher: Any = None) -> None:
        started.append(self)

    monkeypatch.setattr(executor.Kernel, "stop", slow_stop)
    monkeypatch.setattr(executor.Kernel, "ensure_started", start)
    close = asyncio.create_task(
        recorder.ask({"id": 1, "op": "close_session", "session": "s1", "run": "r1"})
    )
    await asyncio.wait_for(stopping.wait(), 5)
    # The cell found the kernel before the close forgot it, and waits for its control.
    cell = {"id": 2, "op": "cell", "session": "s1", "run": "r1", "code": "1"}
    await recorder.executor.handle({**cell, "execution": "exe-late", "timeout": 5, "limit": 100})
    release.set()
    await asyncio.wait_for(close, 5)
    running = recorder.executor.running.get("exe-late")
    if running is not None and running.task is not None:
        await asyncio.wait_for(running.task, 5)
    assert started == []
    assert kernel.retired
    assert kernel not in recorder.executor.kernels.values()
    results = [m for m in recorder.messages if m.get("execution") == "exe-late"]
    assert any("has ended" in str(m) for m in results), results


@pytest.mark.slow
async def test_a_runs_close_forgets_its_kernels_generations(worker: Harness) -> None:
    await worker.run("cell", kind="cell", code="1", run="r1")
    store = worker.manager.store
    assert store.kernel("env1", "s1", "python", run="r1") is not None
    await worker.manager.close_session(SessionCloseRequest(context=SESSION, run="r1"))
    assert store.kernel("env1", "s1", "python", run="r1") is None
    assert store.kernel("env1", "s1", "python") is None  # the session never had a kernel


async def test_a_version_2_store_gains_run_kernels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worker.sqlite3"
    shipped = list(schema.MIGRATIONS)
    monkeypatch.setattr(schema, "MIGRATIONS", shipped[:1])
    store = Store(path)
    store.save_kernel("env1", "s1", "python", KernelRecord("inc-" + "a" * 24, 0, 3))
    store.close()
    monkeypatch.setattr(schema, "MIGRATIONS", shipped)
    store = Store(path)
    try:
        assert store.kernel("env1", "s1", "python") == KernelRecord("inc-" + "a" * 24, 0, 3)
        # The registry saves "unknown" with no executor attached: kept, as a session's is.
        store.save_kernel("env1", "s1", "python", KernelRecord("unknown", 0, 1), run="r3")
        store.save_kernel("env1", "s1", "python", KernelRecord("inc-" + "b" * 24, 3, 5), run="r1")
        store.save_kernel("env1", "s1", "python", KernelRecord("inc-" + "b" * 24, 0, 1), run="r2")
        assert store.kernel("env1", "s1", "python", run="r1") == KernelRecord(
            "inc-" + "b" * 24, 3, 5
        )
        store.forget_run_kernels("env1", "s1", "r1")
        assert store.kernel("env1", "s1", "python", run="r1") is None
        assert store.kernel("env1", "s1", "python", run="r2") is not None
        store.forget_run_kernels("env1", "s1")
        assert store.kernel("env1", "s1", "python", run="r2") is None
        assert store.kernel("env1", "s1", "python") is not None
    finally:
        store.close()
