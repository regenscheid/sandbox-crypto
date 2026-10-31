# SPDX-License-Identifier: MIT
"""Classes on direct listings the host trusts (delegated agents spec DA-7, D96, D99).

The agent runtime's client passes the server's own listing. The host digests each listed
tool as the pins do and asks the real worker (``POST /v1/mcp-tool-classes``, with the run
service's own, non-operator token) for the class the project's classification in force
gives that name with that pin: here the checked-in crypto-research-mcp file under the
example policy, then a project revision accepted during the run. The listings are the
pinned fixtures, so their digests are the pins.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.event_store import EventStore
from sandbox_science.orchestrator.confinement.listings import WorkerToolClasses
from sandbox_science.orchestrator.connectors import Connectors, TrustedClass
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.delegation.narrowing import Narrowing
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.policy import LoadedPolicy
from sandbox_science.policy.mcp_wire import tool_digest
from sandbox_science.policy.store import Acceptance, PolicyStore
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.classifications import ClassificationDesk
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permissions import Permissions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "policy" / "tests" / "fixtures" / "mcp-tools" / "crypto-research-mcp"
SERVER = "crypto-research-mcp"
POOL = "test-secret-classes-pool-token-0000"
HARNESS = "test-secret-classes-harness-token-0000"
READ = "firecrawl-mcp_firecrawl_map"
"""A read with no argument rule: a read for the fence."""
SCRAPE = "firecrawl-mcp_firecrawl_scrape"
"""A read whose arguments can make a call browsing or a write: not a read for the fence."""
WRITE = "firecrawl-mcp_firecrawl_feedback"
"""A write, which the example policy asks the user about."""
DRIFTED = "firecrawl-mcp_firecrawl_research_read_paper"
"""A read whose listed description the test changes, so its digest is not its pin."""
NEW = "firecrawl-mcp_firecrawl_brand_new"
"""A tool the classification does not name."""
DENIED = "portal_toggle_servers"
"""A tool the example policy denies by name."""


def listed() -> list[dict[str, Any]]:
    """crypto-research-mcp's pinned firecrawl and portal tools, one drifted, one new."""
    tools: list[dict[str, Any]] = []
    for group in ("firecrawl-mcp", "portal"):
        fixture = json.loads((FIXTURES / f"{group}.json").read_text())
        tools += [dict(tool) for tool in fixture["tools"]]
    for tool in tools:
        if tool["name"] == DRIFTED:
            tool["description"] = f"{tool.get('description', '')} Now it also posts."
    tools.append({"name": NEW, "description": "Something new.", "inputSchema": {"type": "object"}})
    return tools


class Listing:
    """An MCP client whose one server lists ``listed()``; a listing's ``_meta`` would be
    the runtime's word, so every tool carries a class the host must not take."""

    def __init__(self) -> None:
        self.called: list[str] = []

    async def request(
        self,
        server: str,
        method: str,
        params: Mapping[str, object],
        *,
        session: str,
        run: str | None = None,
    ) -> dict[str, Any]:
        if method == "tools/call":
            self.called.append(str(params["name"]))
            return {"content": [{"type": "text", "text": "ok"}]}
        meta = {"sandbox-science/effect": "read", "sandbox-science/escalates": True}
        return {"tools": [{**tool, "_meta": meta} for tool in listed()]}


@dataclass
class World:
    """The real worker over a policy store with the example policy for ``PROJECT``."""

    store: PolicyStore
    worker: WorkerClient

    def classes(self) -> WorkerToolClasses:
        return WorkerToolClasses(self.worker, PROJECT)

    def accept(self, tools: Mapping[str, str]) -> None:
        """Put a project revision in force that classifies ``tools`` (name to class), each
        pinned to its listed digest."""
        digests = {tool["name"]: tool_digest(tool) for tool in listed()}
        lines = [
            "apiVersion: sandbox-science/v0alpha1",
            "kind: ToolClassification",
            f"server: {SERVER}",
            "tools:",
        ]
        for name, effect in tools.items():
            lines += [
                f"  {name}:",
                f"    class: {effect}",
                '    reason: "Reviewed."',
                f"    sha256: {digests[name]}",
            ]
        acceptance = Acceptance("user", datetime(2026, 10, 31, tzinfo=UTC), "test")
        data = ("\n".join(lines) + "\n").encode()
        self.store.accept_classification(PROJECT, SERVER, data, acceptance)


@pytest_asyncio.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    state = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        state, LocalBackend(LocalBackendSettings(), tmp_path), Blobs(tmp_path / "artifacts"),
        pool_id="test", max_artifact_size=1024, state_dir=tmp_path,
    )  # fmt: skip
    store = PolicyStore(tmp_path / "middleware")
    loaded = store.add_policy((ROOT / "examples" / "policy.yaml").read_bytes())
    policies: dict[str, LoadedPolicy] = {PROJECT: loaded}
    events = EventStore(tmp_path / "middleware" / "events.db", timeout=5)
    permissions = Permissions(events.path)
    desk = ClassificationDesk(
        store,
        permissions,
        policies.get,
        state.classification_receipts,
        listings=state.listing_reports,
    )
    environments = cast(
        Environments,
        SimpleNamespace(
            deleted=[], required_deleted=[], required_deleting=[], confirmed_deleted=[],
            reusing=[], records=SimpleNamespace(listed=lambda _projects: []),
        ),
    )  # fmt: skip
    clients = [
        ClientSettings(name="harness", token_sha256=token_digest(HARNESS), projects=(PROJECT,))
    ]
    app = create_app(manager, POOL, environments, clients, classification_desk=desk)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    worker = WorkerClient(str(server.make_url("/")).rstrip("/"), HARNESS)
    try:
        yield World(store, worker)
    finally:
        await worker.close()
        await server.close()
        permissions.close()
        events.close()
        state.close()


async def connectors_for(world: World, **options: Any) -> Connectors:
    connectors = Connectors(Listing(), [SERVER], session="s-1", classes=world.classes(), **options)
    await connectors.load()
    return connectors


def classes_of(connectors: Connectors) -> dict[str, tuple[str | None, bool]]:
    return {name: (tool.effect_class, tool.escalates) for name, tool in connectors.tools.items()}


async def test_only_a_pinned_classified_tool_has_a_class_the_host_takes(world: World) -> None:
    connectors = await connectors_for(world)
    found = classes_of(connectors)
    assert found[READ] == ("read", False)
    # A read an argument can make browsing or a write is no read for the fence.
    assert found[SCRAPE] == ("browse", False)
    assert found[WRITE] == ("write", True)  # the example policy asks first
    # A drifted digest, an unclassified tool and a denied one have no class, whatever the
    # listing's own _meta said ("read", asks first).
    assert found[DRIFTED] == (None, False)
    assert found[NEW] == (None, False)
    assert found[DENIED] == (None, False)
    _, record = await connectors.call(READ, {})
    assert (record["effect_class"], record["reach"]) == ("read", "session")
    _, record = await connectors.call(DRIFTED, {})
    assert "effect_class" not in record


async def test_a_worker_that_does_not_answer_gives_no_class(world: World) -> None:
    await world.worker.close()
    world.worker = WorkerClient("http://127.0.0.1:9", HARNESS)
    connectors = await connectors_for(world)
    assert {cls for cls, _ in classes_of(connectors).values()} == {None}


async def test_the_worker_names_why_a_tool_has_no_class(world: World) -> None:
    from sandbox_science.contracts.classifications import ToolClassQuery, ToolPin

    digests = {tool["name"]: tool_digest(tool) for tool in listed()}
    pins = tuple(
        ToolPin(name=name, digest=digests[name])
        for name in (READ, SCRAPE, WRITE, DRIFTED, NEW, DENIED)
    )
    answer = await world.worker.mcp_tool_classes(
        ToolClassQuery(project_id=PROJECT, server=SERVER, tools=pins)
    )
    assert answer.classification is not None
    assert {tool.name: (tool.state, tool.effect) for tool in answer.tools} == {
        READ: ("classified", "read"),
        SCRAPE: ("classified", "browse"),
        WRITE: ("classified", "write"),
        DRIFTED: ("changed", None),
        NEW: ("unclassified", None),
        DENIED: ("denied", None),
    }


async def test_a_check_is_offered_only_reads_and_calls_only_reads(world: World) -> None:
    connectors = await connectors_for(world, reads_only=True)
    assert set(connectors.tools) >= {READ}
    assert {tool.effect_class for tool in connectors.tools.values()} == {"read"}
    assert not {SCRAPE, WRITE, DRIFTED, NEW, DENIED} & set(connectors.tools)
    # A revision accepted during the run makes the read a write: the check's next call of
    # it is refused, though it was offered.
    world.accept({READ: "write"})
    classed = await connectors.classed(READ)
    assert classed is not None
    assert classed.effect_class == "write"
    text, record = await connectors.call(READ, {})
    assert record["error"] == "not_permitted_for_role"
    assert "not_permitted_for_role" in text
    client = cast(Listing, connectors.client)
    assert client.called == []


async def test_a_narrowed_task_keeps_its_classes_within_its_narrowing(world: World) -> None:
    narrowing = Narrowing(((f"{SERVER}/{READ}", f"{SERVER}/firecrawl-mcp_firecrawl_feed*"),))
    connectors = await connectors_for(world, narrowing=narrowing)
    assert classes_of(connectors) == {READ: ("read", False), WRITE: ("write", True)}


@pytest.fixture
def session(tmp_path: Path) -> Iterator[tuple[SessionPlans, ProjectStore, ExecutionContext]]:
    projects = ProjectStore(tmp_path / "control")
    documents = DocumentStore(store_path(tmp_path / "control"))
    try:
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        opened = projects.open_session(PROJECT, SESSION, dedicated=False)
        RunState(projects.control).acquire(PROJECT, SESSION, "root", "holder")
        plans = SessionPlans(documents)
        plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")  # a draft
        context = ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=opened.environment_id
        )
        yield plans, projects, context
    finally:
        documents.close()
        projects.close()


async def test_a_childs_fence_passes_only_reads_and_held_calls_by_the_hosts_classes(
    world: World, session: tuple[SessionPlans, ProjectStore, ExecutionContext]
) -> None:
    """Under a draft plan a child's read passes, a call the policy holds for the user goes
    out, and a tool with no class (drifted, unclassified) or one that is no read and goes
    unasked is refused; a revision accepted during the run applies from the next call."""
    connectors = await connectors_for(world)
    plans, projects, context = session
    fence = await asyncio.to_thread(
        ChildFence, plans, projects, context, run_id="child", connectors=connectors.classed
    )

    async def gate(tool: str) -> str | None:
        refused = await fence.gate_call(tool, "effect")
        return None if refused is None else refused.message

    assert await gate(READ) is None
    assert await gate(WRITE) is None  # held for the user, who decides it from its card
    for tool in (SCRAPE, DRIFTED, NEW):
        refused = await gate(tool)
        assert refused is not None, tool
        assert "draft" in refused, tool
    # The project accepts a revision that makes the read browsing (denied: browser
    # automation is off) and pins the drifted tool's new digest as a read.
    world.accept({READ: "browse", DRIFTED: "read", WRITE: "write"})
    refused = await gate(READ)
    assert refused is not None
    assert "draft" in refused
    assert await gate(DRIFTED) is None
    assert connectors.tools[DRIFTED].effect_class == "read"


async def test_the_source_takes_only_the_tools_asked_about() -> None:
    """A class the source answers for a tool not asked about, or outside the closed set, is
    not taken."""

    class Lying:
        async def classes(
            self, server: str, tools: Mapping[str, str]
        ) -> Mapping[str, TrustedClass]:
            return {
                **{name: TrustedClass("nonsense") for name in tools},
                "elsewhere": TrustedClass("read"),
            }

    connectors = Connectors(Listing(), [SERVER], session="s-1", classes=Lying())
    await connectors.load()
    assert {tool.effect_class for tool in connectors.tools.values()} == {None}
    assert "elsewhere" not in connectors.tools
