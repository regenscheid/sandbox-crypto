# SPDX-License-Identifier: MIT
"""Copied numbers (docs/specs/consistency-review.md, CR-39).

Before the model reviews a snapshot, the platform compares each long number that its
ledger entries and document blocks state with the numbers its text and JSON artifacts
hold. A number that matches none of them exactly, but nearly matches one, as a number
copied by hand with a few digits dropped, doubled or changed does, becomes a ``fail``
finding of the review: anchored to the statement and to the artifact, with both values.
The check needs no model; it reads only the snapshot's own items.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from sandbox_science.orchestrator.documents.errors import DocumentError

from .findings import Findings
from .store import Json, ReviewStore, digest, dump, stored_json

MIN_DIGITS: Final = 10
"""The shortest number the check compares: shorter ones are rarely copied by hand."""

MAX_FINDINGS: Final = 4
"""The most findings the check records in one review, which leaves the reviewer room."""

MAX_NUMBERS: Final = 2000
"""The most distinct numbers the check reads from a snapshot's artifacts."""

_JOINS: Final = re.compile(
    r"(?<=\d)(?:\s*(?:\\[,;:! ]|\\allowbreak|\{,\})\s*|[\u2009\u202f]|,(?=\d{3}(?!\d)))(?=\d)"
)
"""What groups the digits of one number in LaTeX or prose: a thin space, ``\\,`` and its
kin, ``\\allowbreak``, ``{,}``, or a comma before three digits."""

_RUN: Final = re.compile(rf"\d{{{MIN_DIGITS},}}")
_KEY: Final = re.compile(r'"([^"\\]{1,64})"\s*:\s*"?[\d,\s]*$')


def numbers(text: str) -> list[str]:
    """The numbers of at least ``MIN_DIGITS`` digits ``text`` states, read through the
    separators that group digits."""
    return _RUN.findall(_JOINS.sub("", text))


def distance(a: str, b: str, bound: int) -> int | None:
    """The edit distance of ``a`` and ``b``, or None when it is past ``bound``."""
    if abs(len(a) - len(b)) > bound:
        return None
    far = bound + 1
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [far] * len(b)
        for j in range(max(1, i - bound), min(len(b), i + bound) + 1):
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a[i - 1] != b[j - 1])
            )
        if min(current) > bound:
            return None
        previous = current
    return previous[-1] if previous[-1] <= bound else None


def near(stated: str, held: set[str]) -> tuple[str, int] | None:
    """The number of ``held`` that ``stated`` was most likely copied from, and how many
    edits apart they are: within 3, or 5 % of its digits if more. None when ``stated`` is
    one of them, or none is that near. A neighbour that differs only in its last two
    digits, such as p - 1 beside p, is a number of its own."""
    if stated in held:
        return None
    bound = max(3, len(stated) * 5 // 100)
    found: tuple[str, int] | None = None
    for number in held:
        if len(number) == len(stated) and number[:-2] == stated[:-2]:
            continue
        apart = distance(stated, number, bound)
        if apart is not None and (found is None or apart < found[1]):
            found = (number, apart)
    return found


@dataclass(frozen=True, slots=True)
class _Stated:
    """A number a snapshot item states, and where."""

    number: str
    anchor: Json
    where: str


@dataclass(frozen=True, slots=True)
class _Held:
    """The numbers an artifact item holds, and its anchor and name."""

    numbers: list[str]
    text: str
    anchor: Json
    name: str


def copied_numbers(
    store: ReviewStore, project: str, session: str, review: str, owner: str
) -> list[Json]:
    """Record a ``fail`` finding for each number the review's own snapshot items state
    that nearly matches, but does not match, a number its artifacts hold (CR-39), as many
    as ``MAX_FINDINGS``; the findings, each its ID and statement."""
    stated, held = _items(store, project, review)
    if not stated or not held:
        return []
    every = {n for h in held for n in h.numbers}
    recorded: list[Json] = []
    seen: set[str] = set()
    for item in stated:
        if len(recorded) >= MAX_FINDINGS:
            break
        found = near(item.number, every)
        if found is None or dump([item.anchor, item.number]) in seen:
            continue
        seen.add(dump([item.anchor, item.number]))
        number, apart = found
        source = next(h for h in held if number in h.numbers)
        statement = _statement(item, source, number, apart)
        raw = {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": statement,
            "anchors": [item.anchor, source.anchor],
            "remedy": "Copy the number from the artifact, or say how it was derived from it.",
            "operation_id": "copied-" + digest(dump([item.anchor, item.number, number]))[:24],
        }
        try:
            made = Findings(store).create(project, session, review, owner, raw)
        except DocumentError:
            continue
        if made.get("cap_reached"):
            break
        recorded.append({"finding": made.get("finding_id"), "statement": statement})
    return recorded


def _items(store: ReviewStore, project: str, review: str) -> tuple[list[_Stated], list[_Held]]:
    """The numbers the review's own items state, and those its artifacts hold."""
    stated: list[_Stated] = []
    held: list[_Held] = []
    total = 0
    with store.documents.control.read() as tx:
        rows = tx.sql.execute(
            "SELECT evidence_id,kind,locator,payload,complete FROM review_evidence "
            "WHERE review_id=? AND kind IN ('ledger','document','artifact') ORDER BY ordinal",
            (review,),
        ).fetchall()
    for evidence, kind, raw_locator, raw_payload, complete in rows:
        locator, payload = stored_json(raw_locator), stored_json(raw_payload)
        if kind == "ledger":
            handle = locator.get("handle", "the entry")
            for field, text in (
                ("statement", str(payload.get("statement", ""))),
                ("fields", json.dumps(payload.get("fields", {}), ensure_ascii=False)),
            ):
                stated += [
                    _Stated(n, {"evidence": evidence, "field": field}, f"{handle}'s {field}")
                    for n in numbers(text)
                ]
        elif kind == "document":
            stated += _blocks(store, project, evidence, locator)
        elif complete and payload.get("availability") == "text" and total < MAX_NUMBERS:
            text = str(payload.get("text") or "")
            found = list(dict.fromkeys(numbers(text)))[: MAX_NUMBERS - total]
            total += len(found)
            reference = payload.get("reference", {})
            name = str(locator.get("path") or reference.get("artifact_id", "the artifact"))
            held.append(_Held(found, _JOINS.sub("", text), {"evidence": evidence}, name))
    return stated, held


def _blocks(store: ReviewStore, project: str, evidence: str, locator: Json) -> list[_Stated]:
    """The numbers each block of a pinned document revision states."""
    try:
        mapped = store.documents.mapped(project, locator["document"], locator["revision"])
    except (DocumentError, KeyError):
        return []
    found: list[_Stated] = []
    for index, block in enumerate(mapped.parsed.blocks):
        text = mapped.parsed.text[block.start : block.end]
        block_id = mapped.block_ids[index]
        found += [
            _Stated(n, {"evidence": evidence, "block": block_id}, f"block {block_id}")
            for n in numbers(text)
        ]
    return found


def _statement(item: _Stated, source: _Held, number: str, apart: int) -> str:
    """The finding's statement: both values, where they differ, and the artifact's key."""
    at = source.text.find(number)
    key = _KEY.search(source.text[max(0, at - 200) : at]) if at >= 0 else None
    named = f"{source.name}" + (f", at `{key.group(1)}`," if key else "")
    first = next(
        (i for i, (a, b) in enumerate(zip(item.number, number, strict=False)) if a != b),
        min(len(item.number), len(number)),
    )
    return (
        "The platform's check of copied numbers (CR-39): "
        f"{item.where} states a {len(item.number)}-digit number that no pinned artifact "
        f"holds; {named} holds a {len(number)}-digit number {apart} edits from it, the "
        f"first difference at digit {first + 1}. Stated: {item.number}. In the artifact: "
        f"{number}. It looks copied by hand."
    )
