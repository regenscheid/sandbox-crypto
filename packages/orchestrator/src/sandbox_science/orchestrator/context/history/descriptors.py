# SPDX-License-Identifier: MIT
"""Extent descriptors for connector tools (docs/specs/context-management.md, CM-12a).

Most papers reach a session through crypto-research-mcp's tools (D64), whose results are
text for the model. A descriptor reads one tool's result, as its server writes it, for
what part of which source it returned; each is written against the tool's published
description and its results as the server returned them on 2026-10-16:

- the e-print server: ``get_eprint`` is a catalog record (``abstract_only``);
  ``eprint_fulltext`` reads pages of a paper (``partial``, with the pages and the paper's
  page count, which add up to ``whole``; since 2026-10-31, as the server returned it
  then); ``search_eprint_fulltext`` returns the passages of one paper matching a phrase
  (``partial``; none matched reads nothing); ``eprint_file`` only links the PDF, which the
  PDF tools then read. Before 2026-10-31 ``eprint_fulltext`` returned the record and the
  passages matching a query (``partial``, or ``abstract_only`` when none matched), which
  archives of then still hold. The text tools' header may carry the server's own lines
  (``eprint_header``, as the server writes them on 2026-11-25);
- the web-fetch server: ``web_fetch`` and ``fetch_page`` return a page, ``whole`` unless
  it ends ``[Truncated. Call fetch_continue(document_id='…', offset=N) …]``;
  ``fetch_continue`` returns the next part as JSON, ``next_offset`` null at the end. The
  parts' character ranges add up to the page (``reads.merged``);
- firecrawl: ``firecrawl_scrape`` returns a page's content in the formats asked for,
  ``whole`` when they hold its text and no page limit was set; ``firecrawl_research_read_paper``
  returns passages (``partial``);
- the NIST servers: ``nist_read`` returns pages of an edition (``partial``: it names no
  page count); ``csrc_fulltext`` returns a publication's header and the passages matching
  a query; ``get_csrc`` a catalog record; ``nist_get_pdf`` and ``csrc_file`` only link
  the PDF;
- the library server (``library-mcp``, as its tools write their results on 2026-11-24):
  ``read_paper`` reads pages of a held text (``partial``, with the pages and the text's
  page count, which add up to ``whole``), ``search_in_paper`` returns passages
  (``partial``); ``paper_statements`` and ``paper_references`` return what was taken from
  a text (``partial``), ``paper_card`` a model's summary of it (``abstract_only``). Its
  finding, fetching, linking and listing tools, and ``paper_notes``, people's notes, read
  none. A work label is keyed as the paper it names (``library_key``), so a paper read
  through the e-print server and the library is one source; the call's ``identifier``,
  when it is exactly an identifier that names another key, is the read's alias
  (``aliases``), the library's word that both name the paper it read (an ePrint report it
  files under its published DOI), and the register counts the read under both.

The reading tools (``PAGED``) return a paper's text page by page or passage by passage:
past its limit the toolbox cuts such a result between units (``paged``), never inside one.

A search or a listing reads no source. Any other connector tool has no descriptor: its
reads are ``unknown`` (``reads.reads_of``), and the register counts them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final, cast

from sandbox_science.orchestrator.papers import exact_paper, library_paper
from sandbox_science.orchestrator.papers import nist_key as nist_key

from .reads import Json, Read

Descriptor = Callable[[Json, str], "list[Read] | None"]
"""A tool's arguments and result text: the reads it made, ``[]`` for a call that read
no source, and ``None`` for a result the descriptor cannot place."""

_PAPER: Final = re.compile(r"\A\[([^\]\n]{1,64})\]")
_MATCHED: Final = re.compile(r"^(\d+) passage\(s\) matching ", re.M)
_NONE_MATCHED: Final = re.compile(r"^No passage matching ", re.M)
_PAGES: Final = re.compile(r"Physical pages (\d{1,6})-(\d{1,6}) of (\d{1,6})\.")
"""A page read's range, on the line under its header (2026-10-31)."""
_FETCHED: Final = re.compile(r"\A\[[a-z_-]{1,32}\] (\S{1,4096}) \(HTTP (\d{3})\)")
_TRUNCATED: Final = re.compile(
    r"\n\[Truncated\. Call fetch_continue\(document_id='([^'\n]{1,128})', "
    r"offset=(\d{1,12})\)[^\n]*\]\s*\Z"
)
"""The server's notice that it cut a page, its last line: a page that quotes one
elsewhere is not cut."""
_NIST: Final = re.compile(r"\ANIST (.{1,64}?) — ", re.M)
_WORK: Final = re.compile(r"\A(\S{1,512}) — [^\n]*\n([^\n]*)\n")
"""A library result's first two lines: the paper's work label and title, then its
rendition (``… rendition by …``) or ``no text held``."""
_LIBRARY_PAGES: Final = re.compile(r"^Physical pages (\d{1,6})-(\d{1,6}) of (\d{1,6})\.", re.M)
_HEADING: Final = re.compile(r"\n\n## Page (\d{1,6})\b[^\n]*\n")
_PHYSICAL: Final = re.compile(r"\n\nPhysical page (\d{1,6})\n")
_LEFT_OUT: Final = re.compile(r"\n\n\[The platform left out [^\n]{1,1024}\]\Z")
"""The toolbox's last line on a reading result it cut at a page boundary
(``paged``)."""
MAX_NOTE: Final = 512
"""The most characters of a ``NOTE:`` line the e-print server's header may hold; a
result with a longer one has no header (``eprint_header``)."""
_NOTE: Final = re.compile(rf"\ANOTE: [^\n]{{1,{MAX_NOTE}}}\n\n")
_COMPATIBILITY: Final = (
    "Compatibility: query now belongs to search_eprint_fulltext; eprint_fulltext reads pages."
)
"""The line ``eprint_fulltext`` puts before a search's answer, then a blank line, when it
is called with a query."""
_CONVERTED: Final = (
    "Converted from a PDF rendered from the PostScript original; mathematical symbols may be "
    "approximate.",
    "Text extracted from PostScript; layout and mathematical symbols may be approximate.",
)
"""The lines the server puts under the paper's line for a PostScript original."""

PAGED: Final[dict[str, str]] = {
    "e-print-mcp_eprint_fulltext": "pages",
    "e-print-mcp_search_eprint_fulltext": "passages",
    "library-mcp_read_paper": "pages",
    "library-mcp_search_in_paper": "passages",
}
"""The reading tools whose results hold a paper's text page by page or passage by
passage, each with its unit: the toolbox gives their results a larger limit and, past
it, cuts them only between units (``paged``)."""


def paper_key(identifier: str) -> str:
    """A paper's ``arxiv:`` or ``doi:`` ID as a register's key, in lower case; an arXiv
    ID without its version (``v2``), and a bare one as arXiv's."""
    found = identifier.strip().casefold()
    scheme, colon, rest = found.partition(":")
    if not colon:
        scheme, rest = (
            ("arxiv", found)
            if re.match(r"^(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})", found)
            else ("doi", found)
        )
    if scheme == "arxiv":
        rest = re.sub(r"v\d+$", "", rest.removeprefix("arxiv.org/abs/"))
    return f"{scheme}:{rest.strip()}"


def library_key(label: str) -> tuple[str, str]:
    """A library work label as the register keys the paper (kind, key): an ePrint, a
    NIST publication, a DOI or arXiv paper as the other servers' reads of it are keyed,
    so that a paper read through both is one source (``papers.library_paper``); a URL
    as its page; any other label, such as an OpenAlex ID, as a paper of its own."""
    named = library_paper(label)
    if named is not None:
        return ledger_key(*named)
    if label.startswith("url:"):
        return "web", url_key(label.removeprefix("url:"))
    return "paper", label.strip().casefold()


def ledger_key(scheme: str, value: str) -> tuple[str, str]:
    """A ledger identifier, in its normal form (``papers.normal``), as the register keys
    the paper (kind, key)."""
    if scheme in ("eprint", "nist"):
        return scheme, value
    return "paper", paper_key(f"{scheme}:{value}")


def aliases(arguments: Json, kind: str, key: str) -> Json:
    """A library read's detail for the paper its call asked for, when the call's
    ``identifier`` is exactly a ledger identifier (``papers.exact_paper``) whose key is not
    the work label's (``kind``, ``key``): ``aliases``, that key; else nothing."""
    asked = arguments.get("identifier")
    exact = exact_paper(asked) if isinstance(asked, str) else None
    if exact is None or (alias := ledger_key(*exact)) == (kind, key):
        return {}
    return {"aliases": [list(alias)]}


def unit_of(tool: str) -> str | None:
    """The unit, ``pages`` or ``passages``, of a reading tool's result, under its portal
    name or a client's longer one; None for any other tool."""
    found = PAGED.get(tool)
    if found is not None:
        return found
    return next((u for name, u in PAGED.items() if tool.endswith(f"-{name}")), None)


def paged(tool: str, text: str, limit: int) -> tuple[str, Json] | None:
    """A reading tool's result ``text`` within ``limit`` characters, cut only between its
    units: the units that fit, whole, then a last line that says which were left out and
    how to ask for them; and what the record keeps of the cut (its ``length``, the
    characters ``shown`` and the ``pages`` left out). None when the result fits, has no
    units, or not even its first unit fits, which ``tools.bound`` cuts in the middle."""
    unit = unit_of(tool)
    if unit is None or len(text) <= limit:
        return None
    starts = list((_HEADING if unit == "pages" else _PHYSICAL).finditer(text))
    for index in range(len(starts) - 1, 0, -1):
        left = [int(found.group(1)) for found in starts[index:]]
        closing = _left_out(unit, left, limit)
        at = starts[index].start()
        if at + len(closing) <= limit:
            return text[:at] + closing, {"length": len(text), "shown": at, "pages": left}
    return None


def _left_out(unit: str, left: list[int], limit: int) -> str:
    from .reads import spans  # which reads this module's Read

    if unit == "pages":
        return (
            f"\n\n[The platform left out pages {spans(left)} of this result, to keep it within "
            f"{limit:,} characters: read them with a call that starts at page {left[0]}.]"
        )
    return (
        f"\n\n[The platform left out {len(left)} more passage(s) of this result, on pages "
        f"{spans(sorted(set(left)))}, to keep it within {limit:,} characters: ask for fewer "
        "passages, or search for a longer phrase.]"
    )


def unpaged(text: str) -> str:
    """A reading result as its server wrote it, without the toolbox's last line naming
    the pages it left out (``paged``); any other text as it is."""
    return _LEFT_OUT.sub("", text)


def eprint_header(text: str, *, compatible: bool = False) -> tuple[str, list[str], str] | None:
    """The paper an e-print text tool's result names on its own first line, the server's
    lines around that line, and the result after them; None for a result that does not
    begin so. The server's lines are exactly those it writes (``eprint_mcp/tools.py``,
    ``_reading_header``): before the paper's line, ``eprint_fulltext``'s compatibility
    line and a blank line, when ``compatible``, then one ``NOTE:`` line and a blank line
    (where the text came from, a newer revision, the fetch budget); after it, one line on
    a PostScript original's conversion. So there are at most three, each one line, in that
    order, and none is the paper's text, its header or a heading."""
    notes: list[str] = []
    if compatible and text.startswith(_COMPATIBILITY + "\n\n"):
        notes.append(_COMPATIBILITY)
        text = text[len(_COMPATIBILITY) + 2 :]
    note = _NOTE.match(text)
    if note is not None:
        notes.append(note.group(0).strip())
        text = text[note.end() :]
    paper = _PAPER.match(text)
    if paper is None:
        return None
    rest = text.partition("\n")[2]
    line, newline, after = rest.partition("\n")
    if newline and line in _CONVERTED:
        notes.append(line)
        rest = after
    return paper.group(1), notes, rest


def _eprint_record(arguments: Json, text: str) -> list[Read] | None:
    paper = _PAPER.match(text)
    if paper is None:
        return None
    return [Read("eprint", paper.group(1), "abstract_only", {}, label=paper.group(1))]


def _eprint_fulltext(arguments: Json, text: str) -> list[Read] | None:
    header = eprint_header(text, compatible=True)
    if header is None:
        return [] if "queue" in text.casefold() else None  # queued: nothing read yet
    paper, _, rest = header
    read = _PAGES.fullmatch(rest.split("\n", 1)[0])
    if read is not None:
        first, last, of = (int(n) for n in read.groups())
        detail: Json = {"pages": list(range(first, min(last, of) + 1)), "of": of}
        extent = "whole" if first <= 1 and last >= of else "partial"
        return [Read("eprint", paper, extent, detail, label=paper)]
    # Called with a query, or as the tool answered before 2026-10-31: the passages
    # matching it.
    matched = _MATCHED.search(rest)
    if matched is not None:
        detail: Json = {"passages": int(matched.group(1)), "query": str(arguments.get("query"))}
        return [Read("eprint", paper, "partial", detail, label=paper)]
    if _NONE_MATCHED.search(rest) is not None:
        return [Read("eprint", paper, "abstract_only", {}, label=paper)]
    return None


def _eprint_search(arguments: Json, text: str) -> list[Read] | None:
    header = eprint_header(text)
    if header is None:
        return [] if "queue" in text.casefold() else None  # queued: nothing read yet
    paper, _, rest = header
    matched = _MATCHED.search(rest)
    if matched is None:
        return []  # no passage matched: nothing of the paper was read
    detail: Json = {"passages": int(matched.group(1)), "query": str(arguments.get("query"))}
    return [Read("eprint", paper, "partial", detail, label=paper)]


def _web_fetch(arguments: Json, text: str) -> list[Read] | None:
    fetched = _FETCHED.match(text)
    url = fetched.group(1) if fetched is not None else str(arguments.get("url") or "")
    if not url or (fetched is not None and not fetched.group(2).startswith("2")):
        return [] if fetched is not None else None  # an error page reads nothing
    # Under the URL asked for, and the one the fetch ended at, if it was redirected.
    asked = str(arguments.get("url") or url)
    final: Json = {"final": url_key(url)} if url_key(url) != url_key(asked) else {}
    cut = _TRUNCATED.search(text)
    if cut is None:
        return [Read("web", url_key(asked), "whole", final, label=asked)]
    detail: Json = {"chars": [[0, int(cut.group(2))]], "document": cut.group(1), **final}
    return [Read("web", url_key(asked), "partial", detail, label=asked)]


def _fetch_continue(arguments: Json, text: str) -> list[Read] | None:
    found = _json(text)
    document, content = found.get("document_id"), found.get("content")
    offset = arguments.get("offset")
    if not isinstance(document, str) or not isinstance(content, str) or type(offset) is not int:
        return None
    following = found.get("next_offset")
    end = following if type(following) is int else offset + len(content)
    detail: Json = {"chars": [[offset, end]], "document": document, "end": following is None}
    # The page is the one whose fetch named this document; the register finds it.
    return [Read("web", f"document:{document}", "partial", detail)]


def _scrape(arguments: Json, text: str) -> list[Read] | None:
    url = str(arguments.get("url") or "")
    if not url:
        return None
    formats = cast(list[object], arguments.get("formats") or ["markdown"])
    whole = any(f in ("markdown", "html", "rawHtml") for f in formats)
    limited = isinstance(arguments.get("pdfOptions"), dict) and "maxPages" in cast(
        Json, arguments["pdfOptions"]
    )
    if whole and not limited:
        return [Read("web", url_key(url), "whole", {}, label=url)]
    detail: Json = {"formats": [str(f) for f in formats][:8]}
    return [Read("web", url_key(url), "partial", detail, label=url)]


def _read_paper(arguments: Json, text: str) -> list[Read] | None:
    paper = str(arguments.get("paperId") or "").strip()
    if not paper:
        return None
    if "unavailable" in text.casefold() and "passage" not in text.casefold():
        return []  # no full text: nothing of the paper was read
    detail: Json = {"question": str(arguments.get("question"))[:200]}
    return [Read("paper", paper_key(paper), "partial", detail, label=paper)]


def _nist_read(arguments: Json, text: str) -> list[Read] | None:
    found = _json(text)
    designation = found.get("designation")
    if not isinstance(designation, str):
        return None
    pages: list[Json] = []
    if isinstance(found.get("page"), dict):
        pages.append(cast(Json, found["page"]))
    pages += [
        cast(Json, p) for p in cast(list[object], found.get("pages") or []) if isinstance(p, dict)
    ]
    numbers = sorted(
        index + 1
        for page in pages
        if type(index := cast(Json, page.get("location") or {}).get("physical_page_index")) is int
    )
    detail: Json = {"pages": numbers, "edition": str(found.get("edition_id"))[:200]}
    return [Read("nist", nist_key(designation), "partial", detail, label=designation)]


def _csrc_fulltext(arguments: Json, text: str) -> list[Read] | None:
    named = _NIST.match(text)
    if named is None:
        return None
    key, label = nist_key(named.group(1)), named.group(1)
    matched = _MATCHED.search(text)
    if matched is not None:
        detail: Json = {"passages": int(matched.group(1)), "query": str(arguments.get("query"))}
        return [Read("nist", key, "partial", detail, label=label)]
    return [Read("nist", key, "abstract_only", {}, label=label)]


def _csrc_record(arguments: Json, text: str) -> list[Read] | None:
    ref = arguments.get("ref") or arguments.get("paper_id")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return [Read("nist", nist_key(ref), "abstract_only", {}, label=ref)]


def _library_read(arguments: Json, text: str) -> list[Read] | None:
    work = _WORK.match(text)
    if work is None:
        return []  # a refusal of the arguments: no paper was read
    kind, key = library_key(work.group(1))
    read = _LIBRARY_PAGES.search(text, work.end())
    if " rendition by " not in work.group(2) or read is None:
        return []  # not held, queued, or a page outside the text: nothing read
    first, last, of = (int(n) for n in read.groups())
    detail: Json = {"pages": list(range(first, min(last, of) + 1)), "of": of}
    detail.update(aliases(arguments, kind, key))
    extent = "whole" if first <= 1 and last >= of else "partial"
    return [Read(kind, key, extent, detail, label=work.group(1))]


def _library_search(arguments: Json, text: str) -> list[Read] | None:
    work = _WORK.match(text)
    if work is None:
        return []
    matched = _MATCHED.search(text, work.end())
    if matched is None:
        return []  # no passage matched, or no text is held: nothing read
    kind, key = library_key(work.group(1))
    detail: Json = {"passages": int(matched.group(1)), "query": str(arguments.get("phrase"))}
    detail.update(aliases(arguments, kind, key))
    return [Read(kind, key, "partial", detail, label=work.group(1))]


def _library_extract(extent: str) -> Descriptor:
    """A library tool that returns what was taken from a held text, such as its numbered
    statements or its reference list (``partial``), or a model's summary of it, its card
    (``abstract_only``): the paper, never its pages."""

    def describe(arguments: Json, text: str) -> list[Read] | None:
        work = _WORK.match(text)
        if work is None or " rendition by " not in work.group(2):
            return []
        kind, key = library_key(work.group(1))
        return [Read(kind, key, extent, aliases(arguments, kind, key), label=work.group(1))]

    return describe


def _no_read(arguments: Json, text: str) -> list[Read] | None:
    return []


DESCRIPTORS: Final[dict[str, Descriptor]] = {
    "e-print-mcp_get_eprint": _eprint_record,
    "e-print-mcp_eprint_fulltext": _eprint_fulltext,
    "e-print-mcp_search_eprint_fulltext": _eprint_search,
    "e-print-mcp_eprint_file": _no_read,
    "e-print-mcp_eprint_originals": _no_read,
    "e-print-mcp_search_eprint": _no_read,
    "e-print-mcp_recent_eprint": _no_read,
    "e-print-mcp_refresh_eprint": _no_read,
    "web-fetch-mcp_web_fetch": _web_fetch,
    "web-fetch-mcp_fetch_page": _web_fetch,
    "web-fetch-mcp_fetch_continue": _fetch_continue,
    "web-fetch-mcp_fetch_links": _no_read,
    "web-fetch-mcp_web_fetch_hosts": _no_read,
    "paper-search-mcp_paper_copies": _no_read,
    "paper-search-mcp_papers_by_ids": _no_read,
    "paper-search-mcp_zbmath_search": _no_read,
    "library-mcp_read_paper": _library_read,
    "library-mcp_search_in_paper": _library_search,
    "library-mcp_paper_statements": _library_extract("partial"),
    "library-mcp_paper_references": _library_extract("partial"),
    "library-mcp_paper_card": _library_extract("abstract_only"),
    "library-mcp_paper_notes": _no_read,
    "library-mcp_paper_status": _no_read,
    "library-mcp_find_papers": _no_read,
    "library-mcp_fetch_paper": _no_read,
    "library-mcp_paper_file": _no_read,
    "library-mcp_search_library": _no_read,
    "library-mcp_related_papers": _no_read,
    "library-mcp_paper_positions": _no_read,
    "library-mcp_imports_of": _no_read,
    "library-mcp_imports_near": _no_read,
    "firecrawl-mcp_firecrawl_scrape": _scrape,
    "firecrawl-mcp_firecrawl_research_read_paper": _read_paper,
    "nist-csrc-mcp_nist_read": _nist_read,
    "nist-csrc-mcp_nist_get_pdf": _no_read,
    "nist-csrc-mcp_nist_search": _no_read,
    "nist-mcp_csrc_fulltext": _csrc_fulltext,
    "nist-mcp_get_csrc": _csrc_record,
    "nist-mcp_csrc_file": _no_read,
    "nist-mcp_search_csrc": _no_read,
}
"""Each reading tool's descriptor, by its name as the portal lists it."""


def descriptor(tool: str) -> Descriptor | None:
    """The descriptor of ``tool``, under its portal name or a client's longer one."""
    found = DESCRIPTORS.get(tool)
    if found is not None:
        return found
    return next((d for name, d in DESCRIPTORS.items() if tool.endswith(f"-{name}")), None)


def url_key(url: str) -> str:
    """A page's URL as a register's key: its host in lower case without ``www.``, and its
    path, without the scheme (``http`` and ``https`` alike), its fragment or a trailing
    slash."""
    url = url.strip().split("#", 1)[0]
    _, sep, rest = url.partition("://")
    if not sep:
        rest = url
    host, slash, path = rest.partition("/")
    return f"{host.lower().removeprefix('www.')}{slash}{path}".rstrip("/")


def _json(text: str) -> Json:
    try:
        found: Any = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return cast(Json, found) if isinstance(found, dict) else {}
