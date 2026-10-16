# SPDX-License-Identifier: MIT
"""A main run's view of its session as it starts (docs/specs/context-management.md,
CM-25 to CM-30, CM-29a to CM-29c): what its model calls see before its own messages.

The view holds the session's main runs' messages from the archive, as they were sent:
their tasks, the user's messages, notices, answers, tool calls and results, and the
delegation outcomes they received; never a delegated run's own messages, nor a state
block sent before. A call without a result, as an interrupted run leaves, gets one saying
it was aborted, and a result without its call is left out (CM-29a). A line before each
earlier run's part names the kernels its cells used and whether each runs now (CM-29c).
The skills whose bodies the view holds count as loaded, and those its discovery notices
showed as shown (CM-29b). The user's chat messages whose text the view holds are
``shown``: one delivered again is answered where it first appeared (CM-29a, RS-12).

Folds are not built yet. Until they are, a view that would pass ``SHARE`` of the run's
input leaves out its oldest messages at a boundary that splits no call from its results,
and a state block, archived and sent first, says so and keeps the user's own words from
them within ``WORDS`` of the input: a long one as its head and tail, and the oldest,
past that share, as one line each, which ``history_read`` returns whole (CM-26, CM-30).
The run's context limit still stops a run whose view does not fit.

``View.parts`` are what folds will work on: each message with its archive row (ID,
position, run), its characters and, at ``View.per_token``, its tokens (CM-28).
``ViewMeter`` measures the view before each call and keeps, in the run's record, its
parts at the start and each call's estimate beside what the gateway reported.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

from sandbox_science.contracts import ExecutionContext, KernelInfo
from sandbox_science.orchestrator.hooks import Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import KERNEL_LINES, REQUESTS, STATE_BLOCK
from sandbox_science.orchestrator.skills.discovery import shown_in
from sandbox_science.orchestrator.skills.tool import given
from sandbox_science.orchestrator.tokens import DEFAULT_CHARS, estimate, view_tokens
from sandbox_science.orchestrator.worker_client import dump

from .archive import PAGE, Archive, Archived, Entry

Json = dict[str, Any]

SHARE: Final = 0.5
"""The share of a run's input, in characters, that its view takes at most, until folds."""
WORDS: Final = 0.1
"""The share of a run's input the state block's copy of the user's words takes at most."""
ABORTED: Final = (
    "(No result: the run that made this call ended before it returned, so whether the "
    "call ran, and what it did, is unknown.)"
)
CELLS: Final = ("python", "sage")
"""The tools that run cells in a kernel."""


@dataclass(frozen=True)
class Part:
    """One message of a view, as it is sent, with the archived row it renders, if any."""

    kind: str
    message: Json
    row: Archived | None = None

    @property
    def chars(self) -> int:
        return len(dump(self.message))


@dataclass(frozen=True)
class View:
    """A run's view as it starts."""

    parts: tuple[Part, ...] = ()
    per_token: float = DEFAULT_CHARS
    """The characters per token of the session's recent calls on the run's model."""
    left_out: tuple[int, int] | None = None
    """The first and last positions of the messages the view leaves out."""
    words: str | None = None
    """The state block's text, before it is archived (``settle``)."""
    unarchived: int = 0
    """The session's main runs that ran before the archive existed."""
    shown: Mapping[str, str] = field(default_factory=dict[str, str])
    """The user's chat messages whose text the view holds: each one's archive ID."""
    loaded: Mapping[str, tuple[str, int]] = field(default_factory=dict[str, tuple[str, int]])
    """The skills whose bodies the view holds: each one's SHA-256 and characters."""
    discovered: frozenset[str] = frozenset()
    """The skills the view's discovery notices showed."""
    cells: Mapping[str, tuple[Json, ...]] = field(default_factory=dict[str, tuple[Json, ...]])
    """Each earlier run's kernels, as its cells reported them."""

    @property
    def messages(self) -> tuple[Json, ...]:
        return tuple(part.message for part in self.parts)

    @property
    def tokens(self) -> int:
        return sum(estimate(part.chars, self.per_token) for part in self.parts)

    def summary(self) -> Json:
        """The view as the run's record keeps it: its parts by archive ID and position."""
        rows = [part.row for part in self.parts if part.row is not None]
        return {
            "parts": len(self.parts),
            "positions": [rows[0].position, rows[-1].position] if rows else None,
            "left_out": list(self.left_out) if self.left_out else None,
            "unarchived_runs": self.unarchived,
            "aborted_calls": sum(part.kind == "aborted" for part in self.parts),
            "chars": sum(part.chars for part in self.parts),
            "tokens": self.tokens,
            "chars_per_token": round(self.per_token, 3),
            "state_block": next(
                (p.row.message_id for p in self.parts if p.kind == "state_block" and p.row), None
            ),
        }


def session_view(
    archive: Archive,
    context: ExecutionContext,
    *,
    budget: int,
    per_token: float = DEFAULT_CHARS,
    runs: Sequence[str] = (),
) -> View:
    """The session's view for a main run whose input may hold ``budget`` characters;
    ``runs`` are the session's main runs with saved records, which tells those from
    before the archive. It changes nothing: ``settle`` archives its state block and adds
    its kernel lines."""
    rows = _rows(archive, context)
    units = _units(rows)
    sizes = [sum(part.chars for part in unit) for unit in units]
    start = 0
    if sum(sizes) > budget * SHARE:
        room, start = int(budget * (SHARE - WORDS)), len(units)
        while start > 0 and sizes[start - 1] <= room:
            room -= sizes[start - 1]
            start -= 1
    kept = [part for unit in units[start:] for part in unit]
    cut = [part.row for unit in units[:start] for part in unit if part.row is not None]
    archived = {row.run for row in rows}
    unarchived = sum(run not in archived for run in runs)
    words, quoted = _words(cut, int(budget * WORDS))
    shown = {
        str(row.entry.refs[0]): row.message_id
        for row in [*[part.row for part in kept if part.row], *quoted]
        if row.entry.kind == "user" and row.entry.refs
    }
    cells: dict[str, list[Json]] = {}
    for part in kept:
        if part.row is not None and part.row.entry.source in CELLS:
            kernel = _kernel(part.row.entry.content or "")
            used = cells.setdefault(part.row.run, [])
            if kernel is not None and kernel not in used:
                used.append(kernel)
    texts = [part.row.entry for part in kept if part.row is not None]
    results = [e.content or "" for e in texts if e.kind == "tool_result" and e.source == "skills"]
    notices = [e.content or "" for e in texts if e.kind == "notice" and e.source == "skills"]
    return View(
        parts=tuple(kept),
        per_token=per_token,
        left_out=(cut[0].position, cut[-1].position) if cut else None,
        words=words if cut or unarchived else None,
        unarchived=unarchived,
        shown=shown,
        loaded=given(results),
        discovered=frozenset(shown_in(notices)),
        cells={run: tuple(found) for run, found in cells.items() if found},
    )


def settle(
    view: View,
    archive: Archive,
    context: ExecutionContext,
    *,
    run: str,
    kernels: Sequence[KernelInfo] | None,
) -> View:
    """``view`` as ``run`` sends it: its state block, if it has one, archived and first,
    and a line before each earlier run's part that ran cells, with whether each of their
    kernels is among ``kernels``, those running now (``None``: unknown)."""
    parts: list[Part] = []
    if view.words is not None:
        left = (
            ""
            if view.left_out is None
            else (f"Left out: the messages at positions {view.left_out[0]} to {view.left_out[1]}. ")
        )
        before = (
            ""
            if not view.unarchived
            else (
                f"{view.unarchived} earlier runs ran before the archive existed: their messages "
                "are neither shown nor searched. "
            )
        )
        entry = Entry("user", "state_block", STATE_BLOCK.text + left + before + view.words)
        row = archive.append(context.project_id, context.session_id, run, entry)
        parts.append(Part("state_block", row.chat(), row))
    running = None if kernels is None else {(k.kernel_id, k.generation) for k in kernels}
    current: str | None = None
    for part in view.parts:
        name = part.row.run if part.row is not None else current
        if name is not None and name != current and name in view.cells:
            used = [{**kernel, "now": _now(kernel, running)} for kernel in view.cells[name]]
            line = KERNEL_LINES.text + dump({"run": name, "kernels": used})
            parts.append(Part("kernels", {"role": "user", "content": line}))
        current = name
        parts.append(part)
    return replace(view, parts=tuple(parts), words=None)


def _now(kernel: Json, running: set[tuple[str, int]] | None) -> str:
    if running is None:
        return "unknown"
    found = (kernel.get("kernel_id"), kernel.get("generation"))
    return "running" if found in running else "restarted or stopped"


class ViewMeter(TurnHook):
    """A run's view in tokens before each call (CM-28), and in its record at the end:
    its parts as it started, and each call's estimate beside the input the gateway
    reported for it."""

    name = "view"

    def __init__(self, view: View) -> None:
        self.view = view
        self.estimated: int | None = None
        self.calls: list[Json] = []

    def tokens(self, run: RunView) -> int:
        """The view's tokens before the run's next call."""
        return view_tokens(run.size, run.usage, self.view.per_token)

    async def notices(self, point: Point, run: RunView) -> Sequence[Any]:
        if point == "turn":
            self.estimated = self.tokens(run)
        return ()

    async def model_returned(self, run: RunView) -> None:
        if len(self.calls) < 100:
            reported = run.usage[-1].input_tokens if run.usage else None
            self.calls.append({"estimated": self.estimated, "reported": reported})

    def summary(self) -> object | None:
        return {**self.view.summary(), "calls": self.calls}


def _rows(archive: Archive, context: ExecutionContext) -> list[Archived]:
    """The session's main runs' archived messages, but their state blocks, in order."""
    found: list[Archived] = []
    start = 1
    while True:
        page = archive.messages(context.project_id, context.session_id, start=start)
        found += [row for row in page if row.parent is None and row.entry.kind != "state_block"]
        if len(page) < PAGE:
            return found
        start = page[-1].position + 1


def _units(rows: Sequence[Archived]) -> list[list[Part]]:
    """The rows as the view's units: an answer with its calls' results, a call that has
    none given an aborted one; a result without its call left out; any other message
    alone."""
    units: list[list[Part]] = []
    answer: list[Part] = []
    waiting: list[str] = []

    def close() -> None:
        if answer:
            aborted = {"role": "tool", "content": ABORTED}
            answer.extend(Part("aborted", {**aborted, "tool_call_id": call}) for call in waiting)
            units.append(answer.copy())
        answer.clear()
        waiting.clear()

    for row in rows:
        part, entry = Part(row.entry.kind, row.chat(), row), row.entry
        if entry.role == "tool":
            if answer and entry.call_id in waiting:
                waiting.remove(entry.call_id)
                answer.append(part)
            continue
        close()
        if entry.tool_calls:
            answer.append(part)
            waiting.extend(str(call.get("id")) for call in entry.tool_calls)
        else:
            units.append([part])
    close()
    return units


def _words(rows: Sequence[Archived], share: int) -> tuple[str, list[Archived]]:
    """The user's own words in ``rows``, oldest first, within ``share`` characters: a
    message longer than a quarter of it as its head and tail, and the oldest, past it,
    as one line each, the newest never (CM-30); and the user's messages shown whole."""
    items: list[tuple[Archived, str, str]] = []
    for row in rows:
        if row.entry.kind in ("task", "user"):
            label = "user message" if row.entry.kind == "user" else "task"
            items.append((row, label, row.entry.content or ""))
        elif row.entry.kind == "comment":
            for request in _requests(row.entry.content or ""):
                label = f"comment on document {request.get('document_id')}"
                items.append((row, label, str(request.get("request_text") or "")))
    if not items:
        return "", []
    cap = max(200, share // 4)
    blocks = [_block(row, label, text, cap) for row, label, text in items]
    total, first = sum(map(len, blocks)), 0
    while total > share and first < len(items) - 1:
        row, label, text = items[first]
        line = (
            f"--- {row.message_id} · {label} · {row.created}: {len(text):,} characters, "
            f"beginning {json.dumps(' '.join(text.split())[:80])}; history_read returns it "
            "whole ---"
        )
        total += len(line) - len(blocks[first])
        blocks[first] = line
        first += 1
    quoted = [row for (row, _, text), block in zip(items, blocks, strict=True) if text in block]
    words = "\n".join(blocks)
    return f"\n--- The user's own words ---\n{words}\n--- End of the user's own words ---", quoted


def _block(row: Archived, label: str, text: str, cap: int) -> str:
    if len(text) > cap:
        half = cap // 2
        text = (
            f"{text[:half]}\n[... {len(text) - 2 * half:,} characters left out here; "
            f"history_read {row.message_id} returns the message whole ...]\n{text[-half:]}"
        )
    return f"--- {row.message_id} · {label} · {row.created} ---\n{text}"


def _requests(content: str) -> list[Json]:
    """The work requests a comment notice delivered, as its message holds them."""
    try:
        found = json.loads(content.removeprefix(REQUESTS.text))
    except ValueError:
        return []
    items = cast(list[object], found) if isinstance(found, list) else []
    return [cast(Json, item) for item in items if isinstance(item, dict)]


def _kernel(content: str) -> Json | None:
    """The kernel a cell's result names: its language, ID and generation."""
    try:
        found: object = json.loads(content)
        if isinstance(found, dict) and isinstance(cast(Json, found).get("tool_result"), str):
            found = json.loads(cast(Json, found)["tool_result"])  # notices rode on it
    except ValueError:
        return None
    kernel = cast(Json, found).get("kernel") if isinstance(found, dict) else None
    if not isinstance(kernel, dict) or "kernel_id" not in kernel:
        return None
    return {key: cast(Json, kernel).get(key) for key in ("language", "kernel_id", "generation")}
