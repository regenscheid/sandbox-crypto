# SPDX-License-Identifier: MIT
"""Durable direct-parent messaging, including replay and failed delivery."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator.agent_messages import AgentMailbox, AgentMessageStore
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState

from .loop_harness import Harness, Reply, call, own
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, results, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE

PROJECT, SESSION = "p", "s"


def test_root_prompt_routes_user_messages_to_plan_or_direct_child() -> None:
    assert "create or revise the session plan" in SYSTEM_PROMPT
    assert "send a targeted update" in SYSTEM_PROMPT
    assert "document, artifact or ledger entry" in SYSTEM_PROMPT


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[tuple[ControlStore, str, str]]:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    control = projects.control
    RunState(control).acquire(PROJECT, SESSION, "root", "holder")
    records = DelegationRecords(control)
    child_id, grandchild_id = new_id("dlg"), new_id("dlg")
    records.start(
        PROJECT,
        SESSION,
        Delegation(
            delegation_id=child_id,
            parent="root",
            run="child",
            kind="task",
            brief={"task": "Research a lemma."},
            started_at=datetime.now(UTC),
        ),
    )
    records.start(
        PROJECT,
        SESSION,
        Delegation(
            delegation_id=grandchild_id,
            parent="child",
            run="grandchild",
            kind="task",
            brief={"task": "Check a case."},
            started_at=datetime.now(UTC),
        ),
    )
    yield control, child_id, grandchild_id
    projects.close()


def test_direct_parent_messages_survive_replay_and_acknowledgment(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child_id, grandchild_id = tree
    mailbox = AgentMessageStore(control)
    sent = mailbox.send_child(PROJECT, SESSION, "root", None, child_id, "Try another case.", "op-1")
    assert (
        mailbox.send_child(PROJECT, SESSION, "root", None, child_id, "Try another case.", "op-1")
        == sent
    )
    assert mailbox.pending(PROJECT, SESSION, child_id) == [sent]
    assert mailbox.pending(PROJECT, SESSION, None) == []
    mailbox.acknowledge(PROJECT, SESSION, child_id, "child", [sent.message_id])
    assert mailbox.pending(PROJECT, SESSION, child_id) == []
    assert mailbox.status(PROJECT, SESSION, "root", sent.message_id).delivered_run == "child"

    up = mailbox.send_parent(PROJECT, SESSION, "grandchild", grandchild_id, "Case fails.", "op-2")
    assert up.recipient_delegation == child_id
    assert mailbox.pending(PROJECT, SESSION, child_id) == [up]
    assert mailbox.pending(PROJECT, SESSION, None) == []
    to_root = mailbox.send_parent(
        PROJECT, SESSION, "child", child_id, "Please revise the plan.", "op-3"
    )
    assert to_root.recipient_delegation is None
    assert mailbox.pending(PROJECT, SESSION, None) == [to_root]


def test_message_access_is_limited_to_live_direct_relatives(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child_id, grandchild_id = tree
    mailbox = AgentMessageStore(control)
    with pytest.raises(ControlError, match="direct child"):
        mailbox.send_child(PROJECT, SESSION, "root", None, grandchild_id, "Skip parent.", "op-a")
    with pytest.raises(ControlError, match="direct child"):
        mailbox.send_child(
            PROJECT, SESSION, "grandchild", grandchild_id, child_id, "Reverse.", "op-b"
        )
    with pytest.raises(ControlError, match="sent by this run"):
        mailbox.status(PROJECT, SESSION, "child", "agm-missing")
    RunState(control).release(PROJECT, SESSION, "holder")
    with pytest.raises(ControlError, match="no longer holds"):
        mailbox.send_parent(PROJECT, SESSION, "child", child_id, "Late report.", "op-c")


def test_unseen_message_to_ended_child_returns_to_root(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child_id, _ = tree
    mailbox = AgentMessageStore(control)
    sent = mailbox.send_child(
        PROJECT, SESSION, "root", None, child_id, "Correct the proof.", "op-1"
    )
    DelegationRecords(control).end(
        PROJECT, SESSION, child_id, Ending(ended="returned", stopped="answered")
    )
    assert mailbox.target_ended(PROJECT, SESSION, child_id)
    assert mailbox.pending(PROJECT, SESSION, None) == [sent]
    mailbox.acknowledge(PROJECT, SESSION, None, "root", [sent.message_id])
    assert mailbox.pending(PROJECT, SESSION, None) == []
    assert mailbox.status(PROJECT, SESSION, "root", sent.message_id).delivered_run == "root"
    assert mailbox.send_child(
        PROJECT, SESSION, "root", None, child_id, "Correct the proof.", "op-1"
    ) == mailbox.status(PROJECT, SESSION, "root", sent.message_id)
    with pytest.raises(ControlError, match="running direct child"):
        mailbox.send_child(PROJECT, SESSION, "root", None, child_id, "Another.", "op-2")


async def test_status_distinguishes_root_fallback_from_child_delivery(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child_id, _ = tree
    store = AgentMessageStore(control)
    sent = store.send_child(PROJECT, SESSION, "root", None, child_id, "Check again.", "op")
    DelegationRecords(control).end(
        PROJECT, SESSION, child_id, Ending(ended="returned", stopped="answered")
    )
    mailbox = AgentMailbox(
        store,
        ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="env"),
        run="root",
        delegation=None,
        may_message_child=True,
    )
    queued, _ = await mailbox.call("agent_message_status", {"message_id": sent.message_id}, "q")
    assert json.loads(queued)["state"] == "recipient_ended"
    store.acknowledge(PROJECT, SESSION, None, "root", [sent.message_id])
    delivered, _ = await mailbox.call("agent_message_status", {"message_id": sent.message_id}, "q")
    assert json.loads(delivered)["state"] == "delivery_failed"


async def test_root_mailbox_wait_wakes_for_user_message(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, _, _ = tree

    async def user_arrived() -> None:
        return

    mailbox = AgentMailbox(
        AgentMessageStore(control),
        ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="env"),
        run="root",
        delegation=None,
        may_message_child=True,
        user_arrived=user_arrived,
    )
    result, _ = await mailbox.call("wait_agent_message", {"timeout_seconds": 1}, "wait")
    assert json.loads(result) == {"state": "user_message"}


async def test_root_correction_reaches_a_child_at_its_next_safe_point(
    delegating: Harness,
) -> None:
    started_call, release = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        started_call.set()
        await release.wait()

    async def after_child_started() -> None:
        await asyncio.wait_for(started_call.wait(), 10)

    async def release_child() -> None:
        release.set()

    def send(messages: list[dict[str, object]]) -> list[tuple[str, dict[str, object]]]:
        found = next(
            re.search(r"dlg-[0-9a-f]{24}", str(message["content"]))
            for message in messages
            if message["role"] == "tool" and re.search(r"dlg-[0-9a-f]{24}", str(message["content"]))
        )
        assert found is not None
        return [
            call(
                "message_child",
                delegation_id=found.group(),
                text="The user corrected the target: check GF(19), not GF(17).",
            )
        ]

    waiting = wait()
    waiting.before = release_child
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("delegate", task="Check the finite-field cases.")]),
            Reply(calls_from=send, before=after_child_started),
            waiting,
            Reply("Done."),
        ],
        routed={
            "Check the finite-field cases.": [
                Reply(calls=[call("list_files", path=".")], before=held),
                Reply(json.dumps(DONE)),
            ]
        },
    )
    (child,) = children(delegating, root)
    (delegation_id,) = started(root)
    assert child.delivered["agent_messages"]
    assert any(
        "The user corrected the target: check GF(19), not GF(17)." in str(message["content"])
        for message in own(child)
        if message["role"] == "user"
    )
    sent = json.loads(results(root)[1])
    assert sent["state"] == "queued"
    projects = ProjectStore(delegating.state)
    try:
        mailbox = AgentMessageStore(projects.control)
        assert mailbox.status(
            PROJECT, SESSION, run_name(root.started_at, root.run_id), sent["message_id"]
        ).delivered_run
    finally:
        projects.close()
    assert delegation_id in json.dumps(results(root))


async def test_child_reports_to_parent_while_working(
    delegating: Harness,
) -> None:
    root = await delegating.run(
        "root",
        [
            Reply(calls=[call("delegate", task="Investigate the relation.")]),
            wait(),
            wait(),
            Reply("Done."),
        ],
        routed={
            "Investigate the relation.": [
                Reply(
                    calls=[call("message_parent", text="First case fails; I am checking another.")]
                ),
                Reply(json.dumps(DONE)),
            ]
        },
    )
    (child,) = children(delegating, root)
    assert any(
        "First case fails; I am checking another." in str(message["content"])
        for message in own(root)
        if message["role"] == "user"
    )
    assert root.delivered["agent_messages"]
    assert "message_parent" in json.dumps(own(child))
