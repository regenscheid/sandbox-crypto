# SPDX-License-Identifier: MIT
"""The ledger tools (docs/specs/ledger.md): reads and changes as the run's own, through
the tool table with their effect classes, and results files that code wrote, which the
agent imports all or none (LG-41)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext, FileContent, ReadFileRequest
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.hooks import Effect, Refusal, TurnHook
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.tool import LedgerTool
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
TASK = {"type": "task", "title": "Run the check", "statement": "Run it over GF(17)."}


class Files:
    """A workspace that holds files the tool reads."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read(self, request: ReadFileRequest) -> FileContent:
        data = self.files[request.path]
        part = data[request.offset : request.offset + request.length]
        return FileContent(
            path=request.path,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            offset=request.offset,
            content_b64=base64.b64encode(part).decode(),
            eof=request.offset + len(part) >= len(data),
        )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents)
    documents.close()


def tool(ledger: Ledger, files: Files | None = None, role: str = "main") -> LedgerTool:
    return LedgerTool(
        ledger,
        CONTEXT,
        run_id="r1",
        role=role,
        alias="main",
        read=files.read if files is not None else None,
    )


async def use(tool: LedgerTool, name: str, operation: str = "call-1", **args: Any) -> Any:
    text, _ = await tool.call(name, args, operation=operation, call_id="tc", model="m")
    return json.loads(text)


@pytest.mark.asyncio
async def test_changes_are_the_runs_own_and_reads_show_them(ledger: Ledger) -> None:
    ledgers = tool(ledger)
    made = await use(ledgers, "ledger_change", reason="Planned.", changes=[TASK])
    assert (made["operation_id"], made["entries"][0]["handle"]) == ("call-1", "T-1")
    again = await use(ledgers, "ledger_change", reason="Planned.", changes=[TASK])
    assert again["replayed"]
    read = await use(ledgers, "ledger_read", entry="T-1")
    assert read["author"] == {
        "kind": "agent",
        "session_id": "s",
        "run_id": "r1",
        "role": "main",
        "model": "m",
        "alias": "main",
        "tool_call": "tc",
    }
    await use(
        ledgers,
        "ledger_change",
        operation="call-2",
        reason="Started.",
        changes=[{"entry": "T-1", "expected": 1, "status": "in_progress"}],
    )
    listed = await use(ledgers, "ledger_list", status="in_progress")
    assert [e["handle"] for e in listed["entries"]] == ["T-1"]
    history = await use(ledgers, "ledger_history", entry="T-1")
    assert [r["status"] for r in history["revisions"]] == ["in_progress", "todo"]
    for bad in ({"author": {"kind": "user", "user": "user"}}, {"changes": [TASK]}):
        refused = await use(ledgers, "ledger_change", operation="call-3", **bad)
        assert refused["error"]["kind"] == "invalid_request"


@pytest.mark.asyncio
async def test_a_delegated_run_creates_but_changes_nothing(ledger: Ledger) -> None:
    await use(tool(ledger), "ledger_change", reason="Planned.", changes=[TASK])
    delegated = tool(ledger, role="delegated")
    made = await use(delegated, "ledger_change", operation="d1", reason="Found.", changes=[TASK])
    assert made["entries"][0]["handle"] == "T-2"
    refused = await use(
        delegated,
        "ledger_change",
        operation="d2",
        reason="Done.",
        changes=[{"entry": "T-1", "expected": 1, "status": "done"}],
    )
    assert refused["error"]["message"].startswith("not_permitted_for_role")


class Fence(TurnHook):
    """A gate that refuses every effect, as the plan fence does before a plan."""

    async def gate(self, effect: Effect) -> Refusal | None:
        return Refusal("policy_denied", "no plan") if effect == "effect" else None


@pytest.mark.asyncio
async def test_the_tool_table_offers_reads_as_reads_and_gates_changes(ledger: Ledger) -> None:
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, ledger=tool(ledger))
    effects = {name: entry.effect for name, entry in toolbox.entries.items()}
    assert {name: effects[name] for name in effects if name.startswith("ledger_")} == {
        "ledger_list": "read",
        "ledger_read": "read",
        "ledger_history": "read",
        "ledger_change": "effect",
        "ledger_import": "effect",
    }
    change = json.dumps({"reason": "Planned.", "changes": [TASK]})
    refused = await toolbox.call(ToolCall("c1", "ledger_change", change), "op", gates=(Fence(),))
    assert "no plan" in refused.text
    listed = await toolbox.call(ToolCall("c2", "ledger_list", "{}"), "op2", gates=(Fence(),))
    assert json.loads(listed.text)["entries"] == []
    done = await toolbox.call(ToolCall("c3", "ledger_change", change), "op3")
    assert json.loads(done.text)["entries"][0]["handle"] == "T-1"
    assert done.record["result"]["entries"][0]["handle"] == "T-1"


def lines(*mutations: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(m) for m in mutations) + "\n").encode()


def mutation(op: str, *changes: dict[str, Any]) -> dict[str, Any]:
    return {"operation_id": op, "reason": "From the helper.", "changes": list(changes)}


@pytest.mark.asyncio
async def test_a_results_file_is_imported_all_or_none_with_its_provenance(ledger: Ledger) -> None:
    files = Files()
    ledgers = tool(ledger, files)
    files.files["out/results.jsonl"] = lines(mutation("helper-1", TASK), mutation("helper-2", TASK))
    imported = await use(ledgers, "ledger_import", path="out/results.jsonl")
    assert [(i["line"], i["entries"][0]["handle"]) for i in imported["imported"]] == [
        (1, "T-1"),
        (2, "T-2"),
    ]
    (revision,) = ledger.history("p", "T-2").revisions
    assert revision.provenance == {
        "file": "out/results.jsonl",
        "sha256": imported["sha256"],
        "line": 2,
    }
    assert revision.author["tool_call"] == "tc"
    again = await use(ledgers, "ledger_import", path="out/results.jsonl")
    assert all(i["replayed"] for i in again["imported"])
    # A line the ledger refuses refuses the file: the line before it is not recorded.
    files.files["bad.jsonl"] = lines(
        mutation("helper-3", TASK),
        mutation("helper-4", {"entry": "T-1", "expected": 9, "status": "done"}),
    )
    refused = await use(ledgers, "ledger_import", path="bad.jsonl")
    assert refused["error"]["message"].startswith("line 2: T-1 is at revision 1")
    assert refused["error"]["line"] == 2
    assert [e.handle for e in ledger.entries("p").entries] == ["T-1", "T-2"]


@pytest.mark.asyncio
async def test_a_results_file_that_is_not_one_is_refused_before_anything(ledger: Ledger) -> None:
    files = Files()
    ledgers = tool(ledger, files)
    files.files["shape.jsonl"] = lines(mutation("ok", TASK)) + b'{"reason": "no changes"}\n'
    files.files["text.jsonl"] = b"\xff\xfe"
    files.files["empty.jsonl"] = b"\n\n"
    files.files["long.jsonl"] = lines(*(mutation(f"m{n}", TASK) for n in range(257)))
    for path, message in (
        ("shape.jsonl", "line 2: operation_id"),
        ("text.jsonl", "UTF-8"),
        ("empty.jsonl", "no changes"),
        ("long.jsonl", "at most 256 lines"),
        ("../outside", "no file in the workspace"),
    ):
        refused = await use(ledgers, "ledger_import", path=path)
        assert message in refused["error"]["message"], path
    assert ledger.entries("p").entries == []
    without = await use(tool(ledger), "ledger_import", path="shape.jsonl")
    assert "no workspace" in without["error"]["message"]
