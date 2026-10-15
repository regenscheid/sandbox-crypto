# SPDX-License-Identifier: MIT
"""The project memory's tools (docs/specs/project-memory.md, PM-3): agents list the
accepted entries and propose; nothing they do changes an entry (PM-F1)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.harness import (
    Finished,
    Harness,
    Role,
    RunRequest,
    Settings,
    Stores,
    admit,
)
from sandbox_science.orchestrator.hooks import Effect, Refusal, TurnHook
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.memory import ProjectMemory
from sandbox_science.orchestrator.memory_tools import MemoryTool
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_run_harness import Sink, Worker

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


@pytest.fixture
def documents(tmp_path: Path) -> Iterator[DocumentStore]:
    store = DocumentStore(store_path(tmp_path))
    yield store
    store.close()


def toolbox(documents: DocumentStore, *, output_chars: int = 20000) -> Toolbox:
    tool = MemoryTool(
        ProjectMemory(documents),
        CONTEXT,
        run_id="r1",
        role="main",
        alias="main",
        output_chars=output_chars,
    )
    return Toolbox(cast(WorkerClient, object()), CONTEXT, memory=tool)


async def call(tools: Toolbox, name: str, gates: tuple[TurnHook, ...] = (), **args: Any) -> Any:
    out = await tools.call(ToolCall("tc-1", name, json.dumps(args)), f"op-{name}", gates=gates)
    return json.loads(out.text)


def accepted(documents: DocumentStore, *texts: str) -> None:
    memory = ProjectMemory(documents)
    for index, text in enumerate(texts):
        change = {"operation_id": f"u{index}", "action": "add", "text": text}
        memory.change("p", change, USER)


async def test_agents_propose_and_a_retry_returns_the_first(documents: DocumentStore) -> None:
    """PM-F1: an addition, a change and a removal wait; the retry of the first returns its
    answer; the entries are as they were."""
    accepted(documents, "Write finite fields as GF(p).")
    tools = toolbox(documents)
    calls = [
        ("c1", {"action": "add", "text": "Use provision.", "reason": "Lost on restart."}),
        ("c2", {"action": "change", "entry": "mem-1", "revision": 1, "text": "GF(q)."}),
        ("c3", {"action": "remove", "entry": "mem-1", "revision": 1}),
    ]
    answers = []
    for call_id, args in calls:
        args.setdefault("reason", "Learned.")
        out = await tools.call(ToolCall(call_id, "memory_propose", json.dumps(args)), call_id)
        answers.append(json.loads(out.text))
    assert [a["proposal_id"] for a in answers] == ["prop-1", "prop-2", "prop-3"]
    assert {a["state"] for a in answers} == {"waiting"}
    retry = await tools.call(ToolCall("c1", "memory_propose", json.dumps(calls[0][1])), "c1")
    assert json.loads(retry.text) == {**answers[0], "replayed": True}
    listed = await call(tools, "memory_list")
    assert listed == {
        "version": 1,
        "entries": [{"entry_id": "mem-1", "revision": 1, "text": "Write finite fields as GF(p)."}],
        "next_from": None,
    }
    (proposal, *_) = ProjectMemory(documents).proposals("p")
    assert proposal.proposer.model_dump(exclude_none=True) == {
        "kind": "agent",
        "session_id": "s",
        "run_id": "r1",
        "role": "main",
        "alias": "main",
        "tool_call": "c1",
    }


async def test_proposing_is_an_effect_and_listing_a_read(documents: DocumentStore) -> None:
    class Fence(TurnHook):
        async def gate(self, effect: Effect) -> Refusal | None:
            return Refusal("policy_denied", "no plan") if effect == "effect" else None

    tools = toolbox(documents)
    effects = {name: entry.effect for name, entry in tools.entries.items()}
    assert {n: e for n, e in effects.items() if n.startswith("memory_")} == {
        "memory_list": "read",
        "memory_propose": "effect",
    }
    refused = await call(tools, "memory_propose", (Fence(),), action="add", text="x", reason="y")
    assert "no plan" in json.dumps(refused)
    assert (await call(tools, "memory_list", (Fence(),)))["entries"] == []
    assert ProjectMemory(documents).proposals("p") == ()


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("memory_propose", {"action": "add", "text": "x", "reason": "y", "author": "user"}),
        ("memory_propose", {"action": "edit", "entry": "mem-1", "text": "x", "reason": "y"}),
        ("memory_propose", {"action": "add", "text": "x" * 1001, "reason": "y"}),
        ("memory_propose", {"action": "add", "text": "x"}),
        ("memory_list", {"from": -1}),
        ("memory_list", {"from": "1"}),
        ("memory_list", {"after": 1}),
    ],
)
async def test_bad_arguments_are_invalid_requests(
    documents: DocumentStore, name: str, args: dict[str, Any]
) -> None:
    out = await call(toolbox(documents), name, **args)
    assert out["error"]["kind"] == "invalid_request", out


async def test_a_long_memory_comes_in_parts(documents: DocumentStore) -> None:
    accepted(documents, *(f"{index:02d}" + "x" * 900 for index in range(12)))
    tools = toolbox(documents, output_chars=4000)
    seen: list[str] = []
    start: int | None = 0
    while start is not None:
        out = await tools.call(ToolCall("tc", "memory_list", json.dumps({"from": start})), "op")
        part = json.loads(out.text)
        assert len(json.dumps(part, ensure_ascii=False, separators=(",", ":"))) <= 4000
        assert part["entries"]
        seen += [entry["entry_id"] for entry in part["entries"]]
        start = part["next_from"]
    assert seen == [f"mem-{n}" for n in range(1, 13)]


async def test_main_and_delegated_runs_have_the_tools(tmp_path: Path) -> None:
    """Both roles are offered the tools, and each proposal is its run's (PM-3)."""
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    propose = {"action": "add", "text": "Install with provision.", "reason": "Lost."}
    turns = [
        ModelTurn(None, (ToolCall("t1", "memory_propose", json.dumps(propose)),)),
        ModelTurn("Proposed."),
    ]
    runs = RunState(projects.control)
    worker = cast(WorkerClient, Worker())
    harness = Harness(
        worker,
        Role(ScriptedModel(turns * 2), "scripted"),
        stores=Stores(projects, documents, JobState(tmp_path), runs),
        settings=Settings(min_isolation_tier=None),
    )

    async def run(request: RunRequest) -> Finished:
        # As the run service does: the run holds the session's lease while it runs.
        admitted = await admit(worker, projects, request, Sink(), min_isolation_tier=None)
        name = run_name(admitted.started_at, admitted.run_id)
        runs.acquire("p", "s", name, "test")
        try:
            return await harness.start(admitted, Sink())
        finally:
            runs.release("p", "s", "test")

    try:
        first = await run(RunRequest("p", "s", "Go", run_id="run-1"))
        parent = run_name(first.started_at, "run-1")
        second = await run(
            RunRequest("p", "s", "Look.", run_id="run-2", parent=parent, delegation="dlg-1")
        )
        proposals = ProjectMemory(documents).proposals("p")
    finally:
        documents.close()
    for finished in (first, second):
        pins = finished.transcript.prompt_provenance
        assert pins is not None
        assert {"memory_list", "memory_propose"} <= set(pins.tools.names)
    assert [(p.proposer.run_id, p.proposer.role) for p in proposals] == [
        ("run-1", "main"),
        ("run-2", "delegated"),
    ]
