# SPDX-License-Identifier: MIT
"""Build the version-9 control store fixture with the code from before B-12, and record
the answers that code gives for it.

Version 10 (B-12) replaced the macro pin tables with ``macro_revision`` columns. Its
migration test must start from a store the code before it wrote, so this script runs
in a checkout of that code (commit 3f28a41, schema version 9), from the checkout's root:

    uv run python /path/to/this/build.py OUT

It writes ``OUT/control-v9.sql.gz`` (the store as SQL) and ``OUT/answers.json.gz``
(what that code answers for it, through ``control_versions.macro_answers``), gzipped
with no timestamp; copy both here. The
store covers settings changed between saves, front-matter overrides, comment checks,
explanations anchored to agreeing and to conflicting documents, a legacy writer's
explanation (no macro pins), edited explanations, confirmation and terminal reviews,
a reflag naming the same anchors in another order, front matter changed between
revisions, and a claimed check never stepped.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path.cwd() / "packages/orchestrator"))  # that checkout's tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # control_versions

from control_versions import macro_answers
from tests.test_comment_math import comment
from tests.test_consistency import anchor, claim, dispatch, read_confirmation
from tests.test_document_math import BAD, MAIN, ScriptedChecker, drain, patch

from sandbox_science.orchestrator import mathchecks
from sandbox_science.orchestrator.consistency import response_bodies
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.response_bodies import ResponseBodies
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.projects import ProjectStore

assert migrations.VERSION == migrations.BASELINE + 7, "run this with the version-9 code"
out = Path(sys.argv[1])
state = out / "state"
shutil.rmtree(state, ignore_errors=True)
state.mkdir(parents=True)
projects = ProjectStore(state)
projects.create("p", "minimal", "sha256:" + "a" * 64)
projects.open_session("p", "s", dedicated=False)
projects.close()
docs = DocumentStore(store_path(state))
mathchecks.subscribe(docs)
reviews = ReviewStore(docs, clock=lambda: 1000.0)
operations = iter(range(10_000))


def setting(macros: dict[str, str]) -> None:
    api = ProjectMacros(docs)
    revision = api.get("p")["revision"]
    api.change(
        "p",
        {"expected": revision, "macros": macros, "operation_id": f"m{next(operations)}"},
        USER,
    )


def make(name: str, text: str) -> str:
    return docs.create(
        "p", name=name, title=name, text=text, author=MAIN, session_id="s"
    ).document_id


def front(definition: str) -> str:
    return f"---\nmacros:\n  '\\F': '{definition}'\n---\n\n"


def evidence_of(review: str, documents: list[str]) -> list[dict[str, Any]]:
    """The review's document evidence for ``documents``, in that order."""
    after, items = 0, []
    while True:
        page = reviews.manifest("p", "s", review, after=after, limit=20)
        items.extend(page["evidence"])
        if page["next_after"] is None:
            break
        after = page["next_after"]
    found = {
        item["locator"]["document"]: item
        for item in items
        if item["kind"] == "document" and item["locator"]["document"] in documents
    }
    return [{"evidence": found[document]["evidence"]} for document in documents]


def raise_on(review: str, anchors: list[dict[str, Any]], operation: str) -> dict[str, Any]:
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
            "operation_id": operation,
        },
    )


def respond(finding: str, action: str, explanation: str, operation: str, **extra: Any) -> None:
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding,
            "expected": 1,
            "action": action,
            "explanation": explanation,
            "operation_id": operation,
            **extra,
        },
        MAIN,
    )


async def build() -> None:
    # A setting changed between saves of a document with failing math.
    setting({r"\F": "before", r"\G": "g"})
    a = make("a", BAD + r" $\F$" + "\n\nOther.\n")
    await drain(reviews, ScriptedChecker())
    setting({r"\F": "after", r"\G": "g"})
    patch(docs, a, r"\unknown{y}", "pa1")
    await drain(reviews, ScriptedChecker())
    setting({r"\F": "third"})
    await drain(reviews, ScriptedChecker())
    # Front matter overriding the setting, and a comment check on that document.
    b = make("b", front("document") + r"$\F + \G$" + "\n")
    comment(docs, b, r"$\F$", op="c1")
    await drain(reviews, ScriptedChecker("accepted"))
    # Explanations anchored to agreeing and to conflicting documents, and a legacy one.
    c1 = make("c1", front("agreed") + "Evidence one.\n")
    c2 = make("c2", front("agreed") + "Evidence two.\n")
    d1 = make("d1", front("one") + "Conflict one.\n")
    d2 = make("d2", front("two") + "Conflict two.\n")
    e1 = make("e1", front("agreed") + "Evidence three.\n")
    reviews.ingest_documents("p", "s")
    claim(reviews)
    first = dispatch(reviews)
    agreed = raise_on(first, evidence_of(first, [c1, c2]), "f-agreed")["finding_id"]
    conflicting = raise_on(first, evidence_of(first, [d1, d2]), "f-conflict")["finding_id"]
    legacy = raise_on(first, evidence_of(first, [c1]), "f-legacy")["finding_id"]
    reorder = raise_on(first, evidence_of(first, [c2, e1]), "f-reorder")["finding_id"]
    reviews.finish("p", "s", first, "owner", "findings", "Original review")
    respond(agreed, "fix", r"Fixed $\F$ here", "resp-agreed", revisions=[(c1, 1)])
    respond(conflicting, "rebut", r"Not so: $\F$", "resp-conflict")
    # A legacy writer: the version-9 code's response_saved (gone since) pins nothing.
    saved = getattr(response_bodies, "response_saved")  # noqa: B009
    setattr(response_bodies, "response_saved", lambda *_args, **_kwargs: None)  # noqa: B010
    try:
        respond(legacy, "rebut", r"Legacy $\F$", "resp-legacy")
    finally:
        setattr(response_bodies, "response_saved", saved)  # noqa: B010
    respond(reorder, "rebut", r"Reorder $\F$", "resp-reorder")
    await drain(reviews, ScriptedChecker("accepted"))
    # Two explanations edited (body revision 2), one of them the legacy one.
    for finding in (agreed, legacy):
        source = ResponseBodies(reviews).get("p", "s", finding, 2)
        ResponseBodies(reviews).edit(
            "p",
            "s",
            {
                "finding": finding,
                "response_revision": 2,
                "expected": 2,
                "base": source["revision"],
                "sha256": source["sha256"],
                "body": r"Edited $\F + \G$",
                "operation_id": f"edit-{finding}",
            },
            MAIN,
        )
    setting({r"\F": "fourth", r"\G": "g4"})
    await drain(reviews, ScriptedChecker("accepted"))
    # A confirmation review, decided inconclusive; then a terminal review rejecting the
    # reorder finding's rebuttal, and raising it again with its anchors in another order.
    confirmation = dispatch(reviews, "confirmation")
    read_confirmation(reviews, confirmation)
    Findings(reviews).confirm(
        "p",
        "s",
        confirmation,
        "owner",
        {
            "finding": agreed,
            "expected": 2,
            "decision": "inconclusive",
            "explanation": "Cannot tell.",
            "anchors": [anchor(reviews, confirmation, "finding")],
            "operation_id": "decide-agreed",
        },
    )
    reviews.finish("p", "s", confirmation, "owner", "findings", "Confirmation")
    claim(reviews, "Another message.", "m2")
    second = dispatch(reviews)
    read_confirmation(reviews, second)
    pinned = next(
        item
        for item in reviews.manifest("p", "s", second, limit=20)["evidence"]
        if item["kind"] == "finding" and item["locator"]["finding"] == reorder
    )
    Findings(reviews).confirm(
        "p",
        "s",
        second,
        "owner",
        {
            "finding": reorder,
            "expected": 2,
            "decision": "reject_rebuttal",
            "explanation": "The rebuttal does not hold.",
            "anchors": [{"evidence": pinned["evidence"]}],
            "operation_id": "decide-reorder",
        },
    )
    raise_on(second, evidence_of(second, [e1, c2]), "f-reorder-again")
    reviews.finish("p", "s", second, "owner", "findings", "Second review")
    # Front matter changed between document revisions, with a comment on the first.
    g = make("g", front("first") + "$\\F$ text\n")
    comment(docs, g, r"$\F + \unknown{c}$", op="c2")
    await drain(reviews, ScriptedChecker())
    docs.save_text(
        "p", g, base=1, text=front("second") + "$\\F\\F$ text\n", author=USER, reason="FM"
    )
    setting({r"\F": "sixth", r"\H": "h"})
    await drain(reviews, ScriptedChecker())
    # A check claimed after a new setting and never stepped.
    setting({r"\F": "fifth"})
    MathJobs(reviews).claim("p", "owner")


def effective(jobs: MathJobs, job: dict[str, Any]) -> Any:
    return job.get("effective_macro_context")


asyncio.run(build())
answers = macro_answers(docs, reviews, effective)
docs.close()
with sqlite3.connect(store_path(state)) as db:
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dump = "\n".join(db.iterdump()) + "\n"
for name, text in (
    ("control-v9.sql.gz", dump),
    ("answers.json.gz", json.dumps(answers, indent=1, sort_keys=True) + "\n"),
):
    with (out / name).open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
        zipped.write(text.encode())
    print("wrote", out / name)
