# SPDX-License-Identifier: MIT
"""The sources of quotes (docs/specs/consistency-review.md, CR-4, as the coordinator
decided on 2026-10-15, and his review of it).

A ledger entry's quote of a reference names what it quotes: a PDF's page and lines,
pinned by the PDF's SHA-256, or an ePrint passage, named by the paper and the date stamp
the e-print tools gave it. When an agent's run calls ``pdf_pages`` or ``pdf_search``, or
the e-print server's ``eprint_fulltext``, the review controller records the call and its
result as a ``source`` observation: the tool, its arguments, and the result's structure,
not its text for the model. For a PDF, the PDF's SHA-256 and path as the sandbox's
helper reported them, and each line it returned, with its page index, label and number;
for an ePrint, the paper and the date stamp from the result's header, and the passages
the server returned as matching, apart from the header (title, authors, abstract) and the
query it echoes. Call and result come from the same run.

The platform looks for a quote in those records: a PDF quote in lines ``first_line`` to
``last_line`` of its page, under the PDF's hash, spanning them (it begins in the first
and ends in the last); an ePrint quote inside one passage of a result for the paper with
that date stamp, and not in the call's arguments, so a query the server echoes is not a
source. White space is read as one space. A quote found brings its record beside the
entry in its review, once in each window; a quote not found has no source, and its
citation cannot be confirmed. What a tool returned may still be an agent's own: a PDF the
agent made, unless the PDF is vouched for (LG-16).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Final, cast

from sandbox_science.orchestrator.ledger.models import EntryView, EvidenceLink

from .store import Json, stored_json

MAX_QUOTED: Final = 8
"""The most quotes of one entry revision whose sources come into a review."""

MAX_CANDIDATES: Final = 50
"""The most source records the platform reads for one quote, newest first."""

MAX_LINES: Final = 4000
"""The most PDF lines one source record keeps."""

PDF_TOOLS: Final = ("pdf_pages", "pdf_search")
EPRINT_TOOL: Final = "e-print-mcp_eprint_fulltext"
"""The e-print server's full-text tool, under the name the server lists it, which a
portal's prefix may precede."""

_MARKER: Final = re.compile(r"\n\n\d+ passage\(s\) matching '.*':\n")
_PASSAGE: Final = re.compile(r"\n?[ \t]*\.\.\.\n")
_PAPER: Final = re.compile(r"\A\[([^\]\n]{1,64})\]")
_DATED: Final = re.compile(r"^Date: (.{1,128})$", re.M)


def squash(text: str) -> str:
    """``text`` with each run of white space one space, and none at either end."""
    return " ".join(text.split())


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
    """The source record of an ``eprint_fulltext`` call: the paper and date stamp from
    the result's header, and the passages the server returned as matching."""
    if not tool.endswith(EPRINT_TOOL):
        return None
    paper = _PAPER.match(text)
    found = _MARKER.search(text)
    if paper is None:
        return None
    header = text if found is None else text[: found.start()]
    dated = _DATED.search(header)
    rest = "" if found is None else text[found.end() :]
    passages = [squash(block) for block in _PASSAGE.split(rest) if block.strip()]
    return {
        "tool": tool,
        "arguments": arguments,
        "eprint": {
            "paper": paper.group(1),
            "dated": None if dated is None else dated.group(1).strip(),
            "passages": passages,
        },
    }


def source(db: sqlite3.Connection, project: str, link: EvidenceLink) -> tuple[Json, Json] | None:
    """The source record that holds what quote ``link`` quotes, as the review feed holds
    it: its locator and payload; None when there is none."""
    locator = link.locator or {}
    named = cast(Json, locator.get("named") or {})
    text = squash(str(locator.get("text", "")))
    if not text:
        return None
    if locator.get("source") == "pdf":
        if link.sha256 is None:
            return None
        mark = link.sha256
    else:
        if not named.get("eprint") or not locator.get("dated"):
            return None
        mark = str(named["eprint"])
    for raw_locator, raw_payload in db.execute(
        "SELECT locator,payload FROM review_feed WHERE project=? AND kind='source' "
        "AND instr(payload,?)>0 ORDER BY seq LIMIT ?",
        (project, mark, MAX_CANDIDATES),
    ):
        payload = stored_json(raw_payload)
        held = (
            _pdf_holds(payload, link.sha256, locator, text)
            if locator.get("source") == "pdf"
            else _eprint_holds(payload, str(named["eprint"]), str(locator["dated"]), text)
        )
        if held:
            return stored_json(raw_locator), payload
    return None


def quoted_items(db: sqlite3.Connection, project: str, view: EntryView) -> list[tuple[Json, Json]]:
    """The source records of the quotes ``view`` holds, as many as ``MAX_QUOTED``, each
    with the quote and the entry revision that holds it."""
    found: list[tuple[Json, Json]] = []
    for link in view.evidence:
        if link.target != "quote" or len(found) >= MAX_QUOTED:
            continue
        held = source(db, project, link)
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


def _eprint_holds(payload: Json, eprint: str, dated: str, text: str) -> bool:
    """Whether an e-print record for ``eprint`` with date stamp ``dated`` holds ``text``
    in one of its passages, and its call did not send it."""
    record = cast(Json, payload.get("eprint") or {})
    if not str(payload.get("tool", "")).endswith(EPRINT_TOOL):
        return False
    if record.get("paper") != eprint or record.get("dated") != dated:
        return False
    sent = squash(json.dumps(payload.get("arguments", {}), ensure_ascii=False)).casefold()
    if text.casefold() in sent:
        return False
    return any(text in str(passage) for passage in cast(list[Any], record.get("passages") or []))
