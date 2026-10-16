# SPDX-License-Identifier: MIT
"""Extent descriptors for connector tools (docs/specs/context-management.md, CM-12a).

Most papers reach a session through crypto-research-mcp's tools (D64), whose results are
text for the model. A descriptor reads one tool's result, as its server writes it, for
what part of which source it returned; each is written against the tool's published
description and its results as the server returned them on 2026-10-16:

- the e-print server: ``get_eprint`` is a catalog record (``abstract_only``);
  ``eprint_fulltext`` returns the record and the passages matching a query (``partial``,
  or ``abstract_only`` when none matched); ``eprint_file`` only links the PDF, which the
  PDF tools then read;
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
  the PDF.

A search or a listing reads no source. Any other connector tool has no descriptor: its
reads are ``unknown`` (``reads.reads_of``), and the register counts them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Final, cast

from .reads import Json, Read

Descriptor = Callable[[Json, str], "list[Read] | None"]
"""A tool's arguments and result text: the reads it made, ``[]`` for a call that read
no source, and ``None`` for a result the descriptor cannot place."""

_PAPER: Final = re.compile(r"\A\[([^\]\n]{1,64})\]")
_MATCHED: Final = re.compile(r"^(\d+) passage\(s\) matching ", re.M)
_NONE_MATCHED: Final = re.compile(r"^No passage matching ", re.M)
_FETCHED: Final = re.compile(r"\A\[[a-z_-]{1,32}\] (\S{1,4096}) \(HTTP (\d{3})\)")
_TRUNCATED: Final = re.compile(
    r"\[Truncated\. Call fetch_continue\(document_id='([^'\n]{1,128})', offset=(\d{1,12})\)"
)
_NIST: Final = re.compile(r"\ANIST (.{1,64}?) — ", re.M)


def nist_key(designation: str) -> str:
    """A NIST publication as a register's key: ``FIPS 203``, ``NIST.FIPS.203`` and
    ``fips203`` alike."""
    found = designation.strip().upper().removeprefix("NIST").lstrip(". ")
    return re.sub(r"[\s._-]+", "", found)


def _eprint_record(arguments: Json, text: str) -> list[Read] | None:
    paper = _PAPER.match(text)
    if paper is None:
        return None
    return [Read("eprint", paper.group(1), "abstract_only", {}, label=paper.group(1))]


def _eprint_fulltext(arguments: Json, text: str) -> list[Read] | None:
    paper = _PAPER.match(text)
    if paper is None:
        return [] if "queue" in text.casefold() else None  # queued: nothing read yet
    matched = _MATCHED.search(text)
    if matched is not None:
        detail: Json = {"passages": int(matched.group(1)), "query": str(arguments.get("query"))}
        return [Read("eprint", paper.group(1), "partial", detail, label=paper.group(1))]
    if _NONE_MATCHED.search(text) is not None:
        return [Read("eprint", paper.group(1), "abstract_only", {}, label=paper.group(1))]
    return None


def _web_fetch(arguments: Json, text: str) -> list[Read] | None:
    fetched = _FETCHED.match(text)
    url = fetched.group(1) if fetched is not None else str(arguments.get("url") or "")
    if not url or (fetched is not None and not fetched.group(2).startswith("2")):
        return [] if fetched is not None else None  # an error page reads nothing
    cut = _TRUNCATED.search(text)
    if cut is None:
        return [Read("web", url_key(url), "whole", {}, label=url)]
    detail: Json = {"chars": [[0, int(cut.group(2))]], "document": cut.group(1)}
    return [Read("web", url_key(url), "partial", detail, label=url)]


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
    return [Read("paper", paper.casefold(), "partial", detail, label=paper)]


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


def _no_read(arguments: Json, text: str) -> list[Read] | None:
    return []


DESCRIPTORS: Final[dict[str, Descriptor]] = {
    "e-print-mcp_get_eprint": _eprint_record,
    "e-print-mcp_eprint_fulltext": _eprint_fulltext,
    "e-print-mcp_eprint_file": _no_read,
    "e-print-mcp_search_eprint": _no_read,
    "e-print-mcp_recent_eprint": _no_read,
    "e-print-mcp_refresh_eprint": _no_read,
    "web-fetch-mcp_web_fetch": _web_fetch,
    "web-fetch-mcp_fetch_page": _web_fetch,
    "web-fetch-mcp_fetch_continue": _fetch_continue,
    "web-fetch-mcp_fetch_links": _no_read,
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
    """A page's URL as a register's key: scheme and host in lower case, without its
    fragment or a trailing slash."""
    url = url.strip().split("#", 1)[0]
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url.rstrip("/")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}".rstrip("/")


def _json(text: str) -> Json:
    try:
        found: Any = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return cast(Json, found) if isinstance(found, dict) else {}
