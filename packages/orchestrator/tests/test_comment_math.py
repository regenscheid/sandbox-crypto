# SPDX-License-Identifier: MIT
"""Exact immutable comment math, ordinary remediation and shared closing delivery."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal
from unittest.mock import patch as mock_patch

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError, comment_bodies
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.mathchecks.notices import MathNotices

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
    with docs.control.write() as tx:
        db = tx.sql
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
    assert all(f["reflags"] == 0 for f in get(reviews))
    assert all(f["anchors"][0]["locator"]["revision"] == 2 for f in get(reviews))
    assert Annotations(docs).body("p", doc, source["entry_id"])["revision"] == 1


class ChangedChecker(ScriptedChecker):
    def __init__(
        self, generation, status: Literal["accepted", "rejected", "unavailable"] = "rejected"
    ):
        super().__init__(status, error=f"Configuration {generation} rejected the formula")
        self.bundle = str(generation) * 64

    def identity(self):
        return {**super().identity(), "bundle_sha256": self.bundle}

    async def check_many(self, formulas):
        return [
            answer.model_copy(update={"bundle_sha256": self.bundle})
            for answer in await super().check_many(formulas)
        ]


def source_fixture(docs, kind):
    text = "---\nmacros:\n  '\\foo': 'x'\n---\n\n"
    doc = create(
        docs, USER if kind == "comment" else MAIN, text + ("Context." if kind == "comment" else BAD)
    )
    entry = comment(docs, doc)["entry_id"] if kind == "comment" else None
    return doc, entry


def edit_formula(docs, doc, entry, tex, operation):
    if entry:
        edit(docs, doc, entry, f"${tex}$", op=operation)
    else:
        from .test_document_math import patch

        patch(docs, doc, tex, operation)


def change_macro(docs, doc, value, operation):
    head = docs.document("p", doc).head
    text = docs.revision("p", doc).text
    result = docs.edit_source(
        "p",
        doc,
        base=head,
        text=text.replace("'x'", f"'{value}'"),
        author=USER,
        reason="Unrelated macro dependency",
        operation_id=operation,
    )
    assert result.outcome == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "comment"])
async def test_dependency_refresh_preserves_failed_block_budget(stores, kind):
    docs, reviews, _ = stores
    doc, entry = source_fixture(docs, kind)
    await drain(reviews, ScriptedChecker())
    for index, value in enumerate(("y", "z"), 1):
        head = docs.document("p", doc).head
        # Replace either prior dependency value without changing the formula block.
        text = docs.revision("p", doc).text
        result = docs.edit_source(
            "p",
            doc,
            base=head,
            text=text.replace("'x'", f"'{value}'").replace("'y'", f"'{value}'"),
            author=USER,
            reason="Macro dependency",
            operation_id=f"macro-{index}",
        )
        assert result.outcome == "applied"
        await drain(reviews, ScriptedChecker(error=f"Macro {value} error"))
        found = get(reviews)[0]
        assert (found["status"], found["reflags"], found["source"]["owner_revision"]) == (
            "open",
            0,
            1,
        )
        assert f"Macro {value} error" in found["statement"]
        assert found["anchors"][0]["locator"]["revision"] == head + 1
    for generation in (1, 2):
        checker = ChangedChecker(generation)
        MathJobs(reviews).retry_unavailable("p", checker.identity())
        await drain(reviews, checker)
        assert get(reviews)[0]["reflags"] == 0
        assert f"Configuration {generation}" in get(reviews)[0]["statement"]
    # Actual failed author block edits still consume exactly two opportunities.
    for index in (1, 2):
        edit_formula(docs, doc, entry, rf"\unknown{{edit{index}}}", f"body-{index}")
        await drain(reviews, ChangedChecker(2))
        assert get(reviews)[0]["reflags"] == index
    assert get(reviews)[0]["status"] == "unaddressed"
    checker = ChangedChecker(3)
    MathJobs(reviews).retry_unavailable("p", checker.identity())
    await drain(reviews, checker)
    assert (get(reviews)[0]["status"], get(reviews)[0]["reflags"]) == ("unaddressed", 2)
    from sandbox_science.orchestrator.consistency.findings import Findings

    history = Findings(reviews).get("p", "s", get(reviews)[0]["finding_id"])["history"]
    assert sum(h["payload"]["action"] == "mechanical_reflag" for h in history) == 2
    assert history[-1]["payload"]["action"] == "mechanical_dependency_refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "comment"])
@pytest.mark.parametrize("legacy_cache", [False, True])
async def test_accepted_owner_and_legacy_cache_do_not_make_dependency_a_failed_edit(
    stores, kind, legacy_cache
):
    docs, reviews, _ = stores
    doc, entry = source_fixture(docs, kind)
    await drain(reviews, ScriptedChecker())
    edit_formula(docs, doc, entry, "x", "accepted-edit")
    await drain(reviews, ScriptedChecker("accepted"))
    found = get(reviews)[0]
    assert found["status"] == "confirmed fixed"
    assert found["source"]["owner_revision"] == 2
    if legacy_cache:
        # Faithful old-code state: accepted observation/check pointer was saved,
        # but the mutable cached owner/identity still described the rejection.
        with docs.control.write() as tx:
            db = tx.sql
            db.execute("UPDATE math_finding_state SET owner_revision=1,input_sha256=?", ("f" * 64,))
    # An intervening unavailable retry cannot hide the prior available proof.
    MathJobs(reviews).retry_unavailable("p", {"operator": "unavailable"})
    await drain(reviews, ScriptedChecker("unavailable"))
    checker = ChangedChecker(1)
    MathJobs(reviews).retry_unavailable("p", checker.identity())
    await drain(reviews, checker)
    found = get(reviews)[0]
    assert (found["status"], found["reflags"], found["source"]["owner_revision"]) == ("open", 0, 2)
    edit_formula(docs, doc, entry, r"\unknown{failed-edit}", "real-failed-edit")
    await drain(reviews, checker)
    assert get(reviews)[0]["reflags"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "comment"])
@pytest.mark.parametrize(
    ("action", "status"), [("confirm", "confirmed fixed"), ("dismiss", "withdrawn")]
)
async def test_dependency_refresh_preserves_explicit_user_decision(stores, kind, action, status):
    from sandbox_science.orchestrator.consistency.findings import Findings

    docs, reviews, _ = stores
    doc, _ = source_fixture(docs, kind)
    await drain(reviews, ScriptedChecker())
    found = get(reviews)[0]
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": found["finding_id"],
            "expected": found["revision"],
            "action": action,
            "explanation": "Explicit USER decision",
            "operation_id": "decision",
        },
        USER,
    )
    change_macro(docs, doc, "z", "macro")
    await drain(reviews, ScriptedChecker(error="New dependency error"))
    for generation in (1, 2):
        checker = ChangedChecker(generation)
        MathJobs(reviews).retry_unavailable("p", checker.identity())
        await drain(reviews, checker)
        current = get(reviews)[0]
        assert (current["status"], current["reflags"]) == (status, 0)
        assert f"Configuration {generation}" in current["statement"]
        assert MathNotices(reviews).pending("p", "s", "closing", set()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"owner": {"revision": 1}}),
        json.dumps({"owner": {"revision": 1, "author": {"kind": "agent"}}}),
        json.dumps({"owner": {"revision": 2, "author": MAIN.model_dump(mode="json")}}),
    ],
)
async def test_previous_assessment_and_user_decision_of_another_shape_are_not_used(stores, payload):
    from sandbox_science.orchestrator.mathchecks.mechanical import MechanicalFindings

    docs, reviews, _ = stores
    doc, entry = source_fixture(docs, "comment")
    await drain(reviews, ScriptedChecker())
    found = get(reviews)[0]
    edit_formula(docs, doc, entry, "x", "new-source")
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "owner")
    assert check
    job = jobs.job(check)
    with docs.control.write() as tx:
        db = tx.sql
        mechanism = MechanicalFindings(reviews)
        anchor = found["anchors"][0]
        item = {"block": anchor["block"], "span": anchor["math"]}
        assert mechanism._previous_owner(db, job, item) == 1
        db.execute("DROP TRIGGER math_observations_update")
        db.execute("UPDATE math_observations SET payload=?", (payload,))
        # Only the new scalar decision path is tested; ordinary full history reads
        # deliberately retain their separate existing contracts.
        db.execute("DROP TRIGGER review_transitions_update")
        db.execute("UPDATE review_transitions SET payload=?", (payload,))
        assert mechanism._previous_owner(db, job, item) is None
        assert mechanism._user_resolved(db, {**found, "status": "confirmed fixed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["{broken", "x" * 40_000])
async def test_an_invalid_or_oversized_observation_is_refused_when_written(stores, payload):
    docs, reviews, _ = stores
    source_fixture(docs, "comment")
    await drain(reviews, ScriptedChecker())
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, docs.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO math_observations SELECT check_id,ordinal+1000,block,span,"
            "input_sha256,status,?,bytes FROM math_observations LIMIT 1",
            (payload,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["unknown", "x" * 20_000_000, "platform:math\0unknown"])
async def test_unknown_retained_resolution_actor_is_bounded_and_preserved(stores, actor):
    import tracemalloc

    from sandbox_science.orchestrator.mathchecks.mechanical import MechanicalFindings

    docs, reviews, _ = stores
    source_fixture(docs, "comment")
    await drain(reviews, ScriptedChecker())
    found = get(reviews)[0]
    with docs.control.write() as tx:
        db = tx.sql
        db.execute("DROP TRIGGER review_transitions_update")
        db.execute("UPDATE review_transitions SET actor=?", (actor,))
        tracemalloc.start()
        try:
            assert MechanicalFindings(reviews)._user_resolved(
                db, {**found, "status": "confirmed fixed"}
            )
            assert tracemalloc.get_traced_memory()[1] < 100_000
        finally:
            tracemalloc.stop()


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
    with docs.control.write() as tx:
        db = tx.sql
        db.execute(
            "CREATE TEMP TRIGGER fail_annotation_receipt BEFORE INSERT ON operation_receipts "
            "WHEN NEW.scope='comment' BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic receipt"):
        edit(docs, doc, source["entry_id"], "changed")
    assert Annotations(docs).body("p", doc, source["entry_id"])["revision"] == 1
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT target FROM comment_math_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM annotation_entries").fetchone()[0] == 1
    with docs.control.write() as tx:
        db = tx.sql
        for sql in (
            "UPDATE annotation_body_revisions SET body='changed'",
            "DELETE FROM annotation_body_revisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)


@pytest.mark.asyncio
async def test_shared_job_quota_holds_math_but_not_comment_writes(stores):
    from sandbox_science.orchestrator.mathchecks.jobs import MathLimits

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
    with docs.control.write() as tx:
        db = tx.sql
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
    with docs.control.write() as tx:
        db = tx.sql
        db.execute("UPDATE math_jobs SET document=? WHERE check_id=?", (other, old))
    with pytest.raises(DocumentError, match="does not bind its comment body"):
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
async def test_an_oversized_blocked_reason_is_refused_when_written(stores):
    docs, reviews, _ = stores
    doc = create(docs, USER, "Context.")
    source = comment(docs, doc)
    await drain(reviews, ScriptedChecker())
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), docs.control.write() as tx:
        tx.sql.execute(
            "UPDATE comment_math_sources SET blocked=? WHERE entry_id=?",
            ("x" * 5000, source["entry_id"]),
        )


def test_a_comment_at_its_byte_limit_is_kept(stores) -> None:
    docs, _, _ = stores
    doc = create(docs)
    body = "\u00e9" * 8192  # 8192 characters, 16384 bytes: the model's and the table's limits
    made = comment(docs, doc, body=body)
    assert Annotations(docs).body("p", doc, made["entry_id"])["body"] == body


@pytest.mark.asyncio
async def test_only_a_comments_current_body_is_rendered(stores) -> None:
    docs, reviews, _ = stores
    doc = create(docs, USER, "Document context.")
    entry = comment(docs, doc, body="First $a$.")["entry_id"]
    for n in range(3):
        edit(docs, doc, entry, f"Body {n} $b_{n}$.", op=f"edit-{n}")
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    assert [formula.tex for formula in checker.calls] == ["b_2"]
