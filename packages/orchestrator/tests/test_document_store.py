# SPDX-License-Identifier: MIT
"""Living documents in the control store: revisions, patches and their events.

The acceptance fixtures of docs/specs/living-documents.md that this slice covers run here
against the store, with the user acting through it as the workspace will (LD-F1, LD-F3,
LD-F4, LD-F5, LD-F14, LD-F18); test_document_tool.py runs them through the harness with a
scripted model. The store's own tests follow: concurrent writers, retries, tombstones and
the records' immutability.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    Mapped,
)

FF = """---
title: Fermat's little theorem over GF(17)
macros:
  \\F: \\mathbb{F}
---
# Fermat's little theorem

Let $p = 17$. For every $a \\in \\F_p^\\times$, the identity below holds.

$$a^{p-1} = 1$$ (fermat)

::: lemma (fermat-lemma)
For every prime $p$ and every $a$ not divisible by $p$, $a^{p-1} \\equiv 1 \\pmod p$.
:::

| $a$ | $a^{16} \\bmod 17$ |
| --- | --- |
| 2 | 1 |
| 3 | 1 |

In summary, every nonzero residue modulo 17 satisfies the identity.
"""
P_TEXT = "Let $p = 17$. For every $a \\in \\F_p^\\times$, the identity below holds."
S_TEXT = "In summary, every nonzero residue modulo 17 satisfies the identity."


def _agent(session: str = "s1", run: str = "run-1", call: str = "call-1") -> Author:
    return Author(
        kind="agent", session_id=session, run_id=run, role="main", model="scripted", tool_call=call
    )


AGENT = _agent()


class Clock:
    """A clock the test moves."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    found = DocumentStore(tmp_path / "control.sqlite3", clock=Clock())
    yield found
    found.close()


def _ff(store: DocumentStore) -> dict[str, str]:
    """ff.md at r1, as the user imports it, and its blocks' IDs by the fixture's names."""
    created = store.create("p1", name="ff", title="Fermat", text=FF, author=USER)
    assert created.outcome == "created"
    assert created.revision == 1
    return _names(store.mapped("p1", "ff"))


def _names(mapped: Mapped) -> dict[str, str]:
    """H, P, E, L, LP (L's paragraph), T and S, the fixture's blocks, by their IDs."""
    names: dict[str, str] = {}
    for block_id, block in zip(mapped.block_ids, mapped.parsed.blocks, strict=True):
        if block.kind == "heading":
            names["H"] = block_id
        elif block.kind == "equation":
            names["E"] = block_id
        elif block.kind == "container":
            names["L"] = block_id
        elif block.kind == "table":
            names["T"] = block_id
        elif block.kind == "paragraph" and block.parent is not None:
            names["LP"] = block_id
        elif block.kind == "paragraph" and block.text.startswith("Let"):
            names["P"] = block_id
        elif block.kind == "paragraph" and block.text.startswith("In summary"):
            names["S"] = block_id
    return names


def _statuses(store: DocumentStore, number: int) -> dict[str, str]:
    revision = store.revision("p1", "ff", number)
    return {entry["id"]: entry["status"] for entry in revision.block_map["blocks"]}  # type: ignore[index, union-attr]


def _spans(store: DocumentStore, number: int, block_id: str) -> list[tuple[str, str]]:
    revision = store.revision("p1", "ff", number)
    for entry in revision.block_map["blocks"]:  # type: ignore[union-attr]
        if entry["id"] == block_id:
            return [(span["id"], span["status"]) for span in entry["spans"]]
    raise AssertionError(f"no block {block_id}")


def _events(store: DocumentStore, kind: str | None = None) -> list[dict[str, Any]]:
    return [
        {"kind": event.kind, "session": event.session_id, **event.payload}
        for event in store.events("p1")
        if kind is None or event.kind == kind
    ]


def test_ld_f1_ids_across_edits(store: DocumentStore) -> None:
    ids = _ff(store)
    first = store.mapped("p1", "ff")
    p_spans = [
        first.span_ids[index] for index in first.parsed.blocks[first.index(ids["P"]) or 0].spans
    ]
    r2 = (
        FF.replace(P_TEXT + "\n\n", "")
        .replace(S_TEXT + "\n", "")
        .replace(
            "# Fermat's little theorem\n",
            "# Fermat's little theorem\n\nWe check this by computation.\n",
        )
        .replace(
            "$$a^{p-1} = 1$$ (fermat)",
            P_TEXT.replace("\\F_p^\\times", "\\F_{17}^\\times") + "\n\n$$a^{p-1} = 1$$ (fermat)",
        )
    )
    # T moves above P: the table goes right after the new paragraph.
    table = "| $a$ | $a^{16} \\bmod 17$ |\n| --- | --- |\n| 2 | 1 |\n| 3 | 1 |\n"
    r2 = r2.replace("\n" + table, "")
    r2 = r2.replace("We check this by computation.\n", "We check this by computation.\n\n" + table)
    saved = store.save_text("p1", "ff", base=1, text=r2, author=USER, reason="reorganized")
    assert saved.outcome == "applied"
    assert saved.revision == 2
    statuses = _statuses(store, 2)
    assert statuses[ids["H"]] == "kept"
    assert statuses[ids["E"]] == "kept"
    assert statuses[ids["L"]] == "kept"
    assert statuses[ids["T"]] == "moved"
    assert statuses[ids["P"]] == "changed"
    added = [block_id for block_id, status in statuses.items() if status == "added"]
    assert len(added) == 1
    assert ids["S"] in saved.changes["removed"]  # type: ignore[operator]
    # P's first span is kept and its second changed, under the same IDs.
    assert _spans(store, 2, ids["P"]) == [(p_spans[0], "kept"), (p_spans[1], "changed")]


def test_ld_f3_a_stale_patch_to_other_blocks_is_rebased(store: DocumentStore) -> None:
    ids = _ff(store)
    store.save_text(
        "p1",
        "ff",
        base=1,
        text=FF.replace("the identity below holds", "the identity below always holds"),
        author=USER,
        reason="clarify",
    )
    patch = {
        "base": 1,
        "operations": [
            {"op": "replace", "block": ids["S"], "markdown": "So $\\F_{17}^\\times$ has order 16."}
        ],
        "reason": "a sharper summary",
        "author": AGENT,
        "operation_id": "summary-1",
    }
    result = store.patch("p1", "ff", **patch)
    assert result.outcome == "applied"
    assert (result.revision, result.base, result.applied_to, result.rebased) == (3, 1, 2, True)
    text = store.revision("p1", "ff").text
    assert "always holds" in text
    assert "So $\\F_{17}^\\times$ has order 16." in text
    info = store.revision("p1", "ff", 3).info
    assert (info.base, info.parent) == (1, 2)
    retry = store.patch("p1", "ff", **patch)
    assert retry.replayed
    assert (retry.outcome, retry.revision) == ("applied", 3)
    assert store.document("p1", "ff").head == 3
    assert len(_events(store, "document.revision_created")) == 3


def test_ld_f4_a_stale_patch_to_the_same_block_conflicts(store: DocumentStore) -> None:
    ids = _ff(store)
    store.save_text(
        "p1",
        "ff",
        base=1,
        text=FF.replace("a^{p-1} = 1$$", "a^{16} = 1$$"),
        author=USER,
        reason="for p = 17",
    )
    first = store.mapped("p1", "ff", 1)
    e_span = first.span_ids[first.parsed.blocks[first.index(ids["E"]) or 0].spans[0]]
    conflict = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="general",
        operation_id="e-1",
        operations=[{"op": "replace_math", "math": e_span, "tex": "a^{p} = a"}],
    )
    assert conflict.outcome == "conflict"
    assert conflict.revision == 2
    (found,) = conflict.conflicts
    assert found["math"] == e_span
    assert (found["at_base"], found["now"]) == ("a^{p-1} = 1", "a^{16} = 1")
    assert found["changed_by"] == [{"revision": 2, "author": {"kind": "user", "user": "user"}}]
    assert store.document("p1", "ff").head == 2
    rejected = store.patches("p1", "ff", states=("conflicted",))
    assert [patch.patch_id for patch in rejected] == [conflict.patch_id]
    assert rejected[0].base == 1
    assert rejected[0].author == AGENT
    assert rejected[0].operations == [{"op": "replace_math", "math": e_span, "tex": "a^{p} = a"}]
    applied = store.patch(
        "p1",
        "ff",
        base=2,
        author=AGENT,
        reason="general",
        operation_id="e-2",
        operations=[{"op": "replace_math", "math": e_span, "tex": "a^{p} = a"}],
    )
    assert (applied.outcome, applied.revision) == ("applied", 3)
    history = store.history("p1", "ff")
    authors = [(entry["number"], entry["author"]["kind"]) for entry in history["revisions"]]
    assert authors == [(1, "user"), (2, "user"), (3, "agent")]
    assert [entry["revision"] for entry in history["blocks"][ids["E"]]] == [1, 2, 3]
    assert _events(store, "document.patch_conflicted")[0]["patch_id"] == conflict.patch_id


def test_ld_f4_a_block_replaced_after_the_base_conflicts_with_its_texts(
    store: DocumentStore,
) -> None:
    ids = _ff(store)
    store.save_text(
        "p1", "ff", base=1, text=FF.replace("(fermat)", "(fermat-17)"), author=USER, reason="label"
    )
    conflict = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="r",
        operation_id="x",
        operations=[{"op": "replace", "block": ids["E"], "markdown": "$$x$$"}],
    )
    (found,) = conflict.conflicts
    assert found["block"] == ids["E"]
    assert found["at_base"] == "$$a^{p-1} = 1$$ (fermat)"
    assert found["now"] == "$$a^{p-1} = 1$$ (fermat-17)"


def test_ld_f5_patches_that_spill_and_nested_blocks(store: DocumentStore) -> None:
    ids = _ff(store)
    refused = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="code",
        operation_id="fence",
        operations=[{"op": "replace", "block": ids["P"], "markdown": "```python\nprint('open')"}],
    )
    assert refused.outcome == "refused"
    assert {ids["E"], ids["L"], ids["S"]} <= set(refused.unnamed)
    assert store.document("p1", "ff").head == 1
    assert _events(store, "document.patch_refused")[0]["unnamed"] == refused.unnamed
    nested = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="sharper",
        operation_id="lemma",
        operations=[
            {
                "op": "replace",
                "block": ids["LP"],
                "markdown": "For every prime $p$, $a^p \\equiv a \\pmod p$.",
            }
        ],
    )
    assert nested.revision == 2
    statuses = _statuses(store, 2)
    assert statuses[ids["L"]] == "changed"
    assert statuses[ids["LP"]] == "changed"
    assert {
        status for block_id, status in statuses.items() if block_id not in (ids["L"], ids["LP"])
    } == {"kept"}


def test_ld_f14_suggestions(store: DocumentStore) -> None:
    ids = _ff(store)
    info = store.set_suggestions("p1", "ff", on=True, author=USER)
    assert info.takes_suggestions
    suggested = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="summary",
        operation_id="s",
        operations=[
            {"op": "replace", "block": ids["S"], "markdown": "Every residue satisfies it."}
        ],
    )
    assert suggested.outcome == "suggested"
    assert store.document("p1", "ff").head == 1
    accepted = store.decide("p1", "ff", suggested.patch_id or "", accept=True, author=USER)
    assert (accepted.outcome, accepted.revision) == ("accepted", 2)
    revision = store.revision("p1", "ff", 2).info
    assert revision.author == AGENT
    assert revision.accepted_by == USER
    second = store.patch(
        "p1",
        "ff",
        base=2,
        author=AGENT,
        reason="p",
        operation_id="p",
        operations=[
            {"op": "replace_text", "block": ids["P"], "quote": "holds", "with": "always holds"}
        ],
    )
    declined = store.decide("p1", "ff", second.patch_id or "", accept=False, author=USER)
    assert declined.outcome == "declined"
    assert store.document("p1", "ff").head == 2
    states = {
        patch.patch_id: (patch.state, patch.decided_by) for patch in store.patches("p1", "ff")
    }
    assert states[suggested.patch_id or ""] == ("accepted", USER)
    assert states[second.patch_id or ""] == ("declined", USER)
    decisions = [event["decision"] for event in _events(store, "document.suggestion_decided")]
    assert decisions == ["accepted", "rejected"]
    assert len(_events(store, "document.suggestion_created")) == 2
    history = store.history("p1", "ff")
    assert [patch["state"] for patch in history["patches"]] == ["accepted", "declined"]
    with pytest.raises(DocumentError, match="not a waiting suggestion"):
        store.decide("p1", "ff", second.patch_id or "", accept=True, author=USER)


def test_suggestions_are_the_users_to_decide(store: DocumentStore) -> None:
    _ff(store)
    with pytest.raises(DocumentError) as raised:
        store.set_suggestions("p1", "ff", on=True, author=AGENT)
    assert raised.value.kind == "policy_denied"


def test_an_accepted_suggestion_whose_block_changed_conflicts(store: DocumentStore) -> None:
    ids = _ff(store)
    store.set_suggestions("p1", "ff", on=True, author=USER)
    suggested = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="s",
        operation_id="s",
        operations=[{"op": "replace", "block": ids["S"], "markdown": "New summary."}],
    )
    store.save_text(
        "p1", "ff", base=1, text=FF.replace("In summary", "To sum up"), author=USER, reason="u"
    )
    result = store.decide("p1", "ff", suggested.patch_id or "", accept=True, author=USER)
    assert result.outcome == "conflict"
    assert [patch.state for patch in store.patches("p1", "ff")] == ["conflicted"]


def test_ld_f18_delimiters_normalized_on_save(store: DocumentStore) -> None:
    ids = _ff(store)
    sentence = (
        "Hence \\( a^{16} = 1 \\) for every nonzero $a$; \\(x\\)2 is a typo, `\\(y\\)` is code "
        "and $5 is a price."
    )
    inserted = (
        "\\[ \\sum_{a} a = 0 \\]\n\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\n\n"
        "```\n\\[z\\]\n```"
    )
    result = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="the computation",
        operation_id="hence",
        operations=[
            {"op": "replace", "block": ids["P"], "markdown": sentence},
            {"op": "insert", "after": ids["P"], "markdown": inserted},
        ],
    )
    assert result.outcome == "applied"
    revision = store.revision("p1", "ff", 2)
    text = revision.text
    assert "Hence $a^{16} = 1$ for every nonzero $a$;" in text
    # Kept as written, pending the user's decision on LD-41 (normalize.AMBIGUOUS_INLINE).
    assert "\\(x\\)2 is a typo" in text
    assert "`\\(y\\)` is code and $5 is a price." in text
    assert "$$ \\sum_{a} a = 0 $$" in text
    assert "$$\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\n$$" in text
    assert "```\n\\[z\\]\n```" in text
    assert revision.info.author == AGENT
    assert revision.info.normalized["converted"] == {"inline": 1, "display": 1, "environment": 1}
    assert revision.info.normalized["kept"] == [{"tex": "x", "why": "a digit follows it"}]
    assert _events(store, "document.revision_created")[-1]["normalized"] is True
    # Blocks the patch did not name are unchanged.
    statuses = _statuses(store, 2)
    for name in ("H", "E", "L", "T", "S"):
        assert statuses[ids[name]] == "kept"


def test_normalization_never_touches_blocks_a_patch_does_not_name(store: DocumentStore) -> None:
    created = store.create(
        "p1", name="raw", title="Raw", author=USER, text="One \\(a\\).\n\nTwo \\(b\\).\n"
    )
    mapped = store.mapped("p1", "raw")
    assert created.normalized["converted"] == {"inline": 2}
    store.save_text(
        "p1", "raw", base=1, text="One \\(a\\).\n\nTwo \\(b\\).\n", author=USER, reason="as written"
    )
    # A whole-text save is normalized as a whole; a patch only where it writes.
    assert store.revision("p1", "raw").text == "One $a$.\n\nTwo $b$.\n"
    store.create("p1", name="kept", title="K", author=USER, text="A \\(x\\)1.\n\nB.\n")
    kept = store.mapped("p1", "kept")
    store.patch(
        "p1",
        "kept",
        base=1,
        author=AGENT,
        reason="b",
        operation_id="b",
        operations=[{"op": "replace", "block": kept.block_ids[1], "markdown": "B \\(y\\)."}],
    )
    assert store.revision("p1", "kept").text == "A \\(x\\)1.\n\nB $y$.\n"
    del mapped


def test_ld_11_a_change_to_identical_text_makes_no_revision(store: DocumentStore) -> None:
    ids = _ff(store)
    same = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="same",
        operation_id="same",
        operations=[{"op": "replace", "block": ids["S"], "markdown": S_TEXT}],
    )
    assert same.outcome == "unchanged"
    assert same.revision == 1
    assert store.document("p1", "ff").head == 1
    saved = store.save_text("p1", "ff", base=1, text=FF, author=USER, reason="same")
    assert saved.outcome == "unchanged"
    assert len(_events(store, "document.revision_created")) == 1


def test_retries_return_the_first_result_and_reused_ids_are_refused(store: DocumentStore) -> None:
    ids = _ff(store)
    request = {
        "base": 1,
        "reason": "summary",
        "operation_id": "op-1",
        "operations": [{"op": "replace", "block": ids["S"], "markdown": "Short."}],
    }
    first = store.patch("p1", "ff", author=AGENT, **request)
    # Another run of the same session, another process: the same result.
    other = DocumentStore(store.path)
    try:
        again = other.patch("p1", "ff", author=_agent(run="run-2", call="call-9"), **request)
    finally:
        other.close()
    assert again.replayed
    assert again.model_copy(update={"replayed": False}) == first
    with pytest.raises(DocumentError, match="was used for another change") as raised:
        store.patch("p1", "ff", author=AGENT, **{**request, "reason": "something else"})
    assert raised.value.kind == "conflict"
    # Another session's operation IDs are its own.
    third = store.patch("p1", "ff", author=_agent(session="s2"), **{**request, "base": 2})
    assert third.outcome == "unchanged"
    assert not third.replayed


def test_create_is_idempotent_too(store: DocumentStore) -> None:
    first = store.create("p1", name="n", title="N", text="x\n", author=AGENT, operation_id="c1")
    again = store.create("p1", name="n", title="N", text="x\n", author=AGENT, operation_id="c1")
    assert again.replayed
    assert again.document_id == first.document_id
    with pytest.raises(DocumentError, match="named 'n' already"):
        store.create("p1", name="n", title="N", text="y\n", author=AGENT, operation_id="c2")


def test_concurrent_writers_in_threads(store: DocumentStore, tmp_path: Path) -> None:
    text = "\n\n".join(f"Paragraph {index}." for index in range(8)) + "\n"
    store.create("p1", name="many", title="Many", text=text, author=USER)
    ids = store.mapped("p1", "many").block_ids
    results: list[str] = []
    errors: list[BaseException] = []

    def write(index: int) -> None:
        writer = DocumentStore(tmp_path / "control.sqlite3")
        try:
            result = writer.patch(
                "p1",
                "many",
                base=1,
                author=_agent(session=f"s{index}"),
                reason="mine",
                operation_id=f"w{index}",
                operations=[
                    {"op": "replace", "block": ids[index], "markdown": f"Changed {index}."}
                ],
            )
            results.append(result.outcome)
        except BaseException as exc:  # reported below
            errors.append(exc)
        finally:
            writer.close()

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sorted(results) == ["applied"] * 8
    final = store.revision("p1", "many")
    assert final.info.number == 9
    for index in range(8):
        assert f"Changed {index}." in final.text
    assert [info.number for info in store.revisions("p1", "many")] == list(range(1, 10))


WRITER = """
import json, sys
from pathlib import Path
from sandbox_science.orchestrator.documents import Author, DocumentStore
path, block, index = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
store = DocumentStore(path, producer="test")
author = Author(kind="agent", session_id=f"s{index}", run_id=f"r{index}", role="main")
result = store.patch("p1", "shared", base=1, author=author, reason="mine", operation_id="w",
    operations=[{"op": "replace", "block": block, "markdown": f"Process {index}."}])
print(json.dumps({"outcome": result.outcome, "revision": result.revision}))
"""


def test_concurrent_writers_in_processes(store: DocumentStore, tmp_path: Path) -> None:
    text = "\n\n".join(f"Paragraph {index}." for index in range(6)) + "\n"
    store.create("p1", name="shared", title="Shared", text=text, author=USER)
    ids = store.mapped("p1", "shared").block_ids
    # Four processes change four blocks; two more change the same block as the first.
    targets = [ids[0], ids[1], ids[2], ids[3], ids[0], ids[0]]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(store.path), block, str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index, block in enumerate(targets)
    ]
    outcomes = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
        outcomes.append(json.loads(out)["outcome"])
    # Of the three on one block, the first to commit applies and the others conflict.
    assert sorted(outcomes) == ["applied"] * 4 + ["conflict"] * 2
    final = store.revision("p1", "shared")
    assert final.info.number == 5
    assert len(store.patches("p1", "shared", states=("conflicted",))) == 2


def test_tombstones_keep_the_history(store: DocumentStore) -> None:
    ids = _ff(store)
    deleted = store.delete("p1", "ff", author=USER, reason="superseded")
    assert (deleted.outcome, deleted.revision) == ("deleted", 2)
    tombstone = store.revision("p1", deleted.document_id, 2)
    assert tombstone.info.tombstone
    assert tombstone.text == ""
    assert {entry["id"] for entry in tombstone.block_map["removed"]} == set(ids.values()) | {  # type: ignore[union-attr]
        entry["id"]
        for entry in store.revision("p1", deleted.document_id, 1).block_map["blocks"]  # type: ignore[union-attr]
    }
    assert store.revision("p1", deleted.document_id, 1).text == FF
    assert store.documents("p1") == []
    assert [info.deleted for info in store.documents("p1", deleted=True)] == [True]
    late = store.patch(
        "p1",
        deleted.document_id,
        base=1,
        author=AGENT,
        reason="late",
        operation_id="late",
        operations=[{"op": "delete", "block": ids["S"]}],
    )
    assert late.outcome == "conflict"
    assert "deleted" in (late.message or "")
    # The name is free again; the old document keeps its history under its ID.
    again = store.create("p1", name="ff", title="Again", text="New.\n", author=USER)
    assert again.document_id != deleted.document_id
    assert len(store.history("p1", deleted.document_id)["revisions"]) == 2
    tomb = _events(store, "document.revision_created")[1]
    assert tomb["tombstone"] is True


def test_ld_12_an_agent_deletes_only_what_its_session_created(store: DocumentStore) -> None:
    _ff(store)
    with pytest.raises(DocumentError) as raised:
        store.delete("p1", "ff", author=AGENT, reason="mine?")
    assert raised.value.kind == "policy_denied"
    store.create("p1", name="scratch", title="S", text="x\n", author=AGENT, session_id="s1")
    other = _agent(session="s2")
    with pytest.raises(DocumentError):
        store.delete("p1", "scratch", author=other, reason="not mine")
    assert store.delete("p1", "scratch", author=AGENT, reason="done").outcome == "deleted"


def test_agents_change_documents_only_with_patches(store: DocumentStore) -> None:
    _ff(store)
    with pytest.raises(DocumentError) as raised:
        store.save_text("p1", "ff", base=1, text="x", author=AGENT, reason="overwrite")
    assert raised.value.kind == "policy_denied"
    with pytest.raises(ValueError, match="needs session_id, run_id"):
        Author(kind="agent")


def test_a_stale_whole_text_save_is_refused(store: DocumentStore) -> None:
    _ff(store)
    store.save_text("p1", "ff", base=1, text=FF + "\nMore.\n", author=USER, reason="more")
    with pytest.raises(DocumentError, match="revision 2 is current") as raised:
        store.save_text("p1", "ff", base=1, text=FF, author=USER, reason="old")
    assert raised.value.kind == "conflict"


def test_revisions_and_events_cannot_be_rewritten(store: DocumentStore) -> None:
    _ff(store)
    db = sqlite3.connect(store.path)
    try:
        for statement in (
            "UPDATE revisions SET text = 'forged'",
            "DELETE FROM revisions",
            "UPDATE events SET kind = 'forged'",
            "DELETE FROM events",
        ):
            with pytest.raises(sqlite3.IntegrityError, match=r"immutable|append-only"):
                db.execute(statement)
    finally:
        db.close()


def test_a_change_and_its_events_commit_together(
    store: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _ff(store)
    original = store.record_event

    def failing(db: sqlite3.Connection, kind: str, *args: Any) -> None:
        if kind == "document.revision_created":
            raise RuntimeError("the event could not be written")
        original(db, kind, *args)

    monkeypatch.setattr(store, "record_event", failing)
    with pytest.raises(RuntimeError):
        store.patch(
            "p1",
            "ff",
            base=1,
            author=AGENT,
            reason="r",
            operation_id="o",
            operations=[{"op": "delete", "block": ids["S"]}],
        )
    monkeypatch.undo()
    assert store.document("p1", "ff").head == 1
    assert [info.number for info in store.revisions("p1", "ff")] == [1]
    assert store.patches("p1", "ff") == []
    assert len(_events(store)) == 2
    # The operation ID was not spent either.
    retry = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="r",
        operation_id="o",
        operations=[{"op": "delete", "block": ids["S"]}],
    )
    assert (retry.outcome, retry.replayed) == ("applied", False)


def test_a_read_only_store_reads_and_never_writes(store: DocumentStore, tmp_path: Path) -> None:
    missing = DocumentStore(tmp_path / "none.sqlite3", readonly=True)
    assert missing.documents("p1") == []
    assert not (tmp_path / "none.sqlite3").exists()
    _ff(store)
    reader = DocumentStore(store.path, readonly=True)
    try:
        assert [info.name for info in reader.documents("p1")] == ["ff"]
        assert reader.revision("p1", "ff").text == FF
        with pytest.raises(DocumentError, match="read-only"):
            reader.create("p1", name="x", title="X", text="x", author=USER)
    finally:
        reader.close()


def test_a_store_in_another_format_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {migrations.VERSION + 1}")
    db.close()
    with pytest.raises(DocumentError, match="newer"):
        DocumentStore(path).documents("p1")
    other = tmp_path / "other.sqlite3"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE something (x)")
    db.close()
    with pytest.raises(DocumentError, match="not a control store"):
        DocumentStore(other).documents("p1")


def test_documents_belong_to_their_project(store: DocumentStore) -> None:
    created = store.create("p1", name="ff", title="F", text="x\n", author=USER)
    with pytest.raises(DocumentError) as raised:
        store.revision("p2", created.document_id)
    assert raised.value.kind == "not_found"
    with pytest.raises(DocumentError):
        store.patch(
            "p2",
            created.document_id,
            base=1,
            author=AGENT,
            reason="r",
            operation_id="o",
            operations=[{"op": "delete", "block": "b1"}],
        )


def test_workbook_documents_and_names(store: DocumentStore) -> None:
    project = store.create("p1", name="notes", title="Project notes", text="P.\n", author=USER)
    mine = store.create(
        "p1", name="notes", title="My notes", text="S.\n", author=AGENT, session_id="s1"
    )
    assert store.document("p1", "notes", "s1").document_id == mine.document_id
    assert store.document("p1", "notes", "s2").document_id == project.document_id
    assert store.document("p1", "notes.md").document_id == project.document_id
    workbook = store.document("p1", mine.document_id)
    assert (workbook.scope, workbook.session_id, workbook.created_session) == (
        "session",
        "s1",
        "s1",
    )
    with pytest.raises(DocumentError, match="invalid name"):
        store.create("p1", name="../escape", title="X", text="x", author=USER)


@pytest.mark.parametrize(
    ("operations", "base", "message"),
    [
        ([{"op": "replace", "block": "b99", "markdown": "x"}], 1, "no block b99"),
        ([{"op": "replace_math", "math": "m99", "tex": "x"}], 1, "no math m99"),
        ([{"op": "delete", "block": "b1"}], 7, "revisions 1 to 1"),
        ([], 1, "1 to 64 operations"),
        ("delete everything", 1, "operations"),
    ],
)
def test_patches_that_cannot_apply_write_nothing(
    store: DocumentStore, operations: object, base: int, message: str
) -> None:
    _ff(store)
    with pytest.raises(DocumentError, match=message) as raised:
        store.patch(
            "p1",
            "ff",
            base=base,
            author=AGENT,
            reason="r",
            operation_id="o",
            operations=operations,
        )
    assert raised.value.kind == "invalid_request"
    assert store.patches("p1", "ff") == []


def test_warnings_on_new_math(store: DocumentStore) -> None:
    ids = _ff(store)
    result = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="warn",
        operation_id="w",
        operations=[
            {
                "op": "replace",
                "block": ids["S"],
                "markdown": "Let $\\gdef\\G{G}\\G$ hold, and $x without end.",
            },
            {
                "op": "insert",
                "after": ids["S"],
                "markdown": "So $$\nx = 1\nThen we have the following\n$$ ok.",
            },
        ],
    )
    kinds = sorted({str(warning["kind"]) for warning in result.warnings})
    assert kinds == ["definition_in_math", "display_swallows_text", "unbalanced_dollar"]
    stored = store.revision("p1", "ff").info.warnings
    assert stored == result.warnings
    assert all(warning["block"] != ids["P"] for warning in stored)


def test_a_quoted_equation_is_read_without_its_markers(store: DocumentStore) -> None:
    # Its TeX keeps the quote's "> ", which reads as Markdown; the checks leave it out.
    text = "> A display:\n>\n> $$\n> \\sum_{a} a = 0\n> $$\n"
    created = store.create("p1", name="q", title="Q", text=text, author=USER)
    assert created.warnings == []
    swallowing = "> $$\n> x = 1\n> Then we have the following\n> $$\n"
    found = store.create("p1", name="s", title="S", text=swallowing, author=USER)
    assert [warning["kind"] for warning in found.warnings] == ["display_swallows_text"]


def test_front_matter_problems_are_warnings(store: DocumentStore) -> None:
    result = store.create(
        "p1", name="bad", title="Bad", text="---\nmacros: [1]\n---\nText.\n", author=USER
    )
    assert [warning["kind"] for warning in result.warnings] == ["front_matter"]


def test_ld_39_events_report_what_changed_and_who_wrote_it(store: DocumentStore) -> None:
    ids = _ff(store)
    lemma = "::: claim\n" + "x" * 150 + "\n:::"
    store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="claim",
        operation_id="c",
        operations=[{"op": "insert", "after": ids["S"], "markdown": lemma}],
    )
    created = _events(store, "document.revision_created")
    agent = created[-1]
    assert agent["author_kind"] == "agent"
    assert agent["agent_characters"] == 150
    assert [entry["kind"] for entry in agent["structural_added"]] == ["container"]
    assert agent["session"] == "s1"
    assert (agent["base"], agent["parent"]) == (1, 1)
    user = created[0]
    assert user["agent_characters"] == 0
    assert {entry["kind"] for entry in user["structural_added"]} == {
        "heading",
        "container",
        "table",
    }


def test_the_platform_and_imports_are_authors_of_their_own(store: DocumentStore) -> None:
    importer = Author(kind="import", user="user", import_id="import-1")
    created = store.create("p1", name="ff", title="F", text="x\n", author=importer)
    repair = Author(kind="platform", service="math-repair")
    saved = store.save_text("p1", "ff", base=1, text="y\n", author=repair, reason="repair")
    assert (created.revision, saved.revision) == (1, 2)
    authors = [info.author for info in store.revisions("p1", "ff")]
    assert authors == [importer, repair]
    assert (importer.actor, repair.actor) == ("user:user", "platform:math-repair")
    with pytest.raises(ValueError, match="needs service"):
        Author(kind="platform")


def test_a_patch_is_checked_before_the_write_lock_and_again_if_the_document_changed(
    store: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _ff(store)
    checked: list[int] = []
    assess = store._assess

    def counting(*args: Any) -> Any:
        checked.append(1)
        return assess(*args)

    monkeypatch.setattr(store, "_assess", counting)
    mine = [{"op": "replace", "block": ids["S"], "markdown": "Mine."}]
    applied = store.patch(
        "p1", "ff", base=1, author=AGENT, reason="r", operation_id="one", operations=mine
    )
    # Checked once, before the lock; under it, the document was as it had been.
    assert (applied.outcome, len(checked)) == ("applied", 1)
    other = DocumentStore(store.path)
    prepare = store._prepare_patch

    def then_another_writer(*args: Any) -> Any:
        prepared = prepare(*args)
        theirs = [{"op": "replace", "block": ids["P"], "markdown": "Theirs."}]
        other.patch(
            "p1", "ff", base=2, author=USER, reason="u", operation_id="u", operations=theirs
        )
        return prepared

    monkeypatch.setattr(store, "_prepare_patch", then_another_writer)
    again = [{"op": "replace", "block": ids["P"], "markdown": "Mine again."}]
    result = store.patch(
        "p1", "ff", base=2, author=AGENT, reason="r", operation_id="two", operations=again
    )
    # Checked again under the lock, against the revision the other writer made.
    assert (result.outcome, len(checked)) == ("conflict", 3)
    assert "Theirs." in store.revision("p1", "ff").text
    other.close()


def test_a_busy_store_says_so(store: DocumentStore) -> None:
    ids = _ff(store)
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    waiting = DocumentStore(store.path, timeout=0.1)
    try:
        with pytest.raises(DocumentError) as raised:
            waiting.patch(
                "p1",
                "ff",
                base=1,
                author=AGENT,
                reason="r",
                operation_id="o",
                operations=[{"op": "delete", "block": ids["S"]}],
            )
        assert raised.value.kind == "busy"
        assert "a retry is safe" in raised.value.message
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        waiting.close()


def test_empty_delimiters_go_but_not_from_links_or_task_boxes(store: DocumentStore) -> None:
    text = (
        "An empty \\(\\) goes.\n\n- \\[ \\] to do\n\n"
        '[the paper](https://example.org/f\\(\\)g "t \\(\\)")\n'
    )
    created = store.create("p1", name="e", title="E", text=text, author=USER)
    assert created.normalized["converted"] == {"empty": 1}
    assert store.revision("p1", "e").text == text.replace("An empty \\(\\) goes", "An empty  goes")
