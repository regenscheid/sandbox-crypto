# SPDX-License-Identifier: MIT
"""Flags for reconsideration (docs/specs/ledger.md, LG-28, LG-29).

A change to an entry flags the entries that rest on it, as their current revisions
say:

- an assumption revised to a new statement flags the entries whose ``assumes`` link pins
  an earlier revision (``assumption_revised``);
- an entry that enters ``disputed``, ``refuted``, ``superseded`` or ``withdrawn``, or
  moves from one of these to another, flags the entries that build on it, assume it or
  hold evidence pinned to it (``dependency_status``; ``assumption_withdrawn`` for a
  withdrawn assumption);
- an entry whose statement changes flags the entries with evidence pinned to an earlier
  revision of it (``evidence_changed``).

An entry also takes a flag when a change makes it rest on such a thing: a link or
evidence it gains to an entry that is set aside, that pins a revision older than the
target's statement, or to an entry with an open flag (``spread``). A flag passes on to the
entries that rest on a flagged entry through the same links (``spread``, naming the flag
it came from), once each. Each flag names its cause and what it came from; an entry has
at most one open flag per cause and source, and its check of itself flags nothing. A flag
changes nothing else, and stays until the user or the main agent clears it with a reason;
the user may open a cleared one again. Flags from document blocks and artifacts, and from
document blocks bound to entries (LD-34), are not built yet, and pins that were stale
before flags were built are not flagged.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from sandbox_science.contracts import new_id

from .models import PREFIXES

Json = dict[str, Any]

SET_ASIDE: Final = frozenset({"disputed", "refuted", "superseded", "withdrawn"})
"""The statuses that flag the entries resting on an entry."""

_CURRENT: Final = "JOIN ledger_entries e ON e.entry_id=x.entry_id AND x.revision=e.head "

_Flag = Callable[[str, str, Json, str | None], None]
"""Flag an entry: its ID, the cause, the source and the flag it spread from."""


@dataclass(frozen=True)
class Changed:
    """An entry a change made or revised, as the change left it."""

    entry_id: str
    handle: str
    type: str
    revision: int
    statement_changed: bool
    status: str
    status_before: str | None
    pins: tuple[tuple[str, str, int | None], ...] = field(default=())
    """What the revision newly rests on: each ``assumes`` or ``builds_on`` link, or
    evidence, it gains to another entry, as (kind, target, pinned revision)."""


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
    db: sqlite3.Connection, project: str, changed: list[Changed], now: str
) -> list[Raised]:
    """Flag the entries that rest on what ``changed`` changed, the entries ``changed``
    newly rests on a set-aside, restated or flagged entry, and those that rest on any of
    them in turn, in the open change ``db``."""
    raised: list[Raised] = []

    def flag(entry: str, cause: str, source: Json, via: str | None) -> None:
        found = _flag(db, project, entry, cause, source, via, now)
        if found is not None:
            raised.append(found)

    for c in changed:
        source = {
            "entry": c.entry_id,
            "handle": c.handle,
            "revision": c.revision,
            "status": c.status,
        }
        if c.type == "assumption" and c.statement_changed:
            for entry, pinned in dependents(db, c.entry_id, ("assumes",), False):
                if (pinned or 0) < c.revision:
                    flag(entry, "assumption_revised", source, None)
        if c.status in SET_ASIDE and c.status != c.status_before:
            cause = _set_aside(c.type, c.status)
            for entry, _ in dependents(db, c.entry_id, ("builds_on", "assumes"), True):
                flag(entry, cause, source, None)
        if c.statement_changed:
            for entry, pinned in dependents(db, c.entry_id, (), True):
                if (pinned or 0) < c.revision:
                    flag(entry, "evidence_changed", source, None)
        for kind, target, pinned in c.pins:
            _rests_on(db, c.entry_id, kind, target, pinned, flag)
    # Spread to the entries that rest on a flagged entry, once each.
    seen = {flag.entry_id for flag in raised}
    queue = list(raised)
    while queue:
        spread = queue.pop(0)
        source = {"entry": spread.entry_id, "handle": spread.handle}
        for entry, _ in dependents(db, spread.entry_id, ("builds_on", "assumes"), True):
            if entry in seen:
                continue
            seen.add(entry)
            before = len(raised)
            flag(entry, "spread", source, spread.flag_id)
            queue.extend(raised[before:])
    return raised


def _set_aside(kind: str, status: str) -> str:
    return (
        "assumption_withdrawn"
        if kind == "assumption" and status == "withdrawn"
        else ("dependency_status")
    )


def _rests_on(
    db: sqlite3.Connection,
    entry: str,
    kind: str,
    target: str,
    pinned: int | None,
    flag: _Flag,
) -> None:
    """Flag ``entry`` for what it newly rests on: a set-aside target, a pin older than
    the target's statement, or a target with an open flag."""
    kind_of, number, head, status = db.execute(
        "SELECT e.type,e.number,e.head,r.status FROM ledger_entries e JOIN ledger_revisions r "
        "ON r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
        (target,),
    ).fetchone()
    handle = f"{PREFIXES[kind_of]}-{number}"
    source = {"entry": target, "handle": handle, "revision": head, "status": status}
    if status in SET_ASIDE:
        flag(entry, _set_aside(kind_of, status), source, None)
    if (
        pinned is not None
        and pinned < head
        and (kind == "evidence" or (kind == "assumes" and kind_of == "assumption"))
        and db.execute(
            "SELECT 1 FROM ledger_revisions r WHERE r.entry_id=? AND r.revision>? "
            "AND r.statement!=(SELECT p.statement FROM ledger_revisions p "
            "WHERE p.entry_id=r.entry_id AND p.revision=?) LIMIT 1",
            (target, pinned, pinned),
        ).fetchone()
        is not None
    ):
        cause = "evidence_changed" if kind == "evidence" else "assumption_revised"
        flag(entry, cause, source, None)
    open_flag = db.execute(
        "SELECT flag_id FROM ledger_flags WHERE entry_id=? AND cleared IS NULL "
        "ORDER BY raised DESC,rowid DESC LIMIT 1",
        (target,),
    ).fetchone()
    if open_flag is not None:
        flag(entry, "spread", {"entry": target, "handle": handle}, open_flag[0])


def raise_one(
    db: sqlite3.Connection, project: str, entry: str, cause: str, source: Json, now: str
) -> Raised | None:
    """One flag on ``entry`` for ``cause`` from ``source``, unless one is open for them."""
    return _flag(db, project, entry, cause, source, None, now)


def key(cause: str, source: Json) -> str:
    """What makes a flag the same as another on its entry: its cause and source."""
    return f"{cause}:{source['entry']}" + (f":{source['revision']}" if "revision" in source else "")


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
    head, kind, number = db.execute(
        "SELECT head,type,number FROM ledger_entries WHERE entry_id=?", (entry,)
    ).fetchone()
    flag = new_id("flag")
    inserted = db.execute(
        "INSERT INTO ledger_flags(flag_id,project,entry_id,revision,cause,source,via,key,raised) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (
            flag,
            project,
            entry,
            head,
            cause,
            json.dumps(source, sort_keys=True),
            via,
            key(cause, source),
            now,
        ),
    ).rowcount
    if not inserted:
        return None
    return Raised(flag, entry, f"{PREFIXES[kind]}-{number}", cause, source, via)
