# SPDX-License-Identifier: MIT
"""The sources of quotes (docs/specs/consistency-review.md, CR-4, as the coordinator
decided): the platform finds a ledger entry's quote in its own record of the tool
results an agent read, a PDF's numbered lines under the PDF's full SHA-256 or an ePrint
passage for the paper and date stamp, and pins that record beside the entry in its
review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.store import ReviewStore, dump
from sandbox_science.orchestrator.ledger import Ledger

from .test_consistency import MAIN, dispatch
from .test_consistency import store as store
from .test_ledger import REFERENCE_FIELDS
from .test_ledger_evidence import publish

STAMP = "2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z"


def read(reviews: ReviewStore, number: int, name: str, arguments: Any, result: str) -> None:
    """The agent's call of tool ``name`` and its result, as the review feed records them."""
    call = f"call-{number}"
    for index, message in enumerate(
        (
            {"role": "assistant", "content": "", "tool_calls": [{"id": call, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]},
            {"role": "tool", "tool_call_id": call, "content": result},
        )
    ):  # fmt: skip
        reviews.observe(
            "p",
            "s",
            f"turn:message:{number}:{index}",
            "transcript",
            {"run": "author-1", "invocation": "turn", "message": 2 * number + index},
            {"role": message["role"], "content": dump(message), "truncated": False},
        )


def quoting(quote: dict[str, Any]) -> dict[str, Any]:
    return {"role": "supports", "type": "source_claim", "entry": "REF-1", "quote": quote}


def test_a_quote_found_in_the_recorded_tool_output_is_pinned_beside_its_entry(
    store: Any, tmp_path: Path
) -> None:
    """CR-4: the quote of the PDF's page 3, lines 30 and 31, and of the ePrint passage,
    are found in the results the agent read, and the review holds those results."""
    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    artifact, digest = publish(ledger, tmp_path, b"%PDF-1.4\n")
    pdf = {"artifact_id": artifact, "revision": 1}
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                 "fields": {**REFERENCE_FIELDS, "pdf": pdf}}  # fmt: skip
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN)
    pages = "\n".join((
        f"paper.pdf (sha256 {digest})", "--- page 3 (printed 2) ---", "[p3:29] Let q be prime.",
        "[p3:30] The twist has order", "[p3:31] 4q' with q' prime.",
    ))  # fmt: skip
    read(reviews, 1, "pdf_pages", {"path": "paper.pdf", "pages": "3"}, pages)
    passage = f"[2024/123] A paper | Date: {STAMP} | ... the twist has order 4q'. ..."
    eprint_tool = "crypto_research_mcp-e-print-mcp_eprint_fulltext"
    read(reviews, 2, eprint_tool, {"paper_id": "2024/123", "query": "twist"}, passage)
    # Another paper's passage holds the same words, and is not this paper's source.
    other = f"[2024/999] Other | Date: {STAMP} | ... the twist has order 4q'. ..."
    read(reviews, 3, eprint_tool, {"paper_id": "2024/999", "query": "twist"}, other)
    found = {"source": "pdf", "page": 3, "first_line": 30, "last_line": 31,
             "text": "The twist has order 4q' with q' prime."}  # fmt: skip
    eprint = {"source": "eprint", "dated": STAMP, "text": "the twist  has order\n4q'."}
    missed = {**found, "first_line": 29, "last_line": 29}
    claim = {"type": "claim", "origin": "published", "title": "Twist", "statement": "s",
             "evidence": [quoting(found), quoting(eprint), quoting(missed)]}  # fmt: skip
    ledger.change("p", {"operation_id": "c", "reason": "r", "changes": [claim]}, MAIN)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    items = [
        item
        for item in reviews.manifest("p", "s", review, limit=50)["evidence"]
        if item["kind"] == "transcript" and "quoted_by" in item["locator"]
    ]
    evidence = [link.evidence_id for link in ledger.entry("p", "C-1").evidence]
    assert [item["locator"]["quoted_by"]["evidence"] for item in items] == evidence[:2]
    sources = [reviews.evidence("p", "s", review, item["evidence"]) for item in items]
    assert f"(sha256 {digest})" in sources[0]["payload"]["content"]
    assert "[2024/123]" in sources[1]["payload"]["content"]
