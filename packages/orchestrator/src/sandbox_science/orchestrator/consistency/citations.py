# SPDX-License-Identifier: MIT
"""The reviewer's check of a quote (docs/specs/consistency-review.md, CR-4, as the
coordinator decided on 2026-10-15).

A review that holds a quote's source (``consistency/quotes.py``): what a tool returned,
with the tool and its arguments, which may be an agent's own PDF unless it is vouched for,
may record
whether it supports the entry revision that quotes it: ``supported`` when the source
holds the quoted text and it supports what the entry states, its numbers included;
``unsupported`` when it does not; ``inconclusive`` when the source cannot decide. The
reviewer must have read all of the source and of the entry revision in this review. The
check keeps the tool and the arguments the source came from: the arguments whole when
they fit 8 KiB of JSON, else a clipped copy, valid JSON, beside the digest of the whole.
The check is bound to the digest of the revision's statement, fields, assumptions and
evidence, so a later revision that keeps them keeps it, and one that changes them needs
another.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Final, Literal, cast

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

_log = logging.getLogger(__name__)


class CitationInput(Contract):
    """A reviewer's verdict on one quote's source."""

    source: EvidenceID
    """The pinned source: a ``source`` item of this review that names ``quoted_by``."""
    verdict: Literal["supported", "unsupported", "inconclusive"]
    explanation: str = Field(min_length=1, max_length=4096)


MAX_ARGUMENTS: Final = 8192
"""The most bytes of a source's arguments a check keeps, as JSON."""


def fitted_text(text: str, limit: int) -> str:
    """``text``, cut to at most ``limit`` bytes of UTF-8 at a character's edge."""
    return text.encode()[:limit].decode(errors="ignore")


def fitted_json(encoded: str, limit: int) -> str:
    """``encoded``, a JSON text, when it is at most ``limit`` bytes; else valid JSON within
    them that says it is clipped and holds the start of it (``{"clipped": ...}``). The
    check keeps the digest of the whole beside it."""
    if len(encoded.encode()) <= limit:
        return encoded
    kept = len(encoded)
    while True:
        clipped = json.dumps({"clipped": encoded[:kept]}, ensure_ascii=False)
        over = len(clipped.encode()) - limit
        if over <= 0:
            return clipped
        kept = max(0, kept - over)  # a character is at least one byte


def read_whole(db: sqlite3.Connection, review: str, evidence: str) -> bool:
    """Whether the review read all of item ``evidence``, from its first character to its
    last, in the slices it asked for."""
    ranges = [
        location
        for (raw,) in db.execute(
            "SELECT locator FROM review_reads WHERE review_id=? AND evidence_id=? "
            "ORDER BY seq LIMIT 1000",
            (review, evidence),
        )
        if (location := cast(Json, json.loads(raw))).get("block") is None
        and (location.get("returned_characters", 0) > 0 or location.get("empty_source"))
    ]
    if any(location.get("empty_source") for location in ranges):
        return True
    total = max((int(location.get("total_characters", 0)) for location in ranges), default=0)
    end = 0
    for location in sorted(ranges, key=lambda value: int(value["start"])):
        if int(location["start"]) > end:
            return False
        end = max(end, int(location["start"]) + int(location["returned_characters"]))
    return total > 0 and end >= total


def record_citation(
    store: ReviewStore, project: str, session: str, review: str, owner: str, raw: object
) -> Json:
    """Record a reviewer's check of the quote whose source ``raw`` names."""
    try:
        value = CitationInput.model_validate(raw)
    except ValidationError:
        raise DocumentError(INVALID_REQUEST, "invalid citation check") from None
    explanation = agent_text(value.explanation, "the explanation")
    with store.documents.control.write() as tx:
        db = tx.sql
        store.owner(store.load_run(db, project, session, review), owner)
        row = db.execute(
            "SELECT kind,locator,payload FROM review_evidence WHERE evidence_id=? AND review_id=?",
            (value.source, review),
        ).fetchone()
        found: object = None if row is None else stored_json(row[1]).get("quoted_by")
        if row is None or row[0] != "source" or not isinstance(found, dict):
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
        if (
            item is None
            or not read_whole(db, review, value.source)
            or not read_whole(db, review, item[0])
        ):
            raise DocumentError(
                POLICY_DENIED,
                "read all of the quote's source and of the entry revision that quotes it "
                "with review_read before checking the citation",
            )
        record = stored_json(row[2])
        tool = fitted_text(str(record.get("tool", "")), 128) or "unknown"
        whole = json.dumps(record.get("arguments", {}), ensure_ascii=False, sort_keys=True)
        material = gate.examined(db, entry, {revision}).get(revision)
        assert material is not None  # noqa: S101 - the revision holds the quote
        check = new_id("citation")
        try:
            db.execute(
                "INSERT INTO citation_checks(check_id,project,review_id,entry_id,"
                "entry_revision,evidence_id,material,source,source_tool,source_arguments,"
                "source_arguments_sha256,verdict,explanation,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    check,
                    project,
                    review,
                    entry,
                    revision,
                    quote,
                    gate.fingerprint(material),
                    value.source,
                    tool,
                    fitted_json(whole, MAX_ARGUMENTS),
                    hashlib.sha256(whole.encode()).hexdigest(),
                    value.verdict,
                    explanation,
                    store.clock(),
                ),
            )
        except sqlite3.IntegrityError:
            # A check that does not fit its record is the reviewer's tool error, never the
            # review's failure (the verification's N3).
            _log.warning("citation check for %s/%s not stored", entry, quote, exc_info=True)
            raise DocumentError(INVALID_REQUEST, "the citation check could not be stored") from None
    return {
        "citation": check,
        "entry": str(quoted.get("handle", entry)),
        "revision": revision,
        "quote": quote,
        "verdict": value.verdict,
    }
