# SPDX-License-Identifier: MIT
"""Exact immutable comment math, ordinary remediation and shared closing delivery."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch as mock_patch

import pytest

from sandbox_science.orchestrator.consistency.mathnotices import MathNotices
from sandbox_science.orchestrator.documents import USER, DocumentError, comment_bodies
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.documents.mathjobs import MathJobs

from .test_document_math import BAD, MAIN, ScriptedChecker, create, drain, get
from .test_document_math import stores as stores


def comment(docs, doc, body=BAD, author=MAIN, op="comment"):
    mapped = docs.mapped("p", doc)
    return Annotations(docs).change(
        "p",
        doc,
        {
            "action": "create",
            "anchor": {"revision": 1, "block": mapped.block_ids[0]},
            "body": body,
            "operation_id": op,
        },
        author,
    )


def edit(docs, doc, entry, body, author=MAIN, op="edit"):
    api = Annotations(docs)
    source = api.body("p", doc, entry)
    return api.change(
        "p",
        doc,
        {
            "action": "edit_body",
            "thread": source["thread"],
            "expected": source["expected"],
            "entry": entry,
            "base": source["revision"],
            "sha256": source["sha256"],
            "body": body,
            "operation_id": op,
        },
        author,
    )


@pytest.mark.asyncio
async def test_comment_rejection_remediation_and_removal(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Document context.")
    source = comment(docs, doc)
    checker = ScriptedChecker()
    await drain(reviews, checker)
    (found,) = get(reviews)
    anchor = found["anchors"][0]
    assert anchor["locator"] == {
        "document": doc,
        "revision": 1,
        "comment": source["entry_id"],
        "body_revision": 1,
    }
    assert found["origin"] == "math"
    assert found["severity"] == "warn"
    assert len(MathNotices(reviews).pending("p", "s", "r1", set())) == 1
    edit(docs, doc, source["entry_id"], r"$\unknown{y}$")
    await drain(reviews, checker)
    assert get(reviews)[0]["reflags"] == 1
    edit(docs, doc, source["entry_id"], "", op="remove")
    await drain(reviews, checker)
    assert get(reviews)[0]["status"] == "confirmed fixed"
    history = Annotations(docs).read_thread("p", doc, source["thread_id"])
    assert history["entries"][0]["body"] == BAD
    assert [e["kind"] for e in history["entries"]] == ["create", "edit_body", "edit_body"]


@pytest.mark.asyncio
async def test_reply_ownership_and_unrelated_edits_do_not_reflag(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Document context.")
    original = comment(docs, doc, "USER opening", USER)
    delegated = MAIN.model_copy(update={"run_id": "delegate", "role": "delegated"})
    reply = Annotations(docs).change(
        "p",
        doc,
        {
            "action": "reply",
            "thread": original["thread_id"],
            "expected": 1,
            "body": BAD,
            "operation_id": "reply",
        },
        delegated,
    )
    await drain(reviews, ScriptedChecker())
    (found,) = get(reviews)
    assert found["source"]["owner_revision"] == 1
    edit(docs, doc, original["entry_id"], "USER unrelated edit", USER)
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 0
    edit(docs, doc, reply["entry_id"], "$x$", MAIN, op="main-corrects")
    await drain(reviews, ScriptedChecker("accepted"))
    assert get(reviews)[0]["status"] == "confirmed fixed"
    current = Annotations(docs).body("p", doc, reply["entry_id"])
    assert current["original_author"]["run_id"] == "delegate"
    assert current["author"]["run_id"] == "r1"


def test_edit_authority_cas_replay_and_requests(stores):
    docs, _, _ = stores
    doc = create(docs, USER, "Document context.")
    source = comment(docs, doc)
    for actor in (
        USER,
        MAIN.model_copy(update={"session_id": "other"}),
        MAIN.model_copy(update={"role": "reviewer"}),
        MAIN.model_copy(update={"role": "delegated", "run_id": "foreign"}),
    ):
        with pytest.raises(DocumentError, match="cannot revise"):
            edit(docs, doc, source["entry_id"], "changed", actor)
    api = Annotations(docs)
    base = api.body("p", doc, source["entry_id"])
    payload = {
        "action": "edit_body",
        "thread": base["thread"],
        "expected": 1,
        "entry": source["entry_id"],
        "base": 1,
        "sha256": base["sha256"],
        "body": "$x$",
        "operation_id": "stable",
    }
    assert api.change("p", doc, payload, MAIN)["revision"] == 2
    assert api.change("p", doc, payload, MAIN)["replayed"] is True
    with pytest.raises(DocumentError, match="body changed"):
        api.change("p", doc, {**payload, "operation_id": "stale"}, MAIN)
    user = comment(docs, doc, "original request", USER, "user")
    # Work requests remain pinned to the original immutable entry.
    with docs._write() as db:
        db.execute(
            "INSERT INTO "
            "annotation_requests(request_id,project_id,session_id,thread_id,entry_id,state) "
            "VALUES('req-000000000000000000000001','p','s',?,?,'queued')",
            (user["thread_id"], user["entry_id"]),
        )
    edit(docs, doc, user["entry_id"], "corrected displayed body", USER, op="user-edit")
    assert api.pending("p", "s", "run", seen=set())[0]["request_text"] == "original request"


@pytest.mark.asyncio
async def test_stale_unavailable_retry_and_macro_context(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Document context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["status"] == "open"
    jobs = MathJobs(reviews)
    jobs.retry_unavailable("p", {"changed": "configuration"})
    check = jobs.claim("p", "retry")
    assert check is not None
    # The old head result cannot close a newer body.
    edit(docs, doc, source["entry_id"], r"$\unknown{new}$")
    while not await jobs.step(check, "retry", ScriptedChecker("accepted")):
        pass
    assert get(reviews)[0]["status"] == "open"
    await drain(reviews, ScriptedChecker("unavailable"))
    assert MathNotices(reviews).pending("p", "s", "run", set()) == []
    jobs.retry_unavailable("p", ScriptedChecker().identity())
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "---\nformula: " + BAD + "\n---\n",
        "::: lemma\n" + BAD + "\n:::\n",
        r"Inline \(\unknown{x}\).",
        "``code $ignored$`` " + BAD,
    ],
)
async def test_ordinary_markdown_matches_display_and_cannot_false_remove(stores, text):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    edit(docs, doc, source["entry_id"], text)
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert any(r"\unknown" in f.tex for f in checker.calls)
    assert any(f["status"] in ("open", "unaddressed") for f in get(reviews))


@pytest.mark.asyncio
async def test_per_block_owner_and_delegated_current_text_boundary(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    delegated = MAIN.model_copy(update={"role": "delegated", "run_id": "child"})
    source = comment(docs, doc, BAD + "\n\nOriginal paragraph.", delegated)
    edit(docs, doc, source["entry_id"], BAD + "\n\nMAIN changed paragraph.")
    await drain(reviews, ScriptedChecker())
    (found,) = get(reviews)
    observation = MathJobs(reviews).evidence("p", found["source"]["check"])["observations"][0]
    assert observation["owner"]["author"]["run_id"] == "child"
    assert observation["owner"]["revision"] == 1
    with pytest.raises(DocumentError, match="cannot revise"):
        edit(docs, doc, source["entry_id"], "overwrite MAIN", delegated, op="delegate-overwrites")
    for actor in (delegated, delegated.model_copy(update={"run_id": "other-child"})):
        current = Annotations(docs).body("p", doc, source["entry_id"])
        with pytest.raises(DocumentError, match="cannot revise"):
            Annotations(docs).change(
                "p",
                doc,
                {
                    "action": "edit",
                    "thread": current["thread"],
                    "expected": current["expected"],
                    "body": "legacy bypass",
                    "operation_id": f"legacy-{actor.run_id}",
                },
                actor,
            )


@pytest.mark.asyncio
async def test_macro_revision_pins_all_spans_and_stale_attempt(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "---\nmacros:\n  '\\foo': 'x'\n---\n\nContext.")
    source = comment(docs, doc, r"$\foo$ and $y$.")
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert [f.macros for f in checker.calls] == [{r"\foo": "x"}] * 2
    jobs = MathJobs(reviews)
    jobs.retry_unavailable("p", {"changed": "identity"})
    pending = jobs.claim("p", "old-context")
    assert pending is not None
    docs.edit_source(
        "p",
        doc,
        base=1,
        text="---\nmacros:\n  '\\foo': 'z'\n---\n\nContext.",
        author=USER,
        reason="Macro edit",
        operation_id="macros",
    )
    while not await jobs.step(pending, "old-context", ScriptedChecker("accepted")):
        pass
    assert all(f["status"] == "open" for f in get(reviews))
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert [f.macros for f in checker.calls] == [{r"\foo": "z"}] * 2
    assert all(f["reflags"] == 1 for f in get(reviews))
    assert all(f["anchors"][0]["locator"]["revision"] == 2 for f in get(reviews))
    assert Annotations(docs).body("p", doc, source["entry_id"])["revision"] == 1


@pytest.mark.asyncio
async def test_legacy_writer_and_unknown_identity_never_veto_edit_or_prove_removal(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    api = Annotations(docs)
    await drain(reviews, ScriptedChecker())
    with mock_patch.object(comment_bodies, "commit"):
        api.change(
            "p",
            doc,
            {
                "action": "edit",
                "thread": source["thread_id"],
                "expected": 1,
                "body": BAD,
                "operation_id": "older-writer",
            },
            MAIN,
        )
    assert MathNotices(reviews).pending("p", "s", "run", set()) == []
    jobs = MathJobs(reviews)
    assert jobs.claim("p", "held") is None
    assert any("Legacy writer" in (r["blocked"] or "") for r in jobs.listing("p")["queue"])
    api.change(
        "p",
        doc,
        {
            "action": "edit",
            "thread": source["thread_id"],
            "expected": 2,
            "body": BAD,
            "operation_id": "modern-edit",
        },
        MAIN,
    )
    await drain(reviews, ScriptedChecker())
    (found,) = get(reviews)
    assert found["status"] == "open"
    assert found["reflags"] == 0
    assert MathNotices(reviews).pending("p", "s", "run", set()) == []


def test_atomic_revision_scheduling_and_append_only_history(stores):
    docs, _, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    with docs._write() as db:
        db.execute(
            "CREATE TEMP TRIGGER fail_annotation_receipt BEFORE INSERT ON annotation_operations "
            "BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic receipt"):
        edit(docs, doc, source["entry_id"], "changed")
    assert Annotations(docs).body("p", doc, source["entry_id"])["revision"] == 1
    with docs._read() as db:
        assert db.execute("SELECT target FROM comment_math_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM annotation_entries").fetchone()[0] == 1
    with docs._write() as db:
        for sql in (
            "UPDATE annotation_body_revisions SET body='changed'",
            "DELETE FROM annotation_body_revisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)


@pytest.mark.asyncio
async def test_shared_job_quota_holds_math_but_not_comment_writes(stores):
    from sandbox_science.orchestrator.documents.mathjobs import MathLimits

    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    jobs = MathJobs(reviews, limits=MathLimits(jobs=1))
    check = jobs.claim("p", "owner")
    assert check is not None
    assert await jobs.step(check, "owner", ScriptedChecker())
    assert jobs.claim("p", "owner") is None
    assert "quota" in jobs.listing("p")["queue"][0]["blocked"]
    edit(docs, doc, source["entry_id"], "Still writable")
    assert Annotations(docs).body("p", doc, source["entry_id"])["revision"] == 2


@pytest.mark.asyncio
async def test_comment_closing_stage_rechecks_body_and_new_claim(stores):
    docs, reviews, clock = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    notices = MathNotices(reviews)
    notice = notices.pending("p", "s", "failed-save", set())[0]
    ids = {notice["notice_id"]}
    notices.bound("p", "s", "failed-save", ids)
    assert get(reviews)[0]["status"] == "open"
    clock[0] += 121
    assert notices.pending("p", "s", "next-saved", set())[0]["notice_id"] in ids
    notices.bound("p", "s", "next-saved", ids)
    notices.acknowledge("p", "s", "failed-save", list(ids))
    assert get(reviews)[0]["status"] == "open"
    edit(docs, doc, source["entry_id"], "$x$", op="new-body-before-ack")
    notices.acknowledge("p", "s", "next-saved", list(ids))
    assert get(reviews)[0]["status"] == "open"
    await drain(reviews, ScriptedChecker("accepted"))
    assert get(reviews)[0]["status"] == "confirmed fixed"


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["p", "q"])
async def test_derived_source_cannot_borrow_another_canonical_parent(stores, project):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Private context.")
    source = comment(docs, doc)
    other = docs.create(
        project, name="other", title="Other", text="Other context.", author=USER
    ).document_id
    await drain(reviews, ScriptedChecker())
    jobs = MathJobs(reviews)
    old = next(r["check_id"] for r in jobs.listing("p")["checks"] if r.get("kind") == "comment")
    original = jobs.job(old)
    with pytest.raises(DocumentError, match="canonical parent"):
        jobs.load({**original, "document": other, "project": project})
    with docs._write() as db:
        db.execute(
            "UPDATE comment_math_sources SET document=?,project=? WHERE entry_id=?",
            (other, project, source["entry_id"]),
        )
    # Scope failure occurs before admission, parsing, rendering or a foreign listing.
    with pytest.raises(DocumentError, match="canonical parent"):
        jobs.claim(project, "foreign")
    with pytest.raises(DocumentError, match="canonical parent"):
        jobs.listing(project)
    with pytest.raises(DocumentError, match="scoped comment body"):
        Annotations(docs).body(project, other, source["entry_id"])
    assert Annotations(docs).body("p", doc, source["entry_id"])["body"] == BAD
    with docs._write() as db:
        db.execute("UPDATE math_jobs SET document=? WHERE check_id=?", (other, old))
    with pytest.raises(DocumentError, match="identity exceeds"):
        jobs.job(old)
    assert MathNotices(reviews).pending("p", "s", "wrong-parent", set()) == []


@pytest.mark.asyncio
async def test_parser_bounds_hold_checks_without_vetoing_canonical_saves(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    deep = "> " * 40 + BAD
    # Both new source and existing-body corrections remain legal ordinary writes.
    user = comment(docs, doc, deep, USER, "deep-user")
    edit(docs, doc, source["entry_id"], deep, op="deep-agent")
    assert Annotations(docs).body("p", doc, source["entry_id"])["body"] == deep
    assert "held" in Annotations(docs).body("p", doc, user["entry_id"])
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    assert checker.calls == []
    assert get(reviews)[0]["status"] == "open"
    assert MathNotices(reviews).pending("p", "s", "held", set()) == []
    assert any(
        "Derived body identity" in (r["blocked"] or "")
        for r in MathJobs(reviews).listing("p")["queue"]
    )
    # Regaining a parseable baseline proves neither old removal nor ownership of
    # unchanged text whose identity was unavailable.
    edit(docs, doc, source["entry_id"], BAD, op="baseline")
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["status"] == "open"
    assert get(reviews)[0]["reflags"] == 0


@pytest.mark.asyncio
async def test_blocked_projection_does_not_materialize_corrupt_reason(stores):
    import tracemalloc

    from sandbox_science.orchestrator.documents.mathcontroller import MathController

    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    jobs = MathJobs(reviews)
    check = next(r["check_id"] for r in jobs.listing("p")["checks"] if r.get("kind") == "comment")
    with docs._write() as db:
        db.execute(
            "UPDATE comment_math_sources SET blocked=? WHERE entry_id=?",
            ("x" * (20 * 1024 * 1024), source["entry_id"]),
        )
    tracemalloc.start()
    try:
        assert MathController(reviews, "p", "s", "bound", checker=None).blocked(check)
        assert tracemalloc.get_traced_memory()[1] < 100_000
    finally:
        tracemalloc.stop()
