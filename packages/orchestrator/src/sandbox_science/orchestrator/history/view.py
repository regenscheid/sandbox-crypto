# SPDX-License-Identifier: MIT
"""A main run's view of its session as it starts (docs/specs/context-management.md,
CM-25 to CM-30, CM-29a to CM-29c): what its model calls see before its own messages.

The view holds the session's main runs' messages from the archive, as they were sent:
their tasks, the user's messages, notices, answers, tool calls and results, and the
delegation outcomes they received; never a delegated run's own messages, nor a state
block sent before. Each call is followed by its result, found by its call ID within its
run; a call without one, as an interrupted run leaves, gets one saying it was aborted,
and a result without its call is left out (CM-29a). A line before each earlier run's
part names the kernels its cells used and whether each runs now (CM-29c). The skills
whose bodies the view holds count as loaded, and those its discovery notices showed as
shown (CM-29b). The user's chat messages whose text the view holds are ``shown``: one
delivered again is answered where it first appeared (CM-29a, RS-12).

Folds are not built yet. Until they are, a view that would pass ``SHARE`` of the run's
input leaves out its oldest messages at a boundary that splits no call from its results,
but always keeps its newest unit and the user's latest message; and a state block,
archived and sent first, says so and keeps the user's own words from them within
``WORDS`` of the input: a long one as its head and tail, the oldest, past that share,
as one line each, which ``history_read`` returns whole, and the oldest of those, past it
still, as one line for all (CM-26, CM-30). The run's context limit still stops a run
whose view does not fit.

A session's runs from before the archive have no messages in it. Their tasks and the
user's chat messages they delivered, as their saved records keep them, join the user's
words in the state block (RS-10's earlier conversation, as far as the user's words go).

``View.parts`` are what folds will work on: each message with its archive row (ID,
position, run), its characters and, at ``View.per_token``, its tokens (CM-28).
``ViewMeter`` measures the view before each call and keeps, in the run's record, its
parts at the start, its kernel lines, and each call's estimate beside what the gateway
reported.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, KernelInfo
from sandbox_science.orchestrator.documents.tool import REQUESTS_TASK
from sandbox_science.orchestrator.hooks import Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import KERNEL_LINES, STATE_BLOCK, requests
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.skills.discovery import shown_in
from sandbox_science.orchestrator.skills.tool import given
from sandbox_science.orchestrator.tokens import DEFAULT_CHARS, estimate, view_tokens
from sandbox_science.orchestrator.worker_client import dump

from .archive import PAGE, Archive, Archived, Entry
from .log import PATIENCE, patiently

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import ProjectStore
    from sandbox_science.orchestrator.runstate import RunState

Json = dict[str, Any]
_log = logging.getLogger(__name__)

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

Said = tuple[str, str, str]
"""A message of the user's that a run's saved record keeps: the run, a label, its text."""
Earlier = Callable[[Sequence[str]], Sequence[Said]]
"""The user's messages that the named runs' saved records keep, oldest first."""


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
    kept_apart: str | None = None
    """The user's latest message, kept apart from the newest messages the view keeps,
    though messages near it are left out."""
    words: str | None = None
    """The state block's text, before it is archived (``settle``)."""
    unarchived: int = 0
    """The session's main runs that ran before the archive existed."""
    recorded: int = 0
    """The user's messages from those runs, as their saved records keep them."""
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
        """The view as the run's record keeps it: its parts by archive ID and position,
        and its kernel lines as they were sent (CM-46)."""
        rows = [part.row for part in self.parts if part.row is not None]
        return {
            "parts": len(self.parts),
            "positions": [rows[0].position, rows[-1].position] if rows else None,
            "left_out": list(self.left_out) if self.left_out else None,
            "kept_apart": self.kept_apart,
            "unarchived_runs": self.unarchived,
            "recorded_messages": self.recorded,
            "aborted_calls": sum(part.kind == "aborted" for part in self.parts),
            "chars": sum(part.chars for part in self.parts),
            "tokens": self.tokens,
            "chars_per_token": round(self.per_token, 3),
            "state_block": next(
                (p.row.message_id for p in self.parts if p.kind == "state_block" and p.row), None
            ),
            "kernel_lines": [str(p.message["content"]) for p in self.parts if p.kind == "kernels"],
        }


def session_view(
    archive: Archive,
    context: ExecutionContext,
    *,
    budget: int,
    per_token: float = DEFAULT_CHARS,
    runs: Sequence[str] = (),
    earlier: Earlier | None = None,
) -> View:
    """The session's view for a main run whose input may hold ``budget`` characters;
    ``runs`` are the session's main runs with saved records, which tells those from
    before the archive, and ``earlier`` reads the user's messages from their records. It
    changes nothing: ``settle`` archives its state block and adds its kernel lines."""
    rows = _rows(archive, context)
    units = _units(rows)
    sizes = [sum(part.chars for part in unit) for unit in units]
    start = 0
    if sum(sizes) > budget * SHARE:
        room, start = int(budget * (SHARE - WORDS)), len(units)
        while start > 0 and sizes[start - 1] <= room:
            room -= sizes[start - 1]
            start -= 1
        start = min(start, len(units) - 1)  # the newest unit, always; the limit stops it
    latest = next((i for i in reversed(range(len(units))) if _from_user(units[i][0])), None)
    apart = latest if latest is not None and latest < start else None
    kept = [part for i, unit in enumerate(units) if i >= start or i == apart for part in unit]
    cut = [
        part.row
        for i, unit in enumerate(units[:start])
        if i != apart
        for part in unit
        if part.row is not None
    ]
    archived = {row.run for row in rows}
    before = [run for run in runs if run not in archived]
    recorded = list(earlier(before)) if earlier is not None and before else []
    words, quoted = _words(cut, int(budget * WORDS), recorded)
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
    first = units[apart][0].row if apart is not None else None
    return View(
        parts=tuple(kept),
        per_token=per_token,
        left_out=(cut[0].position, cut[-1].position) if cut else None,
        kept_apart=first.message_id if first is not None and cut else None,
        words=words if cut or before else None,
        unarchived=len(before),
        recorded=len(recorded),
        shown=shown,
        loaded=given(results),
        discovered=frozenset(shown_in(notices)),
        cells={run: tuple(found) for run, found in cells.items() if found},
    )


def recorded_words(
    projects: ProjectStore, state: RunState, project: str, session: str, runs: Sequence[str]
) -> list[Said]:
    """The user's messages that ``runs``' saved records keep, oldest first: each run's
    task, unless it stands for messages it delivered or for comments, and the chat
    messages it delivered. A record that cannot be read is passed over."""
    found: list[Said] = []
    for run in runs:
        try:
            record = projects.run(project, session, run)
        except ProjectError as error:
            _log.warning("the record of run %s could not be read for the view: %s", run, error)
            continue
        summary = record.summaries.get("messages")
        initial = isinstance(summary, dict) and bool(cast(Json, summary).get("initial"))
        if record.task and not initial and record.task != REQUESTS_TASK:
            found.append((run, "task", record.task))
        found += [
            (run, "user message", text) for _, text in state.delivered_texts(project, session, run)
        ]
    return found


async def settle(
    view: View,
    archive: Archive,
    context: ExecutionContext,
    *,
    run: str,
    kernels: Sequence[KernelInfo] | None,
    patience: float = PATIENCE,
    clock: Clock = SYSTEM_CLOCK,
) -> View:
    """``view`` as ``run`` sends it: its state block, if it has one, archived and first,
    tried again while the store is busy, and a line before each earlier run's part that
    ran cells, with whether each of their kernels is among ``kernels``, those running now
    (``None``: unknown)."""
    parts: list[Part] = []
    if view.words is not None:
        left = ""
        if view.left_out is not None:
            first, last = view.left_out
            kept = next(
                (p.row for p in view.parts if p.row and p.row.message_id == view.kept_apart), None
            )
            apart = (
                f", but for the user's latest message, {view.kept_apart}, which is kept"
                if kept is not None and first < kept.position < last
                else ""
            )
            left = f"Left out: the messages at positions {first} to {last}{apart}. "
        before = ""
        if view.unarchived:
            before = (
                f"{view.unarchived} earlier runs ran before the archive existed: their messages "
                "are neither shown nor searched"
                + (
                    "; the user's messages from them are among the user's words below, as "
                    "their runs' records keep them. "
                    if view.recorded
                    else ". "
                )
            )
        entry = Entry("user", "state_block", STATE_BLOCK.text + left + before + view.words)
        row = await patiently(archive, context, run, entry, patience=patience, clock=clock)
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
    """The rows as the view's units: an answer with its calls' results, each found by its
    call ID among its run's later results, and a call that has none given an aborted one;
    a result no call claims left out; any other message alone."""
    results: dict[tuple[str, str], list[Archived]] = {}
    for row in rows:
        if row.entry.role == "tool":
            results.setdefault((row.run, str(row.entry.call_id)), []).append(row)
    units: list[list[Part]] = []
    for row in rows:
        entry = row.entry
        if entry.role == "tool":
            continue
        unit = [Part(entry.kind, row.chat(), row)]
        for call in entry.tool_calls:
            ident = str(call.get("id"))
            waiting = results.get((row.run, ident), [])
            found = next((result for result in waiting if result.position > row.position), None)
            if found is None:
                aborted = {"role": "tool", "content": ABORTED, "tool_call_id": ident}
                unit.append(Part("aborted", aborted))
            else:
                waiting.remove(found)
                unit.append(Part(found.entry.kind, found.chat(), found))
        units.append(unit)
    return units


def _from_user(part: Part) -> bool:
    """Whether a unit's first part holds the user's own words: a message, or a task that
    is not the stand-in for comments."""
    entry = part.row.entry if part.row is not None else None
    if entry is None or entry.kind not in ("task", "user"):
        return False
    return entry.kind == "user" or entry.content != REQUESTS_TASK


@dataclass(frozen=True)
class _Words:
    """One message of the user's own words: in the archive, or in a run's saved record."""

    name: str
    label: str
    text: str
    row: Archived | None = None

    @property
    def head(self) -> str:
        when = f" · {self.row.created}" if self.row is not None else ""
        return f"--- {self.name} · {self.label}{when}"

    @property
    def whole(self) -> str:
        if self.row is None:
            return "its run's saved record keeps it whole"
        return "history_read returns it whole"


def _words(
    rows: Sequence[Archived], share: int, recorded: Sequence[Said] = ()
) -> tuple[str, list[Archived]]:
    """The user's own words, ``recorded`` first and then those in ``rows``, oldest first,
    within ``share`` characters: a message longer than a quarter of it as its head and
    tail; the oldest, past it, as one line each, the newest never; and the oldest of
    those, past it still, as one line for all (CM-30). Also the user's messages shown
    whole."""
    items = [_Words(f"run {run}", label, text) for run, label, text in recorded]
    for row in rows:
        content = row.entry.content or ""
        if row.entry.kind == "user" or (row.entry.kind == "task" and content != REQUESTS_TASK):
            label = "user message" if row.entry.kind == "user" else "task"
            items.append(_Words(row.message_id, label, content, row))
        elif row.entry.kind == "comment":
            for request in requests(content):
                label = f"comment on document {request.get('document_id')}"
                text = str(request.get("request_text") or "")
                items.append(_Words(row.message_id, label, text, row))
    if not items:
        return "", []
    cap = max(200, share // 4)
    blocks = [_block(item, cap) for item in items]
    total, first = sum(len(block) + 1 for block in blocks), 0
    while total > share and first < len(items) - 1:
        item = items[first]
        line = (
            f"{item.head}: {len(item.text):,} characters, beginning "
            f"{json.dumps(' '.join(item.text.split())[:80])}; {item.whole} ---"
        )
        total += len(line) - len(blocks[first])
        blocks[first] = line
        first += 1
    gone = 0
    if total > share and first > 1:  # even the pointers pass it: the oldest become one line
        freed = 0
        for gone in range(1, first + 1):
            freed += len(blocks[gone - 1]) + 1
            if gone > 1 and total - freed + len(_range(items[:gone])) + 1 <= share:
                break
        total += len(_range(items[:gone])) + 1 - freed
    kept = list(zip(items, blocks, strict=True))[gone:]
    quoted = [item.row for item, block in kept if item.row is not None and item.text in block]
    lines = ([_range(items[:gone])] if gone else []) + [block for _, block in kept]
    words = "\n".join(lines)
    return f"\n--- The user's own words ---\n{words}\n--- End of the user's own words ---", quoted


def _range(items: Sequence[_Words]) -> str:
    """One line for the oldest of the user's messages, past their share even as pointers."""
    found = (
        "history_search and history_read find them"
        if all(item.row is not None for item in items)
        else (
            "history_search and history_read find those in the archive; the others' runs' "
            "saved records keep them"
        )
    )
    return f"--- {len(items):,} earlier messages, {items[0].name} to {items[-1].name}; {found} ---"


def _block(item: _Words, cap: int) -> str:
    text = item.text
    if len(text) > cap:
        half = cap // 2
        where = (
            f"history_read {item.name} returns the message whole"
            if item.row is not None
            else "the run's saved record keeps the message whole"
        )
        text = (
            f"{text[:half]}\n[... {len(text) - 2 * half:,} characters left out here; "
            f"{where} ...]\n{text[-half:]}"
        )
    return f"{item.head} ---\n{text}"


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
