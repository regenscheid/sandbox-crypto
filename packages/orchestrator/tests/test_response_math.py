# SPDX-License-Identifier: MIT
"""Finding explanation sources: ordinary edits, independent decisions and math."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.mathnotices import MathNotices
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.documents import USER, Author, DocumentError
from sandbox_science.orchestrator.documents.mathjobs import MathJobs, MathLimits

from .test_consistency import anchor, claim, dispatch, raise_finding, read_confirmation
from .test_document_math import BAD, MAIN, ScriptedChecker, create, drain
from .test_document_math import stores as stores


def response(reviews, *, body=BAD, action="rebut", actor=MAIN):
    claim(reviews)
    review = dispatch(reviews)
    found = raise_finding(reviews, review, statement=r"Reviewer $\unknown{excluded}$")
    reviews.finish("p", "s", review, "owner", "findings", "Synthetic original review")
    api = Findings(reviews)
    payload = {
        "finding": found["finding_id"],
        "expected": 1,
        "action": action,
        "explanation": body,
        "operation_id": "response",
    }
    if action == "fix":
        payload["revisions"] = [(create(reviews.documents, USER, "Evidence."), 1)]
    changed = api.respond("p", "s", payload, actor)
    return found["finding_id"], changed["revision"]


def edit(reviews, finding, decision, body, *, op="edit", actor=MAIN):
    api = ResponseBodies(reviews)
    source = api.get("p", "s", finding, decision)
    payload = {
        "finding": finding,
        "response_revision": decision,
        "expected": Findings(reviews).get("p", "s", finding)["revision"],
        "base": source["revision"],
        "sha256": source["sha256"],
        "body": body,
        "operation_id": op,
    }
    return api.edit("p", "s", payload, actor)


def maths(reviews):
    return [f for f in Findings(reviews).listing("p", "s")["findings"] if f["origin"] == "math"]


@pytest.mark.asyncio
async def test_main_explanation_exact_source_remediation_and_original_audit(stores):
    docs, reviews, _ = stores
    finding, decision = response(reviews)
    before = Findings(reviews).get("p", "s", finding)
    await drain(reviews, ScriptedChecker())
    (child,) = maths(reviews)
    assert child["anchors"][0]["locator"] == {
        "finding": finding,
        "response_revision": decision,
        "field": "explanation",
        "body_revision": 1,
    }
    check = MathJobs(reviews).job(child["math_check_id"])
    assert check["document"] is None
    assert check["response"]["finding"] == finding
    assert len(MathNotices(reviews).pending("p", "s", "run", set())) == 1
    changed = edit(
        reviews, finding, decision, "$x$", actor=MAIN.model_copy(update={"run_id": "corrector"})
    )
    assert changed["original_author"]["run_id"] == "r1"
    assert changed["author"]["run_id"] == "corrector"
    await drain(reviews, ScriptedChecker("accepted"))
    assert maths(reviews)[0]["status"] == "confirmed fixed"
    after = Findings(reviews).get("p", "s", finding)
    assert (after["status"], after["revision"], after["reflags"]) == (
        before["status"],
        before["revision"],
        before["reflags"],
    )
    original = after["history"][-1]
    assert original["reason"] == BAD
    assert original["payload"]["explanation"] == BAD
    assert original["payload"]["action"] == "rebut"
    assert original["response_body"]["body"] == "$x$"
    assert ResponseBodies(reviews).get("p", "s", finding, decision, body_revision=1)["body"] == BAD
    with docs._read() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_positive_authority_cas_replay_and_parent_decision_independence(stores):
    _, reviews, _ = stores
    finding, decision = response(reviews)
    api = ResponseBodies(reviews)
    for actor in (
        USER,
        MAIN.model_copy(update={"role": "delegated"}),
        MAIN.model_copy(update={"role": "reviewer"}),
        MAIN.model_copy(update={"session_id": "other"}),
        Author(kind="platform", service="math"),
    ):
        with pytest.raises(DocumentError, match="MAIN"):
            edit(reviews, finding, decision, "$y$", actor=actor)
    source = api.get("p", "s", finding, decision)
    payload = {
        "finding": finding,
        "response_revision": decision,
        "expected": decision,
        "base": 1,
        "sha256": source["sha256"],
        "body": "$y$",
        "operation_id": "edit",
    }
    result = api.edit("p", "s", payload, MAIN)
    replay = api.edit("p", "s", payload, MAIN.model_copy(update={"run_id": "retry-run"}))
    assert replay.pop("replayed") is True
    assert replay == result
    with pytest.raises(DocumentError):
        api.edit("p", "s", {**payload, "body": "different"}, MAIN)
    with pytest.raises(DocumentError, match="changed"):
        api.edit("p", "s", {**payload, "operation_id": "stale"}, MAIN)
    with pytest.raises(DocumentError):
        api.get("p", "other", finding, decision)
    user = Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": decision,
            "action": "dismiss",
            "explanation": "Explicit USER decision.",
            "operation_id": "user",
        },
        USER,
    )
    assert user["status"] == "withdrawn"
    assert ResponseBodies(reviews).get("p", "s", finding, decision)["revision"] == 2


@pytest.mark.asyncio
async def test_confirmation_pins_body_not_just_scientific_revision(stores):
    _, reviews, _ = stores
    finding, decision = response(reviews, action="fix")
    confirm = dispatch(reviews, "confirmation")
    read_confirmation(reviews, confirm)
    immutable = reviews.manifest("p", "s", confirm)["evidence"]
    edit(reviews, finding, decision, "$x$")
    with pytest.raises(DocumentError, match="explanation changed"):
        Findings(reviews).confirm(
            "p",
            "s",
            confirm,
            "owner",
            {
                "finding": finding,
                "expected": decision,
                "decision": "confirm",
                "explanation": "Old snapshot",
                "anchors": [anchor(reviews, confirm, "finding")],
                "operation_id": "old",
            },
        )
    assert reviews.manifest("p", "s", confirm)["evidence"] == immutable
    reviews.finish("p", "s", confirm, "owner", "inconclusive", "Old body was changed")
    assert reviews.confirmation_targets("p", "s") == [(finding, decision)]
    fresh = dispatch(reviews, "confirmation")
    read_confirmation(reviews, fresh)
    result = Findings(reviews).confirm(
        "p",
        "s",
        fresh,
        "owner",
        {
            "finding": finding,
            "expected": decision,
            "decision": "confirm",
            "explanation": "Current pinned correction",
            "anchors": [anchor(reviews, fresh, "finding")],
            "operation_id": "fresh",
        },
    )
    assert result["status"] == "confirmed fixed"


@pytest.mark.asyncio
async def test_reflags_unavailable_removal_empty_macros_and_no_recursive_sources(stores):
    _, reviews, _ = stores
    finding, decision = response(reviews, body=BAD + "\n\nOther block.")
    checker = ScriptedChecker()
    await drain(reviews, checker)
    assert all(f.macros == {} for f in checker.calls)
    edit(reviews, finding, decision, BAD + "\n\nUnrelated edit.")
    await drain(reviews, checker)
    assert maths(reviews)[0]["reflags"] == 0
    for i in range(1, 5):
        edit(reviews, finding, decision, rf"$\unknown{{x{i}}}$", op=f"failed-{i}")
        await drain(reviews, checker)
    assert maths(reviews)[0]["reflags"] == 2
    assert maths(reviews)[0]["status"] == "unaddressed"
    edit(reviews, finding, decision, "$x$", op="unavailable")
    await drain(reviews, None)
    assert maths(reviews)[0]["status"] == "unaddressed"
    edit(reviews, finding, decision, "", op="remove")
    await drain(reviews, checker)
    child = maths(reviews)[0]
    assert child["status"] == "confirmed fixed"
    assert (
        Findings(reviews).get("p", "s", child["finding_id"])["history"][-1]["payload"]["action"]
        == "mechanical_removal"
    )
    with pytest.raises(DocumentError):
        ResponseBodies(reviews).get("p", "s", child["finding_id"], child["revision"])
    with reviews.documents._read() as db:
        assert db.execute("SELECT count(*) FROM response_math_sources").fetchone()[0] == 1


def test_body_queue_receipt_atomicity_and_immutable_rows(stores):
    _, reviews, _ = stores
    finding, decision = response(reviews)
    with reviews.documents._write() as db:
        db.execute(
            "CREATE TRIGGER receipt_failure BEFORE INSERT ON review_operations WHEN "
            "NEW.operation_id='edit' BEGIN SELECT RAISE(ABORT,'receipt fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt fault"):
        edit(reviews, finding, decision, "$x$")
    assert ResponseBodies(reviews).get("p", "s", finding, decision)["revision"] == 1
    with reviews.documents._read() as db:
        assert db.execute("SELECT target FROM response_math_sources").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), reviews.documents._write() as db:
        db.execute("UPDATE review_response_bodies SET body='rewrite'")


@pytest.mark.asyncio
async def test_legacy_proven_original_and_shared_quotas(stores):
    _, reviews, _ = stores
    with patch("sandbox_science.orchestrator.consistency.response_bodies.commit"):
        finding, decision = response(reviews)
    assert ResponseBodies(reviews).get("p", "s", finding, decision)["revision"] == 0
    await drain(reviews, ScriptedChecker())
    assert maths(reviews)[0]["source"]["owner_revision"] == 1
    edit(reviews, finding, decision, "$y$")
    jobs = MathJobs(reviews, limits=MathLimits(jobs=1))
    assert jobs.claim("p", "owner") is None
    assert jobs.listing("p")["queue"][0]["blocked"] == "math job history quota reached"
    assert ResponseBodies(reviews).get("p", "s", finding, decision)["body"] == "$y$"


def test_corrupt_raw_body_bounded_before_parse(stores):
    _, reviews, _ = stores
    finding, decision = response(reviews)
    with reviews.documents._write() as db:
        db.execute("DROP TRIGGER review_response_bodies_update")
        db.execute("UPDATE review_response_bodies SET body=?", ("x" * (20 * 1024 * 1024),))
    with pytest.raises(DocumentError, match="bounds"):
        ResponseBodies(reviews).get("p", "s", finding, decision)


@pytest.mark.asyncio
async def test_unknown_legacy_response_does_not_starve_healthy_sources(stores):
    import json

    from sandbox_science.orchestrator.documents.annotations import Annotations

    docs, reviews, _ = stores
    with patch("sandbox_science.orchestrator.consistency.response_bodies.commit"):
        finding, decision = response(reviews)
    with docs._write() as db:
        db.execute("DROP TRIGGER review_transitions_update")
        raw = json.loads(
            db.execute(
                "SELECT payload FROM review_transitions WHERE finding_id=? AND revision=?",
                (finding, decision),
            ).fetchone()[0]
        )
        del raw["author"]["run_id"]
        db.execute(
            "UPDATE review_transitions SET payload=? WHERE finding_id=? AND revision=?",
            (json.dumps(raw), finding, decision),
        )
    doc = create(docs)
    Annotations(docs).change(
        "p",
        doc,
        {
            "action": "create",
            "anchor": {"revision": 1, "block": "b1"},
            "body": BAD,
            "operation_id": "healthy-comment",
        },
        MAIN,
    )
    await drain(reviews, ScriptedChecker())
    assert len(maths(reviews)) == 2
    assert {j["kind"] for j in MathJobs(reviews).listing("p")["checks"]} == {"document", "comment"}
    assert MathJobs(reviews).listing("p")["queue"][0]["blocked"].startswith("Legacy response")
    assert MathJobs(reviews).claim("p", "owner") is None


def test_source_cache_cannot_rebind_canonical_project_session_or_field(stores):
    docs, reviews, _ = stores
    finding, decision = response(reviews)
    with docs._write() as db:
        db.execute("UPDATE response_math_sources SET project='other'")
    with pytest.raises(DocumentError):
        MathJobs(reviews).claim("other", "owner")
    assert maths(reviews) == []
    with docs._write() as db:
        db.execute("UPDATE response_math_sources SET project='p',session='other'")
    with pytest.raises(DocumentError, match="canonical session"):
        MathJobs(reviews).claim("p", "owner")
    # Reads derive scope from the immutable scientific response, never this cache.
    assert ResponseBodies(reviews).get("p", "s", finding, decision)["body"] == BAD


def test_initial_body_queue_failure_rolls_back_scientific_response(stores):
    docs, reviews, _ = stores
    claim(reviews)
    review = dispatch(reviews)
    found = raise_finding(reviews, review)
    reviews.finish("p", "s", review, "owner", "findings", "Original")
    with docs._write() as db:
        db.execute(
            "CREATE TRIGGER queue_fault BEFORE INSERT ON response_math_sources "
            "BEGIN SELECT RAISE(ABORT,'queue fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="queue fault"):
        Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": found["finding_id"],
                "expected": 1,
                "action": "rebut",
                "explanation": BAD,
                "operation_id": "atomic-response",
            },
            MAIN,
        )
    assert Findings(reviews).get("p", "s", found["finding_id"])["revision"] == 1
    with docs._read() as db:
        assert db.execute("SELECT count(*) FROM review_response_bodies").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM review_operations WHERE operation_id=?", ("atomic-response",)
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_latest_unavailable_and_stale_body_save_ack_cannot_resolve_or_bound(stores):
    _, reviews, clock = stores
    finding, decision = response(reviews)
    await drain(reviews, ScriptedChecker())
    notices = MathNotices(reviews)
    first = notices.pending("p", "s", "failed-save", set())
    ids = {n["notice_id"] for n in first}
    notices.bound("p", "s", "failed-save", ids)
    assert maths(reviews)[0]["status"] == "open"
    clock[0] += 121
    assert notices.pending("p", "s", "next-run", set())
    notices.bound("p", "s", "next-run", ids)
    edit(reviews, finding, decision, "$x$")
    notices.acknowledge("p", "s", "failed-save", list(ids))
    notices.acknowledge("p", "s", "next-run", list(ids))
    assert maths(reviews)[0]["status"] == "open"
    await drain(reviews, None)
    assert notices.pending("p", "s", "later", set()) == []
    assert maths(reviews)[0]["status"] == "open"
    MathJobs(reviews).retry_unavailable("p", ScriptedChecker("accepted").identity())
    await drain(reviews, ScriptedChecker("accepted"))
    assert maths(reviews)[0]["status"] == "confirmed fixed"


def test_populated_source_neutral_job_migration_and_fault_rollback():
    from sandbox_science.orchestrator.documents import mathqueue

    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE documents(document_id TEXT PRIMARY KEY,project_id TEXT,head INTEGER)")
    db.execute("CREATE TABLE review_findings(finding_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO documents VALUES('doc','p',1)")
    db.execute("INSERT INTO review_findings VALUES('finding')")
    for sql in mathqueue.SCHEMA:
        db.execute(sql.replace("document TEXT REFERENCES", "document TEXT NOT NULL REFERENCES"))
    db.execute(
        "INSERT INTO math_jobs(rowid,check_id,document,revision,source_sha256,state,created) "
        "VALUES(42,'check','doc',1,?,'finished',1)",
        ("a" * 64,),
    )
    db.execute(
        "INSERT INTO math_observations VALUES('check',0,'b1','m1',?,'rejected','{}',2)", ("b" * 64,)
    )
    db.execute("INSERT INTO math_job_owners VALUES('check','b1',1,'{}',2)")
    db.execute(
        "INSERT INTO math_finding_state VALUES('finding','doc','b1','m1',1,?,1,'check',NULL)",
        ("c" * 64,),
    )
    db.commit()
    before = db.execute("SELECT rowid,* FROM math_jobs").fetchall()
    # A real failure after rebuilding the parent must roll back its old schema,
    # children and IDs together, rather than publishing a partial migration.
    db.execute("BEGIN")
    with (
        pytest.raises(sqlite3.DatabaseError, match="fault"),
        db,
        patch.object(mathqueue, "SCHEMA", (*mathqueue.SCHEMA, "SELECT fault()")),
    ):
        mathqueue.prepare(db)
    assert db.execute("PRAGMA table_info(math_jobs)").fetchall()[1][3] == 1
    assert db.execute("SELECT rowid,* FROM math_jobs").fetchall() == before
    with db:
        db.execute("BEGIN")
        mathqueue.prepare(db)
    assert db.execute("SELECT rowid,* FROM math_jobs").fetchall() == before
    assert db.execute("PRAGMA table_info(math_jobs)").fetchall()[1][3] == 0
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA defer_foreign_keys").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM math_observations").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM math_job_owners").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError), db:
        db.execute("DELETE FROM math_jobs WHERE check_id='check'")
    db.close()


@pytest.mark.asyncio
async def test_ordinary_tool_uses_trusted_main_context_and_bounded_reads(stores):
    import json

    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.projects import ProjectStore

    docs, reviews, _ = stores
    finding, decision = response(reviews, body="x" * 8192)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=(ProjectStore(docs.path.parent).get("p").sessions["s"].environment_id),
    )
    controller = ReviewController(reviews, context, run_id="tool-run", model=None, alias=None)
    encoded, _ = await controller.author_call(
        "review_response_read",
        {
            "finding": finding,
            "response_revision": decision,
            "start": 100,
            "count": 200,
        },
        "read",
    )
    source = json.loads(encoded)
    assert len(source["body"]) == 200
    assert source["next_start"] == 300
    current = ResponseBodies(reviews).get("p", "s", finding, decision)
    payload = {
        "finding": finding,
        "response_revision": decision,
        "expected": decision,
        "base": 1,
        "sha256": current["sha256"],
        "body": "$x$",
    }
    delegated = ReviewController(
        reviews, context, run_id="child", role="delegated", model=None, alias=None
    )
    with pytest.raises(DocumentError, match="MAIN"):
        await delegated.author_call("review_response_edit", payload, "child-edit")
    with pytest.raises(DocumentError, match="invalid response source edit"):
        await controller.author_call("review_response_edit", {**payload, "author": MAIN}, "bad")
    encoded, _ = await controller.author_call("review_response_edit", payload, "main-edit")
    assert "body" not in json.loads(encoded)
    assert (
        ResponseBodies(reviews).get("p", "s", finding, decision)["author"]["run_id"] == "tool-run"
    )


def test_response_replay_receipt_is_bounded_and_binds_its_immutable_revision(stores):
    import json
    import tracemalloc

    docs, reviews, _ = stores
    finding, decision = response(reviews)
    api = ResponseBodies(reviews)
    initial = api.get("p", "s", finding, decision)
    payload = {
        "finding": finding,
        "response_revision": decision,
        "expected": decision,
        "base": 1,
        "sha256": initial["sha256"],
        "body": "$x$",
        "operation_id": "first",
    }
    expected = api.edit("p", "s", payload, MAIN)
    edit(reviews, finding, decision, "$y$", op="later")
    assert api.edit("p", "s", payload, MAIN)["revision"] == 2
    for result in (
        {**expected, "sha256": "f" * 64},
        {**expected, "revision": 3},
        {**expected, "author": {"kind": "user"}},
        {**expected, "finding": "finding-" + "a" * 24},
    ):
        with docs._write() as db:
            db.execute(
                "UPDATE review_operations SET result=? WHERE operation_id='first'",
                (json.dumps(result),),
            )
        with pytest.raises(DocumentError, match="immutable body"):
            api.edit("p", "s", payload, MAIN)
    with docs._write() as db:
        db.execute(
            "UPDATE review_operations SET result=? WHERE operation_id='first'",
            (json.dumps({**expected, "body": "x" * (20 * 1024 * 1024)}),),
        )
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="bounds"):
            api.edit("p", "s", payload, MAIN)
        assert tracemalloc.get_traced_memory()[1] < 300000
    finally:
        tracemalloc.stop()
    assert api.get("p", "s", finding, decision)["body"] == "$y$"
