# SPDX-License-Identifier: MIT
"""A fold's skeleton, its marker lines and its search keys, all built by code from the
span's archived messages (docs/specs/context-management.md, CM-17, CM-18).

The skeleton is a chronological outline of the span, one line an item, each within a
budget in tokens: a user message's ID and first words (its text is in the state block);
an answer's text as its head and tail; a tool call's tool, key arguments, outcome,
output size and result ID, with the result's text when there is room; a ledger, plan or
memory change whole; a delegation outcome's head; a retrieval's query and the IDs it
returned; a notice's source and IDs. Over its share, tool results are squeezed first,
then tool arguments, then answers' text, then delegation outcomes; user lines never.

Marker lines come from the records, never from model text: each decision change with
its reason and reopen condition; each caveat, each evidence scope note and each piece of
evidence that is a finite test or a numerical experiment; each work record that ended at
a limit, an obstruction claim or an inconclusive verdict, and each check that ended
inconclusive; each source the read register says the span read only in part (or, from
before the register, a sample whose saved file it never read whole); and the failed or
timed-out calls of each tool as one line. A skeleton cut to its share keeps the user's
lines and the ledger changes whole. Search
keys are literals found in the span with the message that holds them: hashes, ePrint
IDs, DOIs, numbers of 12 or more digits and ``name = value`` pairs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from .archive import Archived
from .reads import Read, merged, spans

Json = dict[str, Any]

BUDGETS: Final = {
    "user": 60,
    "assistant": 120,
    "args": 40,
    "result": 200,
    "outcome": 150,
    "retrieval": 60,
    "notice": 30,
    "change": 500,
}
"""Each item's budget in tokens: a call's line takes its tool, outcome and ID beside its
arguments' budget, and up to the result's of its text."""
SQUEEZES: Final = ({"result": 0}, {"args": 10}, {"assistant": 30}, {"outcome": 40})
"""What a skeleton over its share gives up, in order."""
CHANGES: Final = frozenset({"ledger_change", "ledger_import", "memory_propose"})
RETRIEVALS: Final = frozenset({"history_search", "history_read"})
KEPT: Final = frozenset({"user", "change"})
"""The items a skeleton keeps whole however it is cut."""
FAILED: Final = frozenset({"failed", "timed_out", "cancelled", "lost", "error"})
_KEYS: Final = (
    re.compile(r"(?<![\w-])[0-9a-fA-F]{32,}(?![\w-])"),  # hashes
    re.compile(r"(?<![\w/])(?:19|20)\d\d/\d{3,5}(?![\w/])"),  # ePrint IDs
    re.compile(r"\b10\.\d{4,9}/[^\s\"'<>\\]+[^\s\"'<>\\.,;:)\]]"),  # DOIs
    re.compile(r"(?<![\w.])\d{12,}(?![\w.])"),  # long numbers
    re.compile(r"\b[A-Za-z_]\w{0,40} ?= ?-?\d[\w.^{}*+-]{0,60}"),  # name = value
)


@dataclass(frozen=True)
class Skeleton:
    lines: tuple[str, ...]
    markers: tuple[Json, ...]
    """Each marker as stored: its ``kind`` (``decision``, ``caveat``, ``read`` or
    ``failures``), its ``subject`` and its ``text``; a failure aggregate has ``count``
    and the ``ids`` of its calls' results instead of text."""
    keys: tuple[tuple[str, str], ...]
    """Each search key with the message that holds it."""


@dataclass
class _Item:
    kind: str
    head: str
    text: str = ""
    args: str = ""
    result: str = ""


@dataclass
class _Span:
    items: list[_Item] = field(default_factory=list[_Item])
    markers: list[Json] = field(default_factory=list[Json])
    failed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    sampled: dict[str, str] = field(default_factory=dict[str, str])
    """Each saved file a sample named, with the message that named it."""
    read: set[str] = field(default_factory=set[str])
    """The files the span read whole."""


def skeleton(
    rows: Sequence[Archived],
    share: int,
    keys_share: int,
    per_token: float,
    reads: Sequence[tuple[str, Read]] = (),
) -> Skeleton:
    """The skeleton of the span ``rows`` within ``share`` characters, its markers, and
    its search keys within ``keys_share`` characters; ``per_token`` turns budgets in
    tokens into characters, and ``reads`` are what the read register says the span's
    messages read, each with its message (CM-12)."""
    span = _Span()
    results = {(row.run, row.entry.call_id): row for row in rows if row.entry.role == "tool"}
    for row in rows:
        _add(span, row, results)
    by_source: dict[tuple[str, str], list[tuple[str, Read]]] = {}
    for at, read in reads:
        by_source.setdefault((read.kind, read.source), []).append((at, read))
    registered = {at for at, _ in reads}
    for found in by_source.values():
        read = merged(read for _, read in found)
        if read is not None and read.extent != "whole":
            name = read.label or read.source
            covered = _covered_text(read)
            extent = read.extent.replace("_", " ")
            text = f"{name}: read {extent}{covered} (result {found[-1][0]}); not read whole"
            source = [read.kind, read.source]
            span.markers.append(
                {"kind": "read", "subject": name, "text": text, "source": source,
                 "at": found[-1][0]}
            )  # fmt: skip
    for path, message in span.sampled.items():  # a sample from before the register
        if path not in span.read and message not in registered:
            text = f"{path}: only a sample was shown (result {message}); not read whole"
            span.markers.append({"kind": "read", "subject": path, "text": text})
    for tool, ids in span.failed.items():
        span.markers.append({"kind": "failures", "subject": tool, "count": len(ids), "ids": ids})
    budgets = {kind: int(tokens * per_token) for kind, tokens in BUDGETS.items()}
    lines = [flat(_line(item, budgets)) for item in span.items]
    for squeeze in SQUEEZES:
        if sum(len(line) + 1 for line in lines) <= share:
            break
        budgets |= {kind: int(tokens * per_token) for kind, tokens in squeeze.items()}
        lines = [flat(_line(item, budgets)) for item in span.items]
    # Text from messages and records keeps to its line: none of it starts a line of its own.
    span.markers = [
        {key: flat(value) if isinstance(value, str) else value for key, value in marker.items()}
        for marker in span.markers
    ]
    kept = [item.kind in KEPT for item in span.items]
    return Skeleton(tuple(_fit(lines, share, kept)), tuple(span.markers), _keys(rows, keys_share))


def _add(span: _Span, row: Archived, results: dict[tuple[str, str | None], Archived]) -> None:
    entry, at = row.entry, row.message_id
    content = entry.content or ""
    if entry.kind in ("user", "task"):
        span.items.append(_Item("user", f"{at} {entry.kind}: {_words(content)}"))
    elif entry.kind == "comment":
        span.items.append(_Item("user", f"{at} comments: {_words(content)}"))
    elif entry.kind == "notice":
        refs = ", ".join(entry.refs) or "no IDs"
        span.items.append(_Item("notice", f"{at} notice from {entry.source}: {refs}"))
    elif entry.kind == "outcome":
        span.items.append(_Item("outcome", f"{at} delegation outcome", text=content))
        _inconclusive(span, content, at)
    elif entry.role == "assistant":
        if content:
            span.items.append(_Item("assistant", f"{at} assistant", text=content))
        for call in entry.tool_calls:
            function = cast(Json, call.get("function") or {})
            name, args = str(function.get("name")), str(function.get("arguments") or "")
            result = results.get((row.run, str(call.get("id"))))
            _call(span, name, args, result)


def _call(span: _Span, name: str, args: str, result: Archived | None) -> None:
    if result is None:
        span.items.append(_Item("call", f"{name}: no result", args=args))
        return
    at, text = result.message_id, result.entry.content or ""
    found = _json(text)
    if name in CHANGES:
        span.items.append(_Item("change", f"{at} {name}: {_change(span, args, found, at)}"))
        return
    if name == "wait_delegations":
        _inconclusive(span, text, at)
    if name in RETRIEVALS:
        ids = sorted(set(re.findall(r"msg-[0-9a-f]{16}", text)))
        shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
        span.items.append(_Item("retrieval", f"{at} {name}({args}) → {shown or 'nothing'}"))
        return
    state = found.get("state") if isinstance(found.get("state"), str) else None
    if "error" in found:
        error: object = found["error"]
        state = "error " + str(cast(Json, error).get("kind") if isinstance(error, dict) else error)
    if state is not None and state.split()[0] in FAILED:
        span.failed.setdefault(name, []).append(at)
    outcome = state or "done"
    if (code := found.get("exit_code")) is not None:
        outcome += f", exit {code}"
    saved = found.get("saved")
    if "sample" in found and isinstance(saved, dict):
        for path in cast(Json, saved).values():
            span.sampled.setdefault(str(path), at)
    if name == "read_file" and "sample" not in found:
        span.read.add(str(_json(args).get("path")))
    head = f"{at} {name} → {outcome}, {len(text):,} characters"
    span.items.append(_Item("call", head, args=args, result=text))


def _change(span: _Span, args: str, found: Json, at: str) -> str:
    """A ledger or memory change's line, and its markers."""
    asked = _json(args)
    reason = str(asked.get("reason") or "")
    entries = _objects(found.get("entries"))
    changes = _objects(asked.get("changes"))
    shown: list[str] = []
    for index, done in enumerate(entries):
        handle, status = str(done.get("handle")), str(done.get("status"))
        shown.append(f"{handle} → {status}")
        about: Json = {"entry": handle, "at": at}
        if isinstance(done.get("revision"), int):
            about["revision"] = done["revision"]
        change = changes[index] if index < len(changes) and len(changes) == len(entries) else {}
        fields = cast(Json, change.get("fields") or {})
        if handle.startswith("D-") and ("status" in change or done.get("created")):
            text = f"{handle} → {status}: {reason}"
            if fields.get("reopen_condition"):
                text += f"; reopen if {fields['reopen_condition']}"
            span.markers.append(
                {"kind": "decision", "subject": handle, "text": text, "status": status, **about}
            )
        for caveat in cast(list[object], fields.get("caveats") or []):
            text = f"caveat on {handle}: {caveat}"
            span.markers.append(
                {"kind": "caveat", "subject": handle, "text": text, "caveat": str(caveat), **about}
            )
        for evidence in _objects(change.get("evidence")):
            kind, scope = evidence.get("type"), evidence.get("scope")
            if kind in ("finite_test", "numerical_experiment"):
                text = f"evidence on {handle} is a {str(kind).replace('_', ' ')}"
                text += f", scoped to: {scope}" if scope else ""
            elif scope:
                text = f"evidence on {handle} ({kind}) is scoped to: {scope}"
            else:
                continue
            evidence = [kind, scope]
            span.markers.append(
                {"kind": "caveat", "subject": handle, "text": text, "evidence": evidence, **about}
            )
    for work in _objects(asked.get("work")):
        ending, verdict = work.get("ending"), work.get("verdict")
        if ending in ("limit", "obstruction_claimed") or verdict == "inconclusive":
            how = f"{ending} ({work.get('limit')})" if ending == "limit" else str(ending)
            if verdict == "inconclusive":
                how = "an inconclusive assessment"
            text = f"work on {work.get('entry')} ended {how}: {_words(str(work.get('tried')))}"
            entry = str(work.get("entry"))
            span.markers.append(
                {"kind": "caveat", "subject": entry, "text": text, "entry": entry,
                 "work": str(verdict or ending), "at": at}
            )  # fmt: skip
    if not shown and "error" in found:
        return "refused"
    return ", ".join(shown) + (f": {reason}" if reason else "")


def _line(item: _Item, budgets: dict[str, int]) -> str:
    if item.kind in ("user", "change"):
        return _cut(item.head, budgets[item.kind])
    if item.kind in ("notice", "retrieval"):
        return _cut(item.head, budgets[item.kind])
    if item.kind == "call":
        line = f"{item.head}; arguments {_cut(_compact(item.args), budgets['args'])}"
        if budgets["result"] and item.result:
            line += f"; {_cut(item.result, budgets['result'])}"
        return line
    budget = budgets["assistant" if item.kind == "assistant" else "outcome"]
    return f"{item.head}: {_cut(item.text, budget)}"


def _fit(lines: list[str], share: int, kept: Sequence[bool] = ()) -> list[str]:
    """``lines`` within ``share`` even after every squeeze: those ``kept`` says are kept
    (the user's lines and ledger changes, never cut), and of the others the first and
    the last that fit, alternately, with a line for each run of lines left out."""
    if sum(len(line) + 1 for line in lines) <= share:
        return lines
    keep = [bool(kept[i]) if i < len(kept) else False for i in range(len(lines))]
    room = share - 100 - sum(len(line) + 1 for line, k in zip(lines, keep, strict=True) if k)
    others = [i for i, k in enumerate(keep) if not k]
    for index in (i for pair in zip(others, reversed(others), strict=True) for i in pair):
        if keep[index]:
            continue
        if len(lines[index]) + 1 + 60 > room:
            break
        keep[index] = True
        room -= len(lines[index]) + 1 + 60
    found: list[str] = []
    gone = 0
    for line, k in zip(lines, keep, strict=True):
        if k:
            if gone:
                found.append(f"[... {gone} lines left out; history_read reads the span ...]")
                gone = 0
            found.append(line)
        else:
            gone += 1
    if gone:
        found.append(f"[... {gone} lines left out; history_read reads the span ...]")
    return found


def _covered_text(read: Read) -> str:
    """What of a source a read covered, in a few words."""
    detail = read.detail
    if detail.get("pages"):
        return f": pages {spans(cast(list[int], detail['pages']))} of {detail.get('of') or '?'}"
    if detail.get("ranges"):
        ranges = ", ".join(f"{a}-{b}" for a, b in detail["ranges"])
        return f": bytes {ranges} of {detail.get('size') or '?'}"
    return ""


def _inconclusive(span: _Span, content: str, at: str) -> None:
    """A marker for each check whose outcome ``content`` reports inconclusive."""
    starts = [i for i in (content.find("{"), content.find("[")) if i >= 0]
    if not starts:
        return
    try:  # the JSON after a notice's framing
        found, _ = json.JSONDecoder().raw_decode(content, min(starts))
    except ValueError:
        return
    stack: list[object] = [cast(object, found)]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            item = cast(Json, value)
            result = item.get("result") if isinstance(item.get("result"), dict) else item
            if cast(Json, result).get("verdict") == "inconclusive":
                subject = str(item.get("delegation_id") or item.get("delegation") or at)
                target = cast(Json, item.get("target") or {}).get("handle")
                of = f" of {target}" if target else ""
                text = f"check {subject}{of} ended inconclusive (in {at})"
                checked: Json = {"check": subject, "at": at}
                if target:
                    checked["entry"] = str(target)
                if isinstance(
                    revision := cast(Json, item.get("target") or {}).get("revision"), int
                ):
                    checked["revision"] = revision
                span.markers.append({"kind": "caveat", "subject": subject, "text": text, **checked})
                continue
            stack.extend(item.values())
        elif isinstance(value, list):
            stack.extend(cast(list[object], value))


def _keys(rows: Sequence[Archived], share: int) -> tuple[tuple[str, str], ...]:
    found: dict[str, str] = {}
    used = 0
    for row in rows:
        if row.entry.kind == "tool_result" and row.entry.source in RETRIEVALS:
            continue
        for pattern in _KEYS:
            for match in pattern.finditer(row.entry.text):
                key = match.group(0)
                if key in found or used + len(key) + 24 > share:
                    continue
                found[key] = row.message_id
                used += len(key) + 24
    return tuple((flat(key), at) for key, at in found.items())


def _cut(text: str, chars: int) -> str:
    """``text`` within ``chars``: its head and tail, with a marker for the cut."""
    text = text.strip()
    if len(text) <= chars:
        return text
    if chars < 40:
        return text[: max(0, chars)] + " [...]"
    half = (chars - 20) // 2
    return f"{text[:half]} [... {len(text) - 2 * half:,} characters ...] {text[-half:]}"


def flat(text: str) -> str:
    """``text`` on one line, its whitespace runs single spaces."""
    return " ".join(text.split())


def _words(text: str) -> str:
    return " ".join(text.split()[:40])


def _compact(args: str) -> str:
    found = _json(args)
    return json.dumps(found, ensure_ascii=False, separators=(",", ":")) if found else args


def _objects(value: object) -> list[Json]:
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(Json, item) for item in items if isinstance(item, dict)]


def _json(text: str) -> Json:
    """A tool result or arguments as an object, notices that rode on a result taken off;
    ``{}`` for anything else."""
    try:
        found: object = json.loads(text)
        if isinstance(found, dict) and isinstance(cast(Json, found).get("tool_result"), str):
            found = json.loads(cast(Json, found)["tool_result"])
    except ValueError:
        return {}
    return cast(Json, found) if isinstance(found, dict) else {}
