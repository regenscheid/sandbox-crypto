# SPDX-License-Identifier: MIT
"""Text revisions and their math checks (B-04): comment bodies and finding explanations
keep their revisions in one table (version 15) and their math-check queues and check
links in one queue and one link table (version 16); a migrated store answers as before."""

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

from .control_versions import TEXT_MATH, TEXT_REVISIONS, older_store
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


async def more(docs: DocumentStore, reviews: ReviewStore, made: dict[str, Any]) -> None:
    """A third revision of the comment and of the explanation, with their checks run."""
    edit_comment(docs, made["doc"], made["entry"], f"third {BAD}", op="edit-3")
    edit_response(reviews, made["finding"], made["decision"], f"third {BAD}", op="explain-3")
    await drain(reviews, ScriptedChecker())


def shape(found: dict[str, Any]) -> dict[str, Any]:
    """The answers without the IDs a new check or finding draws at random."""
    return {
        # The entry that recorded a revision is drawn at random too.
        "comment": {k: v for k, v in found["comment"].items() if k != "history_entry"},
        "explanations": found["explanations"],
        "checks": [
            (c["kind"], c["revision"], c["state"], c["rejected"], c["unavailable"])
            for c in found["checks"]["checks"]
        ],
        "queue": found["checks"]["queue"],
        "findings": sorted(
            (f["origin"], f["status"], f["revision"], f["statement"])
            for f in found["findings"]["findings"]
        ),
        "notices": len(found["notices"]),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [TEXT_REVISIONS - 1, TEXT_MATH - 1])
async def test_texts_from_an_older_version_answer_and_are_checked_as_before(
    stores, tmp_path, version
):
    """A store copied back to version 14 or 15 answers as before after migrating, and
    its texts' next revisions are queued and checked as the original store's are."""
    docs, reviews, clock = stores
    made = await history(docs, reviews)
    before = answers(docs, reviews, made)
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, version)
    copied = DocumentStore(old)
    try:
        copied.initialize()  # the writer brings the store to the current version
        moved = ReviewStore(copied, clock=lambda: clock[0])
        assert answers(copied, moved, made) == before
        await more(copied, moved, made)
        went_on = shape(answers(copied, moved, made))
    finally:
        copied.close()
    await more(docs, reviews, made)
    assert went_on == shape(answers(docs, reviews, made))
    assert len(went_on["checks"]) == len(before["checks"]["checks"]) + 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper", "refusal"),
    [
        (
            "DELETE FROM comment_math_sources",
            r"math check math-[0-9a-f]{24} of comment entry cmt-[0-9a-f]{24} has no math queue",
        ),
        (
            "DELETE FROM response_math_sources",
            r"math check math-[0-9a-f]{24} of explanation response:finding-[0-9a-f]{24}:r\d+:"
            r"explanation has no math queue",
        ),
    ],
)
async def test_a_text_check_without_its_queue_is_refused(stores, tmp_path, tamper, refusal):
    """The refusal names the check and its text, not tables the refused store lacks."""
    docs, reviews, _ = stores
    await history(docs, reviews)
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, TEXT_MATH - 1)
    with sqlite3.connect(old) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(tamper)
    copied = DocumentStore(old)
    try:
        with pytest.raises(ControlError, match=r"stays at version 15.*" + refusal):
            copied.initialize()
    finally:
        copied.close()


@pytest.mark.asyncio
async def test_an_explanation_decision_has_one_math_queue_row(stores):
    docs, reviews, _ = stores
    await history(docs, reviews)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), docs.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO text_math_sources(subject_type,subject_id,project,session,parent,"
            "parent_revision,target) SELECT subject_type,subject_id||'-again',project,session,"
            "parent,parent_revision,target FROM text_math_sources WHERE subject_type='response'"
        )


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
