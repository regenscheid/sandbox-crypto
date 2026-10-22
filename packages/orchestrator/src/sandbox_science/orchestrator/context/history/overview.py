# SPDX-License-Identifier: MIT
"""Overviews: fold records merged into one (docs/specs/context-management.md, CM-21,
CM-21c).

Code builds an overview of its parts, oldest first: their span headers joined, every
stored marker of theirs copied in order, current and retired alike, their search keys
without duplicates within the overview's share (the newest kept, the oldest left out of
it, which the parts' stored records keep), and no outline, which the parts' records keep
too. Built by code alone (``code_merge``), as at the hard wall or after a context
error, its narrative is its parts' narratives, each cut to its head within its share of
the overview's. Otherwise the run's model updates that narrative (``enrich`` with the
``FOLD_OVERVIEW`` framing), and code checks its values against the messages the
overview spans, as a fold's. Either way, an overview whose stored markers are not its
parts' byte for byte is rejected (``verify``), and the records stay as they were.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from .record import FoldRecord

CODE_MERGE: Final = "code_merge"
"""The state of an overview built by code alone (CM-21c)."""
MIN_PARTS: Final = 3
"""First-level folds merged at once, at least."""
FIRST_SHARE: Final = 0.4
"""First-level folds totalling this share of a chunk are merged (CM-21)."""


def merge(parts: Sequence[tuple[str, FoldRecord]], share: int) -> FoldRecord:
    """The overview of ``parts``, each its archive ID and record, oldest first, built by
    code within ``share`` characters for its narrative and keys (CM-21c)."""
    records = [record for _, record in parts]
    keys: dict[str, str] = {}
    for record in reversed(records):  # the newest first: the oldest give way
        for key, at in reversed(record.keys):
            keys.setdefault(key, at)
    kept: list[tuple[str, str]] = []
    used = 0
    for key, at in keys.items():
        if used + len(key) + 24 > share // 10:
            break
        kept.append((key, at))
        used += len(key) + 24
    told = [(fold, record.narrative) for fold, record in parts if record.narrative]
    narrative = None
    if told:
        each = max(200, share // 4 // len(told)) - 40
        narrative = "\n".join(f"From {fold}: {_head(text, each)}" for fold, text in told)
    return FoldRecord(
        level=max(record.level for record in records) + 1,
        first=records[0].first,
        last=records[-1].last,
        runs=tuple(dict.fromkeys(run for record in records for run in record.runs)),
        dates=(records[0].dates[0], records[-1].dates[1]),
        skeleton=(),
        markers=tuple(marker for record in records for marker in record.markers),
        keys=tuple(reversed(kept)),
        narrative=narrative,
        state=CODE_MERGE,
        parts=tuple(fold for fold, _ in parts),
    )


def verify(overview: FoldRecord, parts: Sequence[FoldRecord]) -> str | None:
    """Why ``overview`` cannot stand, or ``None``: its stored markers must be its parts',
    byte for byte and in order (CM-21)."""
    stored = [_bytes(marker) for marker in overview.markers]
    expected = [_bytes(marker) for part in parts for marker in part.markers]
    if stored == expected:
        return None
    if len(stored) != len(expected):
        return f"it holds {len(stored)} marker lines, its parts {len(expected)}"
    index = next(i for i, (a, b) in enumerate(zip(stored, expected, strict=True)) if a != b)
    return f"its marker line {index + 1} differs from its parts'"


def asked(overview: FoldRecord, parts: Sequence[tuple[str, FoldRecord]]) -> str:
    """What the overview's enrichment call is shown: the overview as code built it, but
    with its parts' narratives whole, oldest first, which the model updates."""
    whole = [
        f"--- the narrative of {fold}, level {record.level} ---\n{record.narrative}"
        for fold, record in parts
        if record.narrative
    ]
    shown = overview.text("(this overview)").split("\nNarrative", 1)[0]
    told = "\n".join(whole) if whole else "(no part has a narrative yet)"
    return f"{shown}\nThe parts' narratives, oldest first, as the run's model wrote them:\n{told}"


def _head(text: str, budget: int) -> str:
    """``text`` on one line, cut to its head within ``budget`` characters at a word."""
    flat = " ".join(text.split())
    if len(flat) <= budget:
        return flat
    cut = flat[:budget].rsplit(" ", 1)[0]
    return f"{cut} [...]"


def _bytes(marker: object) -> bytes:
    return json.dumps(marker, ensure_ascii=False).encode()
