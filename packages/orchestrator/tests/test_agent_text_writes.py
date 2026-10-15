# SPDX-License-Identifier: MIT
"""Agents write no text the user may not see (threat model, A3): documents they create
or patch, their comments, and the review text they write, checked when written by the
shared check (``check_agent_text``) with the shared table of hidden content. The user's
own text is kept as written, and text already stored still reads."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.projects import ProjectStore

FIXTURES = Path(__file__).resolve().parents[3] / "web" / "workspace" / "src" / "lib"
FIXTURE: dict[str, Any] = json.loads((FIXTURES / "hidden.fixtures.json").read_text())
# A carriage return in a line end becomes a line feed before the check (line_ends).
HIDDEN = [
    pytest.param(case["text"], id=case["name"])
    for case in FIXTURE["cases"]
    if case["hidden"] and not case.get("line_ends")
] + [
    pytest.param(case["text"], id=case["name"]) for case in FIXTURE["references"] if case["hidden"]
]
AGENT = Author(kind="agent", session_id="s", run_id="r1", role="main", model="m")
WORKER = Author(
    kind="worker", worker_binding="w1", environment_id="env-1", incarnation="i1", role="delegated"
)


@pytest.fixture
def documents(tmp_path: Path) -> Iterator[DocumentStore]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    store = DocumentStore(store_path(tmp_path))
    yield store
    store.close()


def make(documents: DocumentStore, text: str, author: Author = USER, name: str = "notes") -> str:
    return documents.create(
        "p", name=name, title="Notes", text=text, author=author, session_id="s"
    ).document_id


@pytest.mark.parametrize("text", HIDDEN)
def test_an_agent_creates_no_document_with_hidden_text(documents: DocumentStore, text: str) -> None:
    with pytest.raises(DocumentError) as refused:
        make(documents, f"# Notes\n\n{text}\n", AGENT)
    assert refused.value.kind == "invalid_request"
    assert "the document's text holds U+" in refused.value.message


def test_an_agent_names_no_document_with_hidden_text(documents: DocumentStore) -> None:
    title = "Notes " + chr(0x200B)
    with pytest.raises(DocumentError) as refused:
        documents.create("p", name="t", title=title, text="x\n", author=AGENT, session_id="s")
    assert "the title holds U+200B" in refused.value.message


def test_the_users_own_text_is_kept_and_an_agent_patches_around_it(
    documents: DocumentStore,
) -> None:
    hidden = "a" + chr(0x200B) + "b"
    document = make(documents, f"First {hidden}.\n\nSecond.\n")  # the user's, kept
    second = documents.mapped("p", document).block_ids[1]
    done = documents.patch(
        "p", document, base=1, reason="Edit", author=AGENT, operation_id="op-1",
        operations=[{"op": "replace", "block": second, "markdown": "Second, edited."}],
    )  # fmt: skip
    assert done.revision == 2
    assert hidden in documents.revision("p", document).text


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "markdown": "run" + chr(0x200B) + "it"},
        {"op": "insert", "at": "end", "markdown": "evil &#x202E;gnp.exe"},
        {"op": "replace_text", "quote": "Second", "with": "a" + chr(0x2028) + "b"},
    ],
)
@pytest.mark.parametrize("author", [AGENT, WORKER], ids=["agent", "worker"])
def test_an_agents_patch_holds_no_hidden_text(
    documents: DocumentStore, operation: dict[str, Any], author: Author
) -> None:
    document = make(documents, "First.\n\nSecond.\n")
    block = documents.mapped("p", document).block_ids[1]
    if operation["op"] in ("replace", "replace_text"):
        operation = {**operation, "block": block}
    with pytest.raises(DocumentError) as refused:
        documents.patch(
            "p", document, base=1, reason="Edit", author=author, operation_id="op-1",
            operations=[operation],
        )  # fmt: skip
    assert "operation 1's text holds U+" in refused.value.message
    assert documents.revision("p", document).info.number == 1
    documents.patch(
        "p", document, base=1, reason="Edit", author=USER, operation_id="op-2",
        operations=[operation],
    )  # fmt: skip


def test_an_agents_comment_holds_no_hidden_text(documents: DocumentStore) -> None:
    document = make(documents, "A result.\n")
    comments = Annotations(documents)
    change = {
        "action": "create",
        "anchor": {"revision": 1, "block": "b1"},
        "body": "Looks right" + chr(0x202E) + ".",
        "operation_id": "c1",
    }
    with pytest.raises(DocumentError) as refused:
        comments.change("p", document, change, AGENT)
    assert "the comment holds U+202E" in refused.value.message
    comments.change("p", document, change, USER)  # the user's own


@pytest.fixture
def reviews(documents: DocumentStore) -> ReviewStore:
    store = ReviewStore(documents, clock=lambda: 1000.0)
    store.observe(
        "p", "s", "m1", "transcript", {"run": "author-1", "message": 1},
        {"role": "assistant", "content": "The check gave 16."},
    )  # fmt: skip
    return store


def _review(reviews: ReviewStore) -> str:
    review = reviews.dispatch("p", "s", "terminal", author_run="author-1", owner="o", alias="r")
    assert review is not None
    return review


def _finding(reviews: ReviewStore, review: str, **fields: Any) -> dict[str, Any]:
    evidence = reviews.manifest("p", "s", review)["evidence"][0]["evidence"]
    return Findings(reviews).create(
        "p", "s", review, "o",
        {
            "severity": "fail", "category": "contradicting_value",
            "statement": "The output records 15, not 16.", "anchors": [{"evidence": evidence}],
            "operation_id": "finding-1", **fields,
        },
    )  # fmt: skip


@pytest.mark.parametrize("field", ["statement", "remedy"])
@pytest.mark.parametrize("text", HIDDEN[:3])
def test_a_reviewers_finding_holds_no_hidden_text(
    reviews: ReviewStore, field: str, text: str
) -> None:
    review = _review(reviews)
    with pytest.raises(DocumentError) as refused:
        _finding(reviews, review, **{field: f"Check {text}."})
    assert f"the {field} holds U+" in refused.value.message


def test_an_agents_response_holds_no_hidden_text_but_the_users_may(reviews: ReviewStore) -> None:
    review = _review(reviews)
    found = _finding(reviews, review)
    response = {
        "finding": found["finding_id"], "expected": 1, "action": "rebut",
        "explanation": "It is 16" + chr(0x2066) + ".", "revisions": [], "operation_id": "r1",
    }  # fmt: skip
    with pytest.raises(DocumentError) as refused:
        Findings(reviews).respond("p", "s", response, AGENT)
    assert "the explanation holds U+2066" in refused.value.message
    Findings(reviews).respond("p", "s", response, USER)


def test_an_agents_reason_holds_no_hidden_text_but_the_users_may(
    documents: DocumentStore,
) -> None:
    """The reason shows beside the change in the history, and a suggestion's beside Apply."""
    hidden = "Edit " + chr(0x202E) + "now"
    with pytest.raises(DocumentError) as refused:
        documents.create(
            "p", name="a", title="A", text="x\n", author=AGENT, session_id="s", reason=hidden
        )
    assert refused.value.message.startswith("the reason holds U+202E")
    document = make(documents, "First.\n")
    block = documents.mapped("p", document).block_ids[0]
    operations = [{"op": "replace", "block": block, "markdown": "First, edited."}]
    for author in (AGENT, WORKER):
        with pytest.raises(DocumentError) as patched:
            documents.patch(
                "p", document, base=1, reason=hidden, author=author, operation_id="op-r",
                operations=operations,
            )  # fmt: skip
        assert patched.value.message.startswith("the reason holds U+202E")
    with pytest.raises(DocumentError) as deleted:
        documents.delete("p", document, reason=hidden, author=AGENT)
    assert deleted.value.message.startswith("the reason holds U+202E")
    mine = documents.patch(
        "p", document, base=1, reason=hidden, author=USER, operation_id="op-u",
        operations=operations,
    )  # fmt: skip
    assert mine.revision == 2


def test_an_agents_line_ends_are_kept_as_line_feeds(documents: DocumentStore) -> None:
    document = make(documents, "First.\n\nSecond.\n")
    second = documents.mapped("p", document).block_ids[1]
    done = documents.patch(
        "p", document, base=1, reason="Edit", author=AGENT, operation_id="op-1",
        operations=[{"op": "replace", "block": second, "markdown": "Line one\r\nline two."}],
    )  # fmt: skip
    assert documents.revision("p", document, done.revision).text == (
        "First.\n\nLine one\nline two.\n"
    )
