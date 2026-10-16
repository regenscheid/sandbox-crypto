# SPDX-License-Identifier: MIT
"""Folding a main run's view (docs/specs/context-management.md, CM-17 to CM-20, CM-23,
CM-24, CM-27, CM-43 to CM-46).

At each safe point, before a model call, ``Folder`` measures the view in tokens (CM-28).
Past the trigger it folds the oldest span of the raw tail; past the hard wall it folds
again until the view is under the wall or the raw tail is at its floor. A span is about
a chunk of the oldest raw messages, never leaving fewer than the floor, and ends at a
boundary that splits no call from its results: one where a user message arrived or a
plan step changed, when one lies within a quarter of a chunk of that size, and the
nearest otherwise (CM-23). Its record is built by code: the skeleton, the marker lines
and the search keys (CM-18), checked to be at most a tenth of a chunk without its
markers and to free tokens (CM-20); a record that fails changes nothing. The record is
archived, and the view rebuilt from the archive: the state block, the fold region, and
the raw tail after the record's span, its old large tool results now stubs (CM-24). The
rebuilt view replaces the conversation the run sends, and the next call's size is a
fresh estimate (CM-28).

Every fold is skeleton-only until the enrichment call is built (step f1, its second
part). A gateway context error folds once and tries the call once more; a second in a
row stops the run (CM-43). After three folds that each freed less than a tenth of the
view, the run folds no more, and stops if the view then passes the wall (CM-44). A view
still over the wall with nothing left to fold stops the run (CM-45). The run's record
keeps the geometry, each fold and each rebuild's parts, and each fold is a run event of
kind ``fold`` (CM-46). A run whose system message and tools pass a fifth of the ceiling,
or whose scale λ would fall below 0.4, does not start (CM-27).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from itertools import accumulate
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, KernelInfo
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.hooks import RunView, TurnHook
from sandbox_science.orchestrator.plan_tools import NAMES as PLAN_NAMES
from sandbox_science.orchestrator.tokens import chars_per_token, estimate, view_tokens
from sandbox_science.orchestrator.worker_client import dump

from .archive import Archive, Archived, Entry
from .geometry import Geometry, geometry
from .log import PATIENCE, patiently
from .reads import Read, Register
from .record import FORMAT, FoldRecord, stubbed
from .skeleton import skeleton
from .view import Earlier, Unit, View, session_view, settle

if TYPE_CHECKING:
    from sandbox_science.orchestrator.loop import Transcript
    from sandbox_science.orchestrator.projects import ModelUsage

Json = dict[str, Any]
_log = logging.getLogger(__name__)

WEAK: Final = 0.1
"""A fold that frees less than this share of the view is weak (CM-44)."""
BREAKER: Final = 3
"""Weak folds after which a run folds no more."""
PLAN_CHANGES: Final = PLAN_NAMES - {"plan_read"}


class Folder(TurnHook):
    """A main run's folds. Its summary is the run record's ``folds``."""

    name = "folds"

    def __init__(
        self,
        archive: Archive,
        context: ExecutionContext,
        *,
        run: str,
        view: View,
        start: Geometry,
        ceiling: int,
        chunk: int | None = None,
        calls: Sequence[ModelUsage] = (),
        runs: Sequence[str] = (),
        earlier: Earlier | None = None,
        kernels: Sequence[KernelInfo] | None = None,
        digest: Callable[[int], str | None] | None = None,
        events: Callable[[Json], Awaitable[None]] | None = None,
        refit: Callable[[View], None] | None = None,
        clock: Clock = SYSTEM_CLOCK,
        patience: float = PATIENCE,
    ) -> None:
        """``view`` is the run's view as it starts, built under ``start``, a geometry
        before the system message is known; ``calls`` are the session's earlier calls on
        the run's alias, which calibrate its characters per token; ``runs`` and
        ``earlier`` are ``session_view``'s; ``digest`` renders the research state within
        a budget in characters, for the state block of each rebuild (CM-26); ``events``
        writes a ``fold`` event, and
        ``refit`` tells the skills what a rebuilt view holds (CM-29b)."""
        self.archive, self.context, self.run = archive, context, run
        self.view, self.start, self.ceiling, self.chunk = view, start, ceiling, chunk
        self.calls, self.runs, self.earlier, self.kernels = (
            tuple(calls),
            tuple(runs),
            earlier,
            kernels,
        )
        self.events, self.refit, self.clock, self.patience = events, refit, clock, patience
        self.digest = digest
        self.geometry: Geometry | None = None
        self.folds: list[Json] = []
        self.rebuilds: list[Json] = []
        self.weak = 0
        self.overflows = 0
        self.stopped: str | None = None
        """Why the run stopped for its context: ``start``, ``breaker``, ``overflow`` or
        ``irreducible``."""
        self.detail: str | None = None
        self.capped: int | None = None
        """The ceiling a context error capped at the largest input the model took."""

    @property
    def tripped(self) -> bool:
        return self.weak >= BREAKER

    def per_token(self, run: RunView) -> float:
        return chars_per_token([*self.calls, *run.usage])

    def tokens(self, run: RunView) -> int:
        """The view's tokens before the next call (CM-28)."""
        return view_tokens(run.size, run.usage[run.since :], self.per_token(run))

    async def before_call(self, transcript: Transcript) -> bool:
        if self.geometry is None:
            per_token = self.per_token(transcript)
            system = len(dump([transcript.messages[0]])) + transcript.tools_chars
            found = geometry(self.ceiling, estimate(system, per_token), self.chunk)
            self.geometry = found
            if (why := found.refused()) is not None:
                return self._stop("start", why)
            if abs(found.scale - self.start.scale) > 1e-6:
                await self._rebuild(transcript)  # the budgets shrink with λ
        wall, size = self.geometry.wall, self.tokens(transcript)
        while size > wall and await self._fold(transcript, size):
            size = self.tokens(transcript)
        if size > self.geometry.trigger:
            await self._fold(transcript, size)
            size = self.tokens(transcript)
        if size <= wall:
            return True
        if self.tripped:
            return self._stop("breaker", f"three weak folds, and the view is {size:,} tokens")
        return self._stop("irreducible", f"nothing left to fold, and the view is {size:,} tokens")

    async def overflow(self, transcript: Transcript) -> bool:
        self.overflows += 1
        self._cap(transcript)
        if self.overflows == 1 and await self._fold(transcript, self.tokens(transcript)):
            return True
        return self._stop("overflow", "the gateway refused the call for its length")

    def _cap(self, transcript: Transcript) -> None:
        """After a context error: the ceiling no larger than the largest input the model
        took in this run, which it is known to take (CM-43)."""
        taken = [usage.input_tokens for usage in transcript.usage if usage.input_tokens]
        if self.geometry is None or not taken or max(taken) >= self.geometry.ceiling:
            return
        capped = geometry(max(taken), self.geometry.prompt, self.chunk)
        if capped.refused() is not None:  # a report too small to be the model's limit
            return
        self.geometry, self.capped = capped, max(taken)
        _log.warning(
            "run %s caps its ceiling at %d tokens after a context error", self.run, max(taken)
        )

    async def model_returned(self, run: RunView) -> None:
        self.overflows = 0

    def _stop(self, reason: str, detail: str) -> bool:
        self.stopped, self.detail = reason, detail
        _log.warning("run %s stops for its context (%s): %s", self.run, reason, detail)
        return False

    async def _fold(self, transcript: Transcript, before: int) -> bool:
        """One skeleton-only fold of the raw tail's oldest span, applied; whether one was.
        A record too large for its share is built again of half its span, down to one
        unit; one that still cannot be built changes nothing (CM-20)."""
        if self.tripped or self.geometry is None:
            return False
        shape, per_token = self.geometry, self.per_token(transcript)
        view = await threaded(self._view, per_token)
        taken = span(view.units, shape, per_token)
        record, rows = None, []
        while taken:
            rows = [p.row for unit in view.units[:taken] for p in unit if p.row is not None]
            sent = sum(part.chars for unit in view.units[:taken] for part in unit)
            reads = await threaded(self._reads, [row.message_id for row in rows])
            record = build(rows, shape, per_token, sent=sent, reads=reads)
            if record is not None or taken == 1:
                break
            taken = extend(view.units, max(1, taken // 2))
        if record is None:
            if rows:
                await self._event({"state": "rejected", "span": _span(rows), "reason": "too large"})
            return False
        after = [part.row for unit in view.units[taken:] for part in unit if part.row is not None]
        record = replace(record, stubs=stubbed(after, view.stubs))
        entry = Entry(
            "user",
            "fold",
            record.text("this record", whole=True),
            source="folds",
            native=record.stored(),
            native_format=FORMAT,
        )
        row = await patiently(
            self.archive, self.context, self.run, entry, patience=self.patience, clock=self.clock
        )
        await self._rebuild(transcript)
        freed = before - self.tokens(transcript)
        # Folds before the run's first call catch up on a session folded little or not at
        # all, such as one from before folds: the breaker counts the run's own (CM-44).
        if freed < WEAK * before and transcript.usage:
            self.weak += 1
        made = {
            "fold": row.message_id,
            "level": record.level,
            "span": _span(rows),
            "state": record.state,
            "freed": freed,
            "stubs": len(record.stubs),
        }
        self.folds.append(made)
        await self._event(made)
        return True

    def _reads(self, messages: Sequence[str]) -> list[tuple[str, Read]]:
        return Register(self.archive.control).of_messages(self.context.project_id, messages)

    def _view(self, per_token: float) -> View:
        assert self.geometry is not None  # noqa: S101 - set at the first call
        return session_view(
            self.archive,
            self.context,
            geometry=self.geometry,
            per_token=per_token,
            runs=self.runs,
            earlier=self.earlier,
        )

    async def _rebuild(self, transcript: Transcript) -> None:
        """The view rebuilt from the archive, sent from now on, its state block with the
        research state as it is now (CM-26)."""
        per_token = self.per_token(transcript)
        view = await threaded(self._view, per_token)
        if self.digest is not None and self.geometry is not None:
            budget = int(self.geometry.digest * per_token)
            view = replace(view, digest=await threaded(self.digest, budget))
        view = await settle(
            view,
            self.archive,
            self.context,
            run=self.run,
            kernels=self.kernels,
            patience=self.patience,
            clock=self.clock,
        )
        transcript.rebuild(view.messages)
        self.view = view
        if len(self.rebuilds) < 100:
            self.rebuilds.append({"call": transcript.turns, **view.summary()})
        if self.refit is not None:
            self.refit(view)

    async def _event(self, data: Json) -> None:
        if self.events is not None:
            await self.events(data)

    def summary(self) -> object | None:
        if self.geometry is None:
            return None
        found: Json = {
            "geometry": self.geometry.summary(),
            "folds": self.folds,
            "rebuilds": self.rebuilds,
            "weak_folds": self.weak,
        }
        if self.capped is not None:
            found["capped"] = self.capped
        if self.stopped is not None:
            found |= {"stopped": self.stopped, "detail": self.detail}
        return found


def span(units: Sequence[Unit], shape: Geometry, per_token: float) -> int:
    """How many of the raw tail's oldest ``units`` the next fold takes: about a chunk,
    never leaving the raw tail below its floor, ending where a user message arrived or a
    plan step changed when one lies within a quarter of a chunk, and at the nearest unit
    boundary otherwise (CM-23); 0 when nothing can be taken."""
    sizes = [estimate(sum(part.chars for part in unit), per_token) for unit in units]
    room = sum(sizes) - shape.floor
    target = min(shape.chunk, room)
    if target <= 0:
        return 0
    ends = [(end, size) for end, size in enumerate(accumulate(sizes), 1) if size <= room]
    if not ends:
        return 0
    tasks = [
        (end, size)
        for end, size in ends
        if abs(size - target) <= shape.chunk / 4 and _boundary(units, end)
    ]
    return extend(units, min(tasks or ends, key=lambda found: abs(found[1] - target))[0])


def extend(units: Sequence[Unit], taken: int) -> int:
    """``taken`` units and any after them positioned among theirs (a notice between a
    call and its result): a fold covers every position up to its last."""
    last = max(_positions(units[:taken]), default=0)
    while taken < len(units) and min(_positions(units[taken : taken + 1]), default=last) < last:
        last = max(last, *_positions(units[taken : taken + 1]))
        taken += 1
    return taken


def _boundary(units: Sequence[Unit], end: int) -> bool:
    """Whether a task boundary lies after ``units[:end]``: a user message arrives next,
    or the last unit changed the plan."""
    if end < len(units):
        first = units[end][0].row
        if first is not None and first.entry.kind in ("user", "task", "comment"):
            return True
    last = units[end - 1][0].row
    calls = last.entry.tool_calls if last is not None else ()
    names = [cast(Json, call.get("function") or {}).get("name") for call in calls]
    return any(name in PLAN_CHANGES for name in names)


def _positions(units: Sequence[Unit]) -> list[int]:
    return [part.row.position for unit in units for part in unit if part.row is not None]


def _span(rows: Sequence[Archived]) -> list[int]:
    return [rows[0].position, max(row.position for row in rows)] if rows else []


def build(
    rows: Sequence[Archived],
    shape: Geometry,
    per_token: float,
    *,
    sent: int | None = None,
    reads: Sequence[tuple[str, Read]] = (),
) -> FoldRecord | None:
    """A skeleton-only record of ``rows``, or ``None`` when it would pass a tenth of a
    chunk without its markers, or free nothing (CM-20): be no smaller than the span as
    the view sends it, ``sent`` characters, its stubs as stubs. ``reads`` are what the
    read register says the span's messages read."""
    if not rows:
        return None
    share = int(shape.record * per_token)
    made = skeleton(rows, share - share // 10 - 600, share // 10, per_token, reads)
    first, last = rows[0], max(rows, key=lambda row: row.position)
    record = FoldRecord(
        level=1,
        first=(first.position, first.message_id),
        last=(last.position, last.message_id),
        runs=tuple(dict.fromkeys(row.run for row in rows)),
        dates=(first.created, last.created),
        skeleton=made.lines,
        markers=made.markers,
        keys=made.keys,
    )
    spanned = sent if sent is not None else sum(len(dump(row.chat())) for row in rows)
    if record.chars > share or len(record.text("msg-" + "0" * 16)) >= spanned:
        return None
    return record
