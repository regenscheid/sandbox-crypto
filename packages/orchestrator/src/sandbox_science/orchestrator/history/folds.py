# SPDX-License-Identifier: MIT
"""Folding a main run's view (docs/specs/context-management.md, CM-17 to CM-20, CM-23,
CM-24, CM-24a, CM-27, CM-43 to CM-46).

At each safe point, before a model call, ``Folder`` measures the view in tokens (CM-28).
Past the trigger it starts a fold of the oldest span of the raw tail: about a chunk of
the oldest raw messages, never leaving fewer than the floor, ending at a boundary that
splits no call from its results: one where a user message arrived or a plan step
changed, when one lies within a quarter of a chunk of that size, and the nearest
otherwise (CM-23). Code builds its record: the skeleton, the marker lines and the search
keys (CM-18), checked to be at most a tenth of a chunk without its markers and to free
tokens (CM-20); a record that fails changes nothing. The enrichment call then writes its
narrative while the agent goes on (CM-19, ``enrich``), and the fold is archived when it
is ready and applied at the first safe point after: the view is rebuilt from the
archive, the state block, the fold region, and the raw tail after the record's span, its
old large tool results now stubs (CM-24). The rebuilt view replaces the conversation
the run sends, and the next call's size is a fresh estimate (CM-28).

Past the hard wall, a running fold is waited for, at most ``WAIT``, and applied, or its
span folds skeleton-only if it is not ready by then; then the run folds again,
skeleton-only, until the view is under the wall or the raw tail is at its floor. A fold
ready when its run ends is archived already, and the next run's start applies it; one
still running is cancelled with the run, which records that its call was (CM-24a).
Without an enrichment model, as a scripted run's or with the skeleton-only setting (the
control arm of question 4), every fold is skeleton-only and applied at once.

Records merge into overviews (CM-21, ``overview``): the first-level records, once three
or more total 0.4 of a chunk, and the region's two oldest while it would pass its
budget. Below the wall the run's model updates the overview's narrative while the agent
goes on, as it writes a fold's; at the wall, after a context error, and without a model,
code merges at once (CM-21c). A merge whose stored markers are not its parts' is
rejected. Which markers render retired is decided at each rebuild (``markers``).

A gateway context error folds once, skeleton-only, and tries the call once more; a
second in a row stops the run (CM-43). After three folds that each freed less than a
tenth of the view, the run folds no more, and stops if the view then passes the wall
(CM-44). A view still over the wall with nothing left to fold stops the run (CM-45). The
run's record keeps the geometry, each fold with its enrichment calls' usage, and each
rebuild's parts, and each fold is a run event of kind ``fold`` (CM-46). A run whose
system message and tools pass a fifth of the ceiling, or whose scale λ would fall below
0.4, does not start (CM-27).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from itertools import accumulate
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, ExecutionContext, KernelInfo
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.hooks import RunView, TurnHook
from sandbox_science.orchestrator.notices import FOLD_ENRICH, FOLD_OVERVIEW
from sandbox_science.orchestrator.plan_tools import NAMES as PLAN_NAMES
from sandbox_science.orchestrator.tokens import chars_per_token, estimate, view_tokens
from sandbox_science.orchestrator.worker_client import dump

from .archive import Archive, Archived, Entry, escape
from .enrich import Enriched, enrich
from .geometry import Geometry, geometry
from .log import PATIENCE, patiently
from .markers import Judge
from .overview import FIRST_SHARE, MIN_PARTS, asked, merge, verify
from .reads import Read, Register
from .record import FORMAT, FoldRecord, stubbed
from .skeleton import skeleton
from .view import Earlier, Unit, View, session_view, settle, spanned

if TYPE_CHECKING:
    from sandbox_science.orchestrator.loop import Transcript
    from sandbox_science.orchestrator.model import ChatModel, ToolSpec
    from sandbox_science.orchestrator.projects import ModelUsage

Json = dict[str, Any]
_log = logging.getLogger(__name__)

WEAK: Final = 0.1
"""A fold that frees less than this share of the view is weak (CM-44)."""
BREAKER: Final = 3
"""Weak folds after which a run folds no more."""
WAIT: Final = 600.0
"""The longest wait, in seconds, for a running fold past the hard wall (CM-24)."""
PLAN_CHANGES: Final = PLAN_NAMES - {"plan_read"}


@dataclass
class _Running:
    """A fold whose enrichment runs: its span, its record as code built it, and once it
    is ready, the record as archived (``kept``), which the run's end does not undo."""

    record: FoldRecord
    rows: tuple[Archived, ...]
    task: asyncio.Task[None] | None = None
    done: Enriched | None = None
    saving: asyncio.Task[None] | None = None
    kept: tuple[Archived, FoldRecord] | None = None
    asked: int = 0
    """The tokens each enrichment call is sent, estimated: what a cancelled call counts."""
    parts: tuple[tuple[str, FoldRecord], ...] = ()
    """A merge's parts, oldest first; its ``record`` is the overview as code built it."""


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
        model: ChatModel | None = None,
        exists: Callable[[str], bool] | None = None,
        judge: Judge | None = None,
        wait: float = WAIT,
        clock: Clock = SYSTEM_CLOCK,
        patience: float = PATIENCE,
    ) -> None:
        """``view`` is the run's view as it starts, built under ``start``, a geometry
        before the system message is known; ``calls`` are the session's earlier calls on
        the run's alias, which calibrate its characters per token; ``runs`` and
        ``earlier`` are ``session_view``'s; ``digest`` renders the research state within
        a budget in characters, for the state block of each rebuild (CM-26); ``model``
        writes the narratives, none for skeleton-only folds; ``exists`` says whether a
        ledger handle exists; ``judge`` decides which markers are retired. ``events``
        writes a ``fold`` event, ``refit`` tells the skills what a rebuilt view holds
        (CM-29b), and ``specs`` gives the tools the run sends; the harness sets them once
        it has made them."""
        self.archive, self.context, self.run = archive, context, run
        self.view, self.start, self.ceiling, self.chunk = view, start, ceiling, chunk
        self.calls, self.runs, self.earlier, self.kernels = (
            tuple(calls),
            tuple(runs),
            earlier,
            kernels,
        )
        self.model, self.exists, self.wait, self.digest = model, exists, wait, digest
        self.judge = judge
        self.clock, self.patience = clock, patience
        self.events: Callable[[Json], Awaitable[None]] | None = None
        self.refit: Callable[[View], None] | None = None
        self.specs: Callable[[], Sequence[ToolSpec]] = tuple
        self.geometry: Geometry | None = None
        self.running: _Running | None = None
        self.folds: list[Json] = []
        self.rebuilds: list[Json] = []
        self.spent: list[Json] = []
        """Each enrichment call's usage, as the run's costs count it."""
        self.weak = 0
        self.overflows = 0
        self.over = False
        """Whether the region passes its budget with nothing left to merge."""
        self.overs = 0
        self.stopped: str | None = None
        """Why the run stopped for its context: ``start``, ``breaker``, ``overflow`` or
        ``irreducible``."""
        self.detail: str | None = None
        self.capped: int | None = None
        """The ceiling a context error capped at the largest input the model took."""
        self.learned: int | None = None
        """The largest input an earlier run's context error found the model takes: the
        cap a context error falls back to, the first call's among them."""
        self.on_cap: Callable[[int], None] | None = None
        """Told each cap, so later runs learn it."""
        self.propose: Callable[[str, Sequence[Archived], Sequence[object]], Json] | None = None
        """Checks and keeps the proposals of the fold archived as its first argument, over
        the span of the second (CM-15); what they came to. The harness sets it."""

    @property
    def tripped(self) -> bool:
        return self.weak >= BREAKER

    def per_token(self, run: RunView) -> float:
        return chars_per_token([*self.calls, *run.usage])

    def tokens(self, run: RunView) -> int:
        """The view's tokens before the next call (CM-28)."""
        return view_tokens(run.size, run.usage[run.since :], self.per_token(run))

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self._held()

    @asynccontextmanager
    async def _held(self) -> AsyncGenerator[None]:
        """A fold still running when the run ends is cancelled with it (CM-24a)."""
        try:
            yield
        finally:
            if (running := self.running) is not None and running.task is not None:
                running.task.cancel()
                with contextlib.suppress(BaseException):
                    await running.task
                if running.saving is not None:  # a ready fold is kept (CM-24a)
                    with contextlib.suppress(BaseException):
                        await running.saving

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
        if self.running is not None and self.running.kept is not None:
            await self._finish(transcript, self.tokens(transcript))
        wall, size = self.geometry.wall, self.tokens(transcript)
        if size > wall and self.running is not None:
            await self._wait(transcript, size)
            size = self.tokens(transcript)
        while size > wall and await self._fold(transcript, size):
            size = self.tokens(transcript)
        while size > wall and await self._merge(transcript, size):  # no time for a call
            size = self.tokens(transcript)
        running = self.running
        if size > self.geometry.trigger and running is not None and running.parts:
            # A fold is due: the merge's call gives way, its overview built by code, so
            # that the fold's narrative is written (they touch different records).
            if running.task is not None:
                running.task.cancel()
            await self._finish(transcript, size)
            size = self.tokens(transcript)
        if size > self.geometry.trigger and self.running is None and not self.tripped:
            if self.model is None:
                await self._fold(transcript, size)
            else:
                await self._begin(transcript)
            size = self.tokens(transcript)
        if self.running is None and size <= wall:
            due, over = self._due(transcript)
            await self._over(over)
            if due and self.model is None:
                await self._merges(transcript)
            elif due:
                await self._begin_merge(transcript, due)
            size = self.tokens(transcript)
        if size <= wall:
            return True
        if self.tripped:
            return self._stop("breaker", f"three weak folds, and the view is {size:,} tokens")
        return self._stop("irreducible", f"nothing left to fold, and the view is {size:,} tokens")

    async def overflow(self, transcript: Transcript) -> bool:
        self.overflows += 1
        self._cap(transcript)
        if self.overflows == 1:
            size = self.tokens(transcript)
            if self.running is not None:  # its span, skeleton-only now unless it is ready
                merging = bool(self.running.parts)
                if self.running.task is not None:
                    self.running.task.cancel()
                await self._finish(transcript, size)
                if not merging:
                    await self._merges(transcript)
                    return True
                size = self.tokens(transcript)
            if await self._fold(transcript, size):
                await self._merges(transcript)  # merges built by code, if they are due
                return True
        return self._stop("overflow", "the gateway refused the call for its length")

    def _cap(self, transcript: Transcript) -> None:
        """After a context error: the ceiling no larger than the largest input the model
        took in this run, which it is known to take (CM-43)."""
        taken = [usage.input_tokens for usage in transcript.usage if usage.input_tokens]
        if self.learned is not None:  # what an earlier run learned the model takes
            taken.append(self.learned)
        if self.geometry is None or not taken or max(taken) >= self.geometry.ceiling:
            return
        capped = geometry(max(taken), self.geometry.prompt, self.chunk)
        if capped.refused() is not None:  # a report too small to be the model's limit
            return
        self.geometry, self.capped = capped, max(taken)
        if self.on_cap is not None:
            self.on_cap(max(taken))
        _log.warning(
            "run %s caps its ceiling at %d tokens after a context error", self.run, max(taken)
        )

    async def model_returned(self, run: RunView) -> None:
        self.overflows = 0

    def _stop(self, reason: str, detail: str) -> bool:
        self.stopped, self.detail = reason, detail
        _log.warning("run %s stops for its context (%s): %s", self.run, reason, detail)
        return False

    async def _span(self, transcript: Transcript) -> tuple[FoldRecord, list[Archived]] | None:
        """The next span and its record as code builds it, or ``None``: nothing to fold,
        or a record that fails its checks, which changes nothing. A record too large for
        its share is built again of half its span, down to one unit (CM-20)."""
        if self.tripped or self.geometry is None:
            return None
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
                reason = {"state": "rejected", "span": _positions_of(rows), "reason": "too large"}
                await self._event(reason)
            return None
        return record, rows

    def _reads(self, messages: Sequence[str]) -> list[tuple[str, Read]]:
        return Register(self.archive.control).of_messages(self.context.project_id, messages)

    async def _fold(self, transcript: Transcript, before: int) -> bool:
        """One skeleton-only fold of the raw tail's oldest span, applied; whether one was."""
        found = await self._span(transcript)
        if found is None:
            return False
        record, rows = found
        kept = await self._keep(record, self.per_token(transcript))
        await self._applied(transcript, before, kept, rows)
        return True

    async def _begin(self, transcript: Transcript) -> None:
        """A fold whose narrative the enrichment call writes while the run goes on."""
        found = await self._span(transcript)
        if found is None or self.model is None or self.geometry is None:
            return
        record, rows = found
        per_token = self.per_token(transcript)
        running = _Running(record, tuple(rows))
        running.asked = self.tokens(transcript)
        running.task = asyncio.create_task(
            self._enrich(running, self.model, list(transcript.sent), self.specs(), per_token)
        )
        self.running = running
        await self._event({"state": "started", "span": _positions_of(rows)})

    async def _enrich(
        self,
        running: _Running,
        model: ChatModel,
        sent: list[Json],
        specs: Sequence[ToolSpec],
        per_token: float,
    ) -> None:
        """The enrichment, then the record archived: once begun, the archiving is not
        cancelled, so a fold is kept or not, never half."""
        assert self.geometry is not None  # noqa: S101 - set at the first call
        share = int(self.geometry.record * per_token)

        def spent(usage: ModelUsage) -> None:  # counted as each call returns
            self.spent.append(usage.model_dump(mode="json"))

        parts = running.parts
        framing, shown = (
            (FOLD_OVERVIEW, asked(running.record, parts)) if parts else (FOLD_ENRICH, None)
        )
        try:
            running.done = await enrich(
                model,
                sent,
                specs,
                running.record,
                running.rows,
                share=share,
                exists=self.exists,
                spent=spent,
                framing=framing,
                shown=shown,
            )
            if parts and (why := verify(running.done.record, [r for _, r in parts])) is not None:
                running.done = Enriched(running.record, running.done.calls, why)
        except Exception as error:  # the fold stands, skeleton-only
            _log.warning("the enrichment of run %s failed: %r", self.run, error)
            failed = f"it failed ({type(error).__name__})"
            running.done = Enriched(running.record, (), failed)

        async def save() -> None:
            assert running.done is not None  # noqa: S101 - set above
            running.kept = await self._keep(running.done.record, per_token)
            done = running.done
            if self.propose is not None and done.proposals and done.failed is None:
                # Checked against the span once the fold is kept, and kept apart (CM-15).
                fold = running.kept[0].message_id
                try:
                    proposed = await threaded(self.propose, fold, running.rows, done.proposals)
                except Exception as error:  # the fold stands without them
                    _log.warning("the proposals of fold %s were not kept: %r", fold, error)
                    proposed = {"kept": 0, "failed": type(error).__name__}
                running.done = replace(done, proposed=proposed)

        running.saving = asyncio.create_task(save())
        await asyncio.shield(running.saving)

    async def _wait(self, transcript: Transcript, before: int) -> None:
        """Past the wall: the running fold, at most ``wait`` seconds, then applied, or its
        span skeleton-only (CM-24); the wait is a ``fold`` event."""
        running = self.running
        assert running is not None  # noqa: S101 - the caller checked
        assert running.task is not None  # noqa: S101 - every running fold has its task
        if running.parts:  # a merge: no time for its call at the wall, so code's (CM-21c)
            running.task.cancel()
            await self._finish(transcript, before)
            return
        began = self.clock.monotonic()
        await self._event({"state": "waiting"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(running.task), self.wait)
        running.task.cancel()
        waited = round(self.clock.monotonic() - began, 3)
        await self._event({"state": "waited", "seconds": waited, "ready": running.kept is not None})
        await self._finish(transcript, before)

    async def _finish(self, transcript: Transcript, before: int) -> None:
        """The running fold applied: as archived if it is ready, or its span
        skeleton-only."""
        running, self.running = self.running, None
        assert running is not None  # noqa: S101 - the callers checked
        for task in (running.task, running.saving):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task
        if running.kept is not None:
            await self._applied(transcript, before, running.kept, running.rows, running.done)
            return
        if running.done is None:
            self.spent.append(_cancelled(running))
            done = Enriched(running.record, (), "it was not ready")
        else:
            done = replace(
                running.done, record=running.record, failed=running.done.failed or "it was not kept"
            )
        kept = await self._keep(running.record, self.per_token(transcript))
        await self._applied(transcript, before, kept, running.rows, done)

    async def _keep(self, record: FoldRecord, per_token: float) -> tuple[Archived, FoldRecord]:
        """``record`` archived with the stubs it makes of the raw tail after it now; an
        overview makes none (CM-24: stubs are made at folds)."""
        if not record.parts:
            view = await threaded(self._view, per_token)
            last = record.last[0]
            after = [p.row for u in view.units for p in u if p.row and p.row.position > last]
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
        return row, record

    async def _applied(
        self,
        transcript: Transcript,
        before: int,
        kept: tuple[Archived, FoldRecord],
        rows: Sequence[Archived],
        done: Enriched | None = None,
    ) -> None:
        """The view rebuilt with the archived fold ``kept``; its event."""
        row, record = kept
        await self._rebuild(transcript)
        freed = before - self.tokens(transcript)
        # Folds before the run's first call catch up on a session folded little or not at
        # all, such as one from before folds: the breaker counts the run's own (CM-44).
        # A merge frees only the region's room, and is no fold the breaker counts.
        if freed < WEAK * before and transcript.usage and not record.parts:
            self.weak += 1
        calls = [usage.model_dump(mode="json") for usage in (done.calls if done else ())]
        made: Json = {
            "fold": row.message_id,
            "level": record.level,
            "span": _positions_of(rows) if rows else [record.first[0], record.last[0]],
            "state": record.state,
            "freed": freed,
            "stubs": len(record.stubs),
        }
        if record.parts:
            made["parts"] = list(record.parts)
        if done is not None and done.failed:
            made["reason"] = (
                f"{'built by code' if record.parts else 'skeleton-only'}: {done.failed}"
            )
        if done is not None and done.proposed is not None:
            made["proposals"] = done.proposed
        self.folds.append(made)
        await self._event({**made, "calls": calls} if calls else made)

    def _view(self, per_token: float) -> View:
        assert self.geometry is not None  # noqa: S101 - set at the first call
        return session_view(
            self.archive,
            self.context,
            geometry=self.geometry,
            per_token=per_token,
            runs=self.runs,
            earlier=self.earlier,
            judge=self.judge,
        )

    def _due(self, transcript: Transcript) -> tuple[list[tuple[str, FoldRecord]], bool]:
        """The records to merge now, oldest first (CM-21), and whether the region would
        pass its budget even so. The first-level ones, when three or more total 0.4 of a
        chunk; else, while the region would pass its budget, the two oldest if merging
        them frees room, or all of them if only that does. When even one overview of
        every record would pass the budget, as markers alone can, none: merging cannot
        bring it under, and spilling markers is step (f2b)'s."""
        if self.geometry is None:
            return [], False
        per_token, records = self.per_token(transcript), list(self.view.records)
        first = [(fold, record) for fold, record in records if record.level == 1]
        size = sum(estimate(len(record.text(fold)), per_token) for fold, record in first)
        if len(first) >= MIN_PARTS and size >= FIRST_SHARE * self.geometry.chunk:
            return first, False
        budget = int(self.geometry.region * per_token)
        if self.view.wanted <= budget:
            return [], False
        if len(records) < 2 or self._merged(records, per_token) > budget:
            return [], True
        two = records[:2]
        if self._merged(two, per_token) < sum(self._shown(fold, found) for fold, found in two):
            return two, False
        return records, False

    def _shown(self, fold: str, record: FoldRecord) -> int:
        """A record's characters in the region, as the view renders it."""
        return len(escape(record.text(fold, retired=self.view.retired.get(fold)))) + 2

    def _merged(self, parts: Sequence[tuple[str, FoldRecord]], per_token: float) -> int:
        """The characters an overview of ``parts`` built by code would take in the
        region, its parts' retired markers retired in it too."""
        assert self.geometry is not None  # noqa: S101 - the caller checked
        overview = merge(parts, int(self.geometry.record * per_token))
        gone: dict[int, str] = {}
        offset = 0
        for fold, record in parts:
            gone |= {offset + i: why for i, why in self.view.retired.get(fold, {}).items()}
            offset += len(record.markers)
        for index, marker in enumerate(overview.markers):
            if marker.get("kind") == "failures":
                gone.setdefault(index, "its fold was merged into an overview")
        return len(escape(overview.text("msg-" + "0" * 16, retired=gone))) + 2

    async def _over(self, over: bool) -> None:
        """A ``fold`` event when the region passes its budget though nothing is left to
        merge, and once more when it is back under (CM-46); spills are step (f2b)'s."""
        if over == self.over:
            return
        self.over = over
        if over:
            self.overs += 1
        assert self.geometry is not None  # noqa: S101 - set at the first call
        await self._event(
            {"state": "region_over_budget" if over else "region_within_budget",
             "chars": self.view.wanted, "budget_tokens": self.geometry.region}
        )  # fmt: skip

    async def _merge(self, transcript: Transcript, before: int) -> bool:
        """One merge that is due, built by code and applied (CM-21c); whether one was."""
        due, _ = self._due(transcript)
        if not due or self.geometry is None:
            return False
        per_token = self.per_token(transcript)
        record = merge(due, int(self.geometry.record * per_token))
        if (why := verify(record, [found for _, found in due])) is not None:
            await self._rejected(record, why)
            return False
        kept = await self._keep(record, per_token)
        await self._applied(transcript, before, kept, ())
        return True

    async def _merges(self, transcript: Transcript) -> None:
        """Every merge that is due, built by code: each leaves one record fewer."""
        while await self._merge(transcript, self.tokens(transcript)):
            pass

    async def _begin_merge(self, transcript: Transcript, due: list[tuple[str, FoldRecord]]) -> None:
        """A merge whose narrative the run's model updates while the run goes on."""
        if self.model is None or self.geometry is None:
            return
        per_token = self.per_token(transcript)
        record = merge(due, int(self.geometry.record * per_token))
        if (why := verify(record, [found for _, found in due])) is not None:
            await self._rejected(record, why)
            return
        first, last = record.first[0], record.last[0]
        rows = await threaded(spanned, self.archive, self.context, first, last)
        running = _Running(record, tuple(rows), parts=tuple(due))
        running.asked = self.tokens(transcript)
        running.task = asyncio.create_task(
            self._enrich(running, self.model, list(transcript.sent), self.specs(), per_token)
        )
        self.running = running
        await self._event(
            {
                "state": "started",
                "level": record.level,
                "span": [first, last],
                "parts": list(record.parts),
            }
        )

    async def _rejected(self, record: FoldRecord, why: str) -> None:
        _log.warning("run %s rejects a merge: %s", self.run, why)
        span = [record.first[0], record.last[0]]
        await self._event(
            {"state": "rejected", "span": span, "parts": list(record.parts), "reason": why}
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
        running = self.running
        if running is not None and running.task is not None and running.kept is None:
            running.task.cancel()  # the run ended: so does its fold (CM-24a)
            if running.done is None:  # a call was in flight; those returned are counted
                self.spent.append(_cancelled(running))
        if self.geometry is None:
            return None
        found: Json = {
            "geometry": self.geometry.summary(),
            "folds": self.folds,
            "rebuilds": self.rebuilds,
            "calls": self.spent,
            "weak_folds": self.weak,
        }
        if self.capped is not None:
            found["capped"] = self.capped
        if self.overs:
            found["region_over_budget"] = self.overs
        if running is not None and running.kept is not None:
            found["kept"] = running.kept[0].message_id  # ready: the next run applies it
        if self.stopped is not None:
            found |= {"stopped": self.stopped, "detail": self.detail}
        return found


def _cancelled(running: _Running) -> Json:
    """The usage of an enrichment call cancelled before it returned, which the gateway
    never reported: its input estimated, as the view it was sent, and so marked (RS-6a);
    its cost unknown."""
    return {"cancelled": True, "estimated": True, "input_tokens": running.asked, "cost": None}


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


def _positions_of(rows: Sequence[Archived]) -> list[int]:
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
