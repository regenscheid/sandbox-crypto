# SPDX-License-Identifier: MIT
"""Project settings, exact effective inputs and stale mechanical/confirmation fences."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.control.errors import ControlError
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.documents.project_macros import (
    ProjectMacros,
    resolve,
    response_view,
    view,
)
from sandbox_science.orchestrator.mathchecks import jobs as jobs_module
from sandbox_science.orchestrator.mathchecks.contexts import finding_macro_contexts
from sandbox_science.orchestrator.mathchecks.controller import MathController
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.mathchecks.notices import MathNotices

from .control_versions import EMPTY_SHA, older_store
from .test_comment_math import comment
from .test_consistency import anchor, claim, dispatch, raise_finding, read_confirmation
from .test_document_math import MAIN, ScriptedChecker, create, drain, get, patch
from .test_document_math import stores as stores
from .test_response_math import response


def change(docs, macros, *, expected=None, op=None):
    settings = ProjectMacros(docs)
    revision = settings.get("p")["revision"] if expected is None else expected
    return settings.change(
        "p",
        {"expected": revision, "macros": macros, "operation_id": op or f"macros-{revision}"},
        USER,
    )


def test_positive_user_cas_replay_and_retained_revisions(stores):
    docs, _, _ = stores
    api = ProjectMacros(docs)
    assert api.get("p")["revision"] == 0
    payload = {"expected": 0, "macros": {r"\F": r"\mathbb{F}"}, "operation_id": "first"}
    for author in (
        MAIN,
        Author(kind="platform", service="math"),
        Author(kind="import", user="fixture", import_id="fixture"),
    ):
        with pytest.raises(DocumentError, match="USER"):
            api.change("p", payload, author)
    saved = api.change("p", payload, USER)
    change(docs, {r"\F": "x"})
    assert api.change("p", payload, USER) == {**saved, "replayed": True}
    assert api.get("p", 1)["macros"] == {r"\F": r"\mathbb{F}"}
    with pytest.raises(DocumentError, match="operation ID"):
        api.change("p", {**payload, "macros": {}}, USER)
    with pytest.raises(DocumentError, match="retain the draft"):
        api.change("p", {**payload, "operation_id": "stale"}, USER)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), docs.control.write() as tx:
        tx.sql.execute("UPDATE project_macro_revisions SET definitions='{}'")


@pytest.mark.parametrize(
    "bad",
    [
        {"expected": True, "macros": {}, "operation_id": "x"},
        {"expected": 0, "macros": {}, "operation_id": "x", "author": "USER"},
        {"expected": 0, "macros": {r"\x": 1}, "operation_id": "x"},
        {"expected": 0, "macros": {"x": "y"}, "operation_id": "x"},
        {"expected": 0, "macros": {r"\x": r"\def\y{z}"}, "operation_id": "x"},
        {"expected": 0, "macros": {r"\x": "#1#1"}, "operation_id": "x"},
        {"expected": 0, "macros": {r"\x": r"\y", r"\y": r"\x"}, "operation_id": "x"},
        {"expected": 0, "macros": {r"\x": "\ud800"}, "operation_id": "x"},
    ],
)
def test_invalid_settings_refuse_without_creating_revision(stores, bad):
    docs, _, _ = stores
    with pytest.raises(DocumentError):
        ProjectMacros(docs).change("p", bad, USER)
    assert ProjectMacros(docs).get("p")["revision"] == 0


def test_setting_fault_rolls_back_head_receipt_and_revision(stores):
    docs, _, _ = stores
    with docs.control.write() as tx:
        db = tx.sql
        db.execute(
            "CREATE TRIGGER fail_macro_receipt BEFORE INSERT ON project_macro_operations "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        change(docs, {r"\F": "x"})
    assert ProjectMacros(docs).get("p")["revision"] == 0
    with docs.control.read() as tx:
        db = tx.sql
        assert db.execute("SELECT count(*) FROM project_macro_revisions").fetchone() == (0,)


def test_combined_cycles_and_document_override_are_bounded():
    definitions, problems = resolve({r"\F": "p", r"\X": r"\Y"}, {r"\F": "d", r"\Y": r"\X"})
    assert definitions == {r"\F": "d"}
    assert len(problems) == 2


@pytest.mark.asyncio
async def test_document_comment_and_project_text_resolution(stores):
    docs, reviews, _ = stores
    change(docs, {r"\F": "project", r"\G": "g"})
    doc = create(docs, USER, "---\nmacros:\n  '\\F': 'document'\n---\n\nContext.\n")
    comment(docs, doc, r"$\F$")
    plain = docs.create(
        "p", name="plain", text=r"$\F$", title="Plain", author=MAIN, session_id="s"
    ).document_id
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    assert {f.macros[r"\F"] for f in checker.calls} == {"document", "project"}
    assert all(f.macros[r"\G"] == "g" for f in checker.calls)
    assert view(docs, "p", document=doc, revision=1)["macros"][r"\F"] == "document"
    assert view(docs, "p", document=plain, revision=1)["macros"][r"\F"] == "project"
    assert view(docs, "q")["macros"] == {}


@pytest.mark.asyncio
async def test_lazy_refresh_stale_ack_and_dependency_only_budget(stores):
    docs, reviews, _ = stores
    doc = create(docs)
    await drain(reviews, ScriptedChecker())
    original = get(reviews)[0]
    notices = MathNotices(reviews)
    claimed = notices.pending("p", "s", "old", set())
    notices.bound("p", "s", "old", {n["notice_id"] for n in claimed})
    change(docs, {r"\F": "x"})
    assert MathController(reviews, "p", "s", "r").remaining()
    assert notices.pending("p", "s", "new", set()) == []
    notices.acknowledge("p", "s", "old", [n["notice_id"] for n in claimed])
    assert get(reviews)[0]["status"] == "open"
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 0
    change(docs, {r"\F": "y"})
    await drain(reviews, ScriptedChecker())
    assert get(reviews)[0]["reflags"] == 0
    assert MathJobs(reviews).job(original["math_check_id"])["macro_context"]["revision"] == 0
    patch(docs, doc, r"\unknown{first}", "patch1")
    await drain(reviews, ScriptedChecker())
    patch(docs, doc, r"\unknown{second}", "patch2")
    await drain(reviews, ScriptedChecker())
    assert (get(reviews)[0]["reflags"], get(reviews)[0]["status"]) == (2, "unaddressed")


@pytest.mark.asyncio
async def test_context_change_during_renderer_retains_observation_without_finding(stores):
    docs, reviews, _ = stores
    create(docs)

    class Changing(ScriptedChecker):
        async def check_many(self, formulas):
            change(docs, {r"\F": "new"})
            return await super().check_many(formulas)

    jobs = MathJobs(reviews)
    check = jobs.claim("p", "owner")
    assert check
    assert await jobs.step(check, "owner", Changing())
    assert get(reviews) == []
    assert jobs.evidence("p", check)["observations"][0]["check"]["status"] == "rejected"
    await drain(reviews, ScriptedChecker("accepted"))
    assert get(reviews) == []


@pytest.mark.asyncio
async def test_overridden_project_change_refreshes_exact_evidence_without_reflag(stores):
    docs, reviews, _ = stores
    create(docs, MAIN, "---\nmacros:\n  '\\F': 'document'\n---\n\n$\\unknown{x}$")
    change(docs, {r"\F": "project"})
    await drain(reviews, ScriptedChecker())
    old = get(reviews)[0]
    change(docs, {r"\F": "changed"})
    await drain(reviews, ScriptedChecker())
    current = get(reviews)[0]
    assert current["math_check_id"] != old["math_check_id"]
    assert current["reflags"] == 0
    assert MathJobs(reviews).job(current["math_check_id"])["macro_context"]["revision"] == 2


def test_historical_document_context_is_pinned_and_legacy_is_unknown(stores):
    docs, _, _ = stores
    change(docs, {r"\F": "before"})
    doc = create(docs, USER, "$\\F$")
    change(docs, {r"\F": "after"})
    assert view(docs, "p", document=doc, revision=1)["macros"][r"\F"] == "after"
    saved = view(docs, "p", document=doc, revision=1, historical=True)
    assert saved["known"]
    assert saved["macros"][r"\F"] == "before"
    with docs.control.write() as tx:
        db = tx.sql
        # A revision from before revisions recorded their project macros.
        db.execute("DROP TRIGGER revisions_no_update")
        db.execute("UPDATE revisions SET macro_revision=NULL")
    unknown = view(docs, "p", document=doc, revision=1, historical=True)
    assert not unknown["known"]
    assert unknown["held"]
    assert unknown["problems"]


@pytest.mark.asyncio
async def test_response_project_context_and_confirmation_macro_cas(stores):
    docs, reviews, _ = stores
    change(docs, {r"\F": "project"})
    finding, decision = response(reviews, body=r"$\F$", action="fix")
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    assert checker.calls
    assert all(f.macros == {r"\F": "project"} for f in checker.calls)
    confirm = dispatch(reviews, "confirmation")
    read_confirmation(reviews, confirm)
    change(docs, {r"\F": "different"})
    with pytest.raises(DocumentError, match="macro context changed"):
        Findings(reviews).confirm(
            "p",
            "s",
            confirm,
            "owner",
            {
                "finding": finding,
                "expected": decision,
                "decision": "confirm",
                "explanation": "Stale context",
                "anchors": [anchor(reviews, confirm, "finding")],
                "operation_id": "stale-confirm",
            },
        )
    assert Findings(reviews).get("p", "s", finding)["status"] == "claimed fixed"


def test_new_macro_context_queues_a_new_confirmation_without_spending_old_retries(stores):
    docs, reviews, _ = stores
    finding, decision = response(reviews, body=r"$\F$", action="fix")
    first = dispatch(reviews, "confirmation")
    first_key = reviews.run("p", "s", first)["confirmation_key"]
    reviews.finish("p", "s", first, "owner", "inconclusive", "Unconfirmed exact context")
    assert reviews.confirmation_targets("p", "s") == []
    change(docs, {r"\F": "new"})
    assert reviews.confirmation_targets("p", "s") == [(finding, decision)]
    second = dispatch(reviews, "confirmation")
    assert reviews.run("p", "s", second)["confirmation_key"] != first_key


def test_oversized_definitions_are_refused_when_written(stores):
    docs, _, _ = stores
    change(docs, {r"\F": "x"})
    refused = pytest.raises(sqlite3.IntegrityError, match="CHECK")
    with refused, docs.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO project_macro_revisions SELECT project,revision+1,?,sha256,author "
            "FROM project_macro_revisions",
            ('{"x": "' + "x" * 20_000 + '"}',),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("same", [False, True])
async def test_anchored_response_agreement_or_explicit_ambiguity(stores, same):
    docs, reviews, _ = stores
    change(docs, {r"\F": "project"})
    for name, definition in (("one", "one"), ("two", "one" if same else "two")):
        docs.create(
            "p",
            name=name,
            title=name,
            session_id="s",
            author=MAIN,
            text=f"---\nmacros:\n  '\\F': '{definition}'\n---\n\nEvidence.",
        )
    reviews.ingest_documents("p", "s")
    review = dispatch(reviews)
    evidence = [
        e for e in reviews.manifest("p", "s", review)["evidence"] if e["kind"] == "document"
    ]
    scientific = raise_finding(
        reviews, review, anchors=[{"evidence": e["evidence"]} for e in evidence]
    )
    reviews.finish("p", "s", review, "owner", "findings", "Synthetic anchored finding")
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": scientific["finding_id"],
            "expected": 1,
            "action": "rebut",
            "explanation": r"$\F$",
            "operation_id": "response",
        },
        MAIN,
    )
    checker = ScriptedChecker()
    await drain(reviews, checker)
    children = [f for f in get(reviews) if f["origin"] == "math"]
    latest = MathJobs(reviews).listing("p")["checks"][0]
    job = MathJobs(reviews).job(latest["check_id"])
    effective = MathJobs(reviews).effective_context(job)
    assert effective is not None
    assert job["response"]["finding"] == scientific["finding_id"]
    if same:
        assert len(children) == 1
        assert checker.calls[0].macros[r"\F"] == "one"
        assert not effective["held"]
    else:
        assert children == []
        assert checker.calls == []
        assert latest["macro_context_status"] == "held"
        assert effective["held"]
        assert "Conflicting" in effective["problems"][0]
    assert Findings(reviews).get("p", "s", scientific["finding_id"])["status"] == "open"
    pinned_review = dispatch(reviews)
    assert reviews.run("p", "s", pinned_review)["coverage_complete"] == int(same)
    item = next(
        item
        for item in reviews.manifest("p", "s", pinned_review)["evidence"]
        if item["kind"] == "finding" and item["locator"]["finding"] == scientific["finding_id"]
    )
    finding_payload = reviews.evidence("p", "s", pinned_review, item["evidence"])["payload"]
    assert finding_payload["response"]["macro_context"]["held"] is (not same)


@pytest.mark.parametrize("context", ["agreed", "conflicting", "unknown"])
@pytest.mark.parametrize("decision", ["confirm", "reject", "inconclusive"])
def test_confirmation_requires_exact_available_explanation_context(
    stores, monkeypatch, context, decision
):
    from sandbox_science.orchestrator.consistency import response_bodies

    docs, reviews, _ = stores
    change(docs, {r"\F": "project"})
    documents = []
    for name in ("one", "two"):
        definition = name if context == "conflicting" else "agreed"
        documents.append(
            docs.create(
                "p",
                name=name,
                title=name,
                session_id="s",
                author=MAIN,
                text=f"---\nmacros:\n  '\\F': '{definition}'\n---\n\nEvidence.",
            ).document_id
        )
    reviews.ingest_documents("p", "s")
    original = dispatch(reviews)
    evidence = [
        e for e in reviews.manifest("p", "s", original)["evidence"] if e["kind"] == "document"
    ]
    finding = raise_finding(
        reviews, original, anchors=[{"evidence": e["evidence"]} for e in evidence]
    )["finding_id"]
    reviews.finish("p", "s", original, "owner", "findings", "Original anchored finding")
    api = Findings(reviews)
    with monkeypatch.context() as legacy:
        if context == "unknown":
            # A valid older response writer retained the body but no macro revision.
            legacy.setattr(response_bodies, "macro_head", lambda *_args: None)
        api.respond(
            "p",
            "s",
            {
                "finding": finding,
                "expected": 1,
                "action": "fix",
                "explanation": r"Corrected $\F$",
                "revisions": [(documents[0], 1)],
                "operation_id": "claim",
            },
            MAIN,
        )
    confirmation = dispatch(reviews, "confirmation")
    read_confirmation(reviews, confirmation)
    payload = {
        "finding": finding,
        "expected": 2,
        "decision": decision,
        "explanation": "Exact pinned explanation assessment",
        "anchors": [anchor(reviews, confirmation, "finding")],
        "operation_id": "decide",
    }
    if context != "agreed" and decision != "inconclusive":
        with pytest.raises(DocumentError, match="macro context is held or unknown"):
            api.confirm("p", "s", confirmation, "owner", payload)
        assert api.get("p", "s", finding)["status"] == "claimed fixed"
        # The diagnostic context creates no new restriction on explicit USER decisions.
        user_decision = api.respond(
            "p",
            "s",
            {
                "finding": finding,
                "expected": 2,
                "action": "confirm",
                "explanation": "Explicit USER decision",
                "operation_id": "user-confirm",
            },
            USER,
        )
        assert user_decision["status"] == "confirmed fixed"
    else:
        result = api.confirm("p", "s", confirmation, "owner", payload)
        assert (
            result["status"]
            == {
                "confirm": "confirmed fixed",
                "reject": "open",
                "inconclusive": "inconclusive",
            }[decision]
        )


def test_cli_explicit_settings_and_safe_invalid_input(stores, tmp_path, capsys):
    from sandbox_science.orchestrator.cli import main

    docs, _, _ = stores
    source = tmp_path / "macros.json"
    source.write_text('{"\\\\F":"x"}')
    arguments = [
        "project",
        "macros",
        "--state-dir",
        str(docs.path.parent),
        "--project",
        "p",
        "--macros-file",
        str(source),
        "--expected",
        "0",
        "--operation-id",
        "cli",
    ]
    assert main(arguments) == 0
    assert ProjectMacros(docs).get("p")["macros"] == {r"\F": "x"}
    source.write_text('{"\\\\F":"test-secret-invalid-input-not-a-credential"')
    assert main(arguments) == 1
    assert "test-secret-invalid-input-not-a-credential" not in capsys.readouterr().err


def macro_reads(docs, reviews, doc: str, finding: str, decision: int) -> tuple[Any, ...]:
    """What the macro provenance answers: a historical document view, every check with
    its effective context, an explanation body's view, the confirmation targets, and
    the recorded revisions."""

    jobs = MathJobs(reviews)
    checks = []
    for entry in jobs.listing("p")["checks"]:
        job = jobs.job(entry["check_id"])
        checks.append((entry, job, jobs.effective_context(job)))
    with docs.control.read() as tx:
        recorded = [
            tx.sql.execute(sql).fetchall()
            for sql in (
                "SELECT document_id,number,macro_revision FROM revisions ORDER BY rowid",
                "SELECT check_id,macro_revision,macro_held FROM math_jobs ORDER BY rowid",
                "SELECT source,revision,macro_revision FROM review_response_bodies ORDER BY rowid",
                "SELECT review_id,finding_id,macro_revision FROM review_response_pins",
            )
        ]
    return (
        view(docs, "p", document=doc, revision=1, historical=True),
        checks,
        ResponseBodies(reviews).get("p", "s", finding, decision, body_revision=1),
        reviews.confirmation_targets("p", "s"),
        recorded,
    )


@pytest.mark.asyncio
async def test_the_macro_revisions_keep_what_the_pins_held(stores, tmp_path):
    """A version-9 store's pin tables become version 10's macro_revision columns, and the
    store answers as the one it was copied from."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    docs, reviews, _ = stores
    change(docs, {r"\F": "project"})
    doc = docs.create("p", name="macros", title="M", text="$\\F$\n", author=USER).document_id
    finding, decision = response(reviews, body=r"$\F$", action="fix")
    await drain(reviews, ScriptedChecker("accepted"))
    confirmation = dispatch(reviews, "confirmation")
    assert confirmation
    change(docs, {r"\F": "later"})
    before = macro_reads(docs, reviews, doc, finding, decision)
    checks = before[4][1]
    assert checks
    assert all(row[1] == 1 for row in checks)  # the checks used setting revision 1
    old = tmp_path / "old"
    older_store(docs.path, store_path(old), migrations.BASELINE + 7)  # version 9
    copied = DocumentStore(store_path(old))
    copied.initialize()  # the writer brings the store to the current version
    try:
        copied_reviews = ReviewStore(copied, clock=lambda: 1000.0)
        after = macro_reads(copied, copied_reviews, doc, finding, decision)
    finally:
        copied.close()
    assert after == before


# The review's probes of B-12 (#221), kept as tests.
def _every(reviews, review):
    after, items = 0, []
    while True:
        page = reviews.manifest("p", "s", review, after=after, limit=20)
        items.extend(page["evidence"])
        if page["next_after"] is None:
            return items
        after = page["next_after"]


def _documents(reviews, review, order):
    found = {
        e["locator"]["document"]: e
        for e in _every(reviews, review)
        if e["kind"] == "document" and e["locator"]["document"] in order
    }
    return [{"evidence": found[d]["evidence"]} for d in order]


def _raise(reviews, review, anchors, op):
    return Findings(reviews).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "Anchored statement.",
            "anchors": anchors,
            "operation_id": op,
        },
    )


async def _explained_then_reflagged(docs, reviews):
    """A finding on two agreeing documents, explained (rebut), its rebuttal rejected,
    then raised again by a later review naming the same anchors in the other order."""
    one, two = (
        docs.create(
            "p",
            name=name,
            title=name,
            session_id="s",
            author=MAIN,
            text="---\nmacros:\n  '\\F': 'agreed'\n---\n\nEvidence.\n",
        ).document_id
        for name in ("one", "two")
    )
    reviews.ingest_documents("p", "s")
    claim(reviews)
    first = dispatch(reviews)
    finding = _raise(reviews, first, _documents(reviews, first, [one, two]), "f1")["finding_id"]
    reviews.finish("p", "s", first, "owner", "findings", "First")
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": 1,
            "action": "rebut",
            "explanation": r"Not so: $\F$",
            "operation_id": "rebut",
        },
        MAIN,
    )
    await drain(reviews, ScriptedChecker("accepted"))
    claim(reviews, "Another message.", "m2")
    second = dispatch(reviews)
    read_confirmation(reviews, second)
    item = next(
        i
        for i in _every(reviews, second)
        if i["kind"] == "finding" and i["locator"]["finding"] == finding
    )
    Findings(reviews).confirm(
        "p",
        "s",
        second,
        "owner",
        {
            "finding": finding,
            "expected": 2,
            "decision": "reject_rebuttal",
            "explanation": "The rebuttal does not hold.",
            "anchors": [{"evidence": item["evidence"]}],
            "operation_id": "decide",
        },
    )
    return finding, second, (one, two)


@pytest.mark.asyncio
async def test_a_reflag_does_not_change_a_past_explanations_macro_context(stores):
    """An explanation's anchors are its finding's at the explanation's decision
    revision: a later reflag naming the same documents in another order leaves its
    context, key and its checks' effective contexts alone (as main's pin did)."""
    docs, reviews, _ = stores
    finding, second, (one, two) = await _explained_then_reflagged(docs, reviews)
    source = f"response:{finding}:r2:explanation"
    jobs = MathJobs(reviews)
    (check,) = [
        c["check_id"] for c in jobs.listing("p")["checks"] if "response" in jobs.job(c["check_id"])
    ]
    before_view = response_view(docs, "p", source, body_revision=1)
    before_check = jobs.effective_context(jobs.job(check))
    raised = _raise(reviews, second, _documents(reviews, second, [two, one]), "f1-again")
    assert raised["finding_id"] == finding  # a reflag, not a new finding
    after_view = response_view(docs, "p", source, body_revision=1)
    after_check = jobs.effective_context(jobs.job(check))
    assert after_view["macros"] == before_view["macros"]  # the definitions agree...
    assert after_view["anchors"] == before_view["anchors"]  # ...but the anchors moved
    assert after_view["key"] == before_view["key"]
    assert after_check == before_check


@pytest.mark.asyncio
async def test_listing_contexts_parse_a_checked_source_at_most_once(stores, monkeypatch):
    """A check's effective context is derived from its immutable inputs once: a page of
    twenty findings of one check parses the checked source at most once."""
    docs, reviews, _ = stores
    create(docs, MAIN, "".join(f"Paragraph {i}: $\\unknown{{v{i}}}$\n\n" for i in range(20)))
    await drain(reviews, ScriptedChecker())
    page = Findings(reviews).listing("p", "s")["findings"]
    assert len(page) == 20
    parses: list[int] = []
    real = getattr(jobs_module, "attach")  # noqa: B009 - the module's import, not an export
    monkeypatch.setattr(jobs_module, "attach", lambda *a: parses.append(1) or real(*a))
    for finding in page:
        finding_macro_contexts(reviews, "p", finding)
    assert len(parses) <= 1, f"{len(parses)} full parses for one page of findings"


def _v9_copy(docs, tmp_path: Path) -> Path:
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, migrations.BASELINE + 7)
    return old


def _migrate(path: Path) -> None:
    copied = DocumentStore(path)
    try:
        copied.initialize()
    finally:
        copied.close()


@pytest.mark.parametrize(
    "orphan",
    [
        "INSERT INTO response_macro_revisions VALUES"
        "('response:finding-000000000000000000000000:r1:explanation',1,0)",
        f"INSERT INTO review_macro_pins VALUES('review-x','finding-x',0,'{EMPTY_SHA}')",
    ],
)
def test_a_pin_the_migration_cannot_place_is_refused_not_dropped(stores, tmp_path, orphan):
    """A pin version 10 has no row to copy to refuses the store with the control store's
    message, as every other step that cannot carry a row does."""
    docs, _, _ = stores
    create(docs, USER, "Text.\n")
    old = _v9_copy(docs, tmp_path)
    with sqlite3.connect(old) as db:
        db.execute(orphan)
    with pytest.raises(ControlError, match="refuses"):
        _migrate(old)


@pytest.mark.asyncio
async def test_migration_keeps_an_explanations_pinned_anchors(stores, tmp_path):
    """A version-9 store that pinned an explanation's anchors in one order (as main did
    at first save) answers with that order after migrating, whatever the finding's
    current anchors say; the migration checks the pinned anchors against those it
    derives."""
    docs, reviews, _ = stores
    finding, second, (one, two) = await _explained_then_reflagged(docs, reviews)
    source = f"response:{finding}:r2:explanation"
    pinned = response_view(docs, "p", source)["anchors"]
    _raise(reviews, second, _documents(reviews, second, [two, one]), "f1-again")
    old = _v9_copy(docs, tmp_path)

    with sqlite3.connect(old) as db:
        # What main stored: the anchors at first save.
        db.execute("DROP TRIGGER response_macro_anchors_update")
        db.execute(
            "UPDATE response_macro_anchors SET anchors=? WHERE source=?",
            (json.dumps(pinned, separators=(",", ":")), source),
        )
    _migrate(old)
    migrated = DocumentStore(old)
    try:
        assert response_view(migrated, "p", source)["anchors"] == pinned
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_a_checks_recorded_macro_revision_stays_immutable(stores):
    """math_macro_pins and math_macro_contexts refused updates; a trigger keeps their
    replacement columns on math_jobs once set."""
    docs, reviews, _ = stores
    create(docs)
    await drain(reviews, ScriptedChecker())
    with pytest.raises(sqlite3.IntegrityError), docs.control.write() as tx:
        tx.sql.execute("UPDATE math_jobs SET macro_revision=7,macro_held=1")


@pytest.mark.asyncio
async def test_math_check_evidence_still_shows_the_effective_context(stores):
    """GET /math-checks/{check} (MathJobs.evidence) returns the check with its
    effective_macro_context: the exact definitions its formulas were rendered with."""
    docs, reviews, _ = stores
    create(docs, MAIN, "---\nmacros:\n  '\\F': 'document'\n---\n\n$\\unknown{x}$\n")
    await drain(reviews, ScriptedChecker())
    jobs = MathJobs(reviews)
    (entry,) = jobs.listing("p")["checks"]
    check = jobs.evidence("p", entry["check_id"])["check"]
    assert check.get("effective_macro_context", {}).get("effective", {}).get(r"\F") == "document"


def test_a_pin_the_new_column_refuses_gets_the_control_stores_refusal(stores, tmp_path):
    """document_macro_pins never had a range CHECK; the new column does. Such a row
    leaves the store at version 9 with the ControlError (unavailable) every other step
    gives, not a bare sqlite3.IntegrityError."""
    docs, _reviews, _ = stores
    create(docs, USER, "Text.\n")
    old = _v9_copy(docs, tmp_path)
    with sqlite3.connect(old) as db:
        db.execute("DROP TRIGGER document_macro_pins_update")
        db.execute("UPDATE document_macro_pins SET project_revision=-1")
    with pytest.raises(ControlError, match="refuses"):
        _migrate(old)
    with sqlite3.connect(old) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (9,)


FIXTURE = Path(__file__).parent / "fixtures" / "control_v9"


def test_a_version_9_store_from_the_code_before_answers_as_that_code_did(tmp_path):
    """A version-9 store the code before B-12 wrote (fixtures/control_v9, made by its
    build.py) migrates to version 10 and gives every macro answer that code gave for
    it: check contexts and effective contexts, the listing, document and explanation
    views, findings with their contexts, confirmation targets, runs and notices."""
    import gzip

    from .control_versions import macro_answers

    path = store_path(tmp_path)
    with sqlite3.connect(path) as db:
        db.executescript(gzip.decompress((FIXTURE / "control-v9.sql.gz").read_bytes()).decode())
        db.execute(f"PRAGMA application_id = {migrations.APPLICATION_ID}")
        db.execute(f"PRAGMA user_version = {migrations.BASELINE + 7}")
    expected = json.loads(gzip.decompress((FIXTURE / "answers.json.gz").read_bytes()))
    docs = DocumentStore(path)
    try:
        docs.initialize()  # the writer brings the store to the current version
        reviews = ReviewStore(docs, clock=lambda: 1000.0)
        answers = macro_answers(docs, reviews, lambda jobs, job: jobs.effective_context(job))
    finally:
        docs.close()
    assert answers.keys() == expected.keys()
    for key in expected:
        assert answers[key] == expected[key], key
