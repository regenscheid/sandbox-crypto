# SPDX-License-Identifier: MIT
"""Cancellation and loss racing the steps around a call's dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext, ExecutionState, ShellRequest
from sandbox_science.worker.channel import ExecutorChannel

from .harness import Harness

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


async def test_a_cancel_while_the_package_gate_is_acknowledged_never_runs_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client is told the call was cancelled before it started; so it must not run."""
    async with await Harness.start(tmp_path) as worker:
        manager = worker.manager
        await worker.run("warm", kind="shell", script="true")
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(
            original: Callable[[str, ExecutorChannel], Awaitable[bool]],
            environment_id: str,
            channel: ExecutorChannel,
        ) -> bool:
            entered.set()
            await release.wait()
            return await original(environment_id, channel)

        worker.view.wrap_package_gate_sync(monkeypatch, held)
        marker = worker.workspace / "ran-after-cancel"
        execution, _ = await manager.submit(
            ShellRequest(operation_id="op-race", context=CONTEXT, script=f"touch {marker}")
        )
        await asyncio.wait_for(entered.wait(), 10)
        cancelled = await manager.cancel(execution.execution_id)
        assert cancelled.state is ExecutionState.CANCELLED
        release.set()
        # A later call on the same executor runs after the cancelled one would have.
        later = await worker.run("after", kind="shell", script="true")
        assert later["state"] == "succeeded"
        assert not marker.exists(), "the cancelled call ran in the executor"
        assert manager.get(execution.execution_id).state is ExecutionState.CANCELLED


async def test_an_attempt_lost_during_its_final_sync_leaves_the_held_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the sync after a lost attempt returns, its completion takes nothing: what a
    file request's sync found goes to the session's next result."""
    async with await Harness.start(tmp_path, shared_bytes=1 << 20) as worker:
        manager = worker.manager
        assert worker.shared is not None
        up = worker.shared.up
        entered, release, synced = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def held(
            original: Callable[..., Awaitable[list[str]]],
            step: Any,
            context: ExecutionContext,
            channel: ExecutorChannel,
        ) -> list[str]:
            if step != up or release.is_set():
                return await original(step, context, channel)
            entered.set()
            await release.wait()
            found = await original(step, context, channel)
            synced.set()  # the attempt's completion runs as this returns, in this task
            return found

        worker.view.wrap_shared_sync(monkeypatch, held)
        execution, _ = await manager.submit(
            ShellRequest(operation_id="op-a", context=CONTEXT, script="echo hi")
        )
        await asyncio.wait_for(entered.wait(), 15)
        worker.view.lose(execution.execution_id, "the test")
        assert manager.get(execution.execution_id).state is ExecutionState.SUCCEEDED
        notice = "a file request's sync found this"
        worker.view.tell(CONTEXT, [notice])
        release.set()
        await asyncio.wait_for(synced.wait(), 15)
        await asyncio.sleep(0)
        following, _ = await manager.submit(
            ShellRequest(operation_id="op-b", context=CONTEXT, script="true")
        )
        done = await manager.wait(following.execution_id)
        assert done.result is not None
        assert notice in done.result.notices
