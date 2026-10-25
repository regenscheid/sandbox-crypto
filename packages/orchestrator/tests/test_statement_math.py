# SPDX-License-Identifier: MIT
"""Ledger statements' math checks (LG-1, LD-53 to LD-55): a statement saved with a formula
KaTeX rejects becomes a math finding on the entry's statement for the agent that wrote
it, which the next revision's check confirms fixed; text the user wrote gets no finding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.mathchecks.notices import MathNotices

from .ledger_support import AnchoredLedger as Ledger
from .test_document_math import BAD, MAIN, ScriptedChecker, drain
from .test_document_math import stores as stores

DELEGATED = MAIN.model_copy(update={"run_id": "r2", "role": "delegated"})
CLAIM = {"type": "claim", "origin": "project", "title": "Fermat"}


def record(
    reviews: ReviewStore, statement: str, *, author: Author = MAIN, op: str = "op-1"
) -> dict[str, Any]:
    change = {**CLAIM, "statement": statement}
    return (
        Ledger(reviews.documents)
        .change("p", {"operation_id": op, "reason": "r", "changes": [change]}, author)
        .model_dump()
    )


def revise(reviews: ReviewStore, expected: int, op: str, **change: Any) -> None:
    Ledger(reviews.documents).change(
        "p",
        {
            "operation_id": op,
            "reason": "r",
            "changes": [{"entry": "C-1", "expected": expected, **change}],
        },
        MAIN,
    )


def maths(reviews: ReviewStore, session: str = "s") -> list[dict[str, Any]]:
    return [f for f in Findings(reviews).listing("p", session)["findings"] if f["origin"] == "math"]


@pytest.mark.asyncio
async def test_a_statements_bad_formula_is_a_finding_its_fix_confirms(stores) -> None:
    _, reviews, _ = stores
    made = record(reviews, f"For every a, {BAD}.")
    entry = made["entries"][0]["entry_id"]
    await drain(reviews, ScriptedChecker())
    (finding,) = maths(reviews)
    assert finding["status"] == "open"
    assert finding["anchors"][0]["locator"] == {
        "entry": entry,
        "handle": "C-1",
        "field": "statement",
        "body_revision": 1,
    }
    shown = Ledger(reviews.documents).entry("p", "C-1").findings
    assert [(f.finding_id, f.field, f.origin) for f in shown] == [
        (finding["finding_id"], "statement", "math")
    ]
    job = MathJobs(reviews).job(finding["math_check_id"])
    assert (job["document"], job["statement"]["entry"]) == (None, entry)
    (notice,) = MathNotices(reviews).pending("p", "s", "run", set())
    assert "ledger_change for a ledger statement" in notice["detail"]
    listed = MathJobs(reviews).listing("p")
    assert [check["kind"] for check in listed["checks"]] == ["statement"]
    # The agent fixes the formula; the next check makes the finding fixed.
    revise(reviews, 1, "fix", statement="For every $a$, $a^{16}=1$.")
    await drain(reviews, ScriptedChecker("accepted"))
    assert maths(reviews)[0]["status"] == "fixed"


@pytest.mark.asyncio
async def test_removing_the_formula_confirms_its_finding_fixed(stores) -> None:
    _, reviews, _ = stores
    record(reviews, f"Before {BAD}.")
    await drain(reviews, ScriptedChecker())
    revise(reviews, 1, "removed", statement="No formula now.")
    await drain(reviews, ScriptedChecker())
    assert [f["status"] for f in maths(reviews)] == ["fixed"]


@pytest.mark.asyncio
async def test_a_users_statement_gets_no_finding_and_a_delegated_runs_goes_to_its_session(
    stores,
) -> None:
    _, reviews, _ = stores
    record(reviews, f"The user's {BAD}.", author=USER)
    await drain(reviews, ScriptedChecker())
    assert maths(reviews) == []
    record(reviews, f"The child's {BAD}.", author=DELEGATED, op="op-2")
    await drain(reviews, ScriptedChecker())
    (finding,) = maths(reviews)
    assert finding["anchors"][0]["locator"]["handle"] == "C-2"
    assert finding["session"] == "s"


@pytest.mark.asyncio
async def test_only_a_new_statement_is_checked(stores) -> None:
    _, reviews, _ = stores
    record(reviews, "First $a$.")
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    revise(reviews, 1, "title", title="Renamed")
    revise(reviews, 2, "status", status="disputed")
    await drain(reviews, checker)
    assert [formula.tex for formula in checker.calls] == ["a"]
    revise(reviews, 3, "statement", statement="Then $b$.")
    await drain(reviews, checker)
    assert [formula.tex for formula in checker.calls] == ["a", "b"]


@pytest.mark.asyncio
async def test_an_unavailable_renderer_is_tried_again_and_the_queue_shows_statements(
    stores,
) -> None:
    _, reviews, _ = stores
    record(reviews, f"Checked later: {BAD}.")
    jobs = MathJobs(reviews)
    queued = jobs.listing("p")["queue"]
    assert [(item["kind"], item["handle"], item["target"]) for item in queued] == [
        ("statement", "C-1", 1)
    ]
    await drain(reviews, ScriptedChecker("unavailable"))
    assert maths(reviews) == []
    jobs.retry_unavailable("p")
    await drain(reviews, ScriptedChecker())
    assert [f["status"] for f in maths(reviews)] == ["open"]


def test_the_statement_checks_survive_the_migration(tmp_path: Path) -> None:
    import asyncio

    from sandbox_science.orchestrator import mathchecks
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.projects import ProjectStore

    from .control_versions import older_store

    current, old = tmp_path / "current", tmp_path / "old"
    projects = ProjectStore(current)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.close()
    docs = DocumentStore(store_path(current))
    mathchecks.subscribe(docs)
    reviews = ReviewStore(docs, clock=lambda: 1000.0)
    record(reviews, f"Saved before: {BAD}.")
    asyncio.run(drain(reviews, ScriptedChecker()))
    (before,) = maths(reviews)
    docs.close()
    older_store(store_path(current), store_path(old), migrations.VERSION - 1)
    docs = DocumentStore(store_path(old))
    docs.initialize()  # the writer brings the store to the current version
    try:
        reviews = ReviewStore(docs, clock=lambda: 1000.0)
        (after,) = maths(reviews)
        assert after["finding_id"] == before["finding_id"]
        assert MathJobs(reviews).job(after["math_check_id"])["project"] == "p"
        (notice,) = MathNotices(reviews).pending("p", "s", "run", set())
        assert notice["finding"] == before["finding_id"]
    finally:
        docs.close()


@pytest.mark.asyncio
async def test_a_statement_is_checked_with_the_project_macros_and_again_when_they_change(
    stores,
) -> None:
    from sandbox_science.orchestrator.mathchecks.controller import MathController

    from .test_project_macros import change

    docs, reviews, _ = stores
    change(docs, {r"\F": "project"})
    record(reviews, r"Over $\F$.")
    checker = ScriptedChecker("accepted")
    await drain(reviews, checker)
    assert [formula.macros for formula in checker.calls] == [{r"\F": "project"}]
    change(docs, {r"\F": "changed"})
    assert MathController(reviews, "p", "s", "r").remaining()
    await drain(reviews, checker)
    assert [formula.macros for formula in checker.calls][-1] == {r"\F": "changed"}
