# SPDX-License-Identifier: MIT
"""Public executor protocol guarantees across lifecycle/admission changes."""

from pathlib import Path
from typing import Any

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
        gate = dict(op="package_gate", incarnation=hello["package_gate"], revision=None)
        first = await peer.ask(**gate, generation=1, mode="hold", operation="prv-first")
        replay = await peer.ask(**gate, generation=1, mode="hold", operation="prv-first")
        assert first["ok"] and replay["ok"]
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
            assert closed["ok"] and closed["kernels"] == []
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
        assert listing["ok"] and listing["kernels"] == []
    finally:
        await peer.executor.shutdown()
