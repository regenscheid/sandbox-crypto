# SPDX-License-Identifier: MIT
"""A call's written files are its own when its environment is shared (``attribution``):
another session's upload is never among them, a file request's write while it runs is
unattributed, and so is ``shared/`` while another session's attempt runs beside it."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import pytest_asyncio

from sandbox_science.contracts import (
    Execution,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    ShellRequest,
    WriteFileRequest,
)
from sandbox_science.testing import FakeClock
from sandbox_science.worker.attribution import MAX_KEPT, Activity
from sandbox_science.worker.live import Live
from sandbox_science.worker.manager import ExecutionManager

from .harness import Harness, running

pytestmark = pytest.mark.slow

A = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
B = A.model_copy(update={"session_id": "s2"})
WAIT = "while [ ! -e gate ]; do sleep 0.05; done"


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[ExecutionManager]:
    async with await Harness.start(tmp_path) as started:
        yield started.manager


def job(operation: str, script: str, context: ExecutionContext = A) -> JobRequest:
    return JobRequest(
        operation_id=operation,
        context=context,
        script=script,
        purpose="attribution test",
        expected_progress="none",
    )


async def write(worker: ExecutionManager, context: ExecutionContext, path: str) -> None:
    content = base64.b64encode(b"delivered").decode()
    await worker.write_file(
        WriteFileRequest(context=context, path=path, content_b64=content, create_only=True)
    )


async def test_an_upload_and_a_hand_back_during_a_job_are_not_its_files(
    worker: ExecutionManager,
) -> None:
    started, _ = await worker.submit(job("job", f"echo mine > mine.txt; {WAIT}"))
    await running(worker, started.execution_id)
    user = B.model_copy(update={"actor": "user"})
    await write(worker, user, "shared/uploads/s2/u1/source.txt")
    await write(worker, A, "work/d1/returned.txt")
    await write(worker, A, "gate")
    ended = await worker.wait(started.execution_id)
    assert ended.state is ExecutionState.SUCCEEDED
    assert ended.result is not None
    assert "mine.txt" in ended.result.written_files
    every = {*ended.result.written_files, *ended.result.unattributed_files}
    assert "shared/uploads/s2/u1/source.txt" not in every
    assert {"work/d1/returned.txt", "gate"} <= set(ended.result.unattributed_files)
    assert not {"work/d1/returned.txt", "gate"} & set(ended.result.written_files)


async def test_shared_files_are_unattributed_while_another_session_runs_beside(
    worker: ExecutionManager,
) -> None:
    started, _ = await worker.submit(job("long", WAIT))
    await running(worker, started.execution_id)
    beside, _ = await worker.submit(
        ShellRequest(
            operation_id="beside",
            context=B,
            script='echo b > "$PROJECT_SHARED/b.txt"; echo own > own.txt',
        )
    )
    other = await worker.wait(beside.execution_id)
    assert other.result is not None
    assert other.result.written_files == ("own.txt",)
    assert other.result.unattributed_files == ("shared/b.txt",)
    await write(worker, A, "gate")
    ended = await worker.wait(started.execution_id)
    assert ended.result is not None
    assert "shared/b.txt" in ended.result.unattributed_files
    assert "shared/b.txt" not in ended.result.written_files
    # With nothing running beside it, a call's shared files are its own.
    alone, _ = await worker.submit(
        ShellRequest(operation_id="alone", context=B, script='echo c > "$PROJECT_SHARED/c.txt"')
    )
    done = await worker.wait(alone.execution_id)
    assert done.result is not None
    assert done.result.written_files == ("shared/c.txt",)
    assert done.result.unattributed_files == ()


async def test_the_same_session_beside_a_call_leaves_its_shared_files_attributed(
    worker: ExecutionManager,
) -> None:
    started, _ = await worker.submit(job("long", WAIT))
    await running(worker, started.execution_id)
    beside, _ = await worker.submit(
        ShellRequest(operation_id="beside", context=A, script='echo a > "$PROJECT_SHARED/a.txt"')
    )
    done = await worker.wait(beside.execution_id)
    assert done.result is not None
    assert done.result.written_files == ("shared/a.txt",)
    await write(worker, A, "gate")
    await worker.wait(started.execution_id)


# The account itself, with the clock in hand.


def _live(
    clock: FakeClock,
    session: str,
    operation: str,
    area: str | None = None,
    kind: Literal["shell", "cell", "pdf", "job"] = "shell",
) -> Live:
    execution = Execution(
        execution_id=f"exe-{operation}",
        attempt_id=f"att-{operation}",
        operation_id=operation,
        kind=kind,
        context=A.model_copy(update={"session_id": session, "write_area": area}),
        state=ExecutionState.RUNNING,
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    live = Live(execution, "env1", asyncio.get_running_loop().create_future())
    live.dispatched, live.dispatched_at = True, clock.monotonic()
    return live


def _end(clock: FakeClock, live: Live, activity: Activity) -> None:
    live.ended_at = clock.monotonic()
    live.execution = live.execution.model_copy(update={"state": ExecutionState.SUCCEEDED})
    activity.ended(live)


async def test_an_attempt_that_ended_beside_a_call_still_counts() -> None:
    clock = FakeClock()
    lives: dict[str, Live] = {}
    activity = Activity(lives, clock)
    mine = lives["mine"] = _live(clock, "s1", "mine")
    clock.advance(1)
    theirs = lives["theirs"] = _live(clock, "s2", "theirs")
    clock.advance(1)
    _end(clock, theirs, activity)
    del lives["theirs"]  # the journal forgets it soon after it ends
    clock.advance(1)
    mine.ended_at = clock.monotonic()
    wrote, _ = activity.attribute(mine, ["shared/x", "own"], [])
    assert wrote.own == ("own",)
    assert wrote.unattributed == ("shared/x",)


async def test_an_attempt_that_ended_before_a_call_began_does_not() -> None:
    clock = FakeClock()
    lives: dict[str, Live] = {}
    activity = Activity(lives, clock)
    lives["waiting"] = _live(clock, "s3", "waiting")  # keeps the account
    theirs = lives["theirs"] = _live(clock, "s2", "theirs")
    clock.advance(1)
    _end(clock, theirs, activity)
    clock.advance(1)
    mine = lives["mine"] = _live(clock, "s1", "mine")
    del lives["waiting"]
    clock.advance(1)
    mine.ended_at = clock.monotonic()
    wrote, went = activity.attribute(mine, ["shared/x"], ["shared/gone", "shared/uploads/s2/a"])
    assert wrote.own == ("shared/x",)
    assert went.own == ("shared/gone",)


async def test_a_gap_in_the_account_leaves_shared_files_unattributed() -> None:
    clock = FakeClock()
    lives: dict[str, Live] = {}
    activity = Activity(lives, clock)
    mine = lives["mine"] = _live(clock, "s1", "mine")
    for index in range(MAX_KEPT + 1):
        activity.wrote("env1", f"file-{index}", clock.monotonic())
        clock.advance(0.001)
    mine.ended_at = clock.monotonic()
    wrote, _ = activity.attribute(mine, ["shared/x", "own"], [])
    assert wrote.unattributed == ("shared/x",)
    assert wrote.own == ("own",)
    restored = _live(clock, "s1", "restored")
    restored.restored = True
    wrote, _ = activity.attribute(restored, ["shared/y"], [])
    assert wrote.unattributed == ("shared/y",)


async def test_no_account_is_kept_while_nothing_runs() -> None:
    clock = FakeClock()
    activity = Activity({}, clock)
    activity.wrote("env1", "file", clock.monotonic())
    lives = {"mine": _live(clock, "s1", "mine")}
    activity.live = lives
    clock.advance(1)
    lives["mine"].ended_at = clock.monotonic()
    wrote, _ = activity.attribute(lives["mine"], ["file"], [])
    assert wrote.own == ("file",)


async def test_a_delegated_runs_write_area_is_its_own_and_no_one_elses() -> None:
    """A delegated run's processes write only in its write area (DA-20): what changed
    elsewhere is not its own, but for its job's logs; what changed in its area while it
    ran is not another attempt's own."""
    clock = FakeClock()
    lives: dict[str, Live] = {}
    activity = Activity(lives, clock)
    main = lives["main"] = _live(clock, "s1", "main", kind="job")
    child = lives["child"] = _live(clock, "s1", "child", "work/dlg-1", kind="job")
    clock.advance(1)
    child.ended_at = main.ended_at = clock.monotonic()
    paths = ["work/dlg-1/out.txt", "notes.md", f".jobs/{child.execution.execution_id}/stdout.log"]
    wrote, _ = activity.attribute(child, paths, [])
    assert wrote.own == ("work/dlg-1/out.txt", f".jobs/{child.execution.execution_id}/stdout.log")
    assert wrote.unattributed == ("notes.md",)
    wrote, _ = activity.attribute(main, paths, [])
    assert wrote.own == ("notes.md",)
    assert wrote.unattributed == (
        "work/dlg-1/out.txt",
        f".jobs/{child.execution.execution_id}/stdout.log",
    )
    # A PDF helper's render is the platform's, wherever it places it.
    render = _live(clock, "s1", "render", "work/dlg-1", kind="pdf")
    render.ended_at = clock.monotonic()
    wrote, _ = activity.attribute(render, ["figures/p1.png"], [])
    assert wrote.own == ("figures/p1.png",)
