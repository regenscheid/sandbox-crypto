# SPDX-License-Identifier: MIT
"""Startup waits for a sandbox that is still booting before it refuses to start.

The gateway starts the session's VM as the worker starts. The worker tried to reach the
VM's daemon five times in about three seconds, refused to start, and the whole launch
stopped (Mac, 2026-10-21). It now keeps trying for ``startup_recovery`` as well.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker.connections import Connection
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.timeouts import ManagerTimeouts

from .harness import Harness


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True) as value:
        yield value


@pytest.mark.asyncio
async def test_startup_keeps_trying_while_the_sandbox_boots(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery = worker.manager._recovery
    loop = asyncio.get_running_loop()
    ready_at = loop.time() + 4.0  # past five tries of the backoff, about three seconds
    tries: list[float] = []

    async def once(_environment: str, *, startup: bool) -> bool:
        assert startup
        tries.append(loop.time())
        return tries[-1] >= ready_at

    monkeypatch.setattr(recovery, "_recover_once", once)
    monkeypatch.setattr(recovery, "timeouts", ManagerTimeouts(startup_recovery=30.0))
    await asyncio.wait_for(recovery.recover("env1", startup=True), 20)
    assert len(tries) > 5


@pytest.mark.asyncio
async def test_startup_still_refuses_once_the_wait_is_spent(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery = worker.manager._recovery
    loop = asyncio.get_running_loop()
    tries: list[float] = []

    async def unreachable(_environment: str, *, startup: bool) -> bool:
        tries.append(loop.time())
        return False

    monkeypatch.setattr(recovery, "_recover_once", unreachable)
    monkeypatch.setattr(
        recovery, "timeouts", ManagerTimeouts(startup_recovery=1.0, recovery_retry=0.05)
    )
    began = loop.time()
    with pytest.raises(WorkerError, match="startup refused"):
        await asyncio.wait_for(recovery.recover("env1", startup=True), 20)
    assert len(tries) >= 5
    assert loop.time() - began >= 1.0


@pytest.mark.asyncio
async def test_each_failed_startup_attempt_logs_why(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker that cannot reach a daemon at startup is not Ready; its log says why on
    every attempt, with the attempt and the time waited, before it refuses."""
    recovery = worker.manager._recovery

    async def connect(_environment: str) -> Connection:
        raise WorkerError(ErrorKind.UNAVAILABLE, "sandbox lacks the native arm64 scheduler shim")

    monkeypatch.setattr(recovery, "hooks", dataclasses.replace(recovery.hooks, connect=connect))
    monkeypatch.setattr(
        recovery, "timeouts", ManagerTimeouts(startup_recovery=0.2, recovery_retry=0.01)
    )
    caplog.set_level(logging.ERROR, logger="sandbox_science.worker.recovery")
    with pytest.raises(WorkerError, match="startup refused"):
        await asyncio.wait_for(recovery.recover("env1", startup=True), 20)
    failures = [r for r in caplog.records if "startup recovery of env1" in r.getMessage()]
    assert len(failures) >= 5
    assert all(r.levelno == logging.ERROR for r in failures)
    assert "attempt 1 after 0 s: sandbox lacks the native arm64 scheduler shim" in (
        failures[0].getMessage()
    )
    assert "attempt 5 after" in failures[4].getMessage()
