# SPDX-License-Identifier: MIT
"""References to recorded files in living documents, pinned when the text is saved
(docs/specs/figures.md, FG-6 to FG-11; LD-11 as amended)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import USER, Author, DocumentError, DocumentStore
from sandbox_science.orchestrator.documents.pins import text_references
from sandbox_science.orchestrator.documents.tool import _CITING, SPECS, DocumentTool
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_artifact_inventory import CONTEXT, SOURCE, record

AGENT = Author(kind="agent", session_id="s1", run_id="run1", role="main", tool_call="c1")
TEXT = "# Timing\n\nThe plot:\n\n![Timing](plots/t.svg)\n\nIn summary, it grows.\n"


@pytest.fixture
def inventory(tmp_path: Path) -> ArtifactInventory:
    return ArtifactInventory(DocumentStore(tmp_path / "control.sqlite3"))


def _store(inventory: ArtifactInventory) -> DocumentStore:
    return inventory.documents


def _notes(store: DocumentStore, text: str = TEXT, author: Author = AGENT) -> str:
    session = author.session_id
    created = store.create("p1", name="notes", title="Notes", text=text, author=author,
                           session_id=session)  # fmt: skip
    return created.document_id


def _block(store: DocumentStore, document: str, starts: str) -> str:
    mapped = store.mapped("p1", document)
    for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True):
        if block.text.startswith(starts):
            return block_id
    raise AssertionError(starts)


def _pins(store: DocumentStore, document: str, number: int | None = None) -> list[dict[str, Any]]:
    return [dict(entry) for entry in store.pins("p1", document, number)]


def test_a_reference_is_pinned_when_saved_and_keeps_its_pin(inventory: ArtifactInventory) -> None:
    first = record(inventory, "op1", b"<svg>1</svg>", path="plots/t.svg").revision
    assert first is not None
    store = _store(inventory)
    document = _notes(store)
    (pin,) = _pins(store, document)
    figure = _block(store, document, "![Timing]")
    assert pin == {
        "block": figure, "kind": "image", "path": "plots/t.svg",
        "artifact_path": "sessions/s1/plots/t.svg", "session": "s1", "artifact": first.artifact_id,
        "revision": 1, "sha256": hashlib.sha256(b"<svg>1</svg>").hexdigest(),
        "size": 12, "media_type": "application/octet-stream",
    }  # fmt: skip
    record(inventory, "op2", b"<svg>2</svg>", path="plots/t.svg")
    assert _pins(store, document)[0]["latest"] == 2  # a newer revision is shown
    summary = _block(store, document, "In summary")
    changed = store.patch("p1", document, base=1, author=AGENT, operation_id="p1",
                          reason="say more", operations=[
                              {"op": "replace_text", "block": summary, "quote": "grows",
                               "with": "grows quadratically"}])  # fmt: skip
    assert changed.revision == 2
    assert [(entry["revision"], entry["latest"]) for entry in _pins(store, document)] == [(1, 2)]


def test_moving_a_pin_makes_a_revision_with_the_same_text(inventory: ArtifactInventory) -> None:
    record(inventory, "op1", b"one", path="plots/t.svg")
    record(inventory, "op2", b"two", path="plots/t.svg")
    store = _store(inventory)
    record(inventory, "op0", b"zero", path="plots/u.svg")
    document = _notes(store, TEXT.replace("plots/t.svg", "plots/t.svg) ![U](plots/u.svg"))
    figure = _block(store, document, "![Timing]")
    assert [entry["revision"] for entry in _pins(store, document)] == [2, 1]
    moved = store.patch("p1", document, base=1, author=AGENT, operation_id="back",
                        reason="the first plot", operations=[
                            {"op": "repin", "block": figure, "path": "plots/t.svg",
                             "revision": 1}])  # fmt: skip
    assert (moved.outcome, moved.revision, moved.changes["changed"]) == ("applied", 2, [figure])
    revision = store.revision("p1", document)
    assert revision.text == store.revision("p1", document, 1).text
    assert [entry["revision"] for entry in revision.pins] == [1, 1]
    again = store.patch("p1", document, base=2, author=AGENT, operation_id="same",
                        reason="no change", operations=[
                            {"op": "repin", "block": figure, "path": "plots/t.svg",
                             "revision": 1}])  # fmt: skip
    assert (again.outcome, again.revision) == ("unchanged", 2)
    latest = store.patch("p1", document, base=2, author=AGENT, operation_id="latest",
                         reason="the new plot", operations=[
                             {"op": "repin", "block": figure, "path": "plots/t.svg"}])  # fmt: skip
    assert [entry["revision"] for entry in store.revision("p1", document).pins] == [2, 1]
    assert latest.revision == 3
    for operation, message in (
        ({"op": "repin", "block": figure, "path": "plots/t.svg", "revision": 9}, "no revision"),
        ({"op": "repin", "block": figure, "path": "plots/x.svg"}, "holds no reference"),
    ):
        with pytest.raises(DocumentError, match=message):
            store.patch("p1", document, base=3, author=AGENT, operation_id="bad",
                        reason="bad", operations=[operation])  # fmt: skip
    # A revert takes the pins of the revision it restores (FG-11).
    store.revert("p1", document, base=3, target=2, author=USER, reason="back",
                 operation_id="r")  # fmt: skip
    assert [entry["revision"] for entry in store.revision("p1", document).pins] == [1, 1]


def _where(store: DocumentStore, document: str) -> list[tuple[Any, ...]]:
    return [
        (pin["path"], pin.get("artifact_path"), pin.get("revision", pin.get("unresolved")))
        for pin in store.revision("p1", document).pins
    ]


def test_a_pin_stays_with_the_session_that_resolved_it(inventory: ArtifactInventory) -> None:
    """Review of #488: whoever saves next, the same reference in the same place keeps the
    pin it was given, in the session that resolved it; only a new reference is resolved
    in the saver's session (FG-7, FG-11)."""
    s2 = SOURCE.model_copy(update={"context": CONTEXT.model_copy(update={"session_id": "s2"})})
    record(inventory, "op1", b"<svg>s1</svg>", path="plots/t.svg")
    record(inventory, "op2", b"<svg>s2</svg>", source=s2, path="plots/t.svg")
    store = _store(inventory)
    other = AGENT.model_copy(update={"session_id": "s2", "run_id": "run2"})
    document = store.create("p1", name="notes", title="Notes", text=TEXT, author=AGENT).document_id
    first = [("plots/t.svg", "sessions/s1/plots/t.svg", 1)]
    assert _where(store, document) == first
    figure, summary = _block(store, document, "![Timing]"), _block(store, document, "In summary")

    def patch(author: Author, base: int, *operations: dict[str, Any]) -> int | None:
        result = store.patch("p1", document, base=base, author=author, operation_id=f"o{base}",
                             reason="edit", operations=list(operations))  # fmt: skip
        return result.revision

    edit = {"op": "replace_text", "block": summary, "quote": "grows", "with": "grows fast"}
    assert patch(other, 1, edit) == 2  # another session edits another block
    assert patch(USER, 2, {**edit, "quote": "fast", "with": "quickly"}) == 3  # the user does
    caption = {"op": "replace_text", "block": figure, "quote": "Timing", "with": "Time"}
    assert patch(other, 3, caption) == 4  # the caption changes, the path does not
    assert _where(store, document) == first
    added = {"op": "insert", "at": "end", "markdown": "![Again](plots/t.svg)"}
    assert patch(other, 4, added) == 5  # a new reference: the saver's session
    assert patch(USER, 5, {**added, "markdown": "![Mine](plots/t.svg)"}) == 6
    assert _where(store, document) == [
        *first,
        ("plots/t.svg", "sessions/s2/plots/t.svg", 1),
        ("plots/t.svg", None, "needs_session"),
    ]
    # An accepted suggestion and a revert keep them too.
    store.set_suggestions("p1", document, on=True, author=USER)
    suggested = store.patch("p1", document, base=6, author=other, operation_id="sg",
                            reason="edit", operations=[{**edit, "quote": "quickly",
                                                        "with": "fast"}])  # fmt: skip
    assert suggested.patch_id is not None
    accepted = store.decide("p1", document, suggested.patch_id, accept=True, author=USER, base=6)
    assert accepted.revision == 7
    store.revert("p1", document, base=7, target=1, author=USER, reason="back", operation_id="rv")
    assert _where(store, document) == first


def test_a_moved_workbook_document_keeps_its_pins(inventory: ArtifactInventory) -> None:
    record(inventory, "op1", b"one", path="plots/t.svg")
    store = _store(inventory)
    document = _notes(store)
    store.promote("p1", document, base=1, name="notes", author=USER, reason="share",
                  operation_id="pr")  # fmt: skip
    summary = _block(store, document, "In summary")
    store.patch("p1", document, base=1, author=USER, operation_id="u", reason="edit",
                operations=[{"op": "replace_text", "block": summary, "quote": "grows",
                             "with": "grows fast"}])  # fmt: skip
    assert _where(store, document) == [("plots/t.svg", "sessions/s1/plots/t.svg", 1)]


def test_escaped_and_html_images_are_no_references(inventory: ArtifactInventory) -> None:
    record(inventory, "op1", b"one", path="plots/t.svg")
    store = _store(inventory)
    text = (
        "\\![a](plots/t.svg) <!-- ![b](plots/t.svg) --> <span title='![c](plots/t.svg)'>x</span> "
        "\\\\![d](plots/t.svg)\n"
    )
    document = _notes(store, text)
    assert _where(store, document) == [("plots/t.svg", "sessions/s1/plots/t.svg", 1)]


def test_what_is_a_reference_and_what_resolves(inventory: ArtifactInventory) -> None:
    record(inventory, "op1", b"a", path="plots/t.svg")
    record(inventory, "op2", b"b", path="shared/w.png")
    inventory.record_metadata(SOURCE, "big.png", sha256="c" * 64, size=1, media_type="image/png",
                              note="too_large")  # fmt: skip
    record(inventory, "op3", b"d", path="gone.png")
    inventory.record_deletion(SOURCE, "gone.png")
    store = _store(inventory)
    text = (
        "![a](/sessions/s1/plots/t.svg) ![b](shared/w.png) ![c](missing.png) ![d](big.png) "
        "![e](gone.png) ![f](https://example.org/x.png) ![g](a.svg?x=1) ![h](../up.svg) "
        "`![i](plots/t.svg)` $![j](plots/t.svg)$ [![k](sessions/s1/plots/t.svg)](https://x.org)\n"
    )
    document = _notes(store, text)
    found = {entry["path"]: entry.get("unresolved", entry.get("revision")) for entry in
             store.revision("p1", document).pins}  # fmt: skip
    assert found == {
        "/sessions/s1/plots/t.svg": 1, "shared/w.png": 1, "missing.png": "no_file",
        "big.png": "content_not_kept", "gone.png": "deleted", "sessions/s1/plots/t.svg": 1,
    }  # fmt: skip
    result = store.revision("p1", document).info.warnings
    assert sorted(str(w["path"]) for w in result if w["kind"] == "unresolved_reference") == [
        "big.png", "gone.png", "missing.png",
    ]  # fmt: skip
    # A project document the user saves outside any session has no session for a
    # relative path.
    project = store.create("p1", name="p", title="P", text="![x](plots/t.svg)\n", author=USER)
    (pin,) = store.revision("p1", project.document_id).pins
    assert pin["unresolved"] == "needs_session"


def test_a_chart_pins_its_data(inventory: ArtifactInventory) -> None:
    record(inventory, "op1", b"bits,ms\n64,3\n", path="results/t.csv")
    store = _store(inventory)
    chart = {"layer": [{"data": {"url": "results/t.csv"}, "mark": "point"}], "data": {"url": "x"}}
    text = f"```vega-lite\n{json.dumps(chart)}\n```\n\n```json\n{json.dumps(chart)}\n```\n"
    document = _notes(store, text)
    found = [(entry["kind"], entry["path"], entry.get("revision", entry.get("unresolved")))
             for entry in store.revision("p1", document).pins]  # fmt: skip
    assert found == [("chart_data", "x", "no_file"), ("chart_data", "results/t.csv", 1)]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[WorkerClient]:
    async with WorkerClient("http://127.0.0.1:9", "k" * 40) as found:
        yield found


@pytest.mark.asyncio
async def test_the_agent_reads_and_moves_pins(
    inventory: ArtifactInventory, client: WorkerClient
) -> None:
    record(inventory, "op1", b"one", path="plots/t.svg")
    store = _store(inventory)
    document = _notes(store)
    record(inventory, "op2", b"two", path="plots/t.svg")
    figure = _block(store, document, "![Timing]")
    script = [
        {"tool_calls": [{"name": "document_read", "arguments": {"document": "notes"}}]},
        {"tool_calls": [{"name": "document_patch", "arguments": {
            "document": "notes", "base_revision": 1, "reason": "the new plot",
            "operations": [{"op": "repin", "block": figure, "path": "plots/t.svg"}]}}]},
        {"text": "Done."},
    ]  # fmt: skip
    model = ScriptedModel.from_json(script)
    tool = DocumentTool(store, CONTEXT, run_id="run1", alias="scripted")
    await run_task(model, Toolbox(client, tool.context, documents=tool), "update", run_id="run1")
    read, patched = (
        json.loads(message["content"])
        for message in model.requests[-1]
        if message["role"] == "tool"
    )
    (shown,) = [block for block in read["blocks"] if block["id"] == figure]
    assert shown["files"] == [{"path": "plots/t.svg", "revision": 1, "latest": 2}]
    assert (patched["outcome"], patched["revision"]) == ("applied", 2)
    assert [entry["revision"] for entry in store.revision("p1", document).pins] == [2]


def test_the_citing_guidance_agents_read_matches_what_is_pinned() -> None:
    """The document tools tell agents how to cite a file (FG-6); its example is a file
    reference and a plain link to a path is not, so the guidance says what the parser does."""
    for name in ("document_create", "document_patch"):
        assert _CITING in next(spec for spec in SPECS if spec.name == name).description
    # Its template, then its example: the example is what the parser pins.
    template, example = re.findall(r"!\[[^\]]*\]\([^)]*\)", _CITING)
    assert template == "![what it is](path)"
    assert [ref.path for ref in text_references(example, "x")] == [
        "shared/snova/singular_plane.sage"
    ]
    assert text_references("[script](shared/snova/singular_plane.sage)", "x") == []
