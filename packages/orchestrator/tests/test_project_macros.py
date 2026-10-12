# SPDX-License-Identifier: MIT
"""Project settings, exact effective inputs and stale mechanical/confirmation fences."""

from __future__ import annotations

import sqlite3
import tracemalloc

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.mathnotices import MathNotices
from sandbox_science.orchestrator.documents import USER, Author, DocumentError
from sandbox_science.orchestrator.documents.mathcontroller import MathController
from sandbox_science.orchestrator.documents.mathjobs import MathJobs
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros, resolve, view

from .test_comment_math import comment
from .test_consistency import anchor, dispatch, raise_finding, read_confirmation
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
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), docs._write() as db:
        db.execute("UPDATE project_macro_revisions SET definitions='{}'")


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
    with docs._write() as db:
        db.execute(
            "CREATE TRIGGER fail_macro_receipt BEFORE INSERT ON project_macro_operations "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        change(docs, {r"\F": "x"})
    assert ProjectMacros(docs).get("p")["revision"] == 0
    with docs._read() as db:
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
    with docs._write() as db:
        db.execute("DROP TRIGGER document_macro_pins_delete")
        db.execute("DELETE FROM document_macro_pins")
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


def test_corrupt_definition_read_is_bounded_before_parse(stores):
    docs, _, _ = stores
    change(docs, {r"\F": "x"})
    with docs._write() as db:
        db.execute("DROP TRIGGER project_macro_revisions_update")
        db.execute("UPDATE project_macro_revisions SET definitions=?", ("x" * 20_000_000,))
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError, match="bounds"):
            ProjectMacros(docs).get("p")
        assert tracemalloc.get_traced_memory()[1] < 200_000
    finally:
        tracemalloc.stop()


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
    assert job["response"]["finding"] == scientific["finding_id"]
    if same:
        assert len(children) == 1
        assert checker.calls[0].macros[r"\F"] == "one"
        assert not job["effective_macro_context"]["held"]
    else:
        assert children == []
        assert checker.calls == []
        assert latest["macro_context_status"] == "held"
        assert job["effective_macro_context"]["held"]
        assert "Conflicting" in job["effective_macro_context"]["problems"][0]
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


@pytest.mark.asyncio
async def test_effective_context_quota_holds_without_vetoing_source_or_settings(
    stores, monkeypatch
):
    from sandbox_science.orchestrator.documents import project_macros

    docs, reviews, _ = stores
    doc = create(docs)
    monkeypatch.setattr(project_macros, "MAX_CONTEXT_BYTES", 1)
    jobs = MathJobs(reviews)
    check = jobs.claim("p", "owner")
    assert check
    checker = ScriptedChecker()
    assert not await jobs.step(check, "owner", checker)
    assert checker.calls == []
    assert "context quota" in jobs.listing("p")["queue"][0]["blocked"]
    change(docs, {r"\F": "x"})
    patch(docs, doc, "x", "ordinary-edit")
    assert docs.document("p", doc).head == 2


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
