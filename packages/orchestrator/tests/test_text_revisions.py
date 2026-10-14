# SPDX-License-Identifier: MIT
"""Text revisions (B-04): comment bodies and finding explanations keep their revisions
in one table; version 15 moves both there, and a store answers as before."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.project_macros import response_view
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.mathchecks.notices import MathNotices

from .control_versions import TEXT_REVISIONS, older_store
from .test_comment_math import comment
from .test_comment_math import edit as edit_comment
from .test_document_math import BAD, ScriptedChecker, create, drain
from .test_document_math import stores as stores
from .test_response_math import edit as edit_response
from .test_response_math import response


async def history(docs: DocumentStore, reviews: ReviewStore) -> dict[str, Any]:
    """A comment whose body was edited twice and an explanation edited twice, with
    their math checks run."""
    doc = create(docs, text="Paragraph.\n")
    entry = comment(docs, doc, body=BAD)["entry_id"]
    edit_comment(docs, doc, entry, "$x$ fixed", op="edit-1")
    edit_comment(docs, doc, entry, f"again {BAD}", op="edit-2")
    finding, decision = response(reviews)
    edit_response(reviews, finding, decision, "$y$", op="explain-1")
    edit_response(reviews, finding, decision, f"later {BAD}", op="explain-2")
    await drain(reviews, ScriptedChecker())
    return {"doc": doc, "entry": entry, "finding": finding, "decision": decision}


def answers(docs: DocumentStore, reviews: ReviewStore, made: dict[str, Any]) -> dict[str, Any]:
    explanations = ResponseBodies(reviews)
    source = explanations.get("p", "s", made["finding"], made["decision"])["source"]
    return {
        "comment": Annotations(docs).body("p", made["doc"], made["entry"]),
        "explanations": [
            explanations.get("p", "s", made["finding"], made["decision"], body_revision=n)
            for n in range(4)
        ],
        "views": [response_view(docs, "p", source, body_revision=n) for n in (1, 2, 3)],
        "finding": Findings(reviews).get("p", "s", made["finding"]),
        "findings": Findings(reviews).listing("p", "s"),
        "checks": MathJobs(reviews).listing("p"),
        "notices": MathNotices(reviews).pending("p", "s", "answers", set()),
    }


@pytest.mark.asyncio
async def test_bodies_read_back_every_revision(stores):
    docs, reviews, _ = stores
    made = await history(docs, reviews)
    found = answers(docs, reviews, made)
    assert found["comment"]["revision"] == 3
    assert found["comment"]["body"] == f"again {BAD}"
    assert [e["revision"] for e in found["explanations"]] == [0, 1, 2, 3]
    assert [e["body"] for e in found["explanations"][2:]] == ["$y$", f"later {BAD}"]
    assert len(found["checks"]["checks"]) >= 3  # the document's, the comment's, the explanation's
    assert found["notices"]


@pytest.mark.asyncio
async def test_bodies_from_version_14_answer_as_before(stores, tmp_path):
    docs, reviews, clock = stores
    made = await history(docs, reviews)
    before = answers(docs, reviews, made)
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, TEXT_REVISIONS - 1)
    copied = DocumentStore(old)
    try:
        copied.initialize()  # the writer brings the store to the current version
        assert answers(copied, ReviewStore(copied, clock=lambda: clock[0]), made) == before
    finally:
        copied.close()


@pytest.mark.asyncio
async def test_an_explanation_that_names_another_finding_is_refused(stores, tmp_path):
    docs, reviews, _ = stores
    await history(docs, reviews)
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, TEXT_REVISIONS - 1)
    with sqlite3.connect(old) as db:
        db.execute("DROP TRIGGER review_response_bodies_update")
        db.execute("UPDATE review_response_bodies SET decision_revision=decision_revision+1")
    copied = DocumentStore(old)
    try:
        with pytest.raises(ControlError, match=r"stays at version 14.*does not name its finding"):
            copied.initialize()
    finally:
        copied.close()
