# SPDX-License-Identifier: MIT
"""Attempt cleanup joins effects, including recording, without stranding child waits."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import cast

import pytest

from sandbox_science.orchestrator.aio import OwnedExecutor
from sandbox_science.orchestrator.confinement.host import Prepared
from sandbox_science.orchestrator.confinement.runtime_loop import state
from sandbox_science.orchestrator.confinement.transport import Capability, TransportError
from sandbox_science.orchestrator.confinement.wire import (
    HookRequest,
    LogTool,
    RunGiven,
    ToolboxGiven,
    payload,
)
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.hooks import Closing, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome

from .test_tool_journal import RUN, logger
from .test_tool_journal import projects as projects


@pytest.mark.parametrize(
    "operation", ["shell", "delegate", "wait_delegations", "closing", "finishing"]
)
async def test_attempt_drains_before_its_publisher_closes(projects: ProjectStore, operation: str):
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects: list[str] = []
    publisher = OwnedExecutor(4, "drain-test")

    class Children(TurnHook):
        name = "delegations"

        @asynccontextmanager
        async def lease(self):
            yield
            # An admitted delegate must have finished adding its child first.
            if operation == "delegate":
                assert effects == ["recorded"]
            effects.append("children settled")
            release.set()

        async def closing(self, run, answer):
            await observe()
            return Closing()

        async def finishing(self, run, stopped):
            await observe()
            return

    async def observe():
        entered.set()
        await release.wait()
        # This represents the final artifact record after a worker result arrives.
        await publisher.thread(lambda: effects.append("recorded"))

    class Box:
        specs = ()
        revision = 0
        output_chars = 2000
        plans = None
        skills = None
        image_offer = None

        async def call(self, *args, **kwargs):
            await observe()
            return ToolOutcome("observed", {})

    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    given = RunGiven(
        attempt="cap-" + "a" * 48,
        run_id=RUN,
        task=None,
        history=(),
        max_turns=3,
        max_input_chars=100000,
        system="system",
        role="main",
        toolbox=ToolboxGiven(specs=(), revision=0, output_chars=2000),
        hooks=(),
        logged=True,
        folding=False,
        model="scripted",
    )
    prepared = Prepared(
        given, owner, cast(Toolbox, Box()), (Children(),), logger(projects), None, None
    )
    await prepared.call(Capability.HOOK_ENTER, payload(HookRequest(attempt=given.attempt, slot=0)))
    if operation in {"closing", "finishing"}:
        transcript = Transcript(
            messages=[{"role": "system", "content": "system"}], max_turns=3, max_input_chars=100000
        )
        request = HookRequest(
            attempt=given.attempt, slot=0, state=state(transcript), stopped="turn_limit"
        )
        kind = Capability.CLOSING if operation == "closing" else Capability.FINISHING
    else:
        request = LogTool(
            attempt=given.attempt, call=ToolCall("original", operation, "{}"), operation="original"
        )
        # The public intent is normally installed by LOG_TOOL after the assistant append.
        prepared.intent = request
        kind = Capability.TOOL
    active = asyncio.create_task(prepared.call(kind, payload(request)))
    await entered.wait()

    async def finish():
        await prepared.close()
        await publisher.aclose()
        closed.set()

    cleanup = asyncio.create_task(finish())
    if operation in {"shell", "delegate"}:
        await asyncio.sleep(0)  # allow close to fence admission and begin draining
        assert not closed.is_set()
        assert effects == []
        release.set()
    await asyncio.wait_for(asyncio.gather(active, cleanup), 3)
    assert closed.is_set()
    assert sorted(effects) == ["children settled", "recorded"]
    if operation in {"shell", "delegate"}:
        assert effects == ["recorded", "children settled"]
    with pytest.raises(TransportError, match="closing"):
        await prepared.call(
            Capability.HOOK_ENTER, payload(HookRequest(attempt=given.attempt, slot=0))
        )
