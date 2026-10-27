# SPDX-License-Identifier: MIT
"""Delegated runs' connectors (D96, D99; delegated agents spec DA-7): the child fence reads
each connector tool's class as its client listed it.

The end-to-end tests ran through the MCP proxy, which classified its listings; it retired
(D98, 2026-10-26), and the agent runtime's client passes the server's own listing, whose
classes are not taken, so a child's every connector call is outward and fenced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans

from .loop_harness import PLAN, PROJECT, SESSION, USER_PLANS


class Listed:
    def __init__(self, effect_class: str | None, escalates: bool) -> None:
        self.effect_class, self.escalates = effect_class, escalates


def test_the_fence_reads_each_connector_tools_class(tmp_path: Path) -> None:
    """D99: under a draft plan a connector read passes, a call held for the user goes out,
    and one the policy would pass unasked is refused, as is one with no class, as the
    runtime's client lists every tool (D98); once the session was opened again, none
    passes."""
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
        assert gate("papers_crawl") is None  # held for the user
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
