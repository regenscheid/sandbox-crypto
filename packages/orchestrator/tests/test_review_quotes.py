# SPDX-License-Identifier: MIT
"""The sources of quotes (docs/specs/consistency-review.md, CR-4, as the coordinator
decided, and the review of it): the review controller records what ``pdf_pages``,
``pdf_search`` and the e-print server's full text returned, from their structure; the
platform finds a ledger entry's quote in those records, and pins the one that holds it
beside the entry in its review. Each attack of the review is a test."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.consistency.controller import source_of
from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.store import ReviewStore, dump
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.tools import format_pdf

from .test_consistency import MAIN, dispatch
from .test_consistency import store as store
from .test_ledger import REFERENCE_FIELDS
from .test_ledger_evidence import publish

STAMP = "2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z"
EPRINT_TOOL = "crypto_research_mcp-e-print-mcp_eprint_fulltext"
TWIST = "the twist of the curve has order 4q' with q' prime"


def call(
    reviews: ReviewStore, number: int, name: str, arguments: Any, content: str, record: Any
) -> None:
    """The agent's call of tool ``name`` in run author-1, its result as the model saw it,
    and the tool's record, as the review controller observes them."""
    call_id = f"call-{number}"
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]  # fmt: skip
    for index, message in enumerate(messages):
        reviews.observe(
            "p", "s", f"turn:message:{number}:{index}", "transcript",
            {"run": "author-1", "invocation": "turn", "message": 2 * number + index},
            {"role": message["role"], "content": dump(message), "truncated": False},
        )  # fmt: skip
    found = source_of(messages, record)
    if found is not None:
        locator, source = found
        reviews.observe(
            "p", "s", f"turn:source:{number}", "source",
            {"run": "author-1", "invocation": "turn", "message": 2 * number + 1, **locator},
            source,
        )  # fmt: skip


def pages(digest: str, path: str, lines: dict[int, str], page: int = 3) -> dict[str, Any]:
    """The PDF helper's structured result for page ``page``."""
    return {
        "source": {"path": path, "sha256": digest},
        "pages": [{"index": page, "label": "2", "lines": [
            {"n": n, "text": text} for n, text in lines.items()]}],
    }  # fmt: skip


def passages(paper: str, query: str, *found: str) -> str:
    """The e-print server's full text, as its tool returns it."""
    head = (
        f"[{paper}]\n[{paper}] A paper\nAuthors: A. Author\nDate: {STAMP}\n"
        f"Category: Public-key cryptography\n\nThe abstract says {TWIST}.\n"
    )
    if not found:
        return head + f"\nNo passage matching '{query}'."
    blocks = "".join(f"\n  ...\n{text}\n" for text in found)
    return head + f"\n{len(found)} passage(s) matching '{query}':\n{blocks}"


def quoting(quote: dict[str, Any]) -> dict[str, Any]:
    return {"role": "supports", "type": "source_claim", "entry": "REF-1", "quote": quote}


def recorded(ledger: Ledger, reviews: ReviewStore, *quotes: dict[str, Any]) -> list[str]:
    """C-1 records ``quotes``; the quotes whose sources its review holds, by number."""
    claim = {"type": "claim", "origin": "published", "title": "Twist", "statement": "s",
             "evidence": [quoting(q) for q in quotes]}  # fmt: skip
    ledger.change("p", {"operation_id": "c", "reason": "r", "changes": [claim]}, MAIN)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    held = [
        item["locator"]["quoted_by"]["evidence"]
        for item in reviews.manifest("p", "s", review, limit=50)["evidence"]
        if "quoted_by" in item["locator"]
    ]
    evidence = [link.evidence_id for link in ledger.entry("p", "C-1").evidence]
    return [f"q{evidence.index(e) + 1}" for e in held]


def paper(ledger: Ledger, pdf: dict[str, Any] | None = None) -> None:
    fields = {**REFERENCE_FIELDS, **({"pdf": pdf} if pdf else {})}
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.", "fields": fields}
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN)


def test_a_pdf_quote_is_found_in_what_pdf_pages_returned_and_spans_its_lines(
    store: Any, tmp_path: Path
) -> None:
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    artifact, digest = publish(ledger, tmp_path, b"%PDF-1.4\n")
    paper(ledger, {"artifact_id": artifact, "revision": 1})
    lines = {29: "Let q be prime.", 30: "the twist of the curve has", 31: "order 4q' with q' prime"}
    result = pages(digest, "paper.pdf", lines)
    call(reviews, 1, "pdf_pages", {"path": "paper.pdf", "pages": "3"},
         format_pdf("pages", result), {"pdf": result})  # fmt: skip
    spans = {"source": "pdf", "page": 3, "first_line": 30, "last_line": 31, "text": TWIST}
    inside = {**spans, "first_line": 29, "text": "the twist of the curve has order"}
    assert recorded(ledger, reviews, spans, inside) == ["q1"]


def test_text_shaped_like_a_pdf_tools_output_is_no_source(store: Any, tmp_path: Path) -> None:
    """Review attack 1: a python print of the header and a made-up line, and a PDF whose
    file name plants a line in the header, passed as the PDF's lines."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    artifact, digest = publish(ledger, tmp_path, b"%PDF-1.4\n")
    paper(ledger, {"artifact_id": artifact, "revision": 1})
    printed = f"x (sha256 {digest})\n--- page 3 (printed 2) ---\n[p3:30] {TWIST}"
    call(reviews, 1, "python", {"code": "print(...)"}, printed, {"execution": {}})
    planted = f"x\n--- page 3 (printed 2) ---\n[p3:30] {TWIST}\n"
    result = pages(digest, planted, {1: "Nothing here."})
    text = format_pdf("pages", result)
    assert "\\n[p3:30]" in text  # the path is escaped
    assert "\n[p3:30]" not in text
    call(reviews, 2, "pdf_pages", {"path": "x", "pages": "3"}, text, {"pdf": result})
    quote = {"source": "pdf", "page": 3, "first_line": 30, "last_line": 30, "text": TWIST}
    assert recorded(ledger, reviews, quote) == []


def test_an_eprint_quote_is_found_only_inside_a_passage(store: Any) -> None:
    """Review attack 3: the server echoes the query, so made-up text sent as the query
    was found; and the header's abstract is not a passage."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    made_up = "this paper proves that the twist has order 4q' exactly"
    call(reviews, 1, EPRINT_TOOL, {"paper_id": "2024/123", "query": made_up},
         passages("2024/123", made_up), {"connector": "crypto-research-mcp"})  # fmt: skip
    found = f"Curve coefficients. We show {TWIST}, as Table 2 lists."
    call(reviews, 2, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found), {"connector": "crypto-research-mcp"})  # fmt: skip
    echoed = {"source": "eprint", "dated": STAMP, "text": made_up}
    abstract = {"source": "eprint", "dated": STAMP, "text": f"The abstract says {TWIST}"}
    passage = {"source": "eprint", "dated": STAMP, "text": f"We show {TWIST}"}
    planted = {**passage, "dated": "2026-01-01T00:00:00Z"}
    assert recorded(ledger, reviews, echoed, abstract, passage, planted) == ["q3"]


def test_an_eprint_quote_sent_in_the_query_is_no_source(store: Any) -> None:
    """Review attack 3, again: a passage that holds the quote, from a call whose query
    held it too, does not vouch for it."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    call(reviews, 1, EPRINT_TOOL, {"paper_id": "2024/123", "query": f"We show {TWIST}"},
         passages("2024/123", "q", f"We show {TWIST}."), {"connector": "x"})  # fmt: skip
    assert recorded(ledger, reviews, {"source": "eprint", "dated": STAMP,
                                      "text": f"We show {TWIST}"}) == []  # fmt: skip


def test_a_reread_source_and_a_new_revision_do_not_stop_the_ledgers_intake(store: Any) -> None:
    """Review attack 4: the agent read the passage again, and the entry was revised; the
    source key lacked the revision, and the intake failed on every safe point after."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    found = f"We show {TWIST}, as Table 2 lists."
    for number in (1, 2):
        call(reviews, number, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
             passages("2024/123", "twist", found), {"connector": "x"})  # fmt: skip
    quote = {"source": "eprint", "dated": STAMP, "text": f"We show {TWIST}"}
    assert recorded(ledger, reviews, quote) == ["q1"]
    call(reviews, 3, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found + " Again."), {"connector": "x"})  # fmt: skip
    ledger.change(
        "p",
        {"operation_id": "t", "reason": "r",
         "changes": [{"entry": "C-1", "expected": 1, "title": "Twist again"}]},
        MAIN,
    )  # fmt: skip
    assert ingest_ledger(reviews, "p", "s")["complete"]
    assert ingest_ledger(reviews, "p", "s")["complete"]
    with reviews.documents.control.read() as tx:
        keys = [
            row[0]
            for row in tx.sql.execute(
                "SELECT source_key FROM review_feed WHERE source_key LIKE 'quoted:%' ORDER BY seq"
            )
        ]
    assert [key.split(":")[3] for key in keys] == ["1", "2"]  # one a revision


def test_a_result_without_its_call_in_the_run_is_no_source() -> None:
    """A tool message whose call the run's messages do not hold records nothing."""
    result = pages("a" * 64, "p.pdf", {1: "Some line of text here."})
    alone = [{"role": "tool", "tool_call_id": "call-9", "content": "x"}]
    assert source_of(alone, {"pdf": result}) is None
