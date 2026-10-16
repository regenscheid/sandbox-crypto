# SPDX-License-Identifier: MIT
"""References to recorded files in ledger statements and in chat, pinned when the text is
saved (docs/specs/figures.md, FG-7, FG-8, FG-43; LG-28 as amended)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import Author, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .ledger_support import AnchoredLedger as Ledger
from .test_artifact_inventory import SOURCE, record

MAIN = Author(kind="agent", session_id="s", run_id="r1", role="main", model="scripted")
IN_S = SOURCE.model_copy(
    update={"context": ExecutionContext(project_id="p", session_id="s", environment_id="env1")}
)


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[Ledger, ArtifactInventory]]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents), ArtifactInventory(documents)
    documents.close()


def _change(ledger: Ledger, op: str, *changes: dict[str, Any]) -> dict[str, Any]:
    mutation = {"operation_id": op, "reason": "Recorded.", "changes": list(changes)}
    return ledger.change("p", mutation, MAIN).model_dump()


def _revisions(pins: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    return [(pin.get("revision", pin.get("unresolved")), pin.get("latest")) for pin in pins]


def test_a_statement_pins_its_figures_and_moves_them_without_a_flag(
    stores: tuple[Ledger, ArtifactInventory],
) -> None:
    ledger, inventory = stores
    record(inventory, "op1", b"one", source=IN_S, path="plots/t.svg")
    statement = "Time grows as $n^2$:\n\n![Timing](plots/t.svg) ![x](missing.svg)"
    made = _change(
        ledger,
        "c1",
        {"type": "claim", "origin": "project", "title": "Quadratic", "statement": statement,
         "fields": {"kind": "observation"}},
        {"type": "assumption", "title": "n > 1", "statement": "![Timing](plots/t.svg)"},
    )  # fmt: skip
    claim = made["entries"][0]["entry_id"]
    entry = ledger.entry("p", claim)
    assert [pin["artifact_path"] for pin in entry.pins] == [
        "sessions/s/plots/t.svg",
        "sessions/s/missing.svg",
    ]
    assert _revisions(entry.pins) == [(1, None), ("no_file", None)]
    record(inventory, "op2", b"two", source=IN_S, path="plots/t.svg")
    assert _revisions(ledger.entry("p", claim).pins)[0] == (1, 2)
    # A revision that leaves the statement keeps the pin.
    _change(ledger, "c2", {"entry": claim, "expected": 1, "title": "Quadratic time"})
    assert _revisions(ledger.entry("p", claim).pins)[0] == (1, 2)
    moved = _change(
        ledger, "c3", {"entry": claim, "expected": 2, "repin": [{"path": "plots/t.svg"}]}
    )
    assert moved["entries"][0]["revision"] == 3
    after = ledger.entry("p", claim)
    assert (after.statement, _revisions(after.pins)[0]) == (entry.statement, (2, None))
    assert after.flags == []  # a pin moved is no change of the statement (LG-28)
    delegated = MAIN.model_copy(update={"run_id": "r2", "role": "delegated"})
    back = {"entry": claim, "expected": 3, "repin": [{"path": "plots/t.svg", "revision": 1}]}
    mutation = {"operation_id": "d", "reason": "R.", "changes": [back]}
    with pytest.raises(DocumentError, match="a delegated agent"):
        ledger.change("p", mutation, delegated)
    for repin, message in (
        ([{"path": "plots/t.svg"}], "changes nothing"),
        ([{"path": "plots/t.svg", "revision": 7}], "no revision"),
        ([{"path": "other.svg"}], "holds no reference"),
    ):
        with pytest.raises(DocumentError, match=message):
            _change(ledger, "bad", {"entry": claim, "expected": 3, "repin": repin})


def test_chat_pins_are_written_with_the_text_and_never_move(
    stores: tuple[Ledger, ArtifactInventory],
) -> None:
    ledger, inventory = stores
    record(inventory, "op1", b"one", source=IN_S, path="plots/t.svg")
    runs = RunState(ledger.control)
    message = runs.post("p", "s", "m-1", "Compare with ![Timing](plots/t.svg).", author="user")
    assert _revisions(message.pins) == [(1, None)]
    runs.append("p", "s", "run-1", "assistant", {"turn": 1, "text": "Here: ![T](plots/t.svg)"})
    runs.append("p", "s", "run-1", "tool", {"turn": 1, "result": "![T](plots/t.svg)"})
    record(inventory, "op2", b"two", source=IN_S, path="plots/t.svg")
    answer, tool = runs.events("p", "s", "run-1")
    assert (_revisions(answer.pins), tool.pins) == ([(1, None)], [])
    (listed,) = runs.messages("p", "s")
    assert listed.pins == message.pins
    # The same message again is the one written, with its pins.
    again = runs.post("p", "s", "m-1", "Compare with ![Timing](plots/t.svg).", author="user")
    assert again.pins == message.pins
