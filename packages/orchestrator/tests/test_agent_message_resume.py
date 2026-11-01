# SPDX-License-Identifier: MIT
"""Queued direct-relative messages follow explicit Resume, never a replacement task."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import TypeAdapter

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
from sandbox_science.orchestrator.continuity.plans import Plans
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.recovery import Recovery
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.harness import Harness, Role, RunRequest, Settings
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_run_harness import Sink, main, stores
from .test_tool_journal import RUN
from .test_tree_recovery import RecoveryWorker, drive
from .test_work_ownership import CHILD, CONTINUATION, GRANDCHILD, record


@contextmanager
def message_tree(tmp_path):
    state, documents = stores(tmp_path)
    session = state.projects.open_session("p", "s", dedicated=False)
    control = state.projects.control
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    works, records = WorkRecords(control), DelegationRecords(control)
    ids = {}
    for run, parent in ((RUN, None), (CHILD, RUN), (GRANDCHILD, CHILD)):
        owner = works.admit("p", "s", run, parent=parent)
        delegation = None if parent is None else new_id("dlg")
        request = RunRequest(
            "p",
            "s",
            "Continue research",
            parent=parent,
            delegation=delegation,
            tokens=100000 if parent else None,
            alias="scripted",
            memory_version=0,
            root=RUN if parent else None,
            may_delegate=run == CHILD,
        )
        Blueprints(control).save(
            owner,
            Blueprint(
                context=context,
                request=TypeAdapter(RunRequest).dump_python(
                    request,
                    mode="json",
                    exclude={"history", "stop_by"},
                ),
            ),
        )
        if delegation is not None:
            assert parent is not None
            ids[run] = delegation
            records.start(
                "p",
                "s",
                Delegation(
                    delegation_id=delegation,
                    parent=parent,
                    run=run,
                    kind="task",
                    brief={"task": "Continue research"},
                    started_at=datetime.now(UTC),
                ),
            )
    lease = RunState(control)
    lease.acquire("p", "s", RUN, "original")
    mailbox = AgentMessageStore(control)
    messages = [
        mailbox.send_child("p", "s", RUN, None, ids[CHILD], "Parent correction", "down"),
        mailbox.send_child(
            "p",
            "s",
            CHILD,
            ids[CHILD],
            ids[GRANDCHILD],
            "Leaf correction",
            "leaf",
        ),
        mailbox.send_parent("p", "s", GRANDCHILD, ids[GRANDCHILD], "Leaf result", "up"),
        mailbox.send_parent("p", "s", CHILD, ids[CHILD], "Parent result", "root"),
    ]
    for delegation in ids.values():
        records.end("p", "s", delegation, Ending("failed", "error"))
    works.interrupt("p", "s", RUN)
    lease.release("p", "s", "original")
    try:
        yield state, documents, works, context, ids, messages
    finally:
        documents.close()
        state.projects.close()


async def test_root_resume_delivers_both_directions_before_next_model_call(tmp_path):
    with message_tree(tmp_path) as (state, _, works, _, ids, messages):
        control = state.projects.control
        mailbox = AgentMessageStore(control)
        # An ended attempt alone must not return the child's instructions as failed.
        assert mailbox.pending("p", "s", None) == [messages[3]]
        assert not mailbox.target_ended("p", "s", ids[CHILD])
        control.close()  # durable queues and lineage survive a service restart
        model = ScriptedModel(
            [
                ModelTurn(json.dumps({"status": "complete", "summary": "Recovered"}))
                for _ in range(8)
            ]
        )
        harness = Harness(
            cast(WorkerClient, RecoveryWorker()),
            main("Recovered root"),
            delegated=Role(model, "scripted"),
            stores=state,
            settings=Settings(min_isolation_tier=None),
        )
        request = works.request("p", "s")
        assert request is not None
        recovery = Recovery(harness, "p", "s", request)
        sink = Sink()
        assert not await drive(recovery, sink, asyncio.Event()), sink.errors
        latest = {run: works.get("p", "s", run).current for run in (RUN, CHILD, GRANDCHILD)}
        for message, recipient in zip(messages, (CHILD, GRANDCHILD, CHILD, RUN), strict=True):
            received = state.projects.run("p", "s", latest[recipient])
            assert message.message_id in received.delivered["agent_messages"]
            receipt = mailbox.status("p", "s", latest[message.sender_run], message.message_id)
            assert receipt.recipient_delegation == message.recipient_delegation
            assert receipt.delivered_run == latest[recipient]
        child = state.projects.run("p", "s", latest[CHILD])
        assert child.delegation != ids[CHILD]
        assert mailbox.pending("p", "s", child.delegation) == []
        assert mailbox.pending("p", "s", None) == []
        assert any(
            "Parent correction" in str(m) and "Leaf result" in str(m) for m in model.requests
        )
        status = AgentMailbox(
            mailbox, child.context, run=latest[RUN], delegation=None, may_message_child=True
        )
        response, _ = await status.call(
            "agent_message_status", {"message_id": messages[0].message_id}, "status"
        )
        assert json.loads(response)["state"] == "delivered"


async def test_prepared_parent_receives_old_messages_and_wait_wakes_without_consuming(tmp_path):
    with message_tree(tmp_path) as (state, _, works, context, ids, messages):
        control = state.projects.control
        request = works.request("p", "s")
        assert request is not None
        RunState(control).acquire("p", "s", CONTINUATION, "resume")
        _, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
        plans = Plans(control, "p", "s", request)
        with plans.authority(epoch, CONTINUATION):
            plans.prepare(RUN, run=CONTINUATION, started=datetime.now(UTC))
            child = plans.get(CHILD)
            delegation = new_id("dlg")
            DelegationRecords(control).start(
                "p",
                "s",
                Delegation(
                    delegation_id=delegation,
                    parent=CONTINUATION,
                    run=child.run,
                    kind="task",
                    brief={"task": "Continue research"},
                    started_at=child.started,
                ),
            )
        assert works.owner("p", "s", child.run) is None  # leaf-first parent not admitted yet
        store = AgentMessageStore(control)
        hook = AgentMailbox(
            store, context, run=child.run, delegation=delegation, may_message_child=True
        )
        result, _ = await hook.call("wait_agent_message", {"timeout_seconds": 1}, "wait")
        expected = [messages[0].message_id, messages[2].message_id]
        assert json.loads(result) == {"state": "available", "message_ids": expected}
        assert [m.message_id for m in store.pending("p", "s", delegation)] == expected
        notices = await hook.notices("turn", Transcript(context=AgentContext(messages=[])))
        assert [n.ids[0] for n in notices] == expected
        store.acknowledge("p", "s", delegation, child.run, expected)
        assert store.pending("p", "s", ids[CHILD]) == []


def test_new_same_brief_assignment_cannot_receive_or_ack_old_work_messages(tmp_path):
    with message_tree(tmp_path) as (state, _, works, _, ids, messages):
        control = state.projects.control
        # A new root and child are different work, even when the brief matches.
        works.admit("p", "s", CONTINUATION)
        unrelated = "20261018T030001000000Z-replacement"
        works.admit("p", "s", unrelated, parent=CONTINUATION)
        delegation = new_id("dlg")
        DelegationRecords(control).start(
            "p",
            "s",
            Delegation(
                delegation_id=delegation,
                parent=CONTINUATION,
                run=unrelated,
                kind="task",
                brief={"task": "Continue research"},
                started_at=datetime.now(UTC),
            ),
        )
        store = AgentMessageStore(control)
        assert store.pending("p", "s", delegation) == []
        store.acknowledge("p", "s", delegation, unrelated, [messages[0].message_id])
        with pytest.raises(ControlError, match="recipient's work"):
            store.acknowledge("p", "s", ids[CHILD], unrelated, [messages[0].message_id])
        with pytest.raises(ControlError, match="sent by this run"):
            store.status("p", "s", CONTINUATION, messages[0].message_id)
        assert store.status("p", "s", RUN, messages[0].message_id).delivered_run is None


@pytest.mark.parametrize("terminal", ["completed", "stopped"])
def test_terminal_work_returns_undelivered_message_to_root(tmp_path, terminal):
    with message_tree(tmp_path) as (state, _, works, _, ids, messages):
        if terminal == "completed":
            works.finish(record(GRANDCHILD))
        else:
            works.stop("p", "s", GRANDCHILD)
        store = AgentMessageStore(state.projects.control)
        assert store.target_ended("p", "s", ids[GRANDCHILD])
        assert messages[1] in store.pending("p", "s", None, limit=10)
