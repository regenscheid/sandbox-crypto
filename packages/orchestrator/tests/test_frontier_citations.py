# SPDX-License-Identifier: MIT
"""An agent's published result confirmed by the consistency reviewer's citation checks
(docs/specs/ledger.md, LG-16 as the coordinator amended it; CR-4, and the review of it):
each quote of the revision the frontier shows needs a ``supported`` check of that
revision's material, its latest, with no ``unsupported`` check of any revision since; a
PDF quote counts only of a PDF the user vouches for; and a quote of a reference that is
withdrawn confirms nothing. The frontier says who confirmed a result (``confirmed_by``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.citations import record_citation
from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.tools import format_pdf

from .test_consistency import MAIN, dispatch
from .test_consistency import store as store
from .test_ledger import REFERENCE_FIELDS
from .test_ledger_evidence import publish
from .test_ledger_frontier import CLASSICAL, KEY_RECOVERY
from .test_review_quotes import EPRINT_TOOL, STAMP, call, pages, passages, quoting

FIELDS = {"category": "key-recovery", "conditions": CLASSICAL,
          "measures": {"time": 120, "memory": 60}}  # fmt: skip
FOUND = "Results. Our attack recovers the key in time 2^120 with memory 2^60 at most."
TIME = {"source": "eprint", "dated": STAMP, "text": "recovers the key in time 2^120"}
MEMORY = {"source": "eprint", "dated": STAMP, "text": "in time 2^120 with memory 2^60"}
CONFIRMED = ([("R-1", "citation check")], [])
UNVERIFIED = ([], [("R-1", True)])


def setup(reviews: ReviewStore, author: Author = MAIN, pdf: dict[str, Any] | None = None) -> Ledger:
    """Category key-recovery and REF-1, recorded by ``author``; the agent has read the
    ePrint's passage."""
    ledger = Ledger(reviews.documents)
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    fields = {**REFERENCE_FIELDS, **({"pdf": pdf} if pdf else {})}
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.", "fields": fields}
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, author)
    call(reviews, 1, EPRINT_TOOL, {"paper_id": "2024/123", "query": "key"},
         passages("2024/123", "key", FOUND), {"connector": "crypto-research-mcp"})  # fmt: skip
    return ledger


def result(ledger: Ledger, *quotes: dict[str, Any], author: Author = MAIN) -> None:
    """R-1, an agent's published result that quotes REF-1."""
    evidence = [quoting(q) for q in quotes] or [
        {"role": "supports", "type": "source_claim", "entry": "REF-1"}
    ]
    change = {"type": "result", "origin": "published", "title": "P1", "statement": "Time 2^120.",
              "fields": FIELDS, "evidence": evidence}  # fmt: skip
    ledger.change("p", {"operation_id": "r", "reason": "r", "changes": [change]}, author)


def revise(ledger: Ledger, expected: int, time: int) -> None:
    fields = {**FIELDS, "measures": {"time": time, "memory": 60}}
    ledger.change(
        "p",
        {"operation_id": f"t{expected}", "reason": "r",
         "changes": [{"entry": "R-1", "expected": expected, "fields": fields}]},
        MAIN,
    )  # fmt: skip


def shown(ledger: Ledger) -> tuple[list[Any], list[Any]]:
    (group,) = ledger.frontier("p", "key-recovery").groups
    return (
        [(r.handle, r.confirmed_by) for r in group.best],
        [(r.handle, r.unverified) for r in group.candidates],
    )


def review(reviews: ReviewStore) -> tuple[str, dict[str, str]]:
    """A review of what the ledger brought in since the last, whose reviewer has read
    all of R-1's revision and its quotes' sources: the review and the sources, by the
    quotes' evidence IDs."""
    assert ingest_ledger(reviews, "p", "s")["complete"]
    found = dispatch(reviews)
    items = reviews.manifest("p", "s", found, limit=50)["evidence"]
    sources = {
        i["locator"]["quoted_by"]["evidence"]: i["evidence"]
        for i in items
        if "quoted_by" in i["locator"]
    }
    for item in items:
        if item["evidence"] in sources.values() or (
            item["kind"] == "ledger" and item["locator"]["handle"] == "R-1"
        ):
            reviews.read_evidence("p", "s", found, item["evidence"], owner="owner")
    return found, sources


def only(sources: dict[str, str]) -> str:
    (source,) = sources.values()
    return source


def check(reviews: ReviewStore, found: str, source: str, verdict: str) -> None:
    record_citation(reviews, "p", "s", found, "owner",
                    {"source": source, "verdict": verdict, "explanation": "Why."})  # fmt: skip


def test_the_latest_check_decides(store: Any) -> None:
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger, TIME)
    assert shown(ledger) == UNVERIFIED
    found, sources = review(reviews)
    (source,) = sources.values()
    for verdict, then in [("unsupported", UNVERIFIED), ("inconclusive", UNVERIFIED),
                          ("supported", CONFIRMED), ("unsupported", UNVERIFIED),
                          ("supported", CONFIRMED), ("inconclusive", UNVERIFIED),
                          ("supported", CONFIRMED)]:  # fmt: skip
        check(reviews, found, source, verdict)
        assert shown(ledger) == then, verdict


def test_an_unsupported_check_of_another_revision_stands(store: Any) -> None:
    """Review hardening: a result whose newer revision was found unsupported does not
    return to an older ``supported`` check by returning to that revision's material."""
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger, TIME)
    found, sources = review(reviews)
    check(reviews, found, only(sources), "supported")
    reviews.finish("p", "s", found, "owner", "passed", "The citation holds.")
    assert shown(ledger) == CONFIRMED
    revise(ledger, 1, 100)  # needs checks of its own
    assert shown(ledger) == UNVERIFIED
    found, sources = review(reviews)
    check(reviews, found, only(sources), "unsupported")
    reviews.finish("p", "s", found, "owner", "findings", "The source says 2^120.")
    revise(ledger, 2, 120)  # back to the material of revision 1
    assert shown(ledger) == UNVERIFIED
    found, sources = review(reviews)
    check(reviews, found, only(sources), "supported")
    assert shown(ledger) == CONFIRMED


def test_every_quote_needs_its_check(store: Any) -> None:
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger, TIME, MEMORY)
    found, sources = review(reviews)
    time, memory = [sources[link.evidence_id] for link in ledger.entry("p", "R-1").evidence]
    check(reviews, found, time, "supported")
    assert shown(ledger) == UNVERIFIED  # one quote unchecked
    check(reviews, found, memory, "supported")
    assert shown(ledger) == CONFIRMED


def test_a_result_without_quotes_waits_for_the_user(store: Any) -> None:
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger)
    assert shown(ledger) == UNVERIFIED
    ledger.check("p", "R-1", 1, USER, operation_id="checked")
    assert shown(ledger) == ([("R-1", "user")], [])


def test_the_users_published_result_is_confirmed_by_the_user(store: Any) -> None:
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger, TIME, author=USER)
    assert shown(ledger) == ([("R-1", "user")], [])


def test_a_withdrawn_reference_confirms_nothing(store: Any) -> None:
    """Review hardening: a quote of a reference that is withdrawn, refuted or superseded
    does not confirm by its check."""
    reviews, _, _ = store
    ledger = setup(reviews)
    result(ledger, TIME)
    found, sources = review(reviews)
    check(reviews, found, only(sources), "supported")
    assert shown(ledger) == CONFIRMED
    ledger.change(
        "p",
        {"operation_id": "w", "reason": "The paper was withdrawn.",
         "changes": [{"entry": "REF-1", "expected": 1, "status": "withdrawn"}]},
        MAIN,
    )  # fmt: skip
    assert shown(ledger) == UNVERIFIED


def pdf_quote(reviews: ReviewStore, tmp_path: Path, author: Author) -> tuple[Ledger, str]:
    """REF-1, recorded by ``author`` with a PDF; the agent read page 3 of it, and R-1
    quotes lines 30 and 31; a review of it found the quote supported."""
    artifact, digest = publish(Ledger(reviews.documents), tmp_path, b"%PDF-1.4\n")
    ledger = setup(reviews, author, {"artifact_id": artifact, "revision": 1})
    lines = {30: "Our attack recovers the key", 31: "in time 2^120 with memory 2^60."}
    found = pages(digest, "paper.pdf", lines)
    call(reviews, 2, "pdf_pages", {"path": "paper.pdf", "pages": "3"},
         format_pdf("pages", found), {"pdf": found})  # fmt: skip
    text = "recovers the key in time 2^120"
    result(ledger, {"source": "pdf", "page": 3, "first_line": 30, "last_line": 31, "text": text})
    checked, sources = review(reviews)
    assert len(sources) == 1
    check(reviews, checked, only(sources), "supported")
    return ledger, digest


def test_a_pdf_an_agent_named_confirms_nothing_until_the_user_vouches(
    store: Any, tmp_path: Path
) -> None:
    """Review defect 2: an agent may name any artifact as a reference's PDF, one it made
    included, so the check of a quote of it confirms nothing until the user records a
    revision of the reference that names the PDF. A reference holds no evidence, so the
    user cannot check one."""
    reviews, _, _ = store
    ledger, _ = pdf_quote(reviews, tmp_path, MAIN)
    assert shown(ledger) == UNVERIFIED
    with pytest.raises(DocumentError, match="a reference holds no evidence"):
        ledger.check("p", "REF-1", 1, USER, operation_id="vouched")
    assert shown(ledger) == UNVERIFIED
    ledger.change(
        "p",
        {"operation_id": "named", "reason": "I read this PDF.",
         "changes": [{"entry": "REF-1", "expected": 1, "title": "A paper (PDF read)"}]},
        USER,
    )  # fmt: skip
    assert shown(ledger) == CONFIRMED


def test_a_pdf_the_user_named_confirms_by_the_check(store: Any, tmp_path: Path) -> None:
    reviews, _, _ = store
    ledger, _ = pdf_quote(reviews, tmp_path, USER)
    assert shown(ledger) == CONFIRMED
