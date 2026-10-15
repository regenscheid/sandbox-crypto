# SPDX-License-Identifier: MIT
"""The reviewer's check of a quote (docs/specs/consistency-review.md, CR-4, as the
coordinator decided on 2026-10-15).

A review that holds a quote's recorded source (``consistency/quotes.py``) may record
whether it supports the entry revision that quotes it: ``supported`` when the source
holds the quoted text and it supports what the entry states, its numbers included;
``unsupported`` when it does not; ``inconclusive`` when the source cannot decide. The
reviewer must have read the source and the entry revision in this review. The check is
bound to the digest of the revision's statement, fields, assumptions and evidence, so a
later revision that keeps them keeps it, and one that changes them needs another.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.ledger import evidence, gate

from .models import EvidenceID
from .store import Json, ReviewStore, stored_json


class CitationInput(Contract):
    """A reviewer's verdict on one quote's recorded source."""

    source: EvidenceID
    """The pinned source: a transcript item of this review that names ``quoted_by``."""
    verdict: Literal["supported", "unsupported", "inconclusive"]
    explanation: str = Field(min_length=1, max_length=4096)


def record_citation(
    store: ReviewStore, project: str, session: str, review: str, owner: str, raw: object
) -> Json:
    """Record a reviewer's check of the quote whose recorded source ``raw`` names."""
    try:
        value = CitationInput.model_validate(raw)
    except ValidationError:
        raise DocumentError(INVALID_REQUEST, "invalid citation check") from None
    explanation = agent_text(value.explanation, "the explanation")
    with store.documents.control.write() as tx:
        db = tx.sql
        store.owner(store.load_run(db, project, session, review), owner)
        row = db.execute(
            "SELECT kind,locator FROM review_evidence WHERE evidence_id=? AND review_id=?",
            (value.source, review),
        ).fetchone()
        found: object = None if row is None else stored_json(row[1]).get("quoted_by")
        if row is None or row[0] != "transcript" or not isinstance(found, dict):
            raise DocumentError(
                NOT_FOUND, "source is no quote's recorded source in this review's snapshot"
            )
        quoted = cast(Json, found)
        entry, revision, quote = (
            str(quoted["entry"]),
            int(quoted["revision"]),
            str(quoted["evidence"]),
        )
        held = [
            link
            for link in evidence.stored(db, entry, revision)
            if link.evidence_id == quote and link.target == "quote"
        ]
        if not held:
            raise DocumentError(NOT_FOUND, "the entry revision holds no such quote")
        item = db.execute(
            "SELECT evidence_id FROM review_evidence WHERE review_id=? AND kind='ledger' AND "
            "json_extract(locator,'$.entry')=? AND json_extract(locator,'$.revision')=? "
            "ORDER BY ordinal LIMIT 1",
            (review, entry, revision),
        ).fetchone()
        read = {
            e
            for (e,) in db.execute(
                "SELECT DISTINCT evidence_id FROM review_reads WHERE review_id=?", (review,)
            )
        }
        if value.source not in read or item is None or item[0] not in read:
            raise DocumentError(
                POLICY_DENIED,
                "read the quote's recorded source and the entry revision that quotes it "
                "with review_read before checking the citation",
            )
        material = gate.examined(db, entry, {revision}).get(revision)
        assert material is not None  # noqa: S101 - the revision holds the quote
        check = new_id("citation")
        db.execute(
            "INSERT INTO citation_checks(check_id,project,review_id,entry_id,entry_revision,"
            "evidence_id,material,source,verdict,explanation,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                check,
                project,
                review,
                entry,
                revision,
                quote,
                gate.fingerprint(material),
                value.source,
                value.verdict,
                explanation,
                store.clock(),
            ),
        )
    return {
        "citation": check,
        "entry": str(quoted.get("handle", entry)),
        "revision": revision,
        "quote": quote,
        "verdict": value.verdict,
    }
