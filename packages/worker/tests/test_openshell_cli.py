# SPDX-License-Identifier: MIT
"""CLI exit races retain timeout/cancellation and actual subprocess wait ownership."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker.environments import OpenShellCli
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.settings import OpenShellBackendSettings

pytestmark = pytest.mark.asyncio


class Process:
    def __init__(
        self, *, exited: bool = True, ready: bool = False, reap: bool = True, code: int = 0
    ) -> None:
        self.returncode = code if exited else None
        self.exited = exited
        self.code = code
        self.communicating = asyncio.Event()
        self.answer = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.kills = self.waits = self.wait_cancellations = 0
        if ready:
            self.answer.set()
        if reap:
            self.release.set()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicating.set()
        await self.answer.wait()
        return b"unchanged output\n", b"\x1b[31munchanged failure\x1b[0m"

    def kill(self) -> None:
        self.kills += 1
        if self.exited:
            # Process has exited after communicate was cancelled, but its caller
            # still owns the wait. This is the actual asyncio transport exception.
            raise ProcessLookupError

    async def wait(self) -> int:
        self.waits += 1
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.wait_cancellations += 1
            raise
        self.returncode = self.code if self.exited else -9
        return self.returncode


def cli(monkeypatch: pytest.MonkeyPatch, process: Process) -> OpenShellCli:
    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return OpenShellCli(OpenShellBackendSettings(isolation_tier=3), {})


async def test_timeout_after_exit_keeps_unavailable_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process()
    with pytest.raises(WorkerError, match="openshell sandbox get took over") as caught:
        await cli(monkeypatch, process).run("sandbox", "get", "synthetic", timeout=0)
    assert caught.value.kind is ErrorKind.UNAVAILABLE
    assert (process.kills, process.waits, process.wait_cancellations) == (1, 1, 0)


async def test_caller_cancel_after_exit_keeps_original_cancel_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process()
    task = asyncio.create_task(cli(monkeypatch, process).run("sandbox", "get", "synthetic"))
    try:
        await process.communicating.wait()
        task.cancel("original cancellation")
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        assert (process.kills, process.waits, process.wait_cancellations) == (1, 1, 0)
    finally:
        process.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancel_retains_actual_wait_and_first_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process(exited=False, reap=False)
    task = asyncio.create_task(cli(monkeypatch, process).run("sandbox", "get", "synthetic"))
    try:
        await process.communicating.wait()
        task.cancel("original cancellation")
        await asyncio.wait_for(process.waiting.wait(), 1)
        task.cancel("later cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        assert process.wait_cancellations == 0
        process.release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        assert process.waits == 1
    finally:
        process.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_timeout_cleanup_drains_before_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process(exited=False, reap=False)
    task = asyncio.create_task(
        cli(monkeypatch, process).run("sandbox", "get", "synthetic", timeout=0)
    )
    try:
        await asyncio.wait_for(process.waiting.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert process.wait_cancellations == 0
        process.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.waits == 1
    finally:
        process.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_outer_deadline_keeps_its_own_timeout_after_exited_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await cli(monkeypatch, process).run("sandbox", "get", "synthetic")
    assert process.waits == 1


@pytest.mark.parametrize("code", [0, 1])
async def test_normal_success_and_nonzero_error_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    process = Process(ready=True, code=code)
    command = cli(monkeypatch, process)
    if code == 0:
        assert await command.run("sandbox", "get", "synthetic") == "unchanged output\n"
    else:
        with pytest.raises(WorkerError, match="openshell sandbox get failed: unchanged failure"):
            await command.run("sandbox", "get", "synthetic")
    assert process.kills == process.waits == 0


async def test_unexpected_kill_error_is_not_silently_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Process()

    def denied() -> None:
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr(process, "kill", denied)
    with pytest.raises(PermissionError, match="synthetic permission failure"):
        await cli(monkeypatch, process).run("sandbox", "get", "synthetic", timeout=0)
