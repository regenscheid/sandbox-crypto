# SPDX-License-Identifier: MIT
"""The e-print server's results as its tools write them (crypto-research-mcp,
``eprint_mcp/tools.py`` on main, 2026-11-25: ``_reading_header``, ``eprint_fulltext``,
``search_eprint_fulltext``, ``get_eprint`` and ``_date_line``; the notes from
``revisions.note`` and ``throttle.warning``), with made-up page text, for the tests of
quote sources and the read register. ``OBSERVED`` is the head of a result the deployed
platform received on 2026-11-25."""

from __future__ import annotations

from collections.abc import Sequence

READ: str = "e-print-mcp_eprint_fulltext"
SEARCH: str = "e-print-mcp_search_eprint_fulltext"
RECORD: str = "e-print-mcp_get_eprint"
PAPER: str = "2017/634"
TITLE: str = "CRYSTALS -- Kyber: a CCA-secure module-lattice-based KEM"
DATED: str = "2017-06-29 (posted; revised 2020-10-14)"
"""``get_eprint``'s Date line since 2026-11-13 (``_date_line``): the day first posted,
then the latest revision."""

STORED: str = "Source: stored ePrint original."
MIRROR: str = (
    "Source: GitHub ePrint mirror, snapshot 2026-11-01. Metadata records a newer revision: "
    "2020-10-14. Markdown conversion is queued; extracted text is available meanwhile."
)
BUDGET: str = (
    "fetch budget: 6 of 30 hourly fetches left. Later fetches will be declined until the "
    "window rolls over."
)
RENDERED: str = (
    "Converted from a PDF rendered from the PostScript original; mathematical symbols may be "
    "approximate."
)
POSTSCRIPT: str = (
    "Text extracted from PostScript; layout and mathematical symbols may be approximate."
)
COMPATIBILITY: str = (
    "Compatibility: query now belongs to search_eprint_fulltext; eprint_fulltext reads pages."
)
PAGE_ONE: str = "Kyber is a made-up stand-in here. Its IND-CCA2 security rests on the problem."
PAGE_TWO: str = "We give the parameters in Table 1, chosen for a 128-bit security level."

OBSERVED: str = (
    f"NOTE: {STORED}\n\n[{PAPER}] {TITLE}\nPhysical pages 1-2 of 16.\n\n"
    f"## Page 1\n\n{PAGE_ONE}\n\n## Page 2\n\n{PAGE_TWO}\n\n"
    f'Next: eprint_fulltext(paper_id="{PAPER}", start_page=3, page_count=2).'
)
"""The cluster's read of 2017/634, its head as received, with made-up page text."""


def header(note: str | None = STORED, conversion: str | None = None) -> str:
    """``_reading_header``: a ``NOTE:`` line and a blank line when there is a note, the
    paper's line, and a PostScript original's conversion line."""
    head = f"[{PAPER}] {TITLE}\n" + (f"{conversion}\n" if conversion else "")
    return (f"NOTE: {note}\n\n" if note else "") + head


def read(
    texts: Sequence[str],
    *,
    start: int = 1,
    total: int = 16,
    note: str | None = STORED,
    conversion: str | None = None,
) -> str:
    """``eprint_fulltext``'s answer for pages ``start`` on."""
    end = start + len(texts) - 1
    pages = "\n\n".join(f"## Page {start + i}\n\n{text}" for i, text in enumerate(texts))
    footer = (
        "End of paper."
        if end == total
        else f'Next: eprint_fulltext(paper_id="{PAPER}", start_page={end + 1}, page_count=2).'
    )
    return (
        header(note, conversion)
        + f"Physical pages {start}-{end} of {total}.\n\n{pages}\n\n{footer}"
    )


def search(
    query: str,
    found: Sequence[tuple[int, str]],
    *,
    note: str | None = STORED,
    conversion: str | None = None,
) -> str:
    """``search_eprint_fulltext``'s answer: each passage under its physical page."""
    if not found:
        return header(note, conversion) + (
            f"\nNo body passage matching {query!r}. The body text is available (9000 "
            "characters). This search matches a literal phrase within a line, not a "
            f'natural-language question. Read it with eprint_fulltext(paper_id="{PAPER}", '
            "start_page=1)."
        )
    passages = "\n\n".join(f"Physical page {page}\n{text}" for page, text in found)
    return header(note, conversion) + f"\n{len(found)} passage(s) matching {query!r}:\n\n{passages}"


def compatible(query: str, found: Sequence[tuple[int, str]], **head: str | None) -> str:
    """``eprint_fulltext`` called with a query: its line, then the search's answer."""
    return f"{COMPATIBILITY}\n\n" + search(query, found, **head)


def record(dated: str = DATED) -> str:
    """``get_eprint``'s answer: the record's lines, then the abstract."""
    return (
        f"[{PAPER}] {TITLE}\nAuthors: A. Author; B. Author\nDate: {dated}\n"
        f"Category: Public-key cryptography\nArchive page: https://eprint.iacr.org/{PAPER}\n"
        f"Direct PDF: https://eprint.iacr.org/{PAPER}.pdf\n\nThe abstract.\nDate: 2001-01-01"
    )
