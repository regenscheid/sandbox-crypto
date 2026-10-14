# SPDX-License-Identifier: MIT
"""How the executor turns a kernel's messages into a cell's events and result, with a
scripted kernel client in place of Jupyter: streams, outputs and their limits, errors,
foreign and malformed messages, interruption and a kernel that dies."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from sandbox_science.worker import executor

MSG_ID = "m1"


def iopub(kind: str, content: object, parent: str = MSG_ID) -> dict[str, Any]:
    return {"msg_type": kind, "content": content, "parent_header": {"msg_id": parent}}


IDLE = iopub("status", {"execution_state": "idle"})


class Client:
    """A kernel client that answers with the messages it was given, then nothing."""

    def __init__(self, messages: Sequence[object], status: str = "ok") -> None:
        self.messages = list(messages)
        self.status = status

    def execute(self, code: str, **_: object) -> str:
        return MSG_ID

    async def get_iopub_msg(self, timeout: float) -> object:
        if not self.messages:
            raise Empty
        return self.messages.pop(0)

    async def get_shell_msg(self, timeout: float) -> dict[str, Any]:
        return {"parent_header": {"msg_id": MSG_ID}, "content": {"status": self.status}}


class Manager:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.interrupts = 0

    async def is_alive(self) -> bool:
        return self.alive

    async def interrupt_kernel(self) -> None:
        self.interrupts += 1


class Cell:
    """One cell run through ``Executor.execute``, with what it sent."""

    def __init__(self, root: Path, messages: Sequence[object], **options: Any) -> None:
        self.sent: list[dict[str, Any]] = []
        self.executor = executor.Executor(root, self.send)
        self.kernel = executor.Kernel("s1", "python", root, {}, last_used=0.0)
        self.kernel.client = Client(messages, options.pop("status", "ok"))
        self.kernel.manager = Manager(options.pop("alive", True))
        self.running = executor.Running("exe-1", "cell", **options)
        self.outputs: list[dict[str, Any]] = []
        self.captures = {
            "stdout": executor.Capture("stdout", 64),
            "stderr": executor.Capture("stderr", 64),
        }

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def run(self, limit: int = 1000, timeout: float = 30) -> tuple[str, dict[str, Any]]:
        return await self.executor.execute(
            self.running, self.kernel, "code", timeout, limit, self.captures, self.outputs
        )

    def events(self) -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        for message in self.sent:
            if message["event"] == "stream":
                found.append(("stream", (message["name"], message["text"])))
            else:
                found.append((message["event"], message.get("output")))
        return found


@pytest.mark.asyncio
async def test_streams_and_outputs_become_events_and_the_result(tmp_path: Path) -> None:
    cell = Cell(
        tmp_path,
        [
            "not a message",
            iopub("stream", {"name": "stdout", "text": "out"}, parent="m0"),  # another cell's
            iopub("stream", "not a dict"),
            iopub("execute_input", {"execution_count": 7}),
            iopub("stream", {"name": "stdout", "text": "hello "}),
            iopub("stream", {"name": "stderr", "text": "warn"}),
            iopub("stream", {"name": "other", "text": "world"}),  # an unknown stream
            iopub("display_data", {"data": {"text/plain": "shown", "application/json": [1]}}),
            iopub("execute_result", {"data": {"text/plain": "42"}, "execution_count": 7}),
            iopub("execute_result", {"data": "not a bundle", "execution_count": True}),
            IDLE,
            iopub("stream", {"name": "stdout", "text": "after idle"}),
        ],
    )
    state, result = await cell.run()
    assert (state, result) == ("succeeded", {"status": "ok", "execution_count": 7})
    shown = {
        "type": "display",
        "data": {"text/plain": "shown", "application/json": "[1]"},
        "execution_count": None,
    }
    answer = {"type": "result", "data": {"text/plain": "42"}, "execution_count": 7}
    empty = {"type": "result", "data": {}, "execution_count": None}
    assert cell.outputs == [shown, answer, empty]
    assert cell.events() == [
        ("stream", ("stdout", "hello ")),
        ("stream", ("stderr", "warn")),
        ("stream", ("stdout", "world")),
        ("output", shown),
        ("output", answer),
        ("output", empty),
    ]
    assert cell.captures["stdout"].summary()["text"] == "hello world"


@pytest.mark.asyncio
async def test_outputs_over_the_limit_leave_one_note_each_way(tmp_path: Path) -> None:
    cell = Cell(
        tmp_path,
        [
            iopub("display_data", {"data": {"text/plain": "a" * 6}}),
            iopub("display_data", {"data": {"text/plain": "b" * 6}}),  # over: a note
            iopub("display_data", {"data": {"text/plain": "c"}}),  # over: counted
            iopub("display_data", {"data": {"text/plain": "d"}}),  # over: counted
            IDLE,
        ],
    )
    state, _ = await cell.run(limit=10)
    assert state == "succeeded"
    texts = [output["data"]["text/plain"] for output in cell.outputs]
    assert texts == [
        "a" * 6,
        "[output of 6 bytes omitted: over the limit]",
        "[2 more outputs omitted: over the limit]",
    ]
    assert [event for event, _ in cell.events()] == ["output"] * 3


@pytest.mark.asyncio
async def test_outputs_past_the_count_limit_are_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "MAX_OUTPUTS", 1)
    cell = Cell(
        tmp_path,
        [iopub("display_data", {"data": {"text/plain": str(n)}}) for n in range(3)] + [IDLE],
    )
    await cell.run()
    texts = [output["data"]["text/plain"] for output in cell.outputs]
    assert texts == [
        "0",
        "[output of 1 bytes omitted: over the limit]",
        "[1 more outputs omitted: over the limit]",
    ]


@pytest.mark.asyncio
async def test_an_exception_fails_the_cell_with_a_bounded_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "MAX_TRACEBACK_LINES", 2)
    traceback = ["first", "\x1b[0;31msecond\x1b[0m", "third"]
    cell = Cell(
        tmp_path,
        [
            iopub("error", {"ename": "ValueError", "evalue": "bad", "traceback": traceback}),
            IDLE,
        ],
        status="error",
    )
    state, result = await cell.run()
    assert state == "failed"
    assert result == {
        "status": "error",
        "execution_count": None,
        "error": {
            "kind": "exception",
            "message": "ValueError: bad",
            "name": "ValueError",
            "value": "bad",
            "traceback": ["second", "third"],
        },
    }


@pytest.mark.asyncio
async def test_an_error_without_a_traceback_list_keeps_none(tmp_path: Path) -> None:
    cell = Cell(tmp_path, [iopub("error", {"ename": None, "traceback": "x"}), IDLE])
    state, result = await cell.run()
    assert state == "failed"
    assert result["error"]["message"] == ": "
    assert result["error"]["traceback"] == []
    # The kernel's own reply decides the status, here "ok"; the error still fails the cell.
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_a_cancelled_cell_reports_the_kernels_account(tmp_path: Path) -> None:
    cell = Cell(
        tmp_path,
        [
            iopub("error", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ["tb"]}),
            IDLE,
        ],
        status="error",
        cancel_requested=True,
    )
    state, result = await cell.run()
    assert state == "cancelled"
    assert result["error"] == {
        "kind": "cancelled",
        "message": "interrupted on request: KeyboardInterrupt",
        "name": "KeyboardInterrupt",
        "value": "",
        "traceback": ["tb"],
    }
    assert isinstance(cell.kernel.manager, Manager)
    assert cell.kernel.manager.interrupts == 1


@pytest.mark.asyncio
async def test_a_kernel_that_dies_fails_the_cell_and_loses_its_state(tmp_path: Path) -> None:
    cell = Cell(tmp_path, [iopub("stream", {"name": "stdout", "text": "x"})], alive=False)
    state, result = await cell.run()
    assert state == "failed"
    assert result == {
        "status": "error",
        "error": {"kind": "kernel_died", "message": "the kernel process ended"},
    }
    assert cell.kernel.state_lost


def test_a_timed_out_cell_keeps_the_kernels_account_of_the_interrupt() -> None:
    error = {"name": "KeyboardInterrupt", "value": "", "traceback": ["tb"]}
    state, result = executor.cell_outcome(
        "error", 3, error, cancelled=False, timed_out=True, timeout=5
    )
    assert state == "timed_out"
    assert result == {
        "status": "error",
        "execution_count": 3,
        "error": {
            "kind": "timed_out",
            "message": "exceeded the 5 s limit: KeyboardInterrupt",
            "name": "KeyboardInterrupt",
            "value": "",
            "traceback": ["tb"],
        },
    }
    _, plain = executor.cell_outcome("ok", None, None, cancelled=False, timed_out=True, timeout=2.5)
    assert plain["error"] == {"kind": "timed_out", "message": "exceeded the 2.5 s limit"}
