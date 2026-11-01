# SPDX-License-Identifier: MIT
"""Runtime requests that wait on long work (D47): a parent's end-of-run wait for its
children, a long tool, a model call the gateway retries. Such a request has no timeout of
its own: it waits for as long as the host keeps saying it is still working on it, so no
connection-level timeout fails a healthy run, and a peer that goes silent still fails it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import Prepared
from sandbox_science.orchestrator.confinement.runtime_loop import RemoteHook
from sandbox_science.orchestrator.confinement.transport import (
    HEARTBEAT_SECONDS,
    PATIENCE_SECONDS,
    Capability,
    Channel,
    TransportError,
)
from sandbox_science.orchestrator.confinement.wire import HookGiven, RunGiven, ToolboxGiven
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.hooks import RunView, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.testing import FakeClock, eventually

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects

ATTEMPT = "cap-" + "d" * 48
HOURS = 3 * 3600.0


class Children(TurnHook):
    """A parent's delegations: at the run's end it waits until its children end."""

    name = "delegations"

    def __init__(self) -> None:
        self.ended = asyncio.Event()

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        await self.ended.wait()
        return "child_outcome_unavailable"  # any answer: it must reach the runtime


def linked(
    clock: FakeClock, handle: Callable[[Capability, JsonValue], Awaitable[JsonValue]]
) -> tuple[Channel, Channel, list[bool]]:
    """A host and a runtime channel on one fake clock; ``silent[0]`` drops the host's
    frames from then on, as a connection that died without closing."""
    silent = [False]

    async def to_runtime(text: str) -> None:
        if not silent[0]:
            await runtime.receive(text)

    async def refuse(kind: Capability, value: JsonValue) -> JsonValue:
        raise AssertionError("the host asks the runtime nothing here")

    host = Channel(to_runtime, handle, clock=clock)
    runtime = Channel(lambda text: host.receive(text), refuse, clock=clock)
    return host, runtime, silent


async def run_clock(clock: FakeClock, task: asyncio.Task[object], until: float) -> None:
    """Move the fake clock on, one heartbeat at a time, while ``task`` runs, up to
    ``until`` seconds; between steps, let every sleeper due re-arm."""
    while not task.done() and clock.elapsed < until:
        await eventually(lambda: task.done() or clock.sleepers >= 2)
        if not task.done():
            clock.advance(HEARTBEAT_SECONDS)
        await asyncio.sleep(0)


def prepared(projects: ProjectStore, hook: TurnHook) -> Prepared:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    given = RunGiven(
        attempt=ATTEMPT,
        run_id=RUN,
        task=None,
        history=(),
        max_turns=10,
        max_input_chars=100000,
        system="system",
        role="main",
        toolbox=ToolboxGiven(specs=(), revision=0, output_chars=2000),
        hooks=(HookGiven(name=hook.name),),
        logged=True,
        folding=False,
        model="scripted",
    )
    log = RunArchive(Archive(projects.control), CONTEXT, run=RUN, parent=None)
    return Prepared(given, owner, cast(Toolbox, object()), (hook,), log, None, None)


def transcript() -> Transcript:
    return Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "system"}], max_input_chars=100000
        ),
        max_turns=10,
    )


async def test_a_parents_end_of_run_wait_outlasts_30_minutes(projects: ProjectStore) -> None:
    """The parent's ``finishing`` waits three hours for its children, and its answer
    still reaches the runtime."""
    clock = FakeClock()
    hook = Children()
    attempt = prepared(projects, hook)

    async def handle(kind: Capability, value: JsonValue) -> JsonValue:
        return await attempt.call(kind, value)

    host, runtime, _ = linked(clock, handle)
    proxy = RemoteHook(runtime, ATTEMPT, 0, HookGiven(name=hook.name))
    finishing = asyncio.create_task(proxy.finishing(transcript(), "turn_limit"))
    try:
        await run_clock(clock, cast(asyncio.Task[object], finishing), HOURS)
        assert not finishing.done()  # still waiting, well past any 30-minute limit
        hook.ended.set()  # the children end
        assert await finishing == "child_outcome_unavailable"
        assert clock.elapsed >= HOURS
    finally:
        finishing.cancel()
        await asyncio.gather(finishing, return_exceptions=True)
        hook.ended.set()
        await attempt.close()
        await runtime.close()
        await host.close()


async def test_a_long_wait_on_a_peer_that_goes_silent_fails_after_its_patience() -> None:
    clock = FakeClock()
    forever = asyncio.Event()

    async def handle(kind: Capability, value: JsonValue) -> JsonValue:
        await forever.wait()
        return None

    host, runtime, silent = linked(clock, handle)
    waiting = asyncio.create_task(
        runtime.request(Capability.FINISHING, {}, timeout=None, patience=PATIENCE_SECONDS)
    )
    try:
        await run_clock(clock, cast(asyncio.Task[object], waiting), 3600.0)
        assert not waiting.done()  # an hour in, the host still says it is working
        silent[0] = True  # the connection dies without closing
        dead_at = clock.elapsed
        await run_clock(clock, cast(asyncio.Task[object], waiting), dead_at + 2 * PATIENCE_SECONDS)
        with pytest.raises(TransportError, match="outcome is unknown"):
            await waiting
        assert clock.elapsed <= dead_at + PATIENCE_SECONDS + HEARTBEAT_SECONDS
    finally:
        forever.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await runtime.close()
        await host.close()


async def test_a_call_with_patience_has_no_timeout_of_its_own() -> None:
    channel = Channel(lambda text: asyncio.sleep(0), lambda kind, value: asyncio.sleep(0))
    with pytest.raises(ValueError, match="no timeout of its own"):
        await channel.request(Capability.TOOL, {}, timeout=60, patience=PATIENCE_SECONDS)
    await channel.close()
