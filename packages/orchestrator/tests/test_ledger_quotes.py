# SPDX-License-Identifier: MIT
"""Quotes (docs/specs/ledger.md, LG-8; CR-4 as the coordinator decided): a published
claim or result quotes the reference it cites, a PDF's page and lines, pinned by the
PDF's SHA-256, or an ePrint passage, named by the paper and the date stamp the e-print
tools gave it; and version 30, the evidence targets lane S's quotes and lane D's
delegations share."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import DocumentError

from .ledger_support import AnchoredLedger as Ledger
from .test_ledger import NEW, REFERENCE_FIELDS, change
from .test_ledger import ledger as ledger
from .test_ledger_evidence import publish

STAMP = "2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z"
PDF = {"source": "pdf", "page": 3, "label": "2", "first_line": 30, "last_line": 31,
       "text": "The twist has order 4q' with q' prime."}  # fmt: skip
EPRINT = {"source": "eprint", "dated": STAMP, "text": "The twist has order 4q'."}
PUBLISHED = {**NEW["claim"], "origin": "published"}


def quoting(quote: dict[str, Any], reference: str = "new:paper", **more: Any) -> dict[str, Any]:
    return {"role": "supports", "type": "source_claim", "entry": reference, "quote": quote, **more}


def paper(**fields: Any) -> dict[str, Any]:
    return {**NEW["reference"], "key": "paper", "fields": {**REFERENCE_FIELDS, **fields}}


def test_a_published_claim_quotes_its_references_pdf_or_eprint(
    ledger: Ledger, tmp_path: Path
) -> None:
    artifact, digest = publish(ledger, tmp_path, b"%PDF-1.4\n")
    pdf = {"pdf": {"artifact_id": artifact, "revision": 1}}
    change(ledger, paper(**pdf), {**PUBLISHED, "evidence": [quoting(PDF)]})
    (held,) = ledger.entry("p", "C-1").evidence
    assert (held.target, held.handle, held.revision, held.sha256) == ("quote", "REF-1", 1, digest)
    assert held.locator == {**PDF, "named": {"artifact_id": artifact, "revision": 1}}
    # The same reference's ePrint, by the identifier its fields give.
    change(
        ledger,
        {"entry": "C-1", "expected": 1, "evidence": [quoting(EPRINT, "REF-1")]},
        op="eprint",
    )
    _, quoted = ledger.entry("p", "C-1").evidence
    assert (quoted.sha256, quoted.locator) == (None, {**EPRINT, "named": {"eprint": "2024/123"}})
    # A quote is what the claim cites: it is a published claim's citation (LG-15).
    change(ledger, {**PUBLISHED, "evidence": [quoting(EPRINT, "REF-1")]}, op="alone")
    assert ledger.entry("p", "C-2").status == "recorded"


def test_a_quote_names_what_it_quotes(ledger: Ledger) -> None:
    change(ledger, {**NEW["reference"], "fields": {"identifiers": {"doi": "10.1/x"},
                                                   "retrieved": "2026-10-15"}})  # fmt: skip
    change(ledger, NEW["claim"], op="claim")
    for n, (item, message) in enumerate(
        (
            (quoting(PDF, "REF-1"), "quotes a PDF of REF-1, which names none"),
            (quoting(EPRINT, "REF-1"), "quotes an ePrint of REF-1, which names none"),
            (quoting(PDF, "C-1"), "a quote names the reference it quotes"),
            ({**quoting(PDF, "REF-1"), "type": "proof"}, "a quote is source_claim evidence"),
            (quoting({**PDF, "page": None}, "REF-1"), "names its page"),
            (quoting({**PDF, "dated": STAMP}, "REF-1"), "names no date stamp"),
            (quoting({**EPRINT, "dated": None}, "REF-1"), "names the date stamp"),
            (quoting({**PDF, "first_line": 32}, "REF-1"), "run from its first to its last"),
            (quoting(PDF, "REF-1", sha256="a" * 64), "names its reference, its revision"),
            (quoting({**EPRINT, "text": "order 4q'."}, "REF-1"), "at least 20 characters"),
        )
    ):
        with pytest.raises(DocumentError, match=message) as refused:
            change(ledger, {**PUBLISHED, "evidence": [item]}, op=f"bad-{n}")
        assert refused.value.kind in ("invalid_request", "policy_denied")


def test_an_agents_quote_holds_no_hidden_text(ledger: Ledger) -> None:
    change(ledger, paper())
    hidden = quoting({**EPRINT, "text": "The twist​ has order 4q'."}, "REF-1")
    with pytest.raises(DocumentError, match="evidence 1's quote holds U\\+200B"):
        change(ledger, {**PUBLISHED, "evidence": [hidden]}, op="hidden")


def test_a_version_29_store_takes_quotes_after_the_migration(
    ledger: Ledger, tmp_path: Path
) -> None:
    """Version 30 rebuilds ledger_evidence with the quote and delegation targets and a
    locator: the evidence an older store holds reads as it did."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    cited = {"role": "supports", "type": "source_claim", "entry": "new:paper"}
    change(ledger, paper(), {**PUBLISHED, "evidence": [cited]})
    before = ledger.entry("p", "C-1").evidence
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 27)
    with sqlite3.connect(store_path(old)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 29
        columns = {row[1] for row in raw.execute("PRAGMA table_info(ledger_evidence)")}
    assert "locator" not in columns
    documents = DocumentStore(store_path(old))
    documents.initialize()
    try:
        moved = Ledger(documents)
        assert moved.entry("p", "C-1").evidence == before
        moved.change(
            "p",
            {"operation_id": "q", "reason": "r",
             "changes": [{"entry": "C-1", "expected": 1, "evidence": [quoting(EPRINT, "REF-1")]}]},
            moved_author(),
        )  # fmt: skip
        assert moved.entry("p", "C-1").evidence[-1].target == "quote"
    finally:
        documents.close()


def moved_author() -> Any:
    from .test_ledger import MAIN

    return MAIN


LIBRARY = {"source": "library", "page": 4, "revision": "3fa2c1d0e9b8",
           "text": "The twist has order 4q' with q' prime."}  # fmt: skip


def test_a_library_quote_is_named_by_the_references_identifiers(ledger: Ledger) -> None:
    """A library quote names its page and the revision the library's result named, and
    the reference's identifiers in the schemes a library work label can name: a read of
    eprint/2024/123 or doi:10.1007/x_1 is the reference's paper (``papers``)."""
    change(ledger, paper(identifiers={"eprint": "2024/123", "doi": "10.1007/X_1", "isbn": "1"}),
           {**PUBLISHED, "evidence": [quoting(LIBRARY)]})  # fmt: skip
    (held,) = ledger.entry("p", "C-1").evidence
    assert (held.target, held.sha256) == ("quote", None)
    assert held.locator == {**LIBRARY, "named": {"eprint": "2024/123", "doi": "10.1007/X_1"}}


def test_a_library_quote_names_a_page_a_revision_and_a_paper_the_library_labels(
    ledger: Ledger,
) -> None:
    """A library quote of a reference known only by a URL or an ISBN is refused: the
    library labels such a paper by an OpenAlex ID or a URL, which confirms nothing."""
    change(ledger, {**NEW["reference"], "fields": {"identifiers": {"url": "https://x.test/p"},
                                                   "retrieved": "2026-10-15"}})  # fmt: skip
    change(ledger, paper(), op="eprint-ref")
    for n, (item, message) in enumerate(
        (
            (quoting(LIBRARY, "REF-1"), "names no eprint, doi, arxiv or nist identifier"),
            (quoting(LIBRARY, "REF-1"), "by an OpenAlex ID or a URL, cannot be confirmed"),
            (quoting({**LIBRARY, "page": None}, "REF-2"), "names the physical page and the"),
            (quoting({**LIBRARY, "revision": None}, "REF-2"), "names the physical page and the"),
            (quoting({**LIBRARY, "revision": "3FA2"}, "REF-2"), "revision"),
            (quoting({**LIBRARY, "dated": STAMP}, "REF-2"), "names its page and revision only"),
            (quoting({**LIBRARY, "first_line": 1, "last_line": 2}, "REF-2"), "revision only"),
            (quoting({**EPRINT, "revision": "3fa2c1d0e9b8"}, "REF-2"), "only a library quote"),
        )
    ):
        with pytest.raises(DocumentError, match=message) as refused:
            change(ledger, {**PUBLISHED, "evidence": [item]}, op=f"bad-{n}")
        assert refused.value.kind in ("invalid_request", "policy_denied")
