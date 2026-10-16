# SPDX-License-Identifier: MIT
"""A main run's view of its session (docs/specs/context-management.md, CM-17, CM-24 to
CM-30, CM-29a to CM-29c): what its model calls see after the system message, built from
the archive at the run's start and again at each fold.

The view holds the session's main runs' messages from the archive, as they were sent:
their tasks, the user's messages, notices, answers, tool calls and results, and the
delegation outcomes they received; never a delegated run's own messages, nor a state
block sent before. Its folds replace their spans: the messages up to the newest fold's
end are shown only as the fold records, in one user-role message after the state block
(the fold region), and the messages after it are the raw tail, but for the tool results
a fold turned into stubs. Each call in the raw tail is followed by its result, found by
its call ID within its run; a call without one, as an interrupted run leaves, gets one
saying it was aborted, and a result without its call is left out (CM-29a). A line before
each earlier run's part names the kernels its cells used and whether each runs now
(CM-29c). The skills whose bodies the raw tail holds count as loaded, and those its
discovery notices showed as shown (CM-29b). The user's chat messages whose text the view
holds are ``shown``: one delivered again is answered where it first appeared (CM-29a,
RS-12).

The state block, archived and sent first, keeps the user's own words from the folded
messages within their share (CM-26, CM-30): a long one as its head and tail, the
oldest, past that share, as one line each, which ``history_read`` returns whole, and the
oldest of those, past it still, as one line for all. A session's runs from before the
archive have no messages in it: their tasks and the user's chat messages they delivered,
as their saved records keep them, come first among those words (RS-10's earlier
conversation, as far as the user's words go).

The region shows each record from its stored form, a record an overview merged only as
that overview (CM-21), and each record's markers current, or retired as an index line,
as code decides at each rebuild (``markers``, CM-21a). Until markers spill (step f2b), a
region still over its budget after merges leaves out its oldest records, which the state
block names and ``history_read`` returns whole.
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
from sandbox_science.orchestrator.notices import FOLD_REGION, KERNEL_LINES, STATE_BLOCK, requests
from sandbox_science.orchestrator.projects import ProjectError
from sandbox_science.orchestrator.skills.discovery import shown_in
from sandbox_science.orchestrator.skills.tool import given
from sandbox_science.orchestrator.tokens import DEFAULT_CHARS, estimate, view_tokens
from sandbox_science.orchestrator.worker_client import dump

from .archive import PAGE, Archive, Archived, Entry, escape
from .geometry import Geometry
from .log import PATIENCE, patiently
from .markers import Judge
from .record import FoldRecord, marker_line, retired_line, stub

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import ProjectStore
    from sandbox_science.orchestrator.runstate import RunState

Json = dict[str, Any]
_log = logging.getLogger(__name__)

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
Unit = tuple["Part", ...]
"""Messages that go together: an answer with its calls' results, or one message."""


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
    """A run's view: before ``settle``, its raw tail; after, all it sends."""

    parts: tuple[Part, ...] = ()
    per_token: float = DEFAULT_CHARS
    """The characters per token of the session's recent calls on the run's model."""
    units: tuple[Unit, ...] = ()
    """The raw tail, unit by unit, as the next fold chooses its span from it."""
    region: Part | None = None
    """The fold region: the records shown, in one message."""
    folds: tuple[str, ...] = ()
    """The fold records the region shows, oldest first."""
    folds_left_out: tuple[str, ...] = ()
    """The oldest fold records, left out of a region over its budget until spills."""
    markers_left_out: tuple[str, ...] = ()
    """Their marker lines, which the state block keeps until spills (CM-21b)."""
    records: tuple[tuple[str, FoldRecord], ...] = ()
    """The records the region would show whole, those left out among them, oldest
    first: none an overview merged."""
    retired: Mapping[str, Mapping[int, str]] = field(default_factory=dict[str, Mapping[int, str]])
    """The markers each record shown renders as retired, by their index, with why
    (CM-21a)."""
    wanted: int = 0
    """The characters the region would take with every record in ``records``."""
    folded: tuple[int, int] | None = None
    """The first and last positions the folds cover."""
    stubs: tuple[str, ...] = ()
    """The tool results the raw tail shows as stubs."""
    words: str | None = None
    """The state block's text, before it is archived (``settle``)."""
    digest: str | None = None
    """The research state the state block renders, framed (``digest``, CM-16, CM-37a)."""
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
        """The view as the run's record keeps it (CM-46): its parts, the state block's
        archive ID, the fold records it shows and leaves out, its stubs, the raw tail's
        first and last positions, and its kernel lines as they were sent."""
        rows = [part.row for unit in self.units for part in unit if part.row is not None]
        return {
            "parts": len(self.parts),
            "positions": [rows[0].position, rows[-1].position] if rows else None,
            "folded": list(self.folded) if self.folded else None,
            "folds": list(self.folds),
            "folds_left_out": list(self.folds_left_out),
            "retired": {fold: sorted(found) for fold, found in self.retired.items()},
            "stubs": list(self.stubs),
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
    geometry: Geometry,
    per_token: float = DEFAULT_CHARS,
    runs: Sequence[str] = (),
    earlier: Earlier | None = None,
    judge: Judge | None = None,
) -> View:
    """The session's view under ``geometry``; ``runs`` are the session's main runs with
    saved records, which tells those from before the archive, and ``earlier`` reads the
    user's messages from their records; ``judge`` decides which markers are retired, by
    the ledger and the read register when it has them. It changes nothing: ``settle``
    archives its state block and adds its kernel lines."""
    rows = _rows(archive, context)
    every = [(row, found) for row in rows if (found := FoldRecord.of(row)) is not None]
    merged = {part for _, found in every for part in found.parts}
    # By the span each covers: an overview is archived after the records it merged.
    records = sorted(
        ((row, found) for row, found in every if row.message_id not in merged),
        key=lambda pair: pair[1].first[0],
    )
    gone = (judge or Judge()).retired([(row.message_id, found) for row, found in every], rows)
    retired = {row.message_id: gone[row.message_id] for row, _ in records if row.message_id in gone}
    end = max((found.last[0] for _, found in every), default=0)
    folded = [row for row in rows if row.position <= end and row.entry.kind != "fold"]
    raw = [row for row in rows if row.position > end and row.entry.kind != "fold"]
    stubs = {at for _, found in every for at in found.stubs}
    units = [_stubbed(unit, stubs) for unit in _units(raw)]
    region, shown_folds, left, wanted = _region(records, int(geometry.region * per_token), retired)
    kept = [part for unit in units for part in unit]
    archived = {row.run for row in rows}
    before = [run for run in runs if run not in archived]
    recorded = list(earlier(before)) if earlier is not None and before else []
    words, quoted = _words(folded, int(geometry.words * per_token), recorded)
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
    texts = [p.row.entry for p in kept if p.row is not None and p.kind != "stub"]
    results = [e.content or "" for e in texts if e.kind == "tool_result" and e.source == "skills"]
    notices = [e.content or "" for e in texts if e.kind == "notice" and e.source == "skills"]
    return View(
        parts=tuple(kept),
        per_token=per_token,
        units=tuple(units),
        region=region,
        folds=shown_folds,
        folds_left_out=left,
        markers_left_out=_left_markers(
            records, left, int(geometry.region * per_token) // 4, retired
        ),
        records=tuple((row.message_id, found) for row, found in records),
        retired=retired,
        wanted=wanted,
        folded=(folded[0].position, end) if folded else None,
        stubs=tuple(p.row.message_id for p in kept if p.kind == "stub" and p.row),
        words=words if folded or before or left else None,
        unarchived=len(before),
        recorded=len(recorded),
        shown=shown,
        loaded=given(results),
        discovered=frozenset(shown_in(notices)),
        cells={run: tuple(found) for run, found in cells.items() if found},
    )


def _region(
    records: Sequence[tuple[Archived, FoldRecord]],
    budget: int,
    retired: Mapping[str, Mapping[int, str]],
) -> tuple[Part | None, tuple[str, ...], tuple[str, ...], int]:
    """The fold region within ``budget`` characters, the records it shows and those it
    leaves out: the oldest, until spills are built, but never the newest; and the
    characters it would take whole, which tells the folds to merge (CM-21)."""
    texts = [
        (row.message_id, escape(found.text(row.message_id, retired=retired.get(row.message_id))))
        for row, found in records
    ]
    wanted = sum(len(t) + 2 for _, t in texts)
    start = 0
    while start < len(texts) - 1 and sum(len(t) + 2 for _, t in texts[start:]) > budget:
        start += 1
    if not texts:
        return None, (), (), 0
    content = FOLD_REGION.text + "\n\n".join(text for _, text in texts[start:])
    region = Part("fold_region", {"role": "user", "content": content})
    shown = tuple(at for at, _ in texts[start:])
    return region, shown, tuple(at for at, _ in texts[:start]), wanted


def _left_markers(
    records: Sequence[tuple[Archived, FoldRecord]],
    left: Sequence[str],
    budget: int,
    retired: Mapping[str, Mapping[int, str]] | None = None,
) -> tuple[str, ...]:
    """The marker lines of the records ``left`` out, within ``budget`` characters, a
    quarter of the region's, retired ones as their index lines: failure aggregates give
    way first, then retired markers, then caveats and reads, and decision lines last,
    the oldest first in each (CM-21b); the last line says how many gave way and how to
    read them whole."""
    found: list[tuple[int, str, str]] = []
    for row, record in records:
        if row.message_id not in left:
            continue
        gone_here = (retired or {}).get(row.message_id, {})
        for index, marker in enumerate(record.markers):
            if index in gone_here:
                line, rank = retired_line(marker, gone_here[index]), 1
            else:
                line = marker_line(marker, row.message_id)
                kind = marker.get("kind")
                rank = 0 if kind == "failures" else 3 if kind == "decision" else 2
            found.append((rank, line, row.message_id))
    total = sum(len(line) + 3 for _, line, _ in found)
    gone: set[int] = set()
    for index in sorted(range(len(found)), key=lambda i: (found[i][0], i)):
        if total <= budget:
            break
        gone.add(index)
        total -= len(found[index][1]) + 3
    kept = tuple(line for index, (_, line, _) in enumerate(found) if index not in gone)
    if not gone:
        return kept
    spilled = [found[index][2] for index in sorted(gone)]
    first, last = spilled[0], spilled[-1]  # in the records' order, oldest first
    named = first if first == last else f"{first} to {last}"
    return (
        *kept,
        f"{len(gone)} more marker lines, of the records {named}, are left out here; "
        "history_read returns each record whole",
    )


def _stubbed(unit: Sequence[Part], stubs: set[str]) -> Unit:
    """A unit with its results that folds stubbed shown as stubs."""
    calls = {
        str(call.get("id")): call
        for call in (unit[0].row.entry.tool_calls if unit[0].row is not None else ())
    }
    return tuple(
        Part("stub", stub(part.row, calls.get(str(part.row.entry.call_id))), part.row)
        if part.row is not None and part.row.message_id in stubs
        else part
        for part in unit
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
    parent: str | None = None,
    patience: float = PATIENCE,
    clock: Clock = SYSTEM_CLOCK,
) -> View:
    """``view`` as ``run`` sends it: its state block, if it has one, archived and first,
    tried again while the store is busy; its fold region; and its raw tail, with a line
    before each earlier run's part that ran cells, saying whether each of their kernels
    is among ``kernels``, those running as ``run`` started (``None``: unknown). A
    rebuild during ``run`` adds none before its own part. The state block holds the
    research-state digest too, when the view has one; a delegated run's (``parent``)
    holds what CM-37a gives it."""
    parts: list[Part] = []
    text = ""
    if view.words is not None:
        folded = ""
        if view.folded is not None:
            first, last = view.folded
            folded = f"Folded: the messages at positions {first} to {last}. "
        if view.folds_left_out:
            folded += (
                f"Left out of the fold region, over its budget: the fold records "
                f"{', '.join(view.folds_left_out)}, which history_read returns whole"
            )
            folded += (
                "; their marker lines, as code built them:\n"
                + "".join(f"- {line}\n" for line in view.markers_left_out)
                if view.markers_left_out
                else ". "
            )
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
        text = STATE_BLOCK.text + folded + before + view.words
    if view.digest:
        text = f"{text}\n\n{view.digest}" if text else view.digest
    if text:
        entry = Entry("user", "state_block", text)
        row = await patiently(
            archive, context, run, entry, parent=parent, patience=patience, clock=clock
        )
        parts.append(Part("state_block", row.chat(), row))
    if view.region is not None:
        parts.append(view.region)
    running = None if kernels is None else {(k.kernel_id, k.generation) for k in kernels}
    current: str | None = None
    for part in view.parts:
        name = part.row.run if part.row is not None else current
        if name is not None and name not in (current, run) and name in view.cells:
            used = [{**kernel, "now": _now(kernel, running)} for kernel in view.cells[name]]
            line = KERNEL_LINES.text + dump({"run": name, "kernels": used})
            parts.append(Part("kernels", {"role": "user", "content": line}))
        current = name
        parts.append(part)
    return replace(view, parts=tuple(parts), words=None, digest=None)


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
        return view_tokens(run.size, run.usage[run.since :], self.view.per_token)

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


def spanned(archive: Archive, context: ExecutionContext, first: int, last: int) -> list[Archived]:
    """The session's main runs' messages at positions ``first`` to ``last``, as a view
    holds them: neither fold records nor state blocks."""
    return [
        row
        for row in _rows(archive, context)
        if first <= row.position <= last and row.entry.kind != "fold"
    ]


def _rows(archive: Archive, context: ExecutionContext) -> list[Archived]:
    """The session's main runs' archived messages, fold records among them, but their
    state blocks, in order."""
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
