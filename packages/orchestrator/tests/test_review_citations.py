# SPDX-License-Identifier: MIT
"""The reviewer's check of a quote (docs/specs/consistency-review.md, CR-4, as the
coordinator decided, and the review of it): once it has read all of a quote's source and
of the entry revision that quotes it, the reviewer records whether the source supports
it, bound to the revision's material and to the tool and arguments the source came from;
and the reviewer's tool reaches it."""

from __future__ import annotations

import hashlib
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
from .test_review_quotes import EPRINT_TOOL, STAMP, call, passages, quoting

ARGUMENTS = {"paper_id": "2024/123", "query": "twist"}


def quoted(reviews: ReviewStore, arguments: dict[str, Any] = ARGUMENTS) -> tuple[str, str, str]:
    """C-1 quotes the ePrint of REF-1 that the agent read; a review of it: the review,
    the quote's source in it and C-1's revision in it."""
    ledger = Ledger(reviews.documents)
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                 "fields": {"identifiers": {"eprint": "2024/123"},
                            "published": "2024-02-01"}}  # fmt: skip
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN)
    found = "Curve coefficients. We show that the twist has order 4q' with q' prime."
    call(reviews, 1, EPRINT_TOOL, arguments, passages("2024/123", "twist", found),
         {"connector": "crypto-research-mcp"})  # fmt: skip
    quote = {"source": "eprint", "dated": STAMP, "text": "the twist has order 4q' with q' prime"}
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


def read_all(reviews: ReviewStore, review: str, *evidence: str) -> None:
    for item in evidence:
        start: int | None = 0
        while start is not None:
            start = reviews.read_evidence("p", "s", review, item, start=start, owner="owner")[
                "next_start"
            ]


def test_the_reviewer_checks_a_quote_it_has_read_whole(store: Any) -> None:
    reviews, _, _ = store
    review, source, entry = quoted(reviews)
    verdict = {"source": source, "verdict": "supported", "explanation": "It says so."}
    with pytest.raises(DocumentError, match="read all of the quote's source") as unread:
        record_citation(reviews, "p", "s", review, "owner", verdict)
    assert unread.value.kind == "policy_denied"
    read_all(reviews, review, source, entry)
    with pytest.raises(DocumentError, match="no quote's recorded source") as other:
        record_citation(reviews, "p", "s", review, "owner", {**verdict, "source": entry})
    assert other.value.kind == "not_found"
    made = record_citation(reviews, "p", "s", review, "owner", verdict)
    assert (made["entry"], made["revision"], made["verdict"]) == ("C-1", 1, "supported")
    view = Ledger(reviews.documents).entry("p", "C-1")
    with reviews.documents.control.read() as tx:
        material = gate.examined(tx.sql, view.entry_id, {1})[1]
        row = tx.sql.execute(
            "SELECT evidence_id,material,verdict,source_tool,source_arguments FROM citation_checks"
        ).fetchone()
    assert tuple(row[:3]) == (view.evidence[0].evidence_id, gate.fingerprint(material), "supported")
    assert (row[3], json.loads(row[4])) == (EPRINT_TOOL, ARGUMENTS)


def test_one_character_of_the_source_is_no_read_of_it(store: Any) -> None:
    """Review hardening: "read both" means the whole item, not one character of it."""
    reviews, _, _ = store
    review, source, entry = quoted(reviews)
    read_all(reviews, review, entry)
    reviews.read_evidence("p", "s", review, source, count=1, owner="owner")
    verdict = {"source": source, "verdict": "supported", "explanation": "It says so."}
    with pytest.raises(DocumentError, match="read all of the quote's source"):
        record_citation(reviews, "p", "s", review, "owner", verdict)
    total = reviews.read_evidence("p", "s", review, source)["total_characters"]
    reviews.read_evidence("p", "s", review, source, start=2, owner="owner")
    with pytest.raises(DocumentError, match="read all of the quote's source"):
        record_citation(reviews, "p", "s", review, "owner", verdict)  # a gap at 1
    reviews.read_evidence("p", "s", review, source, start=1, count=total - 1, owner="owner")
    assert record_citation(reviews, "p", "s", review, "owner", verdict)["verdict"] == "supported"


def test_each_verdict_is_recorded(store: Any) -> None:
    reviews, _, _ = store
    review, source, entry = quoted(reviews)
    read_all(reviews, review, source, entry)
    for verdict in ("unsupported", "inconclusive", "supported"):
        record_citation(reviews, "p", "s", review, "owner",
                        {"source": source, "verdict": verdict, "explanation": "Why."})  # fmt: skip
    with reviews.documents.control.read() as tx:
        rows = [r[0] for r in tx.sql.execute("SELECT verdict FROM citation_checks ORDER BY rowid")]
    assert rows == ["unsupported", "inconclusive", "supported"]
    with pytest.raises(DocumentError, match="invalid citation check"):
        record_citation(reviews, "p", "s", review, "owner",
                        {"source": source, "verdict": "true", "explanation": "Why."})  # fmt: skip


@pytest.mark.asyncio
async def test_the_reviewers_tool_records_the_check(store: Any) -> None:
    reviews, _, _ = store
    review, source, entry = quoted(reviews)

    def tool(name: str, **arguments: Any) -> dict[str, Any]:
        return {"name": name, "arguments": arguments}

    model = ScriptedModel.from_json(
        [
            {"tool_calls": [tool("review_read", evidence=source),
                            tool("review_read", evidence=entry)]},
            {"tool_calls": [tool("review_citation", source=source, verdict="supported",
                                 explanation="The passage states it.")]},
            {"text": json.dumps({"outcome": "passed", "summary": "The citation holds."})},
        ]
    )  # fmt: skip
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    with reviews.documents.control.read() as tx:
        verdicts = [row[0] for row in tx.sql.execute("SELECT verdict FROM citation_checks")]
    assert verdicts == ["supported"]


def test_long_arguments_are_kept_clipped_and_by_their_digest(store: Any) -> None:
    """The verification's N3: the arguments were cut at 8192 characters, but the column
    holds 8192 bytes of JSON, so a query of two-byte characters failed the review. The
    check keeps valid JSON within the bound, saying it is clipped, and the whole's digest."""
    reviews, _, _ = store
    arguments = {"paper_id": "2024/123", "query": "twist " + "é" * 5000}
    review, source, entry = quoted(reviews, arguments)
    read_all(reviews, review, source, entry)
    verdict = {"source": source, "verdict": "supported", "explanation": "It says so."}
    record_citation(reviews, "p", "s", review, "owner", verdict)
    with reviews.documents.control.read() as tx:
        kept, digest = tx.sql.execute(
            "SELECT source_arguments,source_arguments_sha256 FROM citation_checks"
        ).fetchone()
    whole = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    assert len(kept.encode()) <= 8192
    assert whole.startswith(json.loads(kept)["clipped"])
    assert digest == hashlib.sha256(whole.encode()).hexdigest()


def test_a_check_that_cannot_be_stored_is_the_tools_error(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verification's N3: a storage error is the reviewer's tool error, which the
    reviewer sees, not the review's failure."""
    from sandbox_science.orchestrator.consistency import citations

    reviews, _, _ = store
    review, source, entry = quoted(reviews)
    read_all(reviews, review, source, entry)
    monkeypatch.setattr(citations, "fitted_json", lambda encoded, limit: "x" * (limit + 1))
    verdict = {"source": source, "verdict": "supported", "explanation": "It says so."}
    with pytest.raises(DocumentError, match="could not be stored") as refused:
        record_citation(reviews, "p", "s", review, "owner", verdict)
    assert refused.value.kind == "invalid_request"

def test_a_supported_citation_check_confirms_an_agents_published_result(store: Any) -> None:
    """LG-16 as amended: the main agent's published result is best known once the
    consistency reviewer found its quote supported, and unverified again when its measures
    change, which the check did not cover."""
    from .test_ledger_frontier import CLASSICAL, KEY_RECOVERY

    reviews, _, _ = store
    ledger = Ledger(reviews.documents)
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                 "fields": {"identifiers": {"eprint": "2024/123"},
                            "published": "2024-02-01"}}  # fmt: skip
    ledger.change("p", {"operation_id": "ref", "reason": "r", "changes": [reference]}, MAIN)
    passage = f"[2024/123] A paper | Date: {STAMP} | ... key recovery in time 2^120. ..."
    read(reviews, 1, TOOL, {"paper_id": "2024/123", "query": "time"}, passage)
    quote = {"source": "eprint", "dated": STAMP, "text": "key recovery in time 2^120."}
    fields = {"category": "key-recovery", "conditions": CLASSICAL,
              "measures": {"time": 120, "memory": 60}}  # fmt: skip
    result = {"type": "result", "origin": "published", "title": "P1", "statement": "Time 2^120.",
              "fields": fields, "evidence": [quoting(quote)]}  # fmt: skip
    ledger.change("p", {"operation_id": "r", "reason": "r", "changes": [result]}, MAIN)

    def frontier() -> tuple[list[Any], list[Any]]:
        (group,) = ledger.frontier("p", "key-recovery").groups
        return (
            [(r.handle, r.confirmed_by) for r in group.best],
            [(r.handle, r.unverified) for r in group.candidates],
        )

    assert frontier() == ([], [("R-1", True)])
    assert ingest_ledger(reviews, "p", "s")["complete"]
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    (source,) = [i["evidence"] for i in items if "quoted_by" in i["locator"]]
    (entry,) = [
        i["evidence"] for i in items if i["kind"] == "ledger" and i["locator"]["handle"] == "R-1"
    ]
    for evidence in (source, entry):
        reviews.read_evidence("p", "s", review, evidence, owner="owner")
    record_citation(
        reviews, "p", "s", review, "owner",
        {"source": source, "verdict": "supported", "explanation": "The passage says 2^120."},
    )  # fmt: skip
    assert frontier() == ([("R-1", "citation check")], [])
    faster = {**fields, "measures": {"time": 100, "memory": 60}}
    ledger.change(
        "p",
        {"operation_id": "f", "reason": "r",
         "changes": [{"entry": "R-1", "expected": 1, "fields": faster}]},
        MAIN,
    )  # fmt: skip
    assert frontier() == ([], [("R-1", True)])
