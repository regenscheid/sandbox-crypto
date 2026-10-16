# SPDX-License-Identifier: MIT
"""A fold's enrichment call and the checks on its answer (docs/specs/context-management.md,
CM-19, CM-20).

The call is the run's own model, sent the run's view as it stands, tools included and
``tool_choice`` as the run sends it, so that it reads the view from the prompt cache,
followed by one instruction: the contract, and the span's record as code built it. Its
answer is one JSON object: the narrative, search keys, and proposals, which are kept as
they came (``Enriched.proposals``) for the fold's proposals to check against the span
(``ledger.proposals``, CM-15) once the fold is archived. An answer that calls a tool, is not
that object, has no narrative, or whose narrative passes a quarter of the record's
share fails the attempt; the one retry sets ``tool_choice`` to ``none`` after a tool
call. Two failures leave the record skeleton-only.

Code checks the answer it keeps. A search key that no message of the span holds word
for word is dropped. Every number in the narrative must occur in the span, both read
as the ledger's provenance reads them (``provenance.numbers``): signs, separators,
decimals, powers however written (``2^-99``, ``2^{80}``, ``2**80``, ``2⁸⁰``),
scientific notation (``9.9e-7``, ``1e9``) and numbers with units (``512bit``); each
hexadecimal string of eight or more characters, DOI, ePrint ID and archive ID must
occur in it too, and a ledger handle must occur, as a whole token, or exist. Any other
is replaced by a marker naming a search key. Hidden characters are removed, a line that
looks like the platform's own (a message header, a framing, a record's labels or marker
lines, the lines that mark the user's words) gets a backslash before it, and the record
shows every line of the narrative quoted, as the run's model's.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, cast

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.ledger.provenance import numbers
from sandbox_science.orchestrator.model import (
    ChatModel,
    GatewayModel,
    Message,
    ModelError,
    ModelTurn,
    ToolSpec,
)
from sandbox_science.orchestrator.notices import FOLD_ENRICH, Framing
from sandbox_science.orchestrator.projects import ModelUsage

from .archive import Archived, escape
from .record import FoldRecord, flat, guarded

Json = dict[str, Any]

ATTEMPTS: Final = 2
_VALUES: Final = re.compile(
    r"(?P<message>msg-[0-9a-f]{16})"
    r"|(?P<doi>\b10\.\d{4,9}/[^\s\"'<>\\]*[^\s\"'<>\\.,;:)\]])"
    r"|(?P<eprint>(?<![\w/])(?:19|20)\d\d/\d{3,5}(?![\w/]))"
    r"|(?P<handle>(?<![\w-])(?:REF|[ICRADT])-\d+(?![\w-]))"
    r"|(?P<hex>(?<![\w-])(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8,}(?![\w-]))"
    # Any token a number begins: its sign, separators, decimals, exponent, power (2^-99,
    # 2^{-128}, 2**80, 2⁸⁰), scientific notation (9.9e-7), unit (512bit), and a leading
    # point (.5).
    r"|(?P<number>(?<![\w.])[-+\u2212]?(?:\.(?=\d))?\d(?:[\w.,^{}*⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]|[-+](?=\d))*)"
)


@dataclass(frozen=True)
class Enriched:
    """A record as its enrichment left it: with its narrative and keys, or as code built
    it with ``failed`` saying why; and each call's usage, as the run's costs count it."""

    record: FoldRecord
    calls: tuple[ModelUsage, ...]
    failed: str | None = None
    proposals: tuple[object, ...] = ()
    """The research-state updates the answer proposed, unchecked (CM-15)."""
    proposed: Json | None = None
    """What the fold's proposals came to once checked: kept, dropped and refused."""


async def enrich(
    model: ChatModel,
    view: Sequence[Message],
    specs: Sequence[ToolSpec],
    record: FoldRecord,
    rows: Sequence[Archived],
    *,
    share: int,
    exists: Callable[[str], bool] | None = None,
    spent: Callable[[ModelUsage], None] | None = None,
    framing: Framing = FOLD_ENRICH,
    shown: str | None = None,
) -> Enriched:
    """``record``, the span ``rows``'s as code built it, enriched by ``model`` after the
    run's ``view``, within ``share`` characters without its markers (CM-19, CM-20);
    ``exists`` says whether a ledger handle exists, and ``spent`` takes each call's usage
    as it returns, so that a fold cancelled later still counts its calls. An overview's
    update has its own ``framing`` and is ``shown`` its parts' narratives (CM-21)."""
    limits = (
        f"The narrative takes at most {share // 4:,} characters, and the keys at most "
        f"{share // 10:,} in all. The span:\n"
    )
    content = framing.text + limits + (shown or record.text("(this span)"))
    asked = {"role": "user", "content": content}
    calls: list[ModelUsage] = []
    failed: list[str] = []
    caller = model
    for _ in range(ATTEMPTS):
        try:
            turn = await caller.complete([*view, asked], specs)
        except ModelError as error:
            calls.append(ModelUsage(cost=error.cost))
            if spent is not None:
                spent(calls[-1])
            failed.append(f"the call failed ({type(error).__name__})")
            continue
        calls.append(_usage(turn))
        if spent is not None:
            spent(calls[-1])
        if turn.tool_calls:
            failed.append("it called a tool")
            if isinstance(model, GatewayModel):
                caller = model.choosing("none")
            continue
        # A span, an overview's above all, can be millions of characters: off the loop.
        found, why = await threaded(
            check, turn.text or "", record, rows, share=share, exists=exists
        )
        if found is not None:
            offered = (_object(turn.text or "") or {}).get("proposals")
            proposals = tuple(cast(list[object], offered)) if isinstance(offered, list) else ()
            return Enriched(found, tuple(calls), proposals=proposals)
        failed.append(why)
    return Enriched(record, tuple(calls), "; ".join(failed))


def check(
    text: str,
    record: FoldRecord,
    rows: Sequence[Archived],
    *,
    share: int,
    exists: Callable[[str], bool] | None = None,
) -> tuple[FoldRecord | None, str]:
    """``record`` with the answer's narrative and keys, as code checks them against the
    span ``rows``; ``None`` and why when the answer cannot be used."""
    span = "\n".join(f"{row.message_id}\n{row.entry.text}" for row in rows)
    answer = _object(text) or {}
    narrative = answer.get("narrative")
    if not isinstance(narrative, str):
        return None, "it did not answer with the JSON object asked for"
    narrative = _quoted(_visible(narrative)).strip()
    if not narrative:
        return None, "its narrative was empty"
    if len(narrative) > share // 4:
        return None, f"its narrative passed {share // 4:,} characters"
    keys = dict(record.keys)
    found = numbers(span)
    for key in cast(list[object], answer.get("keys") or []):
        # A key is one line of text a message of the span holds; a message's ID is not.
        if not isinstance(key, str) or "\n" in key or "\r" in key or not flat(key):
            continue
        holder = next((row.message_id for row in rows if key in row.entry.text), None)
        if holder is not None and flat(key) not in keys:
            keys[flat(key)] = holder
    kept: list[tuple[str, str]] = []
    used = 0
    for key, at in keys.items():
        if used + len(key) + 24 <= share // 10:
            kept.append((key, at))
            used += len(key) + 24
    narrative = _VALUES.sub(lambda m: _checked(m, span, found, kept, exists), narrative)
    made = replace(record, narrative=narrative, keys=tuple(kept), state="applied")
    if made.chars > share:
        return None, "the record passed its share"
    return made, ""


def _checked(
    match: re.Match[str],
    span: str,
    found: set[str],
    keys: Sequence[tuple[str, str]],
    exists: Callable[[str], bool] | None,
) -> str:
    value, kind = match.group(0), match.lastgroup
    tail = ""
    if kind == "number":  # a sentence's punctuation after it is not part of it
        stripped = value.rstrip(".,")
        value, tail = stripped, value[len(stripped) :]
        whole = re.sub(r"^([-+\u2212]?)\.", r"\g<1>0.", value)  # .5 is 0.5
        known = numbers(whole) <= found
        if known and whole[0] in "-\u2212":  # a sign is part of the value: the span says it
            digits = re.escape(whole[1:])
            if whole[1:].startswith("0."):  # -0.5 is said so or as -.5
                digits = "0?" + digits[1:]
            known = re.search(rf"(?<![\w.])[-\u2212]\s?{digits}(?![\w.])", span) is not None
    elif kind == "handle":
        within = re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", span) is not None
        known = within or (exists is not None and exists(value))
    else:
        known = value in span
    if known:
        return value + tail
    if not keys:
        return "[a value not in the span]"
    near = max(keys, key=lambda key: len(_common(key[0], value)))[0]
    return f'[a value not in the span; search "{near}"]{tail}'


def _common(a: str, b: str) -> str:
    size = 0
    while size < min(len(a), len(b)) and a[size] == b[size]:
        size += 1
    return a[:size]


def _visible(text: str) -> str:
    """``text`` without hidden characters: format, private-use, unassigned and control
    characters other than line breaks and tabs."""
    return "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in ("Cf", "Co", "Cn", "Cc", "Cs")
    )


def _quoted(text: str) -> str:
    """``text`` with a backslash before each line that looks like the platform's own:
    a message header, the framing of the state block, the digest, the fold region or the
    enrichment, a fold record's labels and marker lines, or the lines that mark the
    user's own words (CM-20, CM-F15). The record renders every line of the narrative
    quoted besides (``FoldRecord.text``)."""
    return "".join(guarded(line) for line in escape(text).splitlines(keepends=True))


def _object(text: str) -> Json | None:
    """The JSON object an answer holds, alone or in a fenced block."""
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body)
    try:
        found: object = json.loads(body)
    except ValueError:
        return None
    return cast(Json, found) if isinstance(found, dict) else None


def _usage(turn: ModelTurn) -> ModelUsage:
    return ModelUsage(
        model=turn.model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cost=turn.cost,
        gateway_version=turn.gateway_version,
        retried=list(turn.retried[:16]),
    )
