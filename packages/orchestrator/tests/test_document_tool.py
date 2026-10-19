# SPDX-License-Identifier: MIT
"""The document tool in the harness's tool loop, with a scripted model (``scripted``).

The user acts through the store, between the scripted model's turns, as the workspace
will; the document tools reach the control store and never the worker.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, DocumentTool
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import (
    Message,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

pytestmark = pytest.mark.asyncio

TOKEN = "k" * 40
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
FF = """---
title: Fermat
macros:
  \\F: \\mathbb{F}
---
# Fermat's little theorem

Let $p = 17$. For every $a \\in \\F_p^\\times$, the identity below holds.

$$a^{p-1} = 1$$ (fermat)

::: lemma
For every prime $p$ and $a$ not divisible by $p$, $a^{p-1} \\equiv 1 \\pmod p$.
:::

| $a$ | $a^{16}$ |
| --- | --- |
| 2 | 1 |

In summary, the identity holds for every nonzero residue.
"""


class Hooked(ScriptedModel):
    """A scripted model that lets the user act before chosen turns."""

    def __init__(self, turns: Sequence[ModelTurn], hooks: dict[int, Callable[[], object]]) -> None:
        super().__init__(turns)
        self.hooks = hooks
        self.asked = 0

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        hook = self.hooks.get(self.asked)
        self.asked += 1
        if hook is not None:
            hook()
        return await super().complete(messages, tools)


@pytest.fixture
def store(tmp_path: Path) -> DocumentStore:
    found = DocumentStore(tmp_path / "control.sqlite3")
    found.create("p1", name="ff", title="Fermat", text=FF, author=USER)
    return found


@pytest_asyncio.fixture
async def client() -> AsyncIterator[WorkerClient]:
    async with WorkerClient("http://127.0.0.1:9", TOKEN) as found:
        yield found


def _ids(store: DocumentStore) -> dict[str, str]:
    mapped = store.mapped("p1", "ff")
    names: dict[str, str] = {}
    for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True):
        if block.kind == "equation":
            names["E"] = block_id
            names["E_math"] = mapped.span_ids[block.spans[0]]
        elif block.kind == "paragraph" and block.text.startswith("In summary"):
            names["S"] = block_id
        elif block.kind == "paragraph" and block.text.startswith("Let"):
            names["P"] = block_id
    return names


def _results(model: ScriptedModel) -> list[dict[str, Any]]:
    """The tool results the model read, in order, from its last request."""
    return [
        json.loads(message["content"])
        for message in model.requests[-1]
        if message["role"] == "tool"
    ]


def _toolbox(client: WorkerClient, store: DocumentStore, run_id: str, **kwargs: Any) -> Toolbox:
    tool = DocumentTool(
        store, kwargs.pop("context", CONTEXT), run_id=run_id, alias="scripted", **kwargs
    )
    return Toolbox(client, tool.context, documents=tool)


async def test_ld_f4_the_run_is_told_and_its_stale_patch_conflicts(
    client: WorkerClient, store: DocumentStore
) -> None:
    ids = _ids(store)

    def user_saves_r2() -> None:
        store.save_text(
            "p1",
            "ff",
            base=1,
            text=FF.replace("a^{p-1} = 1$$", "a^{16} = 1$$"),
            author=USER,
            reason="for p = 17",
        )

    patch = {"op": "replace_math", "math": ids["E_math"], "tex": "a^{p} = a"}
    script = [
        {"tool_calls": [{"name": "document_read", "arguments": {"document": "ff"}}]},
        {
            "tool_calls": [
                {
                    "name": "document_patch",
                    "arguments": {
                        "document": "ff",
                        "base_revision": 1,
                        "operation_id": "e-1",
                        "reason": "state it generally",
                        "operations": [patch],
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "document_patch",
                    "arguments": {
                        "document": "ff",
                        "base_revision": 2,
                        "operation_id": "e-2",
                        "reason": "state it generally",
                        "operations": [patch],
                    },
                }
            ]
        },
        {"text": "Done."},
    ]
    model = Hooked(ScriptedModel.from_json(script)._turns, {1: user_saves_r2})
    transcript = await run_task(model, _toolbox(client, store, "run-a"), "patch E", run_id="run-a")
    assert transcript.final_text == "Done."
    read, conflict, applied = _results(model)
    assert read["revision"] == 1
    # The conflict names r2 and shows E at r1 and at r2, with the user as its author.
    error = conflict["error"]
    assert error["kind"] == "conflict"
    assert error["current_revision"] == 2
    (found,) = error["conflicts"]
    assert (found["at_base"], found["now"]) == ("a^{p-1} = 1", "a^{16} = 1")
    assert found["changed_by"][0]["author"]["kind"] == "user"
    # Told at the next safe point: after the call during which the user saved.
    (notice,) = conflict["platform_notices"]
    assert "revision 2 by the user" in notice
    assert f"{ids['E']} changed" in notice
    assert "platform_notices" not in applied
    assert applied["outcome"] == "applied"
    assert applied["revision"] == 3
    rejected = store.patches("p1", "ff", states=("conflicted",))
    assert [patch.patch_id for patch in rejected] == [error["rejected_patch"]]
    history = store.history("p1", "ff")["revisions"]
    assert [(entry["number"], entry["author"]["kind"]) for entry in history] == [
        (1, "user"),
        (2, "user"),
        (3, "agent"),
    ]
    assert transcript.tool_records[1]["document_notices"] == [notice]


async def test_notices_name_other_runs_once_and_never_the_runs_own(
    client: WorkerClient, store: DocumentStore
) -> None:
    ids = _ids(store)
    other = DocumentTool(
        store,
        ExecutionContext(project_id="p1", session_id="s2", environment_id="env2"),
        run_id="run-b",
    )

    async def other_run_patches() -> None:
        text, _ = await other.call(
            "document_patch",
            {
                "document": "ff",
                "base_revision": 2,
                "reason": "summary",
                "operations": [{"op": "replace", "block": ids["S"], "markdown": "Short."}],
            },
            operation="b-1",
        )
        assert json.loads(text)["revision"] == 3

    toolbox = _toolbox(client, store, "run-a")
    own = {
        "document": "ff",
        "base_revision": 1,
        "operation_id": "a-1",
        "reason": "p",
        "operations": [
            {"op": "replace_text", "block": ids["P"], "quote": "holds", "with": "always holds"}
        ],
    }

    first = await toolbox.call(ToolCall("c1", "document_patch", json.dumps(own)), "op-1")
    assert "platform_notices" not in json.loads(first.text)
    await other_run_patches()
    listed = await toolbox.call(ToolCall("c2", "document_list", "{}"), "op-2")
    (notice,) = json.loads(listed.text)["platform_notices"]
    assert "revision 3 by agent run run-b of session s2" in notice
    assert f"{ids['S']} changed" in notice
    again = await toolbox.call(ToolCall("c3", "document_list", "{}"), "op-3")
    assert "platform_notices" not in json.loads(again.text)


async def test_a_rebased_patch_still_tells_of_the_revisions_it_was_rebased_over(
    client: WorkerClient, store: DocumentStore
) -> None:

    ids = _ids(store)
    other = DocumentTool(
        store,
        ExecutionContext(project_id="p1", session_id="s2", environment_id="env2"),
        run_id="run-b",
    )
    toolbox = _toolbox(client, store, "run-a")
    await toolbox.call(ToolCall("c1", "document_read", json.dumps({"document": "ff"})), "op-1")
    await other.call(
        "document_patch",
        {
            "document": "ff",
            "base_revision": 1,
            "reason": "summary",
            "operations": [{"op": "replace", "block": ids["S"], "markdown": "Short."}],
        },
        operation="b-1",
    )
    mine = {
        "document": "ff",
        "base_revision": 1,
        "reason": "p",
        "operations": [{"op": "replace_text", "block": ids["P"], "quote": "holds", "with": "is"}],
    }
    patched = json.loads(
        (await toolbox.call(ToolCall("c2", "document_patch", json.dumps(mine)), "op-2")).text
    )
    assert (patched["revision"], patched["rebased"]) == (3, True)
    (notice,) = patched["platform_notices"]
    assert "revision 2 by agent run run-b" in notice
    assert "revision 3 by" not in notice


async def test_ld_f13_document_revisions_count_as_text_and_structure(
    client: WorkerClient, store: DocumentStore
) -> None:
    ids = _ids(store)

    def paragraph(number: int) -> str:
        sentence = f"Check {number}: the residue r satisfies r^16 = 1 modulo 17, as computed. "
        return (sentence * 3)[:100]

    calls: list[dict[str, Any]] = []
    for number in range(1, 4):
        calls.append(
            {
                "name": "document_patch",
                "arguments": {
                    "document": "ff",
                    "base_revision": number,
                    "operation_id": f"small-{number}",
                    "reason": "a check",
                    "operations": [
                        {"op": "insert", "after": ids["S"], "markdown": paragraph(number)}
                    ],
                },
            }
        )
    body = ("The group $\\F_{17}^\\times$ is cyclic of order 16. " * 60)[:2100]
    lemma = f"::: lemma\n{body}\n:::"
    calls.append(
        {
            "name": "document_patch",
            "arguments": {
                "document": "ff",
                "base_revision": 4,
                "operation_id": "lemma",
                "reason": "the lemma",
                "operations": [{"op": "insert", "after": ids["S"], "markdown": lemma}],
            },
        }
    )
    model = ScriptedModel.from_json([{"tool_calls": [call]} for call in calls] + [{"text": "ok"}])
    await run_task(model, _toolbox(client, store, "run-c"), "write", run_id="run-c")
    events = [event for event in store.events("p1") if event.kind == "document.revision_created"]
    small, lemma_event = events[1:4], events[4]
    assert [event.payload["agent_characters"] for event in small] == [100, 100, 100]
    assert all(event.payload["structural_added"] == [] for event in small)
    assert lemma_event.payload["agent_characters"] == 2100
    assert [entry["kind"] for entry in lemma_event.payload["structural_added"]] == ["container"]  # pyright: ignore
    # The user's table counts as structure and text in its event, but no agent wrote it.
    table = "| n | n^16 |\n| - | - |\n" + "".join(f"| {n} | 1 |\n" for n in range(2, 300))
    head = store.document("p1", "ff").head
    current = store.revision("p1", "ff").text
    store.save_text(
        "p1", "ff", base=head, text=current + "\n" + table[:3000], author=USER, reason="table"
    )
    user_event = store.events("p1")[-1]
    assert user_event.payload["author_kind"] == "user"
    assert user_event.payload["agent_characters"] == 0
    assert [entry["kind"] for entry in user_event.payload["structural_added"]] == ["table"]  # pyright: ignore


async def test_revisions_are_attributed_to_the_run(
    client: WorkerClient, store: DocumentStore
) -> None:
    ids = _ids(store)
    script = [
        {
            "tool_calls": [
                {
                    "name": "document_patch",
                    "arguments": {
                        "document": "ff",
                        "base_revision": 1,
                        "reason": "shorter",
                        "operations": [{"op": "replace", "block": ids["S"], "markdown": "Short."}],
                        "author": {"kind": "user", "user": "user"},
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "document_patch",
                    "arguments": {
                        "document": "ff",
                        "base_revision": 1,
                        "reason": "shorter",
                        "operations": [{"op": "replace", "block": ids["S"], "markdown": "Short."}],
                    },
                }
            ]
        },
        {"text": "ok"},
    ]
    model = ScriptedModel.from_json(script)
    await run_task(model, _toolbox(client, store, "run-d"), "x", run_id="run-d")
    refused, applied = _results(model)
    # An agent cannot name another author (RA-16): the argument is refused outright.
    assert refused["error"]["kind"] == "invalid_request"
    assert "author" in refused["error"]["message"]
    assert applied["revision"] == 2
    author = store.revision("p1", "ff", 2).info.author
    assert author == Author(
        kind="agent",
        session_id="s1",
        run_id="run-d",
        role="main",
        model="scripted",
        alias="scripted",
        tool_call="call-2",
    )
    # Without an operation ID of its own, the call's is used: the same call made again,
    # as a run with a reused ID makes it, returns the first result.
    again = ScriptedModel.from_json(script)
    await run_task(again, _toolbox(client, store, "run-d"), "x", run_id="run-d")
    replayed = _results(again)[1]
    assert (replayed["revision"], replayed["replayed"]) == (2, True)
    assert store.document("p1", "ff").head == 2


async def test_the_read_view(client: WorkerClient, store: DocumentStore) -> None:
    tool = DocumentTool(store, CONTEXT, run_id="r", output_chars=20000)
    text, record = await tool.call("document_read", {"document": "ff"}, operation="o")
    view = json.loads(text)
    assert view["revision"] == 1
    assert view["macros"] == {"\\F": "\\mathbb{F}"}
    kinds = [block["kind"] for block in view["blocks"]]
    assert kinds == [
        "front_matter",
        "heading",
        "paragraph",
        "equation",
        "container",
        "paragraph",
        "table",
        "paragraph",
    ]
    container = view["blocks"][4]
    assert container["text"] == "::: lemma"
    assert container["container"] == "lemma"
    assert view["blocks"][5]["in"] == container["id"]
    equation = view["blocks"][3]
    assert equation["label"] == "fermat"
    assert equation["math"][0] == {
        "id": equation["math"][0]["id"],
        "tex": "a^{p-1} = 1",
        "display": True,
    }
    paragraph = view["blocks"][2]
    assert [math["tex"] for math in paragraph["math"]] == ["p = 17", "a \\in \\F_p^\\times"]
    shown = [block["id"] for block in view["blocks"]]
    assert record == {"read": {"document": view["document"]["id"], "revision": 1, "blocks": shown}}
    small = DocumentTool(store, CONTEXT, run_id="r", output_chars=900)
    text, partly = await small.call("document_read", {"document": "ff"}, operation="o")
    part = json.loads(text)
    # The record names the blocks the answer showed, as a check's reads count them.
    assert partly["read"]["blocks"] == [block["id"] for block in part["blocks"]]
    assert len(text) <= 900 or len(part["blocks"]) == 1
    rest = part["more"]
    text, _ = await small.call(
        "document_read", {"document": "ff", "from_block": rest["from_block"]}, operation="o"
    )
    assert json.loads(text)["blocks"][0]["id"] == rest["from_block"]


async def test_the_outline_is_revision_pinned_and_can_continue(store: DocumentStore) -> None:
    store.create(
        "p1",
        name="report",
        title="Report",
        text="# Result\n\n## Evidence\n\nDetails.\n\n### Computation\n\n# Limits\n",
        author=USER,
    )
    tool = DocumentTool(store, CONTEXT, run_id="r", output_chars=160)
    text, record = await tool.call("show_outline", {"document": "report"}, operation="read-1")
    first = json.loads(text)
    assert first["revision"] == 1
    assert [heading["title"] for heading in first["headings"]] == ["Result"]
    assert record["read"]["blocks"] == [first["headings"][0]["id"]]

    text, _ = await tool.call(
        "show_outline",
        {"document": "report", "from_heading": first["more"]["from_heading"]},
        operation="read-2",
    )
    rest = json.loads(text)
    assert rest["headings"][0]["title"] == "Evidence"
    assert rest["headings"][0]["parent"] == first["headings"][0]["id"]

    invalid, _ = await tool.call(
        "show_outline", {"document": "report", "from_heading": "missing"}, operation="bad"
    )
    assert json.loads(invalid)["error"]["kind"] == "invalid_request"

    store.save_text(
        "p1",
        "report",
        base=1,
        text="# Result\n\n## Evidence\n\nDetails.\n\n### Computation\n\n# Limits\n\n# Next\n",
        author=USER,
        reason="add next section",
    )
    full = DocumentTool(store, CONTEXT, run_id="r", output_chars=20000)
    historic, _ = await full.call(
        "show_outline", {"document": "report", "revision": 1}, operation="read-old"
    )
    assert json.loads(historic)["current_revision"] == 2
    assert len(json.loads(historic)["headings"]) == 4
    latest, _ = await full.call("show_outline", {"document": "report"}, operation="read-new")
    assert json.loads(latest)["revision"] == 2
    assert [heading["title"] for heading in json.loads(latest)["headings"]][-1] == "Next"


async def test_documents_are_created_listed_and_deleted_by_the_session(
    client: WorkerClient, store: DocumentStore
) -> None:
    tool = DocumentTool(store, CONTEXT, run_id="r")
    text, _ = await tool.call(
        "document_create",
        {"name": "scratch", "title": "Scratch", "text": "Note \\(x\\).", "scope": "session"},
        operation="create-1",
    )
    created = json.loads(text)
    assert created["outcome"] == "created"
    assert created["normalized"]["converted"] == {"inline": 1}
    listed, _ = await tool.call("document_list", {}, operation="l")
    entries = {entry["name"]: entry for entry in json.loads(listed)["documents"]}
    assert entries["scratch"]["scope"] == "session"
    assert entries["scratch"]["session"] == "s1"
    refused, _ = await tool.call(
        "document_delete", {"document": "ff", "reason": "no"}, operation="d"
    )
    assert json.loads(refused)["error"]["kind"] == "policy_denied"
    deleted, _ = await tool.call(
        "document_delete", {"document": "scratch", "reason": "done"}, operation="d2"
    )
    assert json.loads(deleted)["outcome"] == "deleted"


async def test_the_toolbox_offers_document_tools_only_with_a_store(
    client: WorkerClient, store: DocumentStore
) -> None:
    names = {spec.name for spec in Toolbox(client, CONTEXT).specs}
    assert not any(name.startswith("document_") for name in names)
    with_documents = {spec.name for spec in _toolbox(client, store, "r").specs}
    assert {
        "document_list",
        "document_read",
        "document_create",
        "document_patch",
        "document_delete",
    } <= with_documents


async def test_math_in_a_quoted_equation_reads_and_patches_without_the_markers(
    store: DocumentStore,
) -> None:
    store.create("p1", name="q", title="Q", text="> $$\n> q = 1\n> $$\n", author=USER)
    tool = DocumentTool(store, CONTEXT, run_id="r")
    view = json.loads((await tool.call("document_read", {"document": "q"}, operation="o"))[0])
    (math,) = view["blocks"][1]["math"]
    assert math["tex"] == "\nq = 1\n"
    patch = {
        "document": "q",
        "base_revision": 1,
        "reason": "r",
        "operations": [{"op": "replace_math", "math": math["id"], "tex": "\nq = 2\n"}],
    }
    text, _ = await tool.call("document_patch", patch, operation="p")
    assert json.loads(text)["outcome"] == "applied"
    assert store.revision("p1", "q").text == "> $$\n> q = 2\n> $$\n"


async def test_a_notice_covers_the_revisions_it_read(
    store: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _ids(store)
    tool = DocumentTool(store, CONTEXT, run_id="r")
    await tool.call("document_read", {"document": "ff"}, operation="o")
    for number, block in ((1, ids["S"]), (2, ids["P"])):
        store.patch(
            "p1",
            "ff",
            base=number,
            author=USER,
            reason="u",
            operation_id=f"u{number}",
            operations=[{"op": "replace", "block": block, "markdown": f"User {number}."}],
        )
    # The heads read first lag behind the revisions read after them.
    heads = store.heads
    monkeypatch.setattr(store, "heads", lambda found: dict.fromkeys(heads(found), 2))
    (notice,) = await tool.notices()
    assert "revision 2 by the user" in notice
    assert "revision 3 by the user" in notice
    assert notice.endswith("Read revision 3 before you patch it.")
    monkeypatch.undo()
    assert await tool.notices() == []
