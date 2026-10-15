# SPDX-License-Identifier: MIT
"""Flags for reconsideration (docs/specs/ledger.md, LG-28, LG-29).

A change to an entry flags the entries that rest on it, as their current revisions
say:

- an assumption revised to a new statement flags the entries whose ``assumes`` link pins
  an earlier revision (``assumption_revised``); one withdrawn flags every entry that
  assumes it (``assumption_withdrawn``);
- an entry that becomes ``disputed``, ``refuted``, ``superseded`` or ``withdrawn`` flags the
  entries that build on it, assume it or hold evidence pinned to it
  (``dependency_status``);
- an entry whose statement changes flags the entries with evidence pinned to an earlier
  revision of it (``evidence_changed``).

A flag passes on to the entries that rest on a flagged entry through the same links
(``spread``, naming the flag it came from). Each flag names its cause and what it came
from; an entry has at most one open flag per cause and source. A flag changes nothing
else, and stays until the user or the main agent clears it with a reason. Flags from
document blocks and artifacts, and from document blocks bound to entries (LD-34), are not
built yet.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sandbox_science.contracts import new_id

from .models import PREFIXES

Json = dict[str, Any]

SET_ASIDE: Final = frozenset({"disputed", "refuted", "superseded", "withdrawn"})
"""The statuses that flag the entries resting on an entry."""

_CURRENT: Final = "JOIN ledger_entries e ON e.entry_id=x.entry_id AND x.revision=e.head "


@dataclass(frozen=True)
class Changed:
    """An entry a change revised, as the change left it."""

    entry_id: str
    handle: str
    type: str
    revision: int
    statement_changed: bool
    status: str
    status_before: str | None


@dataclass(frozen=True)
class Raised:
    flag_id: str
    entry_id: str
    handle: str
    cause: str
    source: Json
    via: str | None


def dependents(
    db: sqlite3.Connection, entry: str, kinds: tuple[str, ...], evidence: bool
) -> list[tuple[str, int | None]]:
    """The entries other than ``entry`` whose current revision holds a link of ``kinds``
    to it, or evidence pinned to it when ``evidence``, with the revision each pins (an
    entry's own check of itself flags nothing)."""
    found: list[tuple[str, int | None]] = []
    if kinds:
        found += db.execute(
            f"SELECT x.entry_id,x.target_revision FROM ledger_links x {_CURRENT}"  # noqa: S608 - fixed SQL
            "WHERE x.target='entry' AND x.target_id=? AND x.entry_id!=x.target_id "
            f"AND x.kind IN ({','.join('?' * len(kinds))})",
            (entry, *kinds),
        ).fetchall()
    if evidence:
        found += db.execute(
            f"SELECT x.entry_id,x.target_revision FROM ledger_evidence x {_CURRENT}"  # noqa: S608 - fixed SQL
            "WHERE x.target='entry' AND x.target_id=? AND x.entry_id!=x.target_id",
            (entry,),
        ).fetchall()
    return found


def raise_flags(
    db: sqlite3.Connection,
    project: str,
    changed: list[Changed],
    now: str,
    announce: Callable[[Raised], None],
) -> list[Raised]:
    """Flag the entries that rest on what ``changed`` changed, and those that rest on
    them in turn, in the open change ``db``; ``announce`` records each flag's event."""
    raised: list[Raised] = []
    for c in changed:
        source = {
            "entry": c.entry_id,
            "handle": c.handle,
            "revision": c.revision,
            "status": c.status,
        }
        causes: list[tuple[str, list[tuple[str, int | None]]]] = []
        if c.type == "assumption" and c.statement_changed:
            pinned = dependents(db, c.entry_id, ("assumes",), False)
            causes.append(("assumption_revised", [d for d in pinned if (d[1] or 0) < c.revision]))
        entering = c.status in SET_ASIDE and c.status_before not in SET_ASIDE
        if c.type == "assumption" and c.status == "withdrawn" and entering:
            causes.append(("assumption_withdrawn", dependents(db, c.entry_id, ("assumes",), False)))
        elif entering:
            causes.append(
                ("dependency_status", dependents(db, c.entry_id, ("builds_on", "assumes"), True))
            )
        if c.statement_changed:
            pinned = dependents(db, c.entry_id, (), True)
            causes.append(("evidence_changed", [d for d in pinned if (d[1] or 0) < c.revision]))
        for cause, rest in causes:
            for entry in dict.fromkeys(entry for entry, _ in rest):
                flag = _flag(db, project, entry, cause, source, None, now)
                if flag is not None:
                    raised.append(flag)
                    announce(flag)
    # Spread to the entries that rest on a flagged entry, once each.
    seen = {flag.entry_id for flag in raised}
    queue = list(raised)
    while queue:
        flag = queue.pop(0)
        source = {"entry": flag.entry_id, "handle": flag.handle}
        for entry, _ in dependents(db, flag.entry_id, ("builds_on", "assumes"), True):
            if entry in seen:
                continue
            seen.add(entry)
            spread = _flag(db, project, entry, "spread", source, flag.flag_id, now)
            if spread is not None:
                raised.append(spread)
                queue.append(spread)
                announce(spread)
    return raised


def _flag(
    db: sqlite3.Connection,
    project: str,
    entry: str,
    cause: str,
    source: Json,
    via: str | None,
    now: str,
) -> Raised | None:
    """A new open flag on ``entry``, or None when it has one for this cause and source."""
    key = f"{cause}:{source['entry']}" + (f":{source['revision']}" if "revision" in source else "")
    head, kind, number = db.execute(
        "SELECT head,type,number FROM ledger_entries WHERE entry_id=?", (entry,)
    ).fetchone()
    flag = new_id("flag")
    inserted = db.execute(
        "INSERT INTO ledger_flags(flag_id,project,entry_id,revision,cause,source,via,key,raised) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (flag, project, entry, head, cause, json.dumps(source, sort_keys=True), via, key, now),
    ).rowcount
    if not inserted:
        return None
    return Raised(flag, entry, f"{PREFIXES[kind]}-{number}", cause, source, via)
