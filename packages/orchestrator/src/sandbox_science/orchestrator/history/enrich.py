# SPDX-License-Identifier: MIT
"""A fold's enrichment call and the checks on its answer (docs/specs/context-management.md,
CM-19, CM-20).

The call is the run's own model, sent the run's view as it stands, tools included and
``tool_choice`` as the run sends it, so that it reads the view from the prompt cache,
followed by one instruction: the contract, and the span's record as code built it. Its
answer is one JSON object: the narrative, search keys, and proposals, which the fold
proposals step reads (f3) and this one leaves aside. An answer that calls a tool, is not
that object, has no narrative, or whose narrative passes a quarter of the record's
share fails the attempt; the one retry sets ``tool_choice`` to ``none`` after a tool
call. Two failures leave the record skeleton-only.

Code checks the answer it keeps. A search key that does not occur in the span word for
word is dropped. Every number in the narrative must occur in the span, both written in
one form: digits without separators, and powers as ``2^80`` however written (``2^{80}``,
``2**80``, ``2⁸⁰``); each hexadecimal string of eight or more characters, DOI, ePrint ID
and archive ID must occur in it too, and a ledger handle must occur or exist. Any other
is replaced by a marker naming a search key. Hidden characters are removed, and a line
that looks like the platform's framing of the state block, fold records or message
headers is quoted, with a backslash before it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, cast

from sandbox_science.orchestrator.model import (
    ChatModel,
    GatewayModel,
    Message,
    ModelError,
    ModelTurn,
    ToolSpec,
)
from sandbox_science.orchestrator.notices import DIGEST, FOLD_ENRICH, FOLD_REGION, STATE_BLOCK
from sandbox_science.orchestrator.projects import ModelUsage

from .archive import Archived, escape
from .record import FoldRecord

Json = dict[str, Any]

ATTEMPTS: Final = 2
_SUPERSCRIPTS: Final = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_VALUES: Final = re.compile(
    r"(?P<message>msg-[0-9a-f]{16})"
    r"|(?P<doi>\b10\.\d{4,9}/[^\s\"'<>\\]*[^\s\"'<>\\.,;:)\]])"
    r"|(?P<eprint>(?<![\w/])(?:19|20)\d\d/\d{3,5}(?![\w/]))"
    r"|(?P<handle>\b(?:REF|[ICRADT])-\d+\b)"
    r"|(?P<hex>(?<![\w-])(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8,}(?![\w-]))"
    r"|(?P<power>(?<![\w.,])\d[\d,]*(?:\s*\^\s*\{\s*\d+\s*\}|\s*\^\s*\d+|\s*\*\*\s*\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+))"
    r"|(?P<number>(?<![\w.,\-])\d[\d,]*(?:\.\d+)?(?![\w]))"
)
_FRAMINGS: Final = (
    STATE_BLOCK.text[:40],
    DIGEST.text[:40],
    FOLD_REGION.text[:40],
    FOLD_ENRICH.text[:40],
    "--- fold ",
)


@dataclass(frozen=True)
class Enriched:
    """A record as its enrichment left it: with its narrative and keys, or as code built
    it with ``failed`` saying why; and each call's usage, as the run's costs count it."""

    record: FoldRecord
    calls: tuple[ModelUsage, ...]
    failed: str | None = None


async def enrich(
    model: ChatModel,
    view: Sequence[Message],
    specs: Sequence[ToolSpec],
    record: FoldRecord,
    rows: Sequence[Archived],
    *,
    share: int,
    exists: Callable[[str], bool] | None = None,
) -> Enriched:
    """``record``, the span ``rows``'s as code built it, enriched by ``model`` after the
    run's ``view``, within ``share`` characters without its markers (CM-19, CM-20);
    ``exists`` says whether a ledger handle exists."""
    limits = (
        f"The narrative takes at most {share // 4:,} characters, and the keys at most "
        f"{share // 10:,} in all. The span:\n"
    )
    asked = {"role": "user", "content": FOLD_ENRICH.text + limits + record.text("(this span)")}
    calls: list[ModelUsage] = []
    failed: list[str] = []
    caller = model
    for _ in range(ATTEMPTS):
        try:
            turn = await caller.complete([*view, asked], specs)
        except ModelError as error:
            calls.append(ModelUsage(cost=error.cost))
            failed.append(f"the call failed ({type(error).__name__})")
            continue
        calls.append(_usage(turn))
        if turn.tool_calls:
            failed.append("it called a tool")
            if isinstance(model, GatewayModel):
                caller = model.choosing("none")
            continue
        found, why = check(turn.text or "", record, rows, share=share, exists=exists)
        if found is not None:
            return Enriched(found, tuple(calls))
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
    found = _numbers(span)
    for key in cast(list[object], answer.get("keys") or []):
        if isinstance(key, str) and key.strip() and key in span and key not in keys:
            keys[key] = next(row.message_id for row in rows if key in row.entry.text)
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
    if kind in ("power", "number"):
        known = _form(value) in found
    elif kind == "handle":
        known = value in span or (exists is not None and exists(value))
    else:
        known = value in span
    if known:
        return value
    if not keys:
        return "[a value not in the span]"
    near = max(keys, key=lambda key: len(_common(key[0], value)))[0]
    return f'[a value not in the span; search "{near}"]'


def _numbers(text: str) -> set[str]:
    """Every number and power in ``text``, each in one form."""
    return {
        _form(match.group(0))
        for match in _VALUES.finditer(text)
        if match.lastgroup in ("power", "number")
    }


def _form(value: str) -> str:
    """A number without separators; a power as ``base^exponent``."""
    plain = re.sub(r"\s", "", value).replace(",", "")
    if found := re.fullmatch(r"(\d+(?:\.\d+)?)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", plain):
        return f"{found.group(1)}^{found.group(2).translate(_SUPERSCRIPTS)}"
    if found := re.fullmatch(r"(\d+(?:\.\d+)?)(?:\^\{?(\d+)\}?|\*\*(\d+))", plain):
        return f"{found.group(1)}^{found.group(2) or found.group(3)}"
    return plain


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
    """``text`` with a backslash before each line that looks like the platform's own
    framing: a message header, a fold record's or the state block's."""
    lines = escape(text).splitlines(keepends=True)
    return "".join(f"\\{line}" if line.lstrip().startswith(_FRAMINGS) else line for line in lines)


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
    )
