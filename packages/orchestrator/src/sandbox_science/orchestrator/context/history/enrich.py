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
call. A size rejection appends measured budget feedback and a tighter target to
the same final retry; the rejected answer is not echoed. Two failures leave the
record skeleton-only.

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
from collections.abc import Awaitable, Callable, Sequence
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
from sandbox_science.orchestrator.prompts import schema_json
from sandbox_science.orchestrator.worker_client import dump

from .archive import Archived, escape
from .evidence import EvidenceMatches, EvidenceRequest, EvidenceSource, RowEvidence
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
    attempts: tuple[Json, ...] = ()
    """At most two bounded outcomes; never the model's answer, prompt or error text."""


@dataclass
class EnrichmentProgress:
    """Only the current complete call can need an estimated cancellation receipt.

    ``inflight_input_tokens`` is a local request-size estimate, never provider usage.
    The owner records it at most once after the task has actually stopped.
    """

    baseline_input_tokens: int | None = None
    """The owner's already measured view size, avoiding another large serialization."""
    canonical_intent: bool = False
    """A durable paid-call journal owns observed or unknown usage for this dispatch."""
    inflight_input_tokens: int | None = None
    counted: bool = False


@dataclass(frozen=True)
class _Size:
    narrative: int
    limit: int
    rendered: int | None
    share: int
    fixed: int
    target: int


def _size(record: FoldRecord, share: int, narrative: int, rendered: int | None = None) -> _Size:
    # Existing supported keys remain mandatory within their existing allowance.
    keys: list[tuple[str, str]] = []
    used = 0
    for key, at in record.keys:
        if used + len(key) + 24 <= share // 10:
            keys.append((key, at))
            used += len(key) + 24
    # Remove only the temporary quoted content "> x", retaining its header/newline.
    fixed = replace(record, narrative="x", keys=tuple(keys), state="applied").chars - 3
    # Line quoting can take up to three rendered characters per content character.
    # This target never replaces the final semantic and rendered-size validators.
    target = min(share // 8, max(0, (share - fixed) // 3))
    return _Size(narrative, share // 4, rendered, share, fixed, target)


def _repair(size: _Size, framing: Framing) -> Message:
    rendered = (
        f"The validated candidate record measured {size.rendered:,} characters, "
        f"{max(0, size.rendered - size.share):,} over its full-record share. "
        if size.rendered is not None
        else ""
    )
    return {
        "role": "user",
        "content": (
            framing.text
            + "Size repair for the existing final retry. The escaped narrative measured "
            f"{size.narrative:,} characters against its {size.limit:,}-character cap "
            f"(overage {max(0, size.narrative - size.limit):,}). "
            + rendered
            + f"The full-record share remains {size.share:,}; the unchanged outline, metadata "
            f"retained keys and narrative framing occupy {size.fixed:,}, leaving "
            f"{max(0, size.share - size.fixed):,} characters for narrative content and line "
            "quoting. Return the same JSON contract "
            f"with a narrative target of at most {size.target:,} characters and keys: []. "
            "Preserve supported critical handoff facts, unresolved obligations, scope/version "
            "limits and exact source/artifact references needed to continue. Remove repetition, "
            "not qualifications; do not invent facts or references. All original semantic and "
            "size checks still apply. If no nonempty supported narrative fits, the platform "
            "will retain the skeleton-only record. Do not call tools."
        ),
    }


def _size_diagnostic(size: _Size) -> Json:
    """Fixed-size measurement from a rejected answer, with no answer text."""
    return {
        "narrative_chars": size.narrative,
        "narrative_limit": size.limit,
        "record_chars": size.rendered,
        "record_limit": size.share,
        "fixed_chars": size.fixed,
        "repair_target": size.target,
    }


def _outcome(why: str) -> str:
    """Stable diagnostic codes for the validator's locally generated reasons."""
    if why.startswith("its narrative passed "):
        return "narrative_limit"
    return {
        "it did not answer with the JSON object asked for": "invalid_answer",
        "its narrative was empty": "empty_narrative",
        "the record passed its share": "record_share",
    }.get(why, "validation_failed")


async def enrich(
    model: ChatModel,
    view: Sequence[Message],
    specs: Sequence[ToolSpec],
    record: FoldRecord,
    rows: Sequence[Archived] | EvidenceSource,
    *,
    share: int,
    exists: Callable[[str], bool] | None = None,
    spent: Callable[[ModelUsage], None] | None = None,
    framing: Framing = FOLD_ENRICH,
    shown: str | None = None,
    admit: Callable[[Sequence[Message], Sequence[ToolSpec]], Awaitable[bool]] | None = None,
    release: Callable[[], None] | None = None,
    progress: EnrichmentProgress | None = None,
) -> Enriched:
    """``record``, the span ``rows``'s as code built it, enriched by ``model`` after the
    run's ``view``, within ``share`` characters without its markers (CM-19, CM-20);
    ``exists`` says whether a ledger handle exists, and ``spent`` takes each call's usage
    as it returns, so that a fold cancelled later still counts its calls. An overview's
    update has its own ``framing`` and is ``shown`` its parts' narratives (CM-21)."""
    initial = _size(record, share, 0)
    capacity = min(share // 4, max(0, (share - initial.fixed) // 3))
    if capacity == 0:
        return Enriched(
            record,
            (),
            "the retained record leaves no narrative room",
            attempts=(
                {"attempt": 0, "outcome": "no_narrative_room", "size": _size_diagnostic(initial)},
            ),
        )
    limits = (
        f"The narrative target is at most {capacity:,} characters, including room for "
        f"rendered line quoting; the full-record share is {share:,}. The fixed outline, "
        f"retained keys and framing occupy {initial.fixed:,} characters. Keep important "
        "takeaways, failed approaches, open questions and exact retrieval references; "
        "remove repetition rather than qualifications. The keys take at most "
        f"{share // 10:,} in all. The span:\n"
    )
    content = framing.text + limits + (shown or record.text("(this span)"))
    asked = {"role": "user", "content": content}
    calls: list[ModelUsage] = []
    failed: list[str] = []
    attempts: list[Json] = []
    caller = model
    repair: Message | None = None
    for attempt in range(ATTEMPTS):
        messages = [*view, asked, *([repair] if repair else [])]
        if admit is not None and not await admit(messages, specs):
            attempts.append({"attempt": attempt + 1, "outcome": "admission_denied"})
            return Enriched(
                record,
                tuple(calls),
                "child token allowance reserved for reporting",
                attempts=tuple(attempts),
            )
        try:
            if progress is not None:
                # Set only after admission, immediately before the dispatch. Do not
                # retain it while validating a returned answer or awaiting admission.
                if progress.baseline_input_tokens is not None:
                    progress.inflight_input_tokens = progress.baseline_input_tokens
                else:
                    request_chars = len(dump(messages)) + len(schema_json(specs).decode("utf-8"))
                    progress.inflight_input_tokens = max(1, request_chars // 4)
            try:
                turn = await caller.complete(messages, specs)
            except ModelError as error:
                if progress is not None:
                    progress.inflight_input_tokens = None  # the existing receipt owns this call
                calls.append(ModelUsage(cost=error.cost, intent_id=error.intent_id))
                if spent is not None:
                    spent(calls[-1])
                failed.append(f"the call failed ({type(error).__name__})")
                attempts.append({"attempt": attempt + 1, "outcome": "model_error"})
                continue
            if progress is not None:
                progress.inflight_input_tokens = None  # a response, even if validation fails
            calls.append(_usage(turn))
            if spent is not None:
                spent(calls[-1])
        finally:
            if admit is not None and release is not None:
                release()
        if turn.tool_calls:
            failed.append("it called a tool")
            attempts.append({"attempt": attempt + 1, "outcome": "tool_call"})
            if isinstance(model, GatewayModel):
                caller = model.choosing("none")
            continue
        # A span, an overview's above all, can be millions of characters: off the loop.
        sizes: list[_Size] = []
        evidence: list[EvidenceMatches] = []
        found, why = await threaded(
            check,
            turn.text or "",
            record,
            rows,
            share=share,
            exists=exists,
            size=sizes.append,
            evidence_report=evidence.append,
        )
        if found is not None:
            accepted: Json = {"attempt": attempt + 1, "outcome": "accepted"}
            if evidence and (diagnostic := evidence[-1].diagnostic()):
                accepted["evidence"] = diagnostic
            attempts.append(accepted)
            offered = (_object(turn.text or "") or {}).get("proposals")
            proposals = tuple(cast(list[object], offered)) if isinstance(offered, list) else ()
            return Enriched(found, tuple(calls), proposals=proposals, attempts=tuple(attempts))
        failed.append(why)
        diagnostic: Json = {"attempt": attempt + 1, "outcome": _outcome(why)}
        if sizes:
            diagnostic["size"] = _size_diagnostic(sizes[-1])
        attempts.append(diagnostic)
        if sizes and attempt + 1 < ATTEMPTS:
            repair = _repair(sizes[-1], framing)
    return Enriched(record, tuple(calls), "; ".join(failed), attempts=tuple(attempts))


def check(
    text: str,
    record: FoldRecord,
    rows: Sequence[Archived] | EvidenceSource,
    *,
    share: int,
    exists: Callable[[str], bool] | None = None,
    size: Callable[[_Size], None] | None = None,
    evidence_report: Callable[[EvidenceMatches], None] | None = None,
) -> tuple[FoldRecord | None, str]:
    """``record`` with the answer's narrative and keys, as code checks them against the
    span ``rows``; ``None`` and why when the answer cannot be used."""
    answer = _object(text) or {}
    narrative = answer.get("narrative")
    if not isinstance(narrative, str):
        return None, "it did not answer with the JSON object asked for"
    narrative = _quoted(_visible(narrative)).strip()
    if not narrative:
        return None, "its narrative was empty"
    if len(narrative) > share // 4:
        if size is not None:
            size(_size(record, share, len(narrative)))
        return None, f"its narrative passed {share // 4:,} characters"
    keys = dict(record.keys)
    offered: object = answer.get("keys") or []
    candidates = (
        tuple(
            key
            for key in cast(list[object], offered)
            if isinstance(key, str)
            and "\n" not in key
            and "\r" not in key
            and flat(key)
            and len(flat(key)) + 24 <= share // 10
        )
        if isinstance(offered, list)
        else ()
    )
    wanted_numbers: set[str] = set()
    literals: set[str] = set()
    patterns: set[str] = set()
    for match in _VALUES.finditer(narrative):
        value, kind = match.group(0), match.lastgroup
        if kind == "number":
            value = _whole(value.rstrip(".,"))
            wanted_numbers.update(numbers(value))
            if value.startswith(("-", "\u2212")):
                patterns.add(_negative(value))
        elif kind == "handle":
            patterns.add(_handle(value))
        else:
            literals.add(value)
    source = RowEvidence(rows) if isinstance(rows, Sequence) else rows
    supported = source.verify(
        EvidenceRequest(
            frozenset(wanted_numbers), frozenset(literals), frozenset(patterns), candidates
        )
    )
    if evidence_report is not None:
        evidence_report(supported)
    for key in candidates:
        holder = supported.keys.get(key)
        if holder is not None and flat(key) not in keys:
            keys[flat(key)] = holder
    kept: list[tuple[str, str]] = []
    used = 0
    for key, at in keys.items():
        if used + len(key) + 24 <= share // 10:
            kept.append((key, at))
            used += len(key) + 24
    narrative = _VALUES.sub(lambda m: _checked(m, supported, kept, exists), narrative)
    made = replace(record, narrative=narrative, keys=tuple(kept), state="applied")
    if made.chars > share:
        if size is not None:
            size(_size(record, share, len(narrative), made.chars))
        return None, "the record passed its share"
    return made, ""


def _whole(value: str) -> str:
    return re.sub(r"^([-+\u2212]?)\.", r"\g<1>0.", value)


def _negative(value: str) -> str:
    digits = re.escape(value[1:])
    if value[1:].startswith("0."):
        digits = "0?" + digits[1:]
    return rf"(?<![\w.])[-\u2212]\s?{digits}(?![\w.])"


def _handle(value: str) -> str:
    return rf"(?<![\w-]){re.escape(value)}(?![\w-])"


def _checked(
    match: re.Match[str],
    supported: EvidenceMatches,
    keys: Sequence[tuple[str, str]],
    exists: Callable[[str], bool] | None,
) -> str:
    value, kind = match.group(0), match.lastgroup
    tail = ""
    if kind == "number":
        stripped = value.rstrip(".,")
        value, tail = stripped, value[len(stripped) :]
        whole = _whole(value)
        known = numbers(whole) <= supported.numbers
        if known and whole.startswith(("-", "\u2212")):
            known = _negative(whole) in supported.patterns
    elif kind == "handle":
        known = _handle(value) in supported.patterns or (exists is not None and exists(value))
    else:
        known = value in supported.literals
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
        intent_id=turn.intent_id,
        cost_source=turn.cost_source,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_input_tokens=turn.cache_read_input_tokens,
        cache_creation_input_tokens=turn.cache_creation_input_tokens,
        cost=turn.cost,
        gateway_version=turn.gateway_version,
        retried=list(turn.retried[:16]),
    )
