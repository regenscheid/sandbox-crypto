# SPDX-License-Identifier: MIT
"""Attempt cleanup joins effects, including recording, without stranding child waits."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
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
from sandbox_science.orchestrator.consistency.controller import ReviewController, Reviews
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.hooks import Closing, TurnHook
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import FakeClock, eventually

from .test_consistency import claim, dispatch, raise_finding
from .test_tool_journal import RUN, logger
from .test_tool_journal import projects as projects


def prepared_attempt(
    projects: ProjectStore, toolbox: Toolbox, hooks: Sequence[TurnHook], *, child: bool = False
) -> Prepared:
    records = WorkRecords(projects.control)
    records.admit("p", "s", RUN)
    run = "20261016T030100000000Z-run-2" if child else RUN
    owner = records.admit("p", "s", run, parent=RUN) if child else records.owner("p", "s", run)
    assert owner is not None
    given = RunGiven(
        attempt="cap-" + "a" * 48,
        run_id=run,
        task=None,
        history=(),
        max_turns=3,
        max_input_chars=100000,
        system="system",
        role="delegated" if child else "main",
        toolbox=ToolboxGiven(specs=(), revision=0, output_chars=2000),
        hooks=(),
        logged=True,
        folding=False,
        model="scripted",
    )
    log = (
        RunArchive(Archive(projects.control), toolbox.context, run=run, parent=RUN)
        if child
        else logger(projects)
    )
    return Prepared(given, owner, toolbox, hooks, log, None, None)


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

    prepared = prepared_attempt(projects, cast(Toolbox, Box()), (Children(),))
    given = prepared.given
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


@pytest.mark.parametrize("child", [False, True])
async def test_disconnect_interrupts_mailbox_wait_without_consuming_messages(
    projects: ProjectStore, child: bool
) -> None:
    context = ExecutionContext(project_id="p", session_id="s", environment_id="env-p")
    clock = FakeClock()
    child_id = new_id("dlg")
    messages = AgentMessageStore(projects.control)
    mailbox = AgentMailbox(
        messages,
        context,
        run=RUN,
        delegation=child_id if child else None,
        may_message_child=True,
        clock=clock,
    )
    box = Toolbox(WorkerClient("http://unused.invalid", "fixture"), context, mailbox=mailbox)
    prepared = prepared_attempt(projects, box, (mailbox,), child=child)
    RunState(projects.control).acquire("p", "s", RUN, "holder")
    DelegationRecords(projects.control).start(
        "p",
        "s",
        Delegation(
            delegation_id=child_id,
            parent=RUN,
            run=prepared.given.run_id if child else "child",
            kind="task",
            brief={"task": "Observe a message"},
            started_at=datetime.now(UTC),
        ),
    )
    request = LogTool(
        attempt=prepared.given.attempt,
        call=ToolCall("wait-original", "wait_agent_message", '{"timeout_seconds":300}'),
        operation="wait-original",
    )
    await prepared.log.start_tool(request.call, request.operation)
    prepared.intent = request
    waiting = asyncio.create_task(prepared.call(Capability.TOOL, payload(request)))
    await eventually(lambda: clock.sleepers == 1)
    # The next message remains queued during shutdown; the abandoned wait may
    # not consume/acknowledge it on behalf of a dead runtime.
    if child:
        sent = messages.send_child("p", "s", RUN, None, child_id, "Correction", "sent")
    else:
        sent = messages.send_parent("p", "s", "child", child_id, "Result", "sent")
    await asyncio.wait_for(prepared.close(), 2)
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert clock.sleepers == 0
    assert messages.pending("p", "s", mailbox.delegation) == [sent]
    assert sent.delivered_run is None
    assert prepared.tool is None
    assert prepared.intent == request


async def test_all_hook_leases_close_once_despite_a_failed_exit_and_cancelled_cleanup(
    projects: ProjectStore,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    exited: list[str] = []

    class Notice(TurnHook):
        name = "notice"

        @asynccontextmanager
        async def lease(self):
            try:
                yield
            finally:
                entered.set()
                await release.wait()
                exited.append(self.name)

    class Broken(TurnHook):
        name = "broken"

        @asynccontextmanager
        async def lease(self):
            try:
                yield
            finally:
                exited.append(self.name)
                raise RuntimeError("synthetic lease exit failure")

    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=projects.get("p").sessions["s"].environment_id,
    )
    documents = DocumentStore(store_path(projects.control.path.parent))
    reviews = ReviewStore(documents)
    claim(reviews)
    review = dispatch(reviews)
    raise_finding(reviews, review)
    reviews.finish("p", "s", review, "owner", "findings", "Requires correction")
    controller = ReviewController(reviews, context, run_id=RUN, model=None, alias=None)
    box = Toolbox(WorkerClient("http://unused.invalid", "fixture"), context)
    prepared = prepared_attempt(projects, box, (Reviews(controller), Notice(), Broken()))
    try:
        for slot in range(3):
            await prepared.call(
                Capability.HOOK_ENTER,
                payload(HookRequest(attempt=prepared.given.attempt, slot=slot)),
            )
        notices = controller.notice_store.pending("p", "s", RUN, set())
        assert len(notices) == 1
        assert controller.notice_store.pending("p", "s", "replacement", set()) == []
        closing = asyncio.create_task(prepared.close())
        await asyncio.wait_for(entered.wait(), 2)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        assert exited == ["broken"]
        release.set()
        with pytest.raises(RuntimeError, match="synthetic lease exit failure"):
            await closing
        assert exited == ["broken", "notice"]
        assert not prepared.leases
        # Actual ReviewController.active has exited, including its nested renewal
        # lease: the undelivered finding is immediately available to a new run.
        assert controller.notice_store.pending("p", "s", "replacement", set()) == notices
        with pytest.raises(RuntimeError, match="synthetic lease exit failure"):
            await prepared.close()
        assert exited == ["broken", "notice"]
    finally:
        release.set()
        await controller.close()
        documents.close()
