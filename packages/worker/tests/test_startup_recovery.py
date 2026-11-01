# SPDX-License-Identifier: MIT
"""Startup waits for a sandbox that is still booting before it refuses to start.

The gateway starts the session's VM as the worker starts. The worker tried to reach the
VM's daemon five times in about three seconds, refused to start, and the whole launch
stopped (Mac, 2026-10-21). It now keeps trying for ``startup_recovery`` as well.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

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
