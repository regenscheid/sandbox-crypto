# SPDX-License-Identifier: MIT
"""The reviewer's check of a quote (docs/specs/consistency-review.md, CR-4, as the
coordinator decided): once it has read a quote's recorded source and the entry revision
that quotes it, the reviewer records whether the source supports it, bound to the
revision's material; and the reviewer's tool reaches it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.citations import record_citation
from sandbox_science.orchestrator.consistency.ledger import ingest_ledger
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import DocumentError
from sandbox_science.orchestrator.ledger import Ledger, gate
from sandbox_science.orchestrator.model import ScriptedModel

from .test_consistency import MAIN, dispatch
from .test_consistency import store as store
from .test_review_quotes import STAMP, quoting, read

TOOL = "crypto_research_mcp-e-print-mcp_eprint_fulltext"


def quoted(reviews: ReviewStore) -> tuple[str, str, str]:
    """C-1 quotes the ePrint of REF-1 that the agent read; a review of it: the review,
    the quote's recorded source in it and C-1's revision in it."""
    ledger = Ledger(reviews.documents)
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                 "fields": {"identifiers": {"eprint": "2024/123"},
                            "published": "2024-02-01"}}  # fmt: skip
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN)
    passage = f"[2024/123] A paper | Date: {STAMP} | ... the twist has order 4q'. ..."
    read(reviews, 1, TOOL, {"paper_id": "2024/123", "query": "twist"}, passage)
    quote = {"source": "eprint", "dated": STAMP, "text": "the twist has order 4q'."}
    claim = {"type": "claim", "origin": "published", "title": "Twist",
             "statement": "The twist has order $4q'$.", "evidence": [quoting(quote)]}  # fmt: skip
    ledger.change("p", {"operation_id": "c", "reason": "r", "changes": [claim]}, MAIN)
    assert ingest_ledger(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    (source,) = [i["evidence"] for i in items if "quoted_by" in i["locator"]]
    (entry,) = [
        i["evidence"] for i in items if i["kind"] == "ledger" and i["locator"]["handle"] == "C-1"
    ]
    return review, source, entry


def test_the_reviewer_checks_a_quote_it_has_read(store: Any) -> None:
    reviews, _, _ = store
    review, source, entry = quoted(reviews)
    verdict = {"source": source, "verdict": "supported", "explanation": "It says so."}
    with pytest.raises(DocumentError, match="read the quote's recorded source") as unread:
        record_citation(reviews, "p", "s", review, "owner", verdict)
    assert unread.value.kind == "policy_denied"
    for evidence in (source, entry):
        reviews.read_evidence("p", "s", review, evidence, owner="owner")
    with pytest.raises(DocumentError, match="no quote's recorded source") as other:
        record_citation(reviews, "p", "s", review, "owner", {**verdict, "source": entry})
    assert other.value.kind == "not_found"
    made = record_citation(reviews, "p", "s", review, "owner", verdict)
    assert (made["entry"], made["revision"], made["verdict"]) == ("C-1", 1, "supported")
    view = Ledger(reviews.documents).entry("p", "C-1")
    with reviews.documents.control.read() as tx:
        material = gate.examined(tx.sql, view.entry_id, {1})[1]
        row = tx.sql.execute("SELECT evidence_id,material,verdict FROM citation_checks").fetchone()
    assert tuple(row) == (view.evidence[0].evidence_id, gate.fingerprint(material), "supported")


@pytest.mark.asyncio
async def test_the_reviewers_tool_records_the_check(store: Any) -> None:
    reviews, _, _ = store
    review, source, entry = quoted(reviews)

    def call(name: str, **arguments: Any) -> dict[str, Any]:
        return {"name": name, "arguments": arguments}

    model = ScriptedModel.from_json(
        [
            {"tool_calls": [call("review_read", evidence=source),
                            call("review_read", evidence=entry)]},
            {"tool_calls": [call("review_citation", source=source, verdict="supported",
                                 explanation="The passage states it.")]},
            {"text": json.dumps({"outcome": "passed", "summary": "The citation holds."})},
        ]
    )  # fmt: skip
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    with reviews.documents.control.read() as tx:
        verdicts = [row[0] for row in tx.sql.execute("SELECT verdict FROM citation_checks")]
    assert verdicts == ["supported"]
