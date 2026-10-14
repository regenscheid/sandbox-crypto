# SPDX-License-Identifier: MIT
"""Kernel lists an executor sends are bounded, whichever request they answer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext, KernelStatusRequest, SessionCloseRequest
from sandbox_science.contracts.worker import LANGUAGES
from sandbox_science.worker.channel import ExecutorChannel, Message

from .harness import CONTEXT, Harness, view

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]
SCOPE = ExecutionContext.model_validate(CONTEXT)


def kernel(generation: int, language: str = "python") -> dict[str, object]:
    return {"kernel_id": "krn-" + "a" * 24, "language": language, "generation": generation}


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as worker:
        await worker.manager.kernel_status(KernelStatusRequest(context=SCOPE))
        yield worker


@pytest.mark.parametrize(
    ("kernels", "reported"),
    [
        ([kernel(n) for n in range(1, 2001)], 0),  # longer than LANGUAGES: none believed
        ([kernel(1), kernel(2)], 1),  # one kernel per language
        ([kernel(1), kernel(1, "sage")], 2),
    ],
)
async def test_a_session_close_reports_at_most_one_kernel_per_language(
    worker: Harness, monkeypatch: pytest.MonkeyPatch, kernels: list[object], reported: int
) -> None:
    channel = view(worker.manager).channel("env1")
    request = ExecutorChannel.request

    async def replying(peer: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "close_session":
            return {"kernels": kernels}
        return await request(peer, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", replying)
    closed = await worker.manager.close_session(SessionCloseRequest(context=SCOPE))
    assert len(closed.kernels) == reported <= len(LANGUAGES)
    assert len({item.language for item in closed.kernels}) == reported
    assert channel.closed is False
