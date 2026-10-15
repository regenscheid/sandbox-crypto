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

import pytest

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
EPRINT_TOOL = "e-print-mcp_eprint_fulltext"
CONNECTOR = "crypto-research-mcp"
SERVER = {"connector": CONNECTOR}
TWIST = "the twist of the curve has order 4q' with q' prime"


def exchange(number: int, name: str, arguments: Any, content: str) -> list[dict[str, Any]]:
    """The assistant's call of tool ``name`` and its result, as the transcript holds them."""
    call_id = f"call-{number}"
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]  # fmt: skip


def executed(name: str, arguments: Any, record: Any) -> dict[str, Any]:
    """The call as the run executed it, with its record (``tool_records``)."""
    return {"tool": name, "arguments": json.dumps(arguments), **record}


def call(
    reviews: ReviewStore,
    number: int,
    name: str,
    arguments: Any,
    content: str,
    record: Any,
    *,
    session: str = "s",
) -> None:
    """The agent's call of tool ``name`` in run author-1, its result as the model saw it,
    and the tool's record, as the review controller observes them (its key as
    ``ReviewController._source`` writes it)."""
    messages = exchange(number, name, arguments, content)
    for index, message in enumerate(messages):
        reviews.observe(
            "p", session, f"turn:message:{number}:{index}", "transcript",
            {"run": "author-1", "invocation": "turn", "message": 2 * number + index},
            {"role": message["role"], "content": dump(message), "truncated": False},
        )  # fmt: skip
    found = source_of(messages, executed(name, arguments, record), (CONNECTOR,))
    if found is not None:
        mark, locator, source = found
        reviews.observe(
            "p", session, f"source:{mark}:turn:{number}", "source",
            {"run": "author-1", "invocation": "turn", "message": 2 * number + 1, **locator},
            source, complete=not source.get("lines_truncated"),
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
        return head + f"\nNo passage matching {query!r}."
    blocks = "".join(f"\n  ...\n{text}\n" for text in found)
    return head + f"\n{len(found)} passage(s) matching {query!r}:\n{blocks}"


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
         passages("2024/123", made_up), SERVER)  # fmt: skip
    found = f"Curve coefficients. We show {TWIST}, as Table 2 lists."
    call(reviews, 2, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found), SERVER)  # fmt: skip
    echoed = {"source": "eprint", "dated": STAMP, "text": made_up}
    abstract = {"source": "eprint", "dated": STAMP, "text": f"The abstract says {TWIST}"}
    passage = {"source": "eprint", "dated": STAMP, "text": f"We show {TWIST}"}
    planted = {**passage, "dated": "2026-01-01T00:00:00Z"}
    assert recorded(ledger, reviews, echoed, abstract, passage, planted) == ["q3"]


def test_a_search_for_the_quote_then_the_quote_is_a_source(store: Any) -> None:
    """The verification's N5: a quote equal to the query counts when a passage holds it
    with more than the query, as a search for a phrase and then its quote does; a
    passage that is the query alone vouches for nothing. The query is read as the call's
    decoded strings, so a quote with double quotes in it is caught."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    searched = f"We show {TWIST}"
    call(reviews, 1, EPRINT_TOOL, {"paper_id": "2024/123", "query": searched},
         passages("2024/123", searched, f"Coefficients. {searched}, as Table 2 lists."),
         SERVER)  # fmt: skip
    quoted = 'we call "the twist" of order 4q\' a quadratic twist'
    call(reviews, 2, EPRINT_TOOL, {"paper_id": "2024/123", "query": quoted},
         passages("2024/123", quoted, quoted), SERVER)  # fmt: skip
    assert recorded(ledger, reviews, {"source": "eprint", "dated": STAMP, "text": searched},
                    {"source": "eprint", "dated": STAMP, "text": quoted}) == ["q1"]  # fmt: skip


def test_a_query_with_a_single_quote_is_parsed(store: Any) -> None:
    """The verification's N5: the server echoes a query that holds ``'`` in double
    quotes, as ``repr`` does, and its passages are still read."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    query = "order 4q' with q'"
    assert f'matching "{query}"' in passages("2024/123", query, "x")
    call(reviews, 1, EPRINT_TOOL, {"paper_id": "2024/123", "query": query},
         passages("2024/123", query, f"Curve coefficients. We show {TWIST}."),
         SERVER)  # fmt: skip
    assert recorded(ledger, reviews, {"source": "eprint", "dated": STAMP,
                                      "text": f"We show {TWIST}"}) == ["q1"]  # fmt: skip


def test_duplicate_call_ids_and_other_tools_attribute_nothing() -> None:
    """The verification's N1: two calls under one ID, a web fetch and the e-print
    tool, and the web fetch's result was taken for the e-print server's. The executed
    call decides, and an ID the transcript names twice attributes nothing; so does a
    name the transcript does not give, a connector the project does not configure, a
    look-alike name, and a failed call."""
    found = passages("2024/123", "twist", f"We show {TWIST}.")
    fetch = {"name": "web-fetch-mcp_web_fetch", "arguments": json.dumps({"url": "u"})}
    eprint = {"name": EPRINT_TOOL, "arguments": json.dumps({"paper_id": "2024/123"})}
    twice = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": eprint},
            {"id": "call-1", "type": "function", "function": fetch}]},
        {"role": "tool", "tool_call_id": "call-1", "content": found},
    ]  # fmt: skip
    for made in (fetch, eprint):
        assert source_of(twice, {"tool": made["name"], "arguments": made["arguments"],
                                 **SERVER}, (CONNECTOR,)) is None  # fmt: skip
    once = exchange(1, EPRINT_TOOL, {"paper_id": "2024/123"}, found)
    good = executed(EPRINT_TOOL, {"paper_id": "2024/123"}, SERVER)
    assert source_of(once, good, (CONNECTOR,)) is not None
    for wrong in (
        executed("web-fetch-mcp_web_fetch", {"paper_id": "2024/123"}, SERVER),
        executed(EPRINT_TOOL, {"paper_id": "2024/999"}, SERVER),
        {**good, "error": "upstream_failed"},
    ):
        assert source_of(once, wrong, (CONNECTOR,)) is None
    assert source_of(once, good, ("another-server",)) is None
    lookalike = exchange(2, "x-" + EPRINT_TOOL, {"paper_id": "2024/123"}, found)
    assert source_of(lookalike, executed("x-" + EPRINT_TOOL, {"paper_id": "2024/123"},
                                         SERVER), (CONNECTOR,)) is None  # fmt: skip


def test_a_genuine_source_is_found_past_fifty_older_ones_of_its_session(store: Any) -> None:
    """The verification's N2: the lookup read the project's 50 oldest records that
    named the paper, the platform's copies included, so a genuine source after them was
    never pinned. It reads the session's records of the quote's mark, newest first; a
    source in another session is not this session's."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    for number in range(1, 61):
        call(reviews, number, EPRINT_TOOL, {"paper_id": "2024/123", "query": "curve"},
             passages("2024/123", "curve", f"Curve number {number} is ordinary."),
             SERVER)  # fmt: skip
    found = f"Curve coefficients. We show {TWIST}, as Table 2 lists."
    call(reviews, 61, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found), SERVER, session="other")  # fmt: skip
    quote = {"source": "eprint", "dated": STAMP, "text": f"We show {TWIST}"}
    assert recorded(ledger, reviews, quote) == []
    call(reviews, 62, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found), SERVER)  # fmt: skip
    ledger.change(
        "p",
        {"operation_id": "t", "reason": "r",
         "changes": [{"entry": "C-1", "expected": 1, "title": "Twist again"}]},
        MAIN,
    )  # fmt: skip
    assert ingest_ledger(reviews, "p", "s")["complete"]
    with reviews.documents.control.read() as tx:
        pinned = [
            json.loads(row[0])["call"]
            for row in tx.sql.execute(
                "SELECT locator FROM review_feed WHERE source_key LIKE 'quoted:%'"
            )
        ]
    assert pinned == ["call-62"]


def test_a_read_no_quote_cites_stays_out_of_the_review(store: Any, tmp_path: Path) -> None:
    """The verification's N4: every raw source came into each review, and a long read
    cut short made its coverage incomplete. Only a source a quote cites comes in."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    artifact, digest = publish(ledger, tmp_path, b"%PDF-1.4\n")
    paper(ledger, {"artifact_id": artifact, "revision": 1})
    long = {n: f"Line {n} of a long paper, with words." for n in range(1, 4500)}
    result = pages(digest, "paper.pdf", long, page=5)
    call(reviews, 1, "pdf_pages", {"path": "paper.pdf", "pages": "5"},
         format_pdf("pages", result), {"pdf": result})  # fmt: skip
    with reviews.documents.control.read() as tx:
        (complete,) = tx.sql.execute(
            "SELECT complete FROM review_feed WHERE source_key LIKE 'source:%'"
        ).fetchone()
    assert complete == 0  # the record was cut short
    review = dispatch(reviews)
    assert reviews.run("p", "s", review)["coverage_complete"] == 1
    kinds = {i["kind"] for i in reviews.manifest("p", "s", review, limit=50)["evidence"]}
    assert "source" not in kinds


@pytest.mark.asyncio
async def test_the_controller_records_the_executed_call_under_its_mark(store: Any) -> None:
    """The review controller records a source from the call the run executed, through
    a connector the project configures, under a key that begins with its mark."""
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.consistency.quotes import mark

    from .test_consistency import context_for

    reviews, _, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None,
        connectors=(CONNECTOR,),
    )  # fmt: skip
    arguments = {"paper_id": "2024/123", "query": "twist"}
    messages = exchange(1, EPRINT_TOOL, arguments, passages("2024/123", "twist", TWIST))
    await controller.safe_point(
        messages, tool=SERVER, call=executed(EPRINT_TOOL, arguments, SERVER)
    )
    fetched = executed("web-fetch-mcp_web_fetch", arguments, SERVER)
    await controller.safe_point(messages, tool=SERVER, call=fetched)
    with reviews.documents.control.read() as tx:
        keys = [
            r[0] for r in tx.sql.execute("SELECT source_key FROM review_feed WHERE kind='source'")
        ]
    (key,) = keys
    assert key.startswith(f"source:{mark('eprint', '2024/123')}:")


def test_a_reread_source_and_a_new_revision_do_not_stop_the_ledgers_intake(store: Any) -> None:
    """Review attack 4: the agent read the passage again, and the entry was revised; the
    source key lacked the revision, and the intake failed on every safe point after."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    paper(ledger)
    found = f"We show {TWIST}, as Table 2 lists."
    for number in (1, 2):
        call(reviews, number, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
             passages("2024/123", "twist", found), SERVER)  # fmt: skip
    quote = {"source": "eprint", "dated": STAMP, "text": f"We show {TWIST}"}
    assert recorded(ledger, reviews, quote) == ["q1"]
    call(reviews, 3, EPRINT_TOOL, {"paper_id": "2024/123", "query": "twist"},
         passages("2024/123", "twist", found + " Again."), SERVER)  # fmt: skip
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
