# SPDX-License-Identifier: MIT
"""A task brief narrows its child's connectors and never widens them (delegated agents
spec DA-6, DA-2): the child's index and loads hold only what every narrowing above it
admits, a brief that names a server no connector is, or more than its parent has, is
refused, and the delegation records the narrowing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.connectors import Connectors
from sandbox_science.orchestrator.delegation.narrowing import Narrowing, narrow, of_brief
from sandbox_science.orchestrator.delegation.specs import TaskBrief, brief_of
from sandbox_science.orchestrator.harness import McpAccess
from sandbox_science.orchestrator.projects import run_name
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .loop_harness import PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .scripted import ToolCall
from .test_delegation import children, configure, delegation, results, started, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE
from .test_run_service import ended, post, serving

PORTAL = "portal"
"""A configured server with servers behind it, as crypto-research-mcp is."""
WEB = "web"
LISTED: dict[str, tuple[str, ...]] = {
    PORTAL: (
        "e-print-mcp_search_eprint",
        "e-print-mcp_get_eprint",
        "paper-search-mcp_search_papers",
    ),
    WEB: ("fetch", "crawl"),
}
CONTEXT = ExecutionContext(project_id=PROJECT, session_id="s-1", environment_id="e")


class FakeClient:
    """An MCP client whose servers list ``LISTED`` and answer every call."""

    classifies = False

    def __init__(self) -> None:
        self.asked: list[tuple[str | None, str, str]] = []

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        if method == "tools/list":
            self.asked.append((run, server, method))
            tools = [
                {"name": name, "description": f"{name}.", "inputSchema": {"type": "object"}}
                for name in LISTED[server]
            ]
            return {"tools": tools}
        self.asked.append((run, server, str(params.get("name"))))
        return {"content": [{"type": "text", "text": f"{params.get('name')} answered"}]}


async def loaded(narrowing: Narrowing | None, client: FakeClient | None = None) -> Connectors:
    found = Connectors(
        client or FakeClient(), [PORTAL, WEB], session="s-1", run="r", narrowing=narrowing
    )
    await found.load()
    return found


# --- Applying a narrowing -------------------------------------------------------------------


async def test_a_narrowing_offers_only_what_it_names_and_lists_no_other_server() -> None:
    client = FakeClient()
    narrowing = Narrowing((("portal/e-print-mcp",),))
    connectors = await loaded(narrowing, client)
    assert set(connectors.tools) == {"e-print-mcp_search_eprint", "e-print-mcp_get_eprint"}
    assert connectors.narrowed == {"paper-search-mcp_search_papers"}
    assert {server for _, server, _ in client.asked} == {PORTAL}  # web is not listed
    assert connectors.index().startswith(
        "Your brief narrows your connectors to: portal/e-print-mcp."
    )
    assert connectors.summary()["narrowing"] == [["portal/e-print-mcp"]]
    # Loading a tool it left out is refused for the role; the others load.
    async with WorkerClient("http://127.0.0.1:9", "test-secret-worker") as worker_client:
        toolbox = Toolbox(worker_client, CONTEXT, connectors=connectors, delegated=True)
        names = '{"names": ["e-print-mcp_get_eprint", "paper-search-mcp_search_papers"]}'
        outcome = await toolbox.call(ToolCall("c1", "mcp_tools", names), "op-1")
        answer = json.loads(outcome.text)
        assert answer["loaded"] == ["e-print-mcp_get_eprint"]
        assert answer["not_permitted"] == ["paper-search-mcp_search_papers"]
        assert "not_permitted_for_role" in answer["note"]
        # Called by name, it is not one of the run's tools.
        called = await toolbox.call(ToolCall("c2", "paper-search-mcp_search_papers", "{}"), "op-2")
        assert "not_permitted_for_role" in called.text
    assert ("r", PORTAL, "paper-search-mcp_search_papers") not in client.asked


async def test_every_level_must_admit_a_tool() -> None:
    """A grandchild named a whole server its narrowed parent had a part of: it gets only
    that part (DA-2)."""
    both = Narrowing((("portal/e-print-mcp_*", "web/fetch"), ("portal", "web/crawl")))
    connectors = await loaded(both)
    assert set(connectors.tools) == {"e-print-mcp_search_eprint", "e-print-mcp_get_eprint"}
    nothing = await loaded(Narrowing(((),)))
    assert nothing.tools == {}
    assert nothing.servers == ()


def test_a_recorded_brief_gives_its_levels_and_a_malformed_one_narrows_to_nothing() -> None:
    assert of_brief({"task": "x"}) is None
    assert of_brief({"connectors": ["web"]}) == Narrowing((("web",),))
    assert of_brief({"connectors": ["web"], "inherited_connectors": [["web", "portal"]]}) == (
        Narrowing((("web", "portal"), ("web",)))
    )
    assert of_brief({"inherited_connectors": [["portal"]]}) == Narrowing((("portal",),))
    assert of_brief({"connectors": "web"}) == Narrowing(((),))
    assert of_brief({"inherited_connectors": ["web"]}) == Narrowing(((),))


# --- Checking a brief -----------------------------------------------------------------------


def refusal(entries: list[str], offered: Connectors | None) -> WorkerAPIError:
    with pytest.raises(WorkerAPIError) as raised:
        narrow(entries, configured=(PORTAL, WEB, "off"), offered=offered)
    return raised.value


async def test_a_brief_naming_an_unknown_server_is_refused() -> None:
    parent = await loaded(None)
    for entry in ("nowhere", "nowhere/fetch", "nowhere-mcp"):
        refused = refusal([entry], parent)
        assert refused.kind == ErrorKind.INVALID_REQUEST
        assert "unknown_connector" in refused.message
    refused = refusal(["web/fetch; rm"], parent)
    assert refused.kind == ErrorKind.INVALID_REQUEST
    # Without connectors at all, every server is unknown to the brief.
    with pytest.raises(WorkerAPIError, match="unknown_connector"):
        narrow(["web"], configured=(), offered=None)


async def test_a_brief_cannot_widen_past_its_parents_connectors() -> None:
    parent = await loaded(Narrowing((("portal/e-print-mcp", "web/fetch"),)))
    # Within the parent's: normalized as the delegation records it.
    assert narrow(
        ["e-print-mcp", "portal/e-print-mcp_search_*", "web/fetch", "web/fetch"],
        configured=(PORTAL, WEB),
        offered=parent,
    ) == ("portal/e-print-mcp", "portal/e-print-mcp_search_*", "web/fetch")
    # A tool, a server behind the portal or a pattern the parent lacks is a widening, and
    # so is a server the user turned off, which no run is offered.
    for entry in ("web/crawl", "portal/paper-search-mcp", "portal/paper-*", "off"):
        refused = refusal([entry], parent)
        assert refused.kind == ErrorKind.POLICY_DENIED
        assert "not_permitted_for_role" in refused.message


async def test_a_check_takes_no_narrowing_and_a_brief_names_at_most_32() -> None:
    with pytest.raises(WorkerAPIError, match="not_permitted_for_role"):
        brief_of({"check": {"entry": "C-1", "revision": 1}, "connectors": ["web"]})
    assert isinstance(brief_of({"task": "Read.", "connectors": []}), TaskBrief)
    with pytest.raises(ValueError, match="at most 32"):
        brief_of({"task": "Read.", "connectors": ["web"] * 33})


# --- End to end -----------------------------------------------------------------------------


async def test_a_delegated_task_gets_only_the_connectors_its_brief_names(
    delegating: Harness,
) -> None:
    """DA-F3: the brief's narrowing is recorded, the child's index and loads hold only
    those tools, and a brief naming an unknown server starts nothing."""
    client = FakeClient()
    post(delegating, "m-1", "Read the papers.")
    root_script = [
        Reply(calls=[call("delegate", task="Search ePrint.", connectors=["e-print-mcp"])]),
        Reply(calls=[call("delegate", task="Search elsewhere.", connectors=["nowhere"])]),
        wait(),
        Reply("Done."),
    ]
    delegating.gateway.routed = {
        "Search ePrint.": [
            Reply(calls=[call("mcp_tools", names=["e-print-mcp_search_eprint", "fetch"])]),
            Reply(calls=[call("e-print-mcp_search_eprint", query="SNOVA")]),
            Reply(calls=[call("paper-search-mcp_search_papers", query="SNOVA")]),
            Reply(json.dumps(DONE)),
        ]
    }
    async with serving(delegating, *root_script) as service:
        service.harness.mcp = McpAccess(client, (PORTAL, WEB), PROJECT)
        found = await ended(delegating, 2)
    (root,) = [record for record in found if record.parent is None]
    (child,) = children(delegating, root)
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert found.brief["connectors"] == ["portal/e-print-mcp"]
    assert "inherited_connectors" not in found.brief
    assert "unknown_connector" in results(root)[1]
    loaded_text, searched, refused = results(child)[:3]
    loads = json.loads(loaded_text)
    assert loads["loaded"] == ["e-print-mcp_search_eprint"]
    assert loads["unknown"] == ["fetch"]  # web is not listed for it at all
    assert searched == "e-print-mcp_search_eprint answered"
    assert "not_permitted_for_role" in refused
    connectors = child.summaries["connectors"]
    assert isinstance(connectors, dict)
    assert connectors["narrowing"] == [["portal/e-print-mcp"]]
    assert connectors["servers"] == {PORTAL: 2}
    asked = {
        (server, method)
        for run, server, method in client.asked
        if run == run_name(child.started_at, child.run_id)
    }
    assert asked == {(PORTAL, "tools/list"), (PORTAL, "e-print-mcp_search_eprint")}


async def test_a_narrowed_task_narrows_its_own_children_and_never_widens_them(
    delegating: Harness,
) -> None:
    """DA-2 over two levels: a narrowed task's brief for its own child is refused when it
    names more, and a child it names a whole server for gets only the part its parent
    had, which the grandchild's brief records."""
    configure(delegating, depth=2)
    client = FakeClient()
    post(delegating, "m-1", "Read the papers.")
    lead = call("delegate", task="Lead.", delegate=True, connectors=["e-print-mcp"])
    root_script = [Reply(calls=[lead]), wait(), Reply("Done.")]
    delegating.gateway.routed = {
        "Lead.": [
            Reply(calls=[call("delegate", task="Fetch the web.", connectors=["web"])]),
            Reply(calls=[call("delegate", task="Part.", connectors=["portal"])]),
            wait(),
            Reply(json.dumps(DONE)),
        ],
        "Part.": [
            Reply(calls=[call("mcp_tools", names=["paper-search-mcp_search_papers"])]),
            Reply(json.dumps(DONE)),
        ],
    }
    async with serving(delegating, *root_script) as service:
        service.harness.mcp = McpAccess(client, (PORTAL, WEB), PROJECT)
        found = await ended(delegating, 3)
    (root,) = [record for record in found if record.parent is None]
    (child,) = children(delegating, root)
    (grandchild,) = children(delegating, child)
    widened = json.loads(results(child)[0])
    assert widened["error"]["kind"] == "policy_denied"
    assert "not_permitted_for_role" in widened["error"]["message"]
    (part,) = started(child)
    brief = delegation(delegating, part).brief
    assert brief["connectors"] == ["portal"]
    assert brief["inherited_connectors"] == [["portal/e-print-mcp"]]
    loads = json.loads(results(grandchild)[0])
    assert loads["not_permitted"] == ["paper-search-mcp_search_papers"]
    connectors = grandchild.summaries["connectors"]
    assert isinstance(connectors, dict)
    assert connectors["narrowing"] == [["portal/e-print-mcp"], ["portal"]]
    assert connectors["listed"] == 2
