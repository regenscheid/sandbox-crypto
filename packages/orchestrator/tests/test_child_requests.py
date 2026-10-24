# SPDX-License-Identifier: MIT
"""The delegated runs' fence gates outward effects only, and a child's held call is the
user's to answer: attributed to the child with its parent's brief, told to the parent,
which may withdraw it but not approve it (D99; delegated agents spec DA-7 to DA-10)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.orchestrator.agent_messages import (
    WITHDRAW_SPEC,
    AgentMailbox,
    AgentMessageStore,
)
from sandbox_science.orchestrator.chat import RunChat
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.delegation.effects import SESSION_TOOLS, reach
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords
from sandbox_science.orchestrator.delegation.requests import ChildApproval, ChildRequests
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.orchestrator.tools import PDF_READS

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS
from .test_capability_assembly import offered

COMPUTE = ("shell", "python", "sage", "start_job", "write_file", "provision", "pdf_render")
OUTWARD = ("papers_fetch", "publish", "admit_hosts", "delegate", "ledger_change")

# --- The fence ------------------------------------------------------------------------------


def test_every_session_tool_is_a_built_in_tool_and_the_pdf_reads_are_reads() -> None:
    entries = offered("task").entries
    assert set(entries) >= SESSION_TOOLS
    assert all(entries[name].effect == "effect" for name in SESSION_TOOLS)
    assert set(PDF_READS) == {"pdf_info", "pdf_pages", "pdf_search"}
    assert all(entries[name].effect == "read" for name in PDF_READS)
    assert entries["pdf_render"].effect == "effect"
    # A tool the classification does not name, a connector tool among them, is outward.
    assert reach("papers_fetch") == "outward"
    assert reach("") == "outward"


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[ProjectStore, SessionPlans, ExecutionContext]]:
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    session = projects.open_session(PROJECT, SESSION, dedicated=False)
    context = ExecutionContext(
        project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
    )
    try:
        yield projects, SessionPlans(documents), context
    finally:
        documents.close()
        projects.close()


def _gate(fence: ChildFence, tool: str, effect: Any = "effect") -> str | None:
    refused = asyncio.run(fence.gate_call(tool, effect))
    return None if refused is None else refused.message


def test_a_draft_plan_holds_only_outward_effects(
    stores: tuple[ProjectStore, SessionPlans, ExecutionContext],
) -> None:
    projects, plans, context = stores
    plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")  # a draft
    fence = ChildFence(plans, projects, context, run_id="child")
    for tool in COMPUTE:
        assert _gate(fence, tool) is None, tool
    for tool in PDF_READS:
        assert _gate(fence, tool, "read") is None
    for tool in OUTWARD:
        refused = _gate(fence, tool)
        assert refused is not None, tool
        assert "draft" in refused, tool
    # A call the fence cannot name, as through the class-only gate, is outward.
    held = asyncio.run(fence.gate("effect"))
    assert held is not None
    assert "draft" in held.message
    assert fence.summary() is not None


def test_a_user_edit_of_the_plan_holds_only_outward_effects(
    stores: tuple[ProjectStore, SessionPlans, ExecutionContext],
) -> None:
    projects, plans, context = stores
    first = plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")
    current = plans.current(USER_PLANS)
    assert current is not None
    plans.set_status(
        USER_PLANS, "p2", first.ref, current.head.status_revision,
        current.head.progress_cursor, "active", "Go.",
    )  # fmt: skip
    fence = ChildFence(plans, projects, context, run_id="child")
    assert _gate(fence, "papers_fetch") is None  # an active plan, unchanged
    plans.revise(USER_PLANS, "p3", first.ref, PlanBody.model_validate(PLAN), "Edited.")
    for tool in COMPUTE:
        assert _gate(fence, tool) is None, tool
    refused = _gate(fence, "papers_fetch")
    assert refused is not None
    assert refused.startswith("plan_changed")
    assert fence.summary() == {"refused": refused}


def test_a_session_opened_again_holds_the_childs_compute_too(
    stores: tuple[ProjectStore, SessionPlans, ExecutionContext],
) -> None:
    projects, plans, context = stores
    fence = ChildFence(plans, projects, context, run_id="child")
    assert _gate(fence, "python") is None
    projects.close_session(PROJECT, SESSION)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    refused = _gate(fence, "python")
    assert refused is not None
    assert "closed" in refused


# --- Held calls -----------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Iterator[tuple[ControlStore, str, str]]:
    """A root holding the session, its task child, and the child's own child."""
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    control = projects.control
    RunState(control).acquire(PROJECT, SESSION, "root", "holder")
    child, grandchild = new_id("dlg"), new_id("dlg")
    for delegation, parent, run, task in (
        (child, "root", "child", "Find the SNOVA parameter sets in the round-2 submission."),
        (grandchild, "child", "grandchild", "Check one parameter set."),
    ):
        DelegationRecords(control).start(
            PROJECT,
            SESSION,
            Delegation(
                delegation_id=delegation, parent=parent, run=run, kind="task",
                brief={"task": task, "context": "The parent's notes."},
                started_at=datetime.now(UTC),
            ),
        )  # fmt: skip
    yield control, child, grandchild
    projects.close()


class FakeChat:
    """The child's chat as ``ChildApproval`` uses it: its waiting events and words."""

    def __init__(self, run: str, said: str) -> None:
        self.run, self.said = run, said
        self.waits: list[dict[str, object] | None] = []

    async def approval(self, wait: dict[str, object] | None) -> None:
        self.waits.append(wait)


CONTEXT = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")


def _mailbox(control: ControlStore, run: str, delegation: str | None) -> AgentMailbox:
    return AgentMailbox(
        AgentMessageStore(control),
        CONTEXT,
        run=run,
        delegation=delegation,
        may_message_child=True,
        requests=ChildRequests(control),
    )


async def test_a_held_call_is_the_childs_with_its_brief_and_the_parent_is_told(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child, _ = tree
    chat = FakeChat("child", "I need the round-2 page to read the parameters.")
    wait = ChildApproval(
        cast(RunChat, chat),
        ChildRequests(control),
        AgentMessageStore(control),
        CONTEXT,
        delegation=child,
    )
    held = {"permission_request": 41, "server": "papers", "tool": "papers_fetch"}
    await wait(held)
    assert chat.waits == [held]  # the chat's own waiting event, as for a main run
    (request,) = ChildRequests(control).in_session(PROJECT, SESSION).values()
    assert (request.request, request.delegation_id, request.run, request.parent) == (
        41, child, "child", "root",
    )  # fmt: skip
    assert (request.server, request.tool, request.kind) == ("papers", "papers_fetch", "task")
    assert request.brief == "Find the SNOVA parameter sets in the round-2 submission."
    assert request.words == "I need the round-2 page to read the parameters."
    # The root is told through its mailbox, once however often the call is retried.
    await wait(held)
    (notice,) = AgentMessageStore(control).pending(PROJECT, SESSION, None)
    assert (notice.sender_run, notice.sender_delegation) == ("child", child)
    assert "permission request 41" in notice.body
    assert "withdraw_child_request" in notice.body
    assert "cannot approve" in notice.body
    await wait(None)
    assert ChildRequests(control).in_session(PROJECT, SESSION) == {}


async def test_the_parent_withdraws_a_request_and_no_one_else_can(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child, grandchild = tree
    requests = ChildRequests(control)
    requests.record(
        PROJECT, SESSION, run="grandchild", delegation=grandchild, request=7,
        server="papers", tool="papers_fetch", words="Fetching.",
    )  # fmt: skip
    args = {"delegation_id": grandchild, "permission_request": 7}
    # The root is not the grandchild's parent, and a child cannot withdraw its own.
    with pytest.raises(ControlError, match="not_permitted_for_role"):
        await _mailbox(control, "root", None).call(WITHDRAW_SPEC.name, args, "op-1")
    with pytest.raises(ControlError, match="not_permitted_for_role"):
        requests.withdraw(PROJECT, SESSION, parent="grandchild", delegation=grandchild, request=7)
    with pytest.raises(ControlError, match="no request 7 of delegation"):
        requests.withdraw(PROJECT, SESSION, parent="root", delegation=child, request=7)
    text, record = await _mailbox(control, "child", child).call(WITHDRAW_SPEC.name, args, "op-2")
    assert json.loads(text)["state"] == "withdrawn"
    assert record["child_request"]["permission_request"] == 7
    assert requests.withdrawn(PROJECT, [7, 8]) == {7}
    assert requests.in_session(PROJECT, SESSION)[7].withdrawn_by == "child"
    # The same withdrawal again is the same; a request that no longer waits is refused.
    again = requests.withdraw(PROJECT, SESSION, parent="child", delegation=grandchild, request=7)
    assert again.withdrawn_by == "child"
    requests.record(
        PROJECT, SESSION, run="child", delegation=child, request=8,
        server="papers", tool="papers_fetch", words="",
    )  # fmt: skip
    requests.ended(PROJECT, SESSION, 8, "child")
    with pytest.raises(ControlError, match="no longer waits"):
        requests.withdraw(PROJECT, SESSION, parent="root", delegation=child, request=8)
    # A record is kept: nothing rewrites who asked.
    with control.write() as tx, pytest.raises(Exception, match="kept"):
        tx.sql.execute("UPDATE child_requests SET run='root' WHERE request=7")


def test_only_the_delegations_own_run_records_a_request(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child, _ = tree
    with pytest.raises(ControlError, match="no delegation"):
        ChildRequests(control).record(
            PROJECT, SESSION, run="root", delegation=child, request=3,
            server="papers", tool="papers_fetch", words="",
        )  # fmt: skip


async def test_a_parent_can_withdraw_but_has_no_way_to_approve() -> None:
    """DA-8: only the user answers. The parent's tools withdraw, and nothing approves."""
    for role in ("main", "task"):
        names = set(offered(role).entries)
        assert not any("approve" in name or "answer_permission" in name for name in names)
    schema = WITHDRAW_SPEC.parameters["properties"]
    assert set(schema) == {"delegation_id", "permission_request"}  # no decision to give
    toolbox = offered("task")
    refused = await toolbox.call(
        ToolCall("c1", "approve_child_request", '{"permission_request": 7}'), "op-1"
    )
    assert "not_permitted_for_role" in refused.text


def test_a_parent_is_offered_withdrawal_with_its_messaging_and_a_leaf_is_not(
    tree: tuple[ControlStore, str, str],
) -> None:
    control, child, _ = tree
    assert WITHDRAW_SPEC in _mailbox(control, "root", None).specs
    leaf = AgentMailbox(
        AgentMessageStore(control),
        CONTEXT,
        run="child",
        delegation=child,
        may_message_child=False,
        requests=ChildRequests(control),
    )
    assert WITHDRAW_SPEC not in leaf.specs
