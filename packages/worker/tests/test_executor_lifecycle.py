# SPDX-License-Identifier: MIT
"""Public executor protocol guarantees across lifecycle/admission changes."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.testing import FakeClock, eventually
from sandbox_science.worker import provisioning
from sandbox_science.worker.executor import Executor


class Peer:
    def __init__(self, root: Path) -> None:
        self.messages: list[dict[str, Any]] = []
        self.executor = Executor(root, self.send)

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def ask(self, **request: Any) -> dict[str, Any]:
        await self.executor.handle({"id": len(self.messages), **request})
        return self.messages[-1]


async def test_gate_replays_only_the_same_generation_and_state(tmp_path: Path) -> None:
    peer = Peer(tmp_path)
    try:
        hello = await peer.ask(op="hello")
        gate = {"op": "package_gate", "incarnation": hello["package_gate"], "revision": None}
        first = await peer.ask(**gate, generation=1, mode="hold", operation="prv-first")
        replay = await peer.ask(**gate, generation=1, mode="hold", operation="prv-first")
        assert first["ok"]
        assert replay["ok"]
        conflict = await peer.ask(**gate, generation=1, mode="ready", operation="")
        stale = await peer.ask(**gate, generation=0, mode="ready", operation="")
        assert conflict["error"]["kind"] == stale["error"]["kind"] == "conflict"
        ready = await peer.ask(**gate, generation=2, mode="ready", operation="")
        assert ready["ok"]
    finally:
        await peer.executor.shutdown()


async def test_retiring_an_unstarted_run_refuses_late_cells_and_restarts(tmp_path: Path) -> None:
    peer = Peer(tmp_path)
    try:
        for _ in range(2):
            closed = await peer.ask(op="close_session", session="s1", run="r1")
            assert closed["ok"]
            assert closed["kernels"] == []
        late = await peer.ask(op="cell", session="s1", run="r1", execution="late", code="1")
        restart = await peer.ask(op="restart", session="s1", run="r1")
        assert late["error"]["kind"] == restart["error"]["kind"] == "conflict"
    finally:
        await peer.executor.shutdown()


async def test_run_retirement_is_scoped_to_its_session(tmp_path: Path) -> None:
    peer = Peer(tmp_path)
    try:
        await peer.ask(op="close_session", session="s1", run="r1")
        # A supported run proceeds to code validation in the other session; retirement
        # would instead report conflict before validating code.
        other = await peer.ask(op="cell", session="s2", run="r1", execution="other", code=None)
        assert other["error"]["kind"] == "invalid_request"
        listing = await peer.ask(op="kernels", session="s2")
        assert listing["ok"]
        assert listing["kernels"] == []
    finally:
        await peer.executor.shutdown()


@pytest.mark.parametrize("package_turn", [False, True])
async def test_admission_deadline_uses_the_injected_clock(
    tmp_path: Path, package_turn: bool
) -> None:
    clock = FakeClock()
    clock.advance(1000)
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    executor = Executor(tmp_path, send, clock=clock)
    state = provisioning.State(tmp_path / ".executor" / "provisioning")
    held = state.try_lock(exclusive=True) if package_turn else None
    try:
        await executor.handle(
            {
                "id": 1,
                "op": "shell",
                "session": "s1",
                "execution": "waiting",
                "argv": ["/bin/true"],
                "timeout": 5,
            }
        )
        assert messages[0]["type"] == "reply"
        assert messages[0]["ok"]
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(6)
        end = await eventually(lambda: next((m for m in messages if m.get("event") == "end"), None))
        assert end["state"] == "timed_out"
        assert end["result"]["elapsed_seconds"] == 6
        assert "did not start" in end["result"]["error"]["message"]
    finally:
        if held is not None:
            import os

            os.close(held)
        await executor.shutdown()


async def test_close_keeps_its_acknowledgement_owned_through_cancellation(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        if message.get("id") == 1:
            entered.set()
            await release.wait()
        messages.append(message)

    executor = Executor(tmp_path, send)
    closing = asyncio.create_task(
        executor.handle({"id": 1, "op": "close_session", "session": "s1", "run": "r1"})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 5)
        assert messages[0]["ok"]
        await executor.handle({"id": 2, "op": "restart", "session": "s1", "run": "r1"})
        assert messages[-1]["error"]["kind"] == "conflict"
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await executor.shutdown()
