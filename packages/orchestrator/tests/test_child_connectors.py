# SPDX-License-Identifier: MIT
"""Delegated runs' connectors (D96, D99; delegated agents spec DA-3, DA-4, DA-7 to DA-13),
end to end through the real MCP proxy and its fake server.

A child is offered the project's connector tools through the same index, client and
policy as its root, and every call names its own run. Reads pass the plan fence; under a
draft plan, a call the proxy holds for the user still reaches it, and its card is the
child's, with its parent's brief, while the parent is told and may withdraw it; a call
the policy would let through unasked is refused. A grant covers a child's call as a main
run's, and no agent answers a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext
from sandbox_science.mcp_proxy.callers import Caller, token_digest
from sandbox_science.orchestrator import connectors as connectors_module
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.delegation.requests import ChildRequests
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.project_grants import Asked, ProjectGrants
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.policy.approvals import Approvals, prepare_answer
from sandbox_science.testing.mcp import Tls
from sandbox_science.workspace.approvals import GRANT_HOURS, ApprovalClient, asked_of

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS, Harness, Message, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_connectors import TOKEN, Proxied, serve_proxy
from .test_connectors import tls as tls
from .test_delegation import children, delegation, name, results, started, wait
from .test_delegation import delegating as delegating

OPERATOR = "test-secret-workspace-operator-token"
CLASSIFIED = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: child-connectors
defaults:
  action: escalate
mcp:
  proxy:
    worker_access: false
    max_argument_bytes: 1Ki
    max_response_bytes: 4Mi
  servers:
    papers:
      url: https://{host}:{port}/mcp
      revision: "2025-11-25"
      auth: {{mode: bearer, provider: papers-token}}
      classify: {{search: read, crawl: write, large: write}}
      tools:
        allow: [large]
        deny: ["admin_*"]
"""
"""``search`` is a read, which the policy allows; ``crawl`` a write, which it holds for
the user (D97's shipped default, project grants); ``large`` a write a rule allows unasked."""
TASK = call(
    "delegate",
    task="Find the SNOVA parameter sets in the round-2 submission.",
    context="We need them for the comparison table.",
)
DONE = {
    "status": "complete",
    "summary": "I searched the papers server for the parameter sets.",
    "deviations": [],
    "open_questions": [],
}
_DELEGATION = re.compile(r"dlg-[0-9a-f]+")
_REQUEST = re.compile(r"permission request (\d+)")


@pytest_asyncio.fixture
async def classified(tls: Tls, tmp_path: Path) -> AsyncIterator[Proxied]:
    operator = (token_digest(OPERATOR), Caller("workspace", "operator", PROJECT))
    async with serve_proxy(tls, tmp_path / "proxy", CLASSIFIED, [operator]) as found:
        yield found


@pytest.fixture
def quick_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connectors_module, "APPROVAL_POLL", (0.05, 0.05))


def options(harness: Harness, proxied: Proxied) -> list[str]:
    token = harness.tmp / "mcp.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return [
        "--mcp-proxy", proxied.url, "--mcp-token-file", str(token),
        "--mcp-project", PROJECT, "--mcp-server", "papers",
    ]  # fmt: skip


def tool_records(proxied: Proxied, method: str) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], record)
        for record in proxied.sink.kinds("mcp-call")
        if record["method"] == method
    ]


def offered(harness: Harness, role: str) -> list[set[str]]:
    return [{tool["function"]["name"] for tool in tools} for tools in harness.gateway.tools[role]]


@contextlib.asynccontextmanager
async def standing_in(step: Callable[[], Awaitable[bool]]) -> AsyncIterator[None]:
    """Run ``step`` every 50 ms beside the run until it says it is done: the workspace's
    relay, which answers held requests, stood in for with its public pieces."""

    async def loop() -> None:
        while not await step():
            await asyncio.sleep(0.05)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        if task.done():
            task.result()  # a stand-in that failed says why
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_child_lists_and_calls_a_read_tool_under_its_own_run(
    delegating: Harness, classified: Proxied
) -> None:
    """DA-3, DA-4: the child gets the index and loads and calls a read tool as its root
    would; the proxy's records of its calls name the child's run, not its root's."""
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        options=options(delegating, classified),
        delegated=[
            Reply(calls=[call("mcp_tools", names=["search"])]),
            Reply(calls=[call("search", query="SNOVA parameter sets")]),
            Reply(json.dumps(DONE)),
        ],
    )
    (child,) = children(delegating, root)
    first, *later = offered(delegating, "delegated")
    assert "mcp_tools" in first
    assert "search" not in first
    assert "search" in later[-1]  # loaded for the rest of its run
    loaded, searched = results(child)[:2]
    assert json.loads(loaded)["loaded"] == ["search"]
    assert searched.startswith("search")
    assert classified.fake.tool_calls() == ["search"]
    (called,) = tool_records(classified, "tools/call")
    assert called["attribution"]["run"] == name(child)
    assert called["attribution"]["session"] == SESSION
    assert called["decision"]["effect"] == "read"
    runs = {record["attribution"]["run"] for record in tool_records(classified, "tools/list")}
    assert runs == {name(root), name(child)}  # each run lists under its own name
    summary = cast(dict[str, Any], child.summaries["connectors"])
    assert summary["loaded"][0]["name"] == "search"


def _withdraw(messages: list[Message]) -> list[tuple[str, dict[str, Any]]]:
    """The root's withdrawal of the request its child's notice names."""
    text = "\n".join(str(message["content"]) for message in messages)
    (request,) = set(_REQUEST.findall(text))
    delegation_id = _DELEGATION.findall(text)[0]
    return [
        call(
            "withdraw_child_request",
            delegation_id=delegation_id,
            permission_request=int(request),
        )
    ]


async def test_a_childs_write_under_a_draft_plan_is_held_for_the_user_and_withdrawn(
    delegating: Harness, classified: Proxied, quick_waits: None
) -> None:
    """D99, DA-7 to DA-10: under a draft plan the child's read goes on; its write reaches
    the proxy, which holds it as a request attributed to the child with its parent's brief;
    the parent is told, withdraws it, and the call ends as a denied one. A write the policy
    would pass unasked is refused by the fence, and the child cannot withdraw or answer."""

    async def plan() -> None:
        delegating.user_plan()  # a draft, published while the child runs

    def withdrawn() -> list[int]:
        store = ProjectStore(delegating.state)
        try:
            waiting = ChildRequests(store.control).in_session(PROJECT, SESSION)
        finally:
            store.close()
        return [seq for seq, found in waiting.items() if found.withdrawn_by is not None]

    denied: list[int] = []

    async def relay() -> bool:
        # The workspace denies a withdrawn request at the proxy (D99; its own tests).
        for seq in await asyncio.to_thread(withdrawn):
            if seq not in denied:
                approvals = Approvals()
                approvals.load(classified.sink.records(0))
                classified.sink.append(
                    *prepare_answer(
                        approvals, seq, approve=False, scope=None, hours=None,
                        now=classified.sink.now(),
                    )
                )  # fmt: skip
                denied.append(seq)
        return bool(denied)

    async with standing_in(relay):
        root = await delegating.run(
            "root",
            [
                Reply(calls=[TASK]),
                wait(),
                Reply(calls_from=_withdraw),
                wait(),
                Reply(calls=[call("plan_read")]),
                Reply("Withdrawn."),
            ],
            options=options(delegating, classified),
            delegated=[
                # A tool called before it is loaded is loaded and called (DA-3).
                Reply(calls=[call("search", query="SNOVA")], before=plan),
                Reply(calls=[call("crawl", url="https://example.org/round-2")]),
                Reply(
                    calls=[
                        call("large"),
                        call("withdraw_child_request", delegation_id="x", permission_request=1),
                    ]
                ),
                Reply(json.dumps(DONE)),
            ],
        )
    (child,) = children(delegating, root)
    searched, crawled, large, answered = results(child)
    assert searched.startswith("search")
    crawl_error = json.loads(crawled)["error"]
    assert crawl_error["kind"] == "approval_denied"
    large_error = json.loads(large)["error"]
    assert (large_error["kind"], "draft" in large_error["message"]) == ("policy_denied", True)
    assert json.loads(answered)["error"]["message"].startswith("not_permitted_for_role")
    assert classified.fake.tool_calls() == ["search"]  # neither write reached the server
    # The held request is the child's, with its parent's brief, as the card shows it.
    (seq,) = denied
    (escalation,) = [cast(dict[str, Any], found) for found in classified.sink.kinds("escalation")]
    assert escalation["attribution"]["run"] == name(child)
    assert escalation["caller_kind"] == "agent"
    assert escalation["effect"] == "write"
    store = ProjectStore(delegating.state)
    try:
        with store.control.read() as tx:
            row = tx.sql.execute(
                "SELECT run, delegation_id, tool, withdrawn_by, ended_at FROM child_requests "
                "WHERE request=?",
                (seq,),
            ).fetchone()
    finally:
        store.close()
    (delegation_id,) = started(root)
    assert tuple(row[:4]) == (name(child), delegation_id, "crawl", name(root))
    assert row[4] is not None  # the child's wait ended with the denial
    assert delegation(delegating, delegation_id).brief["task"] == TASK[1]["task"]
    # The root was told, as the child's notice, and withdrew it; it could not approve it.
    told = [
        message
        for request in delegating.gateway.requests["main"]
        for message in request
        if f"permission request {seq}" in str(message.get("content", ""))
    ]
    assert told
    assert "withdraw_child_request" in str(told[0]["content"])
    withdrew = json.loads(results(root)[2])
    assert withdrew["state"] == "withdrawn"


@pytest.mark.parametrize("scope", ["project", "session"])
async def test_a_grant_covers_a_childs_call_as_a_main_runs(
    delegating: Harness, classified: Proxied, quick_waits: None, scope: str
) -> None:
    """DA-13, D97: the child's call is the agent's in the root's session, so the user's
    class grant for the project, or for the session, answers it: the grants relay's own
    matching, through the proxy's operator listing, approves it and the call proceeds."""
    documents = delegating.documents()
    try:
        # As the user's answer to an earlier write of the root's would have given it.
        asked = Asked("mcp", "agent", SESSION, "project", server="papers", effect="write")
        ProjectGrants(documents).give(PROJECT, asked, scope, USER, datetime.now(UTC))
    finally:
        documents.close()
    client = ApprovalClient(classified.url, OPERATOR)
    seen: list[dict[str, Any]] = []

    async def relay() -> bool:
        for held in await client.pending():
            documents = DocumentStore(store_path(delegating.state))
            try:
                found = ProjectGrants(documents).covering(
                    PROJECT, asked_of(held), datetime.now(UTC)
                )
            finally:
                documents.close()
            if found:
                seen.append(held.model_dump(mode="json"))
                await client.answer(held.seq, approve=True, scope="call", hours=GRANT_HOURS)
        return bool(seen)

    async with standing_in(relay):
        root = await delegating.run(
            "root",
            [Reply(calls=[TASK]), wait(), Reply("Done.")],
            options=options(delegating, classified),
            delegated=[
                Reply(calls=[call("crawl", url="https://example.org/round-2")]),
                Reply(json.dumps(DONE)),
            ],
        )
    (child,) = children(delegating, root)
    (crawled,) = results(child)
    assert crawled.startswith("crawl")
    assert classified.fake.tool_calls() == ["crawl"]
    (held,) = seen
    assert (held["run"], held["session"], held["caller_kind"]) == (name(child), SESSION, "agent")
    assert (held["effect"], held["max_grant"]) == ("write", "project")


async def test_a_check_is_offered_the_read_tools_only(
    delegating: Harness, classified: Proxied
) -> None:
    """DA-1, PD-33: a check gets the connector tools the proxy classes ``read``; a write
    is not in its index, and a call to it is refused for its role."""
    check = call("delegate", check={"entry": "C-1", "revision": 1}, note="Look it up.")
    root = await delegating.run(
        "root",
        [Reply(calls=[check]), wait(), Reply("Done.")],
        options=options(delegating, classified),
        delegated=[
            Reply(calls=[call("search", query="Fermat"), call("crawl", url="https://e.org/")]),
            Reply("Verdict: inconclusive"),
        ],
    )
    (child,) = children(delegating, root)
    index = next(
        tool["function"]["description"]
        for tool in delegating.gateway.tools["delegated"][0]
        if tool["function"]["name"] == "mcp_tools"
    )
    assert "  search" in index
    assert "crawl" not in index
    searched, crawled = results(child)[:2]
    assert searched.startswith("search")
    assert json.loads(crawled)["error"]["message"].startswith("not_permitted_for_role")
    assert classified.fake.tool_calls() == ["search"]


# --- The fence, by the proxy's word on each tool ----------------------------------------


class Listed:
    def __init__(self, effect_class: str | None, escalates: bool) -> None:
        self.effect_class, self.escalates = effect_class, escalates


def test_the_fence_reads_each_connector_tools_class(tmp_path: Path) -> None:
    """D99: under a draft plan a connector read passes, a call the proxy holds for the
    user reaches it, and one the policy would pass unasked is refused; once the session
    was opened again, none passes."""
    projects = ProjectStore(tmp_path)
    documents = DocumentStore(store_path(tmp_path))
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        session = projects.open_session(PROJECT, SESSION, dedicated=False)
        RunState(projects.control).acquire(PROJECT, SESSION, "root", "holder")
        plans = SessionPlans(documents)
        plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")  # a draft
        tools = {
            "papers_search": Listed("read", False),
            "papers_crawl": Listed("write", True),
            "papers_post": Listed("write", False),
            "papers_unknown": Listed(None, False),
        }
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
        )
        fence = ChildFence(plans, projects, context, run_id="child", connectors=tools.get)

        def gate(tool: str) -> str | None:
            refused = asyncio.run(fence.gate_call(tool, "effect"))
            return None if refused is None else refused.message

        assert gate("papers_search") is None
        assert gate("papers_crawl") is None  # the proxy holds it for the user
        for tool in ("papers_post", "papers_unknown", "publish"):
            refused = gate(tool)
            assert refused is not None, tool
            assert "draft" in refused, tool
        projects.close_session(PROJECT, SESSION)
        projects.open_session(PROJECT, SESSION, dedicated=False)
        for tool in ("papers_search", "papers_crawl"):
            refused = gate(tool)
            assert refused is not None, tool
            assert "closed" in refused, tool
    finally:
        documents.close()
        projects.close()
