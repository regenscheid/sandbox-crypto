# SPDX-License-Identifier: MIT
"""The sources of quotes (docs/specs/consistency-review.md, CR-4, as the coordinator
decided on 2026-10-15).

A ledger entry's quote of a reference names what it quotes: a PDF's page and lines,
pinned by the PDF's SHA-256, or an ePrint passage, named by the paper and the date stamp
the e-print tools gave it. The platform looks for the quote in its own record of what an
agent of the project read: the tool results the review feed holds, as the model saw them.
For a PDF, a ``pdf_pages`` or ``pdf_search`` result whose header names the PDF's full
SHA-256 and whose numbered lines of that page hold the quoted text; for an ePrint, an
``eprint_fulltext`` or ``get_eprint`` result for that paper, with that date stamp, that
holds it. White space is read as one space. A quote found is pinned beside the entry in
its review, once in each window, so the reviewer reads the source the platform recorded,
not the agent's copy of it; a quote not found has no source, and its citation cannot be
confirmed.
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

MAX_CANDIDATES: Final = 200
"""The most recorded tool results the platform reads for one quote, newest first."""

_LINE: Final = re.compile(r"^\[p(\d+):(\d+)\] (?:\(printed [^)]*\) )?(.*)$", re.M)
_EPRINT_TOOLS: Final = ("eprint_fulltext", "get_eprint")


def squash(text: str) -> str:
    """``text`` with each run of white space one space, and none at either end."""
    return " ".join(text.split())


def source(db: sqlite3.Connection, project: str, link: EvidenceLink) -> tuple[Json, Json] | None:
    """The recorded tool result that holds what quote ``link`` quotes, as the review feed
    holds it: its locator and payload; None when there is none."""
    locator = link.locator or {}
    named = cast(Json, locator.get("named") or {})
    text = squash(str(locator.get("text", "")))
    if not text:
        return None
    if locator.get("source") == "pdf":
        if link.sha256 is None:
            return None
        marks = (f"(sha256 {link.sha256})",)
    else:
        if not named.get("eprint") or not locator.get("dated"):
            return None
        marks = (f"[{named['eprint']}]", f"Date: {locator['dated']}")
    for raw_locator, raw_payload in db.execute(
        "SELECT locator,payload FROM review_feed WHERE project=? AND kind='transcript' "  # noqa: S608 - placeholders only
        + "".join(" AND instr(payload,?)>0" for _ in marks)
        + " ORDER BY seq DESC LIMIT ?",
        (project, *marks, MAX_CANDIDATES),
    ):
        message = _message(stored_json(raw_payload))
        if message is None or message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        held = (
            _pdf_holds(content, locator, text)
            if locator.get("source") == "pdf"
            else text in squash(content) and _eprint_call(db, project, message, named["eprint"])
        )
        if held:
            return stored_json(raw_locator), stored_json(raw_payload)
    return None


def quoted_items(db: sqlite3.Connection, project: str, view: EntryView) -> list[tuple[Json, Json]]:
    """The recorded sources of the quotes ``view`` holds, as many as ``MAX_QUOTED``, each
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


def _message(payload: Json) -> Json | None:
    """The message a transcript observation copied, when it was copied whole."""
    if payload.get("truncated"):
        return None
    try:
        message: object = json.loads(str(payload.get("content", "")))
    except ValueError:
        return None
    return cast(Json, message) if isinstance(message, dict) else None


def _pdf_holds(content: str, locator: Json, text: str) -> bool:
    """Whether a PDF tool's result holds lines ``first_line`` to ``last_line`` of page
    ``page``, and they hold ``text``."""
    page, first, last = locator.get("page"), locator.get("first_line"), locator.get("last_line")
    if not isinstance(page, int) or not isinstance(first, int) or not isinstance(last, int):
        return False
    lines: dict[int, str] = {}
    for match in _LINE.finditer(content):
        if int(match.group(1)) == page:
            lines[int(match.group(2))] = match.group(3)
    if any(n not in lines for n in range(first, last + 1)):
        return False
    return text in squash(" ".join(lines[n] for n in range(first, last + 1)))


def _eprint_call(db: sqlite3.Connection, project: str, message: Json, eprint: str) -> bool:
    """Whether the tool result ``message`` answers an e-print tool's call for ``eprint``."""
    call = str(message.get("tool_call_id", ""))
    if not call:
        return False
    for (raw,) in db.execute(
        "SELECT payload FROM review_feed WHERE project=? AND kind='transcript' "
        "AND instr(payload,?)>0 ORDER BY seq DESC LIMIT 20",
        (project, call),
    ):
        asked = _message(stored_json(raw))
        if asked is None:
            continue
        for made in cast(list[Any], asked.get("tool_calls") or []):
            function = cast(Json, cast(Json, made).get("function") or {})
            if cast(Json, made).get("id") != call:
                continue
            if not str(function.get("name", "")).endswith(_EPRINT_TOOLS):
                return False
            try:
                arguments = cast(Json, json.loads(str(function.get("arguments", "{}"))))
            except ValueError:
                return False
            return eprint in (arguments.get("paper_id"), arguments.get("ref"))
    return False
