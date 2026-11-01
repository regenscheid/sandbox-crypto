# SPDX-License-Identifier: MIT
"""The fold region rendered within its bound (docs/specs/context-management.md,
CM-21b).

At each rebuild code renders each record from its stored form beside the new state
block. A current marker takes its short form when the state block's digest holds the
same information exactly, field by field as the digest recorded what its line shows
(``Rendered.fields``): a decision's status, its reason and its reopen condition, each
whole; a caveat line among its entry's caveats shown whole; an evidence type with its
scope shown whole. Otherwise it is shown in full. A retired marker is an index line.

When the region would pass its budget, code reduces it in CM-21b's order, stopping as
soon as it fits (merges, step 1, are the folds'): it collapses each record's index of
retired markers into a count by kind, oldest records first; spills retired markers and
failure aggregates into a spill record, oldest records first; spills the current caveats
and incomplete reads the digest does not show, those on entries the current plan step or
an open question names last, and before them reads of sources an active reference
cites, the oldest first; then lets current decision lines grow
the region up to its cap (``Geometry.region_cap``); past it, shortens the least relevant
of this session's decisions to one line each (those named last, the oldest first, the
user's standing constraints never); and, if even those do not fit, says so, for the run
to stop (CM-45, ``research_state``). One pointer line, at the region's end, names what
was spilled and the spill record that ``history_read`` returns whole.

A consolidation run, after a ``research_state`` stop (CM-45, the coordinator's decision of
2026-10-16), shows every current decision in its one-line form, past the cap if need be,
and never stops for them.

Not built: reads in their short form (the digest names references, not the sources the
register keys).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from .archive import escape
from .record import FoldRecord, flat, marker_line, retired_line

if TYPE_CHECKING:
    from sandbox_science.orchestrator.digest import Rendered

Json = dict[str, Any]

SPILL: Final = "{spill}"
"""Where the pointer line names its spill record, once it is archived."""
INDEX: Final = "Retired markers, kept as an index; history_read returns each:"
BRIEF: Final = 100
"""The characters of its reason a shortened decision line keeps."""
NAMED: Final = 10
"""The most handles the pointer line names; it counts the others."""


@dataclass(frozen=True)
class Bounded:
    """The region as rendered: each record's text, oldest first; the spill record's text
    and its pointer line, if anything was spilled; the characters before and after the
    reductions; and what each step did."""

    texts: tuple[tuple[str, str], ...]
    spill: str | None = None
    pointer: str | None = None
    wanted: int = 0
    size: int = 0
    steps: tuple[str, ...] = ()
    spilled: tuple[str, ...] = ()
    """The handles of the current caveats and reads spilled (step 4)."""
    shortened: tuple[str, ...] = ()
    stop: str | None = None
    """Why the run stops: the decisions that do not fit even shortened (CM-45)."""
    rendering: Mapping[str, Json] = field(default_factory=dict[str, Json])
    """How each record was rendered: its markers short, retired, collapsed, spilled or
    shortened, by index (CM-46)."""


def bound(
    records: Sequence[tuple[str, FoldRecord]],
    retired: Mapping[str, Mapping[int, str]],
    digest: Rendered | None,
    budget: int,
    cap: int,
    *,
    consolidating: bool = False,
) -> Bounded:
    """``records``, oldest first, rendered within ``budget`` characters, current decision
    lines up to ``cap`` (CM-21b); ``consolidating``, every current decision in its one
    line, whatever the cap (CM-45)."""
    plan = _Plan(records, retired, digest)
    wanted = total = plan.total()
    steps: dict[str, None] = {}
    if consolidating:
        for fold, index in plan.ranked(("decision",), everyone=True):
            plan.brief(fold, index)
        steps["consolidating"] = None
        cap = max(cap, plan.total())
        total = plan.total()
    if total > budget:  # 2: the indexes of retired markers collapse into counts
        for fold, _ in records:
            if plan.collapse(fold):
                steps["collapsed"] = None
                if (total := plan.total()) <= budget:
                    break
    if total > budget:  # 3: retired markers and failure aggregates spill
        for fold, record in records:
            chosen = [
                index
                for index, mode in enumerate(plan.modes[fold])
                if mode in ("retired", "collapsed")
                or (mode == "full" and record.markers[index].get("kind") == "failures")
            ]
            if chosen:
                plan.spill(fold, chosen)
                steps["spilled"] = None
                if (total := plan.total()) <= budget:
                    break
    if total > budget:  # 4: current caveats and reads the digest does not show
        for fold, index in plan.ranked(("caveat", "read")):
            plan.spill(fold, [index])
            steps["spilled"] = None
            if (total := plan.total()) <= budget:
                break
    # 5: what is left over is current decision lines', when the rest fits the budget:
    # they may grow the region up to its cap, and past it the least relevant shorten.
    decided = total > budget and total - plan.decisions() <= budget
    if decided and total > cap:
        for fold, index in plan.ranked(("decision",)):
            plan.brief(fold, index)
            steps["shortened"] = None
            if (total := plan.total()) <= cap:
                break
    if decided:
        steps["grown"] = None
    stop = None
    if decided and total > cap:  # even shortened, the decisions do not fit
        stop = (
            f"the session's current decisions do not fit its fold region even shortened: "
            f"{plan.decisions():,} characters of decision lines, past the region's "
            f"{cap:,} with what else it holds; the least relevant: {', '.join(plan.least(5))}"
        )
    return Bounded(
        texts=tuple((fold, plan.text(fold)) for fold, _ in records),
        spill=plan.spill_text(),
        pointer=plan.pointer(),
        wanted=wanted,
        size=total,
        steps=tuple(steps),
        spilled=tuple(dict.fromkeys(plan.spilled_handles)),
        shortened=tuple(dict.fromkeys(plan.shortened)),
        stop=stop,
        rendering=plan.rendering(),
    )


class _Plan:
    """Each marker's mode: ``full``, ``short``, ``brief`` (current); ``retired``,
    ``collapsed``; ``spilled``."""

    def __init__(
        self,
        records: Sequence[tuple[str, FoldRecord]],
        retired: Mapping[str, Mapping[int, str]],
        digest: Rendered | None,
    ) -> None:
        self.records = dict(records)
        self.order = [fold for fold, _ in records]
        self.retired = retired
        self.digest = digest
        self.modes: dict[str, list[str]] = {}
        self.shorts: dict[tuple[str, int], str] = {}
        for fold, record in records:
            modes: list[str] = []
            for index, marker in enumerate(record.markers):
                if index in retired.get(fold, {}):
                    modes.append("retired")
                elif (short := _short(marker, digest)) is not None:
                    modes.append("short")
                    self.shorts[fold, index] = short
                else:
                    modes.append("full")
            self.modes[fold] = modes
        self.base = {fold: len(escape(record.text(fold, block=()))) + 2 for fold, record in records}
        self.sizes = {fold: self._size(fold) for fold in self.order}
        self.spilled_handles: list[str] = []
        self.shortened: list[str] = []

    def total(self) -> int:
        pointer = self.pointer()
        return sum(self.sizes.values()) + (len(pointer) + 2 if pointer else 0)

    def decisions(self) -> int:
        """The characters of the current decision lines, as they are rendered now."""
        return sum(
            len(line) + 3
            for fold in self.order
            for index, line in self._lines(fold)
            if self.records[fold].markers[index].get("kind") == "decision"
            and self.modes[fold][index] in ("full", "short", "brief")
        )

    def collapse(self, fold: str) -> bool:
        modes = self.modes[fold]
        if "retired" not in modes:
            return False
        self.modes[fold] = ["collapsed" if mode == "retired" else mode for mode in modes]
        self.sizes[fold] = self._size(fold)
        return True

    def spill(self, fold: str, chosen: Sequence[int]) -> None:
        modes, markers = self.modes[fold], self.records[fold].markers
        for index in chosen:
            if modes[index] == "full" and markers[index].get("kind") != "failures":
                self.spilled_handles.append(_subject(markers[index]))
            modes[index] = "spilled"
        self.sizes[fold] = self._size(fold)

    def brief(self, fold: str, index: int) -> None:
        self.modes[fold][index] = "brief"
        self.shortened.append(_subject(self.records[fold].markers[index]))
        self.sizes[fold] = self._size(fold)

    def ranked(self, kinds: Sequence[str], *, everyone: bool = False) -> list[tuple[str, int]]:
        """The current markers of ``kinds`` shown in full, the least relevant first:
        those on entries the plan's current step or an open question names last, reads
        of sources an active reference cites before them, the oldest first; never a
        user's standing constraint. ``everyone``: every current one, short ones too."""
        digest = self.digest
        named = digest.named if digest is not None else frozenset[str]()
        pinned = digest.pinned if digest is not None else frozenset[str]()
        cited = digest.cited if digest is not None else frozenset[tuple[str, str]]()
        found: list[tuple[bool, bool, int, int, str]] = []
        for position, fold in enumerate(self.order):
            for index, marker in enumerate(self.records[fold].markers):
                mode = self.modes[fold][index]
                if marker.get("kind") not in kinds:
                    continue
                if mode != "full" and not (everyone and mode == "short"):
                    continue
                subject = _subject(marker)
                if subject in pinned and not everyone:
                    continue
                source = marker.get("source")
                pair = cast(list[object], source) if isinstance(source, list) else []
                read = len(pair) == 2 and (str(pair[0]), str(pair[1])) in cited
                found.append((subject in named, read, position, index, fold))
        return [(fold, index) for _, _, _, index, fold in sorted(found)]

    def least(self, count: int) -> list[str]:
        ranked = [_subject(self.records[f].markers[i]) for f, i in self.ranked(("decision",))]
        ranked += [h for h in self.shortened if h not in ranked]
        return list(dict.fromkeys(ranked))[:count]

    def _lines(self, fold: str) -> list[tuple[int, str]]:
        record = self.records[fold]
        found: list[tuple[int, str]] = []
        for index, marker in enumerate(record.markers):
            mode = self.modes[fold][index]
            if mode == "full":
                found.append((index, marker_line(marker, fold)))
            elif mode == "short":
                found.append((index, self.shorts[fold, index]))
            elif mode == "brief":
                found.append((index, _brief(marker)))
        return found

    def block(self, fold: str) -> list[str]:
        """The record's marker lines as the region shows them."""
        record, modes = self.records[fold], self.modes[fold]
        lines: list[str] = []
        current = self._lines(fold)
        if current:
            lines += ["Markers:", *(f"- {line}" for _, line in current)]
        retired = [i for i, mode in enumerate(modes) if mode == "retired"]
        if retired:
            why = self.retired.get(fold, {})
            lines += [INDEX, *(f"- {retired_line(record.markers[i], why[i])}" for i in retired)]
        collapsed: dict[str, int] = {}
        for index, mode in enumerate(modes):
            if mode == "collapsed":
                kind = flat(str(record.markers[index].get("kind")))
                collapsed[kind] = collapsed.get(kind, 0) + 1
        if collapsed:
            counted = ", ".join(f"{count} {kind}" for kind, count in sorted(collapsed.items()))
            lines.append(f"Retired markers: {counted}; history_read {fold} returns them.")
        return lines

    def _size(self, fold: str) -> int:
        return self.base[fold] + sum(len(escape(line)) + 1 for line in self.block(fold))

    def text(self, fold: str) -> str:
        return escape(self.records[fold].text(fold, block=self.block(fold)))

    def _spilled(self) -> list[tuple[str, int]]:
        return [
            (fold, index)
            for fold in self.order
            for index, mode in enumerate(self.modes[fold])
            if mode == "spilled"
        ]

    def spill_text(self) -> str | None:
        spilled = self._spilled()
        if not spilled:
            return None
        lines = ["Spilled marker lines of the fold region, as code built them, by record:"]
        for fold in self.order:
            here = [index for f, index in spilled if f == fold]
            if not here:
                continue
            lines.append(f"--- fold {fold} ---")
            record, why = self.records[fold], self.retired.get(fold, {})
            for index in here:
                marker = record.markers[index]
                line = (
                    retired_line(marker, why[index]) if index in why else marker_line(marker, fold)
                )
                lines.append(f"- {line}")
        return "\n".join(lines)

    def pointer(self) -> str | None:
        spilled = self._spilled()
        if not spilled:
            return None
        retired = sum(1 for f, i in spilled if i in self.retired.get(f, {}))
        failures = sum(
            1
            for f, i in spilled
            if i not in self.retired.get(f, {})
            and self.records[f].markers[i].get("kind") == "failures"
        )
        folds = [f for f in self.order if any(s == f for s, _ in spilled)]
        named = folds[0] if len(folds) == 1 else f"{folds[0]} to {folds[-1]}"
        parts: list[str] = []
        if retired or failures:
            parts.append(f"{retired} retired markers and {failures} failure aggregates")
        handles = list(dict.fromkeys(self.spilled_handles))
        if handles:
            more = f" and {len(handles) - NAMED} more" if len(handles) > NAMED else ""
            parts.append(f"the current caveats and reads on {', '.join(handles[:NAMED])}{more}")
        return f"spilled: {' and '.join(parts)} of {named} → history_read {SPILL}"

    def rendering(self) -> dict[str, Json]:
        found: dict[str, Json] = {}
        for fold in self.order:
            modes = self.modes[fold]
            shown = {
                mode: [i for i, m in enumerate(modes) if m == mode]
                for mode in ("short", "retired", "collapsed", "spilled", "brief")
            }
            kept = {mode: indices for mode, indices in shown.items() if indices}
            if kept:
                found[fold] = kept
        return found


def _subject(marker: Mapping[str, Any]) -> str:
    return flat(str(marker.get("entry") or marker.get("subject") or ""))


def _reason(marker: Mapping[str, Any]) -> str:
    """A decision line's reason: as stored beside it, or its text after its status."""
    if isinstance(marker.get("reason"), str):
        return flat(str(marker["reason"]))
    text = flat(str(marker.get("text") or ""))
    return text.split(": ", 1)[1] if ": " in text else text


def _brief(marker: Mapping[str, Any]) -> str:
    """A decision as one short line: its handle, status, the head of its reason, and
    where to read it whole (CM-21b, step 5)."""
    handle, status = _subject(marker), flat(str(marker.get("status") or ""))
    reason = _reason(marker)
    head = reason if len(reason) <= BRIEF else reason[: BRIEF - 1] + "…"
    shown = f" → {status}" if status else ""
    return f"decision: {handle}{shown}: {head} (shortened; ledger_read {handle})"


def _short(marker: Mapping[str, Any], digest: Rendered | None) -> str | None:
    """The marker's short form, when the digest's line for its entry holds the same
    information exactly, field by field as the digest recorded it; ``None`` otherwise."""
    if digest is None:
        return None
    handle = flat(str(marker.get("entry") or ""))
    shown = digest.fields.get(handle) if handle else None
    if shown is None:
        return None
    kind = marker.get("kind")
    if kind == "decision":
        status, reason = marker.get("status"), marker.get("reason")
        if not isinstance(status, str) or not isinstance(reason, str):
            return None
        reopen = marker.get("reopen")
        held = (
            shown.get("status") == status
            and shown.get("reason") == flat(reason)
            and shown.get("reopen") == (flat(reopen) if isinstance(reopen, str) else "")
        )
        return f"decision: {handle} → {status}, in the state block" if held else None
    if kind != "caveat" or "work" in marker or "check" in marker:
        return None
    if "caveat" in marker:
        caveats = cast(frozenset[str], shown.get("caveats") or frozenset[str]())
        held = flat(str(marker["caveat"])) in caveats
        return f"caveat: {handle}, its caveat in the state block" if held else None
    evidence = marker.get("evidence")
    pair = cast(list[object], evidence) if isinstance(evidence, list) else []
    if len(pair) == 2:
        held_pairs = cast(frozenset[tuple[str, str]], shown.get("evidence") or frozenset())
        if (flat(str(pair[0])), flat(str(pair[1] or ""))) in held_pairs:
            return f"caveat: {handle}, its evidence in the state block"
    return None
