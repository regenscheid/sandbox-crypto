# SPDX-License-Identifier: MIT
"""The sources of quotes (docs/specs/consistency-review.md, CR-4, as the coordinator
decided on 2026-10-15, and his review of it).

A ledger entry's quote of a reference names what it quotes: a PDF's page and lines,
pinned by the PDF's SHA-256; an ePrint passage, named by the paper and the date stamp
the e-print tools gave it; or a library text's physical page, named by the paper and the
revision the library's result gave. When an agent's run calls ``pdf_pages`` or
``pdf_search``, or one of the e-print server's tools that return a paper's text or its
date stamp, or the library's ``read_paper`` or ``search_in_paper``, through a
connector the project configures, the review controller records the call it executed and
its result as a ``source`` observation: the tool, its arguments, and the result's
structure, not its text for the model. For a PDF, the PDF's SHA-256 and path as the
sandbox's helper reported them, and each line it returned, with its page index, label and
number. For an ePrint, the paper from the result's header, and:

- ``search_eprint_fulltext`` (since 2026-10-31): the passages it returned as matching the
  phrase, each with its physical page, apart from the header and the phrase it echoes;
- ``eprint_fulltext`` (since 2026-10-31): the pages it read, each page's text apart from
  the header and the server's pointer to the next pages;
- ``get_eprint``: the date stamp from its ``Date`` line, and no text. Neither tool above
  prints the date stamp, so a quote of their text names the one ``get_eprint`` gave the
  paper in the same session;
- ``eprint_fulltext`` called with a query: the search's passages, after the server's line
  that says so;
- ``eprint_fulltext`` as it was before 2026-10-31, in records and archives of then: the
  date stamp from its header and the passages it returned as matching its query.

The text tools' header may hold the server's own lines (``descriptors.eprint_header``): a
``NOTE:`` before the paper's line, such as ``NOTE: Source: stored ePrint original.``, and a
line on a PostScript original's conversion after it. The record keeps them as ``notes``,
never as the paper's text; they are no header, heading or passage, and confirm nothing.

For a library text (``library_record``), the paper's work label and the ledger identifier
it names (``papers``), and the call's ``identifier`` when it is exactly another one, which
the library resolved to that paper (an ePrint report the library files under its
published DOI), under each of which the record is found (``source_keys``); the
rendition's tier, converter, page count and revision; and the pages ``read_paper``
returned or the passages ``search_in_paper`` found, each with its physical page: a page
without the notes people made on it, which are theirs, not the paper's. A page or
passage the toolbox cut in the middle is in no record, from either server.

Call and result come from the same run.

The platform looks for a quote in those records: a PDF quote in lines ``first_line`` to
``last_line`` of its page, under the PDF's hash, spanning them (it begins in the first
and ends in the last); an ePrint quote inside one passage or page of a result for the
paper, with the date stamp the record holds or, for a record without one, a date stamp a
``get_eprint`` record of the session gave that paper; a library quote inside a page or
passage of its physical page, in a record of the reference's paper at the quote's
revision, under any identifier of the reference's that the record is under; the phrase
or query the server echoes is never a passage, and a quote the call sent counts only in a
passage that holds more than what was sent. White space is read as one space. Each quote
reads the newest records of its mark in the session, by the feed's key index. A quote
found brings its record beside the entry in its review, once in each window (raw sources
are not review evidence); a quote not found has no source, and its citation cannot be
confirmed. What a tool returned may still be an agent's own: a PDF the agent made, unless
the PDF is vouched for (LG-16).

When an agent records an ePrint or library quote no record of the session holds yet for a
mechanical reason, ``unmatched`` says so, from those records alone, in the change's
result: the date stamps ``get_eprint`` gave the paper, or the revisions and pages read,
or, with no read of the paper, the papers the session's library reads are of. It is
advice; the quote is recorded as given, and the review reads the records again.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Collection
from typing import Any, Final, cast

from sandbox_science.orchestrator.context.history.descriptors import eprint_header
from sandbox_science.orchestrator.context.history.reads import spans
from sandbox_science.orchestrator.ledger.models import EntryView, EvidenceLink
from sandbox_science.orchestrator.papers import LEDGER_SCHEMES, exact_paper, library_paper, paper_id

from .store import Json, stored_json

MAX_QUOTED: Final = 8
"""The most quotes of one entry revision whose sources come into a review."""

MAX_CANDIDATES: Final = 50
"""The most source records the platform reads for one quote, newest first."""

MAX_LINES: Final = 4000
"""The most PDF lines one source record keeps."""

PDF_TOOLS: Final = ("pdf_pages", "pdf_search")
EPRINT_TOOL: Final = "e-print-mcp_eprint_fulltext"
"""The e-print server's full-text tool, under the name its portal lists it: the
``e-print-mcp_`` prefix names the e-print server behind it. Since 2026-10-31 it reads a
paper by page; before, it returned the passages matching a query."""
EPRINT_SEARCH: Final = "e-print-mcp_search_eprint_fulltext"
"""The e-print server's search of one paper's text for a phrase (since 2026-10-31)."""
EPRINT_RECORD: Final = "e-print-mcp_get_eprint"
"""The e-print server's catalog record of a paper: its date stamp, and no text."""
EPRINT_TOOLS: Final = (EPRINT_TOOL, EPRINT_SEARCH, EPRINT_RECORD)
EPRINT_TEXT_TOOLS: Final = (EPRINT_TOOL, EPRINT_SEARCH)
"""The e-print tools whose results hold a paper's own text."""
LIBRARY_READ: Final = "library-mcp_read_paper"
"""The library server's read of a held paper's text by physical page."""
LIBRARY_SEARCH: Final = "library-mcp_search_in_paper"
"""The library server's search of one held paper's text for a literal phrase."""
LIBRARY_TOOLS: Final = (LIBRARY_READ, LIBRARY_SEARCH)
"""The library tools whose results hold a paper's own text. Not its cards
(``paper_card``, a model's summary), its numbered statements (``paper_statements``, put
together from blocks of the text, across a page break and with footnotes and running
heads left out, so not a passage the page holds as it stands), its notes
(``paper_notes``, people's) or its other extracts."""
QUOTED_TOOLS: Final = (*EPRINT_TOOLS, *LIBRARY_TOOLS)
"""The connector tools whose results the review controller records as sources."""
MAX_FIELDS: Final = 8
"""The most further fields of a library result's rendition line a record keeps."""
MAX_NAMED: Final = 3
"""The most date stamps, revisions or papers an ``unmatched`` notice names."""
ALIAS: Final = "alias_of"
"""A source row's payload field that holds the key of the row with its record: the row
that indexes a library record under one of its further identifiers (``source_keys``)."""

# The server echoes the query as ``repr`` gives it: in single quotes, or in double quotes
# when it holds a single quote. ``repr`` keeps it on one line.
_MARKER: Final = re.compile(r"""\n\n\d+ passage\(s\) matching (?:'.*'|".*"):\n""")
_PASSAGE: Final = re.compile(r"\n?[ \t]*\.\.\.\n")
_PAPER: Final = re.compile(r"\A\[([^\]\n]{1,64})\]")
_DATED: Final = re.compile(r"^Date: (.{1,128})$", re.M)
# A search's passages (2026-10-31): the count and the phrase under the header, after a blank
# line; each passage begins with its physical page, after a blank line.
_SEARCHED: Final = re.compile(r"""\A\n\d+ passage\(s\) matching (?:'.*'|".*"):\n""")
_SEARCH_PASSAGE: Final = re.compile(r"(?:\A\n*|\n\n)Physical page (\d{1,6})\n")
# A page read (2026-10-31): its range under the header, then each page under its heading,
# then a pointer to the next pages or the end.
_PAGES: Final = re.compile(r"\APhysical pages (\d{1,6})-(\d{1,6}) of (\d{1,6})\.\n")
_PAGE: Final = re.compile(r"(?:\A|\n)\n?## Page (\d{1,6})\n")
_TRAILER: Final = re.compile(r"\n+(?:Next: eprint_fulltext\(.*\)\.|End of paper\.)\s*\Z")
# The toolbox's mark where it cut a result in the middle (``tools.bound``): a page or
# passage that holds it lacks its middle, and is no source.
_OMITTED: Final = re.compile(r"\[… \d{1,12} characters omitted …\]")
# The library's results (library_mcp/tools.py): the paper's work label and title, then its
# rendition, with the revision (the PDF's SHA-256, its first 12 characters or more) and
# any further ", key value" fields; then a read's range or a search's echo of the phrase.
_WORK: Final = re.compile(r"\A(\S{1,512}) — [^\n]*\n")
_RENDITION: Final = re.compile(
    r"([a-z][a-z0-9_-]{0,31}) rendition by ([^\n,]{1,128}), (\d{1,6}|\?) pages, "
    r"revision ([0-9a-f]{12,64})((?:, [^\n, ]{1,64}(?: [^\n, ]{1,64})* [^\n, ]{1,128})*)\n"
)
_FIELD: Final = re.compile(r", ([^\n,]{1,64}) ([^\n, ]{1,128})")
_LIBRARY_PAGES: Final = re.compile(
    r"Physical pages (\d{1,6})-(\d{1,6}) of (\d{1,6})\.(?: In the web reader: \S{1,2048})?\n"
)
# A page's heading may say a person corrected it, as a suffix in parentheses: a flag on
# the page, not its text.
_LIBRARY_PAGE: Final = re.compile(r"(?:\A\n*|\n\n)## Page (\d{1,6})(?: \(([^()\n]{1,120})\))?\n")
_LIBRARY_TRAILER: Final = re.compile(r"\n+(?:Next: read_paper\([^\n]*\)\.|End of text\.)\s*\Z")
# After a page's text, the notes people made on it, one a line, each opening a line with
# "[Note by ": theirs, never the paper's. Everything from the first to the page's end is
# left out, so a quote that matches only inside a note is found nowhere.
_NOTES: Final = re.compile(r"\n\n\[Note by ")
# The server's word after a block whose mathematics the conversion lost (reading.marked).
_MATH_NOTE: Final = re.compile(
    r"\n\n\[The mathematics above is unreliable in this text \([^\n]{0,400}?\); read it in "
    r"the PDF\.\]"
)
_NO_TEXT: Final = "(no text on this page in this rendition)"
_LIBRARY_MATCHED: Final = re.compile(r"""\d{1,3} passage\(s\) matching (?:'.*'|".*"):\n""")


def squash(text: str) -> str:
    """``text`` with each run of white space one space, and none at either end."""
    return " ".join(text.split())


def mark(kind: str, value: str) -> str:
    """What a source's key begins with: a digest of the PDF's hash or the paper's
    identifier, so that the feed's key index finds a quote's candidates."""
    return hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()


def mark_of(record: Json) -> str:
    """The mark of source record ``record``."""
    if "pdf" in record:
        return mark("pdf", str(cast(Json, record["pdf"]).get("sha256")))
    if "library" in record:
        library = cast(Json, record["library"])
        return mark("paper", str(library.get("named") or library.get("paper")))
    return mark("eprint", str(cast(Json, record.get("eprint") or {}).get("paper")))


def source_keys(record: Json, suffix: str) -> list[str]:
    """The keys the review controller records source record ``record`` under, each
    ``source:<mark>:<suffix>``: its mark's first, which holds the record, then, for a
    library record, the mark of each further identifier the library vouched for
    (``aliases``), whose row holds only the first key (``ALIAS``), so that the feed's key
    index finds the record under every name of its paper."""
    marks = [mark_of(record)]
    library = cast(Json, record.get("library") or {})
    marks += [mark("paper", str(a)) for a in cast(list[Any], library.get("aliases") or [])]
    return [f"source:{found}:{suffix}" for found in dict.fromkeys(marks)]


def pdf_record(tool: str, arguments: Json, result: Json) -> Json | None:
    """The source record of a ``pdf_pages`` or ``pdf_search`` call: the PDF's SHA-256
    and path, and each line returned, from the helper's structured result."""
    source = cast(Json, result.get("source") or {})
    sha256 = source.get("sha256")
    if tool not in PDF_TOOLS or not isinstance(sha256, str) or len(sha256) != 64:
        return None
    lines: list[Json] = []
    for page in cast(list[Any], result.get("pages") or []):
        page = cast(Json, page)
        for line in cast(list[Any], page.get("lines") or []):
            line = cast(Json, line)
            lines.append(
                {"page": page.get("index"), "label": page.get("label"), "n": line.get("n"),
                 "text": str(line.get("text", ""))}
            )  # fmt: skip
    for hit in cast(list[Any], result.get("hits") or []):
        hit = cast(Json, hit)
        lines.append(
            {"page": hit.get("page_index"), "label": hit.get("page_label"), "n": hit.get("n"),
             "text": str(hit.get("text", ""))}
        )  # fmt: skip
    return {
        "tool": tool,
        "arguments": arguments,
        "pdf": {"sha256": sha256, "path": str(source.get("path", ""))[:1024]},
        "lines": lines[:MAX_LINES],
        "lines_truncated": len(lines) > MAX_LINES,
    }


def eprint_record(tool: str, arguments: Json, text: str) -> Json | None:
    """The source record of an e-print call: the paper from the result's header, the date
    stamp where the result prints one, the passages or pages of the paper's text it
    returned, and the server's own lines in the header (``notes``). None for a result that
    names no paper, and for a ``get_eprint`` record without a date stamp."""
    if tool not in EPRINT_TOOLS:
        return None
    if tool == EPRINT_RECORD:
        paper = _PAPER.match(text)
        # Only the record's lines, before its abstract, which may hold a line like one.
        line = _DATED.search(text.split("\n\n", 1)[0])
        if paper is None or line is None:
            return None
        found: Json = {"paper": paper.group(1), "dated": line.group(1).strip(), "passages": []}
        return {"tool": tool, "arguments": arguments, "eprint": found}
    header = eprint_header(text, compatible=tool == EPRINT_TOOL)
    if header is None:
        return None
    named, notes, rest = header
    read = _PAGES.match(rest) if tool == EPRINT_TOOL else None
    searched = _SEARCHED.match(rest)
    dated: str | None = None
    passages: list[str] = []
    pages: list[int | None] = []
    if read is not None:
        pages, passages = _page_texts(_TRAILER.sub("", rest[read.end() :]))
    elif searched is not None:
        pages, passages = _search_passages(rest[searched.end() :])
    elif tool == EPRINT_TOOL:
        # eprint_fulltext as it was before 2026-10-31: the record's lines, with the date
        # stamp, then passages after the query's echo. Only a Date line among the record's
        # lines: a passage of the paper may hold a line like one.
        stamp = _DATED.search(rest.split("\n\n", 1)[0])
        dated = None if stamp is None else stamp.group(1).strip()
        echoed = _MARKER.search(rest) if dated is not None else None
        if echoed is not None:
            passages = [
                squash(block)
                for block in _PASSAGE.split(rest[echoed.end() :])
                if block.strip() and not _OMITTED.search(block)
            ]
            pages = [None] * len(passages)
    record: Json = {"paper": named, "dated": dated, "passages": passages}
    if any(page is not None for page in pages):
        record["pages"] = pages
    if notes:
        record["notes"] = notes
    return {"tool": tool, "arguments": arguments, "eprint": record}


def library_record(
    tool: str, arguments: Json, text: str, *, left_out: Collection[int] = ()
) -> Json | None:
    """The source record of a library call: the paper's work label from the result's
    first line, with the ledger identifier it names (``papers.library_paper``), and the
    call's ``identifier`` argument as a further one (``aliases``) when it is exactly an
    identifier in a ledger scheme (``papers.exact_paper``) that names another; from the
    second, the rendition's tier, converter, page count and revision, and any further
    ``, key value`` fields; and each page ``read_paper`` returned, or each passage
    ``search_in_paper`` did, with its physical page. A page's text leaves out the notes
    people made on it, the server's word on mathematics the conversion lost and the
    server's pointer to the next pages; its heading's suffix, such as ``corrected by a
    reader on 2026-11-20``, is a flag on the page. A page the toolbox cut in the middle,
    and every page after it, is left out (``cut``); pages the toolbox left out at a page
    boundary (``left_out``) are not expected. None for a result that names no paper or
    revision, holds no text, or whose headings are not the pages its range names, as a
    note's quote that imitates a heading would make them.

    The library resolves ``identifier`` to the work it reads (crypto-research-mcp's
    ``Tools._resolve`` and ``Catalog.resolve``): by the work's own label, or by an
    identifier its catalogue holds for the work, exactly, after reading the argument by its
    grammar, which also takes an identifier out of a longer text, such as a publisher's
    page with a DOI in its path. Its answer for an argument that is one identifier is thus
    the library's word that the label and that identifier name one paper: an ePrint
    report and the DOI of its published version, say. An argument the strict grammar does
    not read as one identifier is no alias: what the library took from it is not shown."""
    if tool not in LIBRARY_TOOLS:
        return None
    work = _WORK.match(text)
    rendition = _RENDITION.match(text, work.end()) if work is not None else None
    if work is None or rendition is None:
        return None
    tier, converter, count, revision, more = rendition.groups()
    fields = dict(cast(list[tuple[str, str]], _FIELD.findall(more)))
    if len(fields) > MAX_FIELDS:
        return None
    rest = text[rendition.end() :]
    cut = _OMITTED.search(rest)
    found: tuple[list[int], list[str], list[Json]] | None
    if tool == LIBRARY_READ:
        found = _library_pages(rest, left_out, cut=cut is not None)
    else:
        found = _library_passages(rest, None if count == "?" else int(count))
    if found is None or not found[1]:
        return None
    pages, passages, flags = found
    label = work.group(1)
    named = library_paper(label)
    key = None if named is None else f"{named[0]}:{named[1]}"
    asked = arguments.get("identifier")
    exact = exact_paper(asked) if isinstance(asked, str) else None
    alias = None if exact is None else f"{exact[0]}:{exact[1]}"
    record: Json = {
        "paper": label,
        "named": key,
        "tier": tier,
        "converter": converter,
        "page_count": None if count == "?" else int(count),
        "revision": revision,
        "fields": fields,
        "pages": pages,
        "passages": passages,
    }
    if alias is not None and alias != key:
        record["aliases"] = [alias]
    if flags:
        record["flags"] = flags
    if " — WITHDRAWN (" in work.group(0):
        record["withdrawn"] = True
    if cut is not None:
        record["cut"] = True
    return {"tool": tool, "arguments": arguments, "library": record}


def _library_pages(
    rest: str, left_out: Collection[int], *, cut: bool
) -> tuple[list[int], list[str], list[Json]] | None:
    """A library read's pages after its rendition line: their numbers, texts and flags;
    None when the headings are not the range's pages, those left out at a boundary
    aside, in order, once each. In a result cut in the middle, the pages before the cut
    are kept, as far as they run on from the first, and the page the cut is in and all
    after it are left out."""
    span = _LIBRARY_PAGES.match(rest)
    if span is None:
        return None
    first, last, total = (int(n) for n in span.groups())
    body = _LIBRARY_TRAILER.sub("", rest[span.end() :])
    omitted = _OMITTED.search(body)
    if omitted is not None:
        body = body[: omitted.start()]
    parts = _LIBRARY_PAGE.split(body)
    if parts[0].strip() or last > total:
        return None
    headings = [int(parts[index]) for index in range(1, len(parts), 3)]
    expected = [n for n in range(first, last + 1) if n not in left_out]
    if cut:
        # The page the cut is in is the last heading before it; the rest is unseen.
        if headings != expected[: len(headings)]:
            return None
        parts = parts[: max(1, len(parts) - 3)]
    elif headings != expected:
        return None
    pages: list[int] = []
    passages: list[str] = []
    flags: list[Json] = []
    for index in range(1, len(parts), 3):
        number, suffix, page = int(parts[index]), parts[index + 1], parts[index + 2]
        notes = _NOTES.search(page)
        if notes is not None:
            page = page[: notes.start()]
        page = _MATH_NOTE.sub("", page)
        if page.strip() == _NO_TEXT:
            page = ""
        if suffix:
            flags.append({"page": number, "flag": suffix})
        pages.append(number)
        passages.append(squash(page))
    return pages, passages, flags


def _library_passages(
    rest: str, total: int | None
) -> tuple[list[int], list[str], list[Json]] | None:
    """A library search's passages after its echo of the phrase, each with its physical
    page, in the text's order; none from a passage the toolbox cut in the middle."""
    echoed = _LIBRARY_MATCHED.match(rest)
    if echoed is None:
        return None
    parts = _SEARCH_PASSAGE.split(rest[echoed.end() :])
    if parts[0].strip():
        return None
    pages: list[int] = []
    passages: list[str] = []
    for index in range(1, len(parts) - 1, 2):
        page, block = int(parts[index]), parts[index + 1]
        if (pages and page < pages[-1]) or (total is not None and page > total):
            return None
        if _OMITTED.search(block):
            continue
        if squash(block):
            pages.append(page)
            passages.append(squash(block))
    return pages, passages, []


def _search_passages(rest: str) -> tuple[list[int | None], list[str]]:
    """A search's passages, each with the physical page it is on."""
    parts = _SEARCH_PASSAGE.split(rest)
    pages: list[int | None] = []
    passages: list[str] = []
    for index in range(1, len(parts) - 1, 2):
        block = squash(parts[index + 1])
        if block and not _OMITTED.search(parts[index + 1]):
            pages.append(int(parts[index]))
            passages.append(block)
    return pages, passages


def _page_texts(rest: str) -> tuple[list[int | None], list[str]]:
    """A page read's pages, each with its number."""
    parts = _PAGE.split(rest)
    pages: list[int | None] = []
    passages: list[str] = []
    for index in range(1, len(parts) - 1, 2):
        block = squash(parts[index + 1])
        if block and not _OMITTED.search(parts[index + 1]):
            pages.append(int(parts[index]))
            passages.append(block)
    return pages, passages


def source(
    db: sqlite3.Connection, project: str, session: str, link: EvidenceLink
) -> tuple[Json, Json] | None:
    """The source record that holds what quote ``link`` quotes, as the review feed holds
    it: its locator and payload; None when there is none. It reads the session's records
    of the quote's mark, newest first, by the feed's key index, and never the copies the
    platform pinned beside entries (``quoted:``), at most ``MAX_CANDIDATES`` of them."""
    locator = link.locator or {}
    named = cast(Json, locator.get("named") or {})
    text = squash(str(locator.get("text", "")))
    if not text:
        return None
    if locator.get("source") == "pdf":
        if link.sha256 is None:
            return None
        prefixes = [f"source:{mark('pdf', link.sha256)}:"]
    elif locator.get("source") == "library":
        papers = _named_papers(named)
        if not papers or not locator.get("revision") or not isinstance(locator.get("page"), int):
            return None
        prefixes = [f"source:{mark('paper', key)}:" for key in sorted(papers)]
    else:
        if not named.get("eprint") or not locator.get("dated"):
            return None
        prefixes = [f"source:{mark('eprint', str(named['eprint']))}:"]
    rows = _candidates(db, project, session, prefixes)
    if locator.get("source") == "pdf":
        return next(
            (
                (at, payload)
                for at, payload in rows
                if _pdf_holds(payload, link.sha256, locator, text)
            ),
            None,
        )
    if locator.get("source") == "library":
        papers, revision = _named_papers(named), str(locator["revision"]).casefold()
        page = cast(int, locator["page"])
        return next(
            (
                (at, payload)
                for at, payload in rows
                if _library_holds(payload, papers, revision, page, text)
            ),
            None,
        )
    eprint, dated = str(named["eprint"]), str(locator["dated"])
    # The date stamps get_eprint gave the paper in this session: what dates the text of a
    # record that prints none.
    stamped = any(
        payload.get("tool") == EPRINT_RECORD
        and cast(Json, payload.get("eprint") or {}).get("paper") == eprint
        and cast(Json, payload.get("eprint") or {}).get("dated") == dated
        for _, payload in rows
    )
    for at, payload in rows:
        if _eprint_holds(payload, eprint, dated, text, stamped=stamped):
            if cast(Json, payload.get("eprint") or {}).get("dated") is None:
                # Say what dates it: the record of the same session that gave the stamp.
                payload = {**payload, "dated_by": {"tool": EPRINT_RECORD, "dated": dated}}
            return at, payload
    return None


def unmatched(db: sqlite3.Connection, project: str, session: str, link: EvidenceLink) -> str | None:
    """Why no source record of session ``session`` holds ePrint or library quote ``link``
    yet, when the reason is mechanical, from those records alone: no ``get_eprint`` record
    of the paper, none with the quote's date stamp, or no read of its text; no library
    read of the paper, none at the quote's revision, or none of its page at that revision.
    None when a record holds the quote, when the records have its date stamp and a read of
    the paper's text, or its page at its revision (its text is then what differs), and for
    a PDF quote. It names only what the session's tools returned."""
    locator = link.locator or {}
    named = cast(Json, locator.get("named") or {})
    if locator.get("source") not in ("eprint", "library") or not named:
        return None
    if source(db, project, session, link) is not None:
        return None
    if locator["source"] == "eprint":
        eprint, dated = str(named.get("eprint")), str(locator.get("dated"))
        rows = _candidates(db, project, session, [f"source:{mark('eprint', eprint)}:"])
        stamps = list(
            dict.fromkeys(
                str(found["dated"])
                for _, payload in rows
                if (found := cast(Json, payload.get("eprint") or {})).get("paper") == eprint
                and found.get("dated") is not None
            )
        )
        if dated in stamps:
            texts = [payload for _, payload in rows if payload.get("tool") in EPRINT_TEXT_TOOLS]
            if texts:
                return None
            return (
                f"no eprint_fulltext or search_eprint_fulltext result for ePrint {eprint} is "
                "recorded in this session yet: the citation check finds an ePrint quote only "
                "in a page or passage read in the session"
            )
        if not stamps:
            return (
                f"no get_eprint result for ePrint {eprint} is recorded in this session yet: "
                "the citation check finds an ePrint quote only under the date stamp "
                "get_eprint printed, the whole value of its Date line"
            )
        given = " or ".join(repr(stamp) for stamp in stamps[:MAX_NAMED])
        return (
            f"get_eprint gave ePrint {eprint} the date stamp {given} in this session, and "
            f"this quote's dated is {dated!r}: the citation check finds the quote only under "
            "the date stamp as printed, the whole value of the Date line"
        )
    papers = _named_papers(named)
    revision, page = str(locator.get("revision", "")).casefold(), locator.get("page")
    rows = _candidates(
        db, project, session, [f"source:{mark('paper', k)}:" for k in sorted(papers)]
    )
    reads = [
        found
        for _, payload in rows
        if payload.get("tool") in LIBRARY_TOOLS
        and _papers_of(found := cast(Json, payload.get("library") or {})) & papers
    ]
    if not reads:
        others = _library_reads(db, project, session)
        if not others:
            return (
                "no read_paper or search_in_paper result for this paper is recorded in this "
                "session yet: the citation check finds a library quote only in a page read "
                "in the session"
            )
        given = "; ".join(others[:MAX_NAMED])
        return (
            "no read_paper or search_in_paper result is recorded in this session under this "
            f"reference's identifiers ({', '.join(sorted(papers))}); the session's library reads "
            f"are of {given}: the citation check finds a library quote only in a read of the "
            "reference's own paper, recorded under one of the reference's identifiers. If "
            "one of those is this paper, read it by an identifier the reference carries, or "
            "give the reference the identifier the library printed"
        )
    at = [found for found in reads if str(found.get("revision", "")).casefold() == revision]
    if not at:
        revisions = list(dict.fromkeys(str(found.get("revision")) for found in reads))
        given = " or ".join(repr(r) for r in revisions[:MAX_NAMED])
        return (
            f"this session read the paper at revision {given}, and this quote's revision is "
            f"{str(locator.get('revision'))!r}: give the revision from the result's second "
            "line, exactly as printed"
        )
    pages = sorted({n for found in at for n in cast(list[Any], found.get("pages") or [])
                    if isinstance(n, int)})  # fmt: skip
    if page in pages:
        return None
    return (
        f"no read of physical page {page} at this revision is recorded in this session "
        f"(pages read at it: {spans(pages) or 'none'}): the citation check finds a library "
        "quote only on the page it names"
    )


def _library_reads(db: sqlite3.Connection, project: str, session: str) -> list[str]:
    """The papers of the session's newest library reads, as many as ``MAX_CANDIDATES``
    of them, newest first, once each: each by the identifiers its records are under, the
    work label's first, or by its work label when none is a ledger identifier."""
    rows = db.execute(
        "SELECT json_extract(payload,'$.library') FROM review_feed WHERE project=? AND "
        "session=? AND kind='source' AND source_key GLOB 'source:*' AND "
        "json_extract(locator,'$.tool') IN (?,?) AND "
        "json_extract(payload,'$.library.paper') IS NOT NULL ORDER BY seq DESC LIMIT ?",
        (project, session, *LIBRARY_TOOLS, MAX_CANDIDATES),
    )
    papers: list[list[str]] = []
    for (raw,) in rows:
        found = stored_json(str(raw))
        names = [str(n) for n in (found.get("named"), *cast(list[Any], found.get("aliases") or []))
                 if isinstance(n, str)] or [str(found.get("paper"))]  # fmt: skip
        same = next((p for p in papers if set(p) & set(names)), None)
        if same is None:
            papers.append(names)
        else:
            same += [n for n in names if n not in same]
    return [p[0] + (f" (also {', '.join(p[1:])})" if p[1:] else "") for p in papers]


def _candidates(
    db: sqlite3.Connection, project: str, session: str, prefixes: list[str]
) -> list[tuple[Json, Json]]:
    """The session's source records under any of ``prefixes``, newest first, at most
    ``MAX_CANDIDATES``: the key index alone (its rows' seq), then each candidate's row. A
    row that indexes a record under a further identifier (``ALIAS``) is read as the
    record's own row, once however many of its keys the prefixes find."""
    found: set[int] = set()
    for prefix in prefixes:
        found.update(
            seq
            for (seq,) in db.execute(
                "SELECT seq FROM review_feed WHERE project=? AND session=? AND source_key>=? "
                "AND source_key<? ORDER BY seq DESC LIMIT ?",
                (project, session, prefix, prefix[:-1] + ";", MAX_CANDIDATES),
            )
        )
    rows: list[tuple[Json, Json]] = []
    seen: set[str] = set()
    for seq in sorted(found, reverse=True)[:MAX_CANDIDATES]:
        key, raw_locator, raw_payload = db.execute(
            "SELECT source_key,locator,payload FROM review_feed WHERE seq=? AND kind='source'",
            (seq,),
        ).fetchone() or (None, None, None)
        if raw_payload is None:
            continue
        payload = stored_json(raw_payload)
        if isinstance(payload.get(ALIAS), str):
            key = str(payload[ALIAS])
            raw_locator, raw_payload = db.execute(
                "SELECT locator,payload FROM review_feed WHERE project=? AND session=? AND "
                "source_key=? AND kind='source'",
                (project, session, key),
            ).fetchone() or (None, None)
            if raw_payload is None:
                continue
            payload = stored_json(raw_payload)
        if key in seen:
            continue
        seen.add(str(key))
        rows.append((stored_json(str(raw_locator)), payload))
    return rows


def _named_papers(named: Json) -> set[str]:
    """The papers a library quote's reference names, one key each (``papers.paper_id``):
    its identifiers in the schemes a library work label can name."""
    return {
        key
        for scheme in LEDGER_SCHEMES
        if isinstance(value := named.get(scheme), str)
        and (key := paper_id(scheme, value)) is not None
    }


def _papers_of(record: Json) -> set[str]:
    """The ledger identifiers library record ``record`` is under: the one its work label
    names, and the further ones the library vouched for (``aliases``)."""
    names = [record.get("named"), *cast(list[Any], record.get("aliases") or [])]
    return {name for name in names if isinstance(name, str)}


def quoted_items(
    db: sqlite3.Connection, project: str, session: str, view: EntryView
) -> list[tuple[Json, Json]]:
    """The source records in session ``session`` of the quotes ``view`` holds, as many as
    ``MAX_QUOTED``, each with the quote and the entry revision that holds it."""
    found: list[tuple[Json, Json]] = []
    for link in view.evidence:
        if link.target != "quote" or len(found) >= MAX_QUOTED:
            continue
        held = source(db, project, session, link)
        if held is None:
            continue
        locator, payload = held
        by = {
            "entry": view.entry_id,
            "handle": view.handle,
            "revision": view.revision,
            "evidence": link.evidence_id,
        }
        found.append(({**locator, "quoted_by": by}, payload))
    return found


def _pdf_holds(payload: Json, sha256: str | None, locator: Json, text: str) -> bool:
    """Whether a PDF tool's record, of the PDF ``sha256``, holds lines ``first_line`` to
    ``last_line`` of page ``page``, and ``text`` spans them."""
    pdf = cast(Json, payload.get("pdf") or {})
    if payload.get("tool") not in PDF_TOOLS or pdf.get("sha256") != sha256:
        return False
    page, first, last = locator.get("page"), locator.get("first_line"), locator.get("last_line")
    if not isinstance(page, int) or not isinstance(first, int) or not isinstance(last, int):
        return False
    lines = {
        int(line["n"]): squash(str(line.get("text", "")))
        for line in cast(list[Json], payload.get("lines") or [])
        if line.get("page") == page and isinstance(line.get("n"), int)
    }
    if any(n not in lines for n in range(first, last + 1)):
        return False
    joined = " ".join(lines[n] for n in range(first, last + 1))
    if text not in joined:
        return False
    # It spans the lines it cites: not inside the lines after the first, nor before the last.
    return first == last or (
        text not in " ".join(lines[n] for n in range(first + 1, last + 1))
        and text not in " ".join(lines[n] for n in range(first, last))
    )


def _eprint_holds(
    payload: Json, eprint: str, dated: str, text: str, *, stamped: bool = False
) -> bool:
    """Whether an e-print record for ``eprint`` with date stamp ``dated`` holds ``text``
    in one of its passages or pages. A record that prints no date stamp (the tools of
    2026-10-31) holds it only when ``stamped``: a ``get_eprint`` record of the session
    gave the paper that stamp. The server's echo of the query is never a passage; and a
    quote the call sent (in its arguments, as decoded strings) counts only in a passage
    that holds more than what was sent, so a server that answered with the query alone
    vouches for nothing, while a search for a phrase, then its quote, works."""
    record = cast(Json, payload.get("eprint") or {})
    if payload.get("tool") not in EPRINT_TEXT_TOOLS or record.get("paper") != eprint:
        return False
    if record.get("dated") != dated and not (record.get("dated") is None and stamped):
        return False
    sent = [s for s in _strings(payload.get("arguments", {})) if text.casefold() in s.casefold()]
    return any(
        text in passage and not any(passage.casefold() == s.casefold() for s in sent)
        for passage in (str(p) for p in cast(list[Any], record.get("passages") or []))
    )


def _library_holds(payload: Json, papers: set[str], revision: str, page: int, text: str) -> bool:
    """Whether a library record under one of ``papers`` (``_papers_of``), read at
    ``revision``, holds ``text`` in a page or passage on physical page ``page``. As for
    an ePrint, a quote the call sent counts only in a passage that holds more than what
    was sent."""
    record = cast(Json, payload.get("library") or {})
    if payload.get("tool") not in LIBRARY_TOOLS or not _papers_of(record) & papers:
        return False
    if str(record.get("revision", "")).casefold() != revision:
        return False
    sent = [s for s in _strings(payload.get("arguments", {})) if text.casefold() in s.casefold()]
    return any(
        number == page
        and text in passage
        and not any(passage.casefold() == s.casefold() for s in sent)
        for number, passage in zip(
            cast(list[Any], record.get("pages") or []),
            (str(p) for p in cast(list[Any], record.get("passages") or [])),
            strict=False,
        )
    )


def _strings(value: object) -> list[str]:
    """The strings in ``value``, a decoded JSON value, white space squashed."""
    if isinstance(value, str):
        return [squash(value)]
    if isinstance(value, dict):
        return [s for item in cast(Json, value).values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in cast(list[Any], value) for s in _strings(item)]
    return []
