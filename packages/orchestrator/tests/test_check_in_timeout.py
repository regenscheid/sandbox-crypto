# SPDX-License-Identifier: MIT
"""A confined child's check-in (D102): when its parent never answers, the automatic
decision answers the runtime's ``halt`` before the runtime stops waiting for it, so the
child goes on rather than failing on a lost control request."""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import JsonValue

from sandbox_science.orchestrator.confinement.host import Prepared
from sandbox_science.orchestrator.confinement.runtime_loop import RemoteHook
from sandbox_science.orchestrator.confinement.transport import Capability, Channel
from sandbox_science.orchestrator.confinement.wire import HookGiven, RunGiven, ToolboxGiven
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.hooks import DECISION_WAIT_SECONDS, HALT_TIMEOUT_SECONDS
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.resource_hooks import WAIT_SECONDS, Resources
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.testing import FakeClock, eventually

from .test_resource_requests import setup, went_on
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD

ATTEMPT = "cap-" + "c" * 48


def test_the_runtime_waits_longer_for_a_halt_than_a_check_in_waits_for_its_parent() -> None:
    assert WAIT_SECONDS == DECISION_WAIT_SECONDS
    assert HALT_TIMEOUT_SECONDS >= DECISION_WAIT_SECONDS + 300


async def test_an_unanswered_check_in_gets_the_automatic_decision_before_the_timeout(
    projects: ProjectStore,
) -> None:
    clock = FakeClock()
    _, parent, child, requests = setup(projects, turns=1)
    went_on(requests, child)  # its first check-in at a soft cap went on
    hook = Resources(requests, child, turns=1, tokens=None, delegated=True, clock=clock)
    given = RunGiven(
        attempt=ATTEMPT,
        run_id=CHILD,
        task=None,
        history=(),
        max_turns=10,
        max_input_chars=100000,
        system="system",
        role="delegated",
        toolbox=ToolboxGiven(specs=(), revision=0, output_chars=2000),
        hooks=(HookGiven(name=hook.name, veto=True),),
        logged=True,
        folding=False,
        model="scripted",
    )
    log = RunArchive(Archive(projects.control), CONTEXT, run=CHILD, parent=RUN)
    prepared = Prepared(given, child, cast(Toolbox, object()), (hook,), log, None, None)

    async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
        return await prepared.call(kind, value)

    async def refuse(kind: Capability, value: JsonValue) -> JsonValue:
        raise AssertionError("the host asks the runtime nothing here")

    # The runtime measures its requests' timeouts in the same (fake) time as the hook.
    host = Channel(lambda text: runtime.receive(text), host_handle)
    runtime = Channel(lambda text: host.receive(text), refuse, clock=clock)
    run = Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "system"}], max_input_chars=100000
        ),
        max_turns=10,
        turns=1,  # at the child's soft turn cap of one
    )
    proxy = RemoteHook(runtime, ATTEMPT, 0, given.hooks[0])
    halted = asyncio.create_task(proxy.halt(run))
    try:
        await eventually(lambda: bool(requests.pending(parent)))  # it checked in; nobody answers
        while not halted.done():
            # Both the runtime's timeout and the hook's poll are asleep: move time on to
            # the next wake-up, and let them run.
            await eventually(lambda: halted.done() or clock.sleepers >= 2)
            if not halted.done():
                clock.advance(60.0)
            await asyncio.sleep(0)
        assert await halted is None  # the child goes on, no failed control request
        assert DECISION_WAIT_SECONDS <= clock.elapsed < HALT_TIMEOUT_SECONDS
        request, _ = requests.page("p", "s").requests  # latest first; then the one that went on
        assert request.decision is not None
        assert request.decision.automatic
        assert request.decision.request.action == "extend"
    finally:
        halted.cancel()
        await asyncio.gather(halted, return_exceptions=True)
        await prepared.close()
        await runtime.close()
        await host.close()
