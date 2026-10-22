# SPDX-License-Identifier: MIT
"""Upgrade admission and actual owned file-read cancellation; no installer/service."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.contracts import CondaPackage, ExecutionContext, Lockfile, ProvisionRequest
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.provisioner import Provisioner, _Stop


def request() -> ProvisionRequest:
    return ProvisionRequest(
        operation_id="upgrade",
        context=ExecutionContext(project_id="p", session_id="s", environment_id="e"),
        purpose="synthetic",
        conda=("pari",),
        action="upgrade",
    )


@pytest.mark.asyncio
async def test_copied_unknown_action_refuses_before_encoder_or_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visits: list[str] = []

    class Copied(str):
        def __eq__(self, other: object) -> bool:
            visits.append("eq")
            return True

    original = request()

    def encoder(*args: object, **kwargs: object) -> str:
        visits.append("encode")
        raise AssertionError("unsupported action encoded")

    monkeypatch.setattr(ProvisionRequest, "model_dump_json", encoder)
    owner = object.__new__(Provisioner)
    for action in ("remove", None, True, Copied("install")):
        with pytest.raises(WorkerError) as caught:
            await owner.submit(original.model_copy(update={"action": action}))
        assert caught.value.kind == "invalid_request"
    assert visits == []


@pytest.mark.asyncio
async def test_upgrade_target_read_retains_actual_thread_fd_through_repeated_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = object.__new__(Provisioner)
    revision = SimpleNamespace(revision=1)
    owner.store = cast(Any, SimpleNamespace(revision=lambda e: revision))
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    file = tmp_path / "original-lockfile"
    file.write_bytes(b"synthetic")
    fds: list[int] = []
    lockfile = Lockfile(conda=(CondaPackage(name="pari", version="2", build="0"),))

    def read() -> Lockfile:
        with file.open("rb") as held:
            fds.append(held.fileno())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "fixture release deadline"
            assert held.read() == b"synthetic"
        return lockfile

    async def selected(environment: str, number: int) -> Lockfile:
        assert environment == "e"
        assert number == 1
        return await asyncio.to_thread(read)

    monkeypatch.setattr(owner, "reconciliation", SimpleNamespace(lockfile=selected), raising=False)
    op = cast(Any, SimpleNamespace(record=SimpleNamespace(request=request())))
    task = asyncio.create_task(owner._upgrade_targets(op, ("pari",)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel("first")
        await asyncio.sleep(0)
        task.cancel("second")
        await asyncio.sleep(0)
        assert not task.done()
        assert len(fds) == 1
        os.fstat(fds[0])
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("first",)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fds[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "missing", "changed"])
async def test_upgrade_target_history_unknown_missing_or_changed_refuses(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = object.__new__(Provisioner)
    revision = SimpleNamespace(revision=1)
    reads = 0

    def current(e: str) -> object:
        nonlocal reads
        reads += 1
        return SimpleNamespace(revision=2) if case == "changed" and reads > 1 else revision

    owner.store = cast(Any, SimpleNamespace(revision=current))

    async def selected(e: str, revision: int) -> Lockfile | None:
        return (
            None
            if case == "unknown"
            else Lockfile(
                conda=(
                    CondaPackage(
                        name="other" if case == "missing" else "pari", version="2", build="0"
                    ),
                )
            )
        )

    monkeypatch.setattr(owner, "reconciliation", SimpleNamespace(lockfile=selected), raising=False)
    with pytest.raises(_Stop) as caught:
        await owner._upgrade_targets(
            cast(Any, SimpleNamespace(record=SimpleNamespace(request=request()))), ("pari",)
        )
    assert caught.value.reason == "resolve_failed"
