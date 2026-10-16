# SPDX-License-Identifier: MIT
"""How the ledger's reads label failure, claimed impossibility and settled impossibility
(docs/specs/context-management.md, CM-11).

Three things stay apart:

- an attempt that did not succeed (``attempt``): a work record ending ``limit``, or an
  observation whose evidence that counts is only finite tests or numerical experiments;
- a claimed obstruction or negative result that is not settled: a work record ending
  ``obstruction_claimed`` that no decision settled, and a negative result that is
  proposed, or recorded from a source nobody confirmed (``claimed``), or ``disputed``,
  whatever evidence it holds;
- a settled impossibility (``impossible``), in the ledger's settled statuses only: an
  issue obstructed by a decision still in effect, the user's, or the main agent's naming
  its excluded scope and the user's accepted assessment of an obstruction claimed on it
  (LG-7); a project negative result established by a decision still in effect (LG-17);
  or a published one that is recorded and confirmed (LG-16), labelled as its source's.

The settling decision is the one put in effect by the change that moved the entry into
its status, never a later one, so no later decision widens a settled scope. A settled
entry, or its settling decision, with an open flag (LG-28) reads ``flagged``,
unresolved, with the flag's cause, until the flag is cleared. Labels are read from the
records at each read, never stored, and only an entry's current revision has one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from . import evidence, frontier, gate
from .models import PREFIXES, EvidenceLink, Standing

Json = dict[str, Any]

FINITE: Final = frozenset({"finite_test", "numerical_experiment"})
"""Evidence of finitely many cases or samples: an observation resting on it alone is an
attempt. A negative result is labelled by how the ledger's rules settled it, whatever its
evidence, and an impossibility names its scope, which says what that evidence covered."""

_SET_ASIDE: Final = frozenset({"refuted", "superseded", "withdrawn"})


@dataclass(frozen=True)
class Decider:
    """The decision that moved an entry into its status, as that change wrote it."""

    entry_id: str
    handle: str
    fields: Json
    revision: int
    by_user: bool
    in_effect: bool
    """Whether it is still in effect."""


def of_entry(
    db: sqlite3.Connection,
    entry: str,
    kind: str,
    origin: str | None,
    status: str,
    fields: Json,
    revision: int,
) -> Standing | None:
    """The label of ``entry`` as it reads at ``revision``, its current one; None when
    CM-11 gives it none."""
    if kind == "issue":
        return obstructed(db, entry)[0] if status == "obstructed" else None
    claimed = fields.get("kind")
    if kind not in ("claim", "result") or status in _SET_ASIDE:
        return None
    if claimed not in ("negative_result", "observation"):
        return None
    held = evidence.stored(db, entry, revision)
    if claimed == "negative_result":
        return _negative(db, entry, origin, status, revision, held)
    types = {link.type for link in held if gate.counts(db, link)}
    if types and types <= FINITE:
        named = " and ".join(sorted(t.replace("_", " ") for t in types))
        return Standing(
            label="attempt",
            text=f"an observation from {named} evidence, within its scope: not an impossibility",
        )
    return None


def _negative(
    db: sqlite3.Connection,
    entry: str,
    origin: str | None,
    status: str,
    revision: int,
    held: list[EvidenceLink],
) -> Standing:
    if status == "disputed":
        return Standing(label="disputed", text="disputed")
    confirmed = frontier.confirmed(db, entry, revision) if status == "recorded" else None
    if status == "proposed" or (status == "recorded" and confirmed is None):
        words = "claimed, not established" if origin == "project" else "claimed by its source"
        return Standing(label="claimed", text=f"{words}: not an impossibility")
    by = confirmed
    if status == "established":
        found = decider(db, entry, status)
        if found is None or not found.in_effect:
            return _undecided("established")
        if (flag := _flagged(db, found.entry_id, found.handle)) is not None:
            return flag
        by = found.handle
    if (flag := _flagged(db, entry)) is not None:
        return flag
    scopes = [link.scope for link in held if link.scope and gate.counts(db, link)]
    return Standing(
        label="impossible",
        text="impossible, within its statement and its evidence's scope"
        if origin == "project"
        else "impossible, as its source states it",
        scope="; ".join(scopes) or None,
        assumes=_assumes(db, entry, revision),
        by=by,
    )


def obstructed(db: sqlite3.Connection, issue: str) -> tuple[Standing, str | None]:
    """The label of ``issue``, which is obstructed, and the obstruction claimed on it
    that settled it: read from the decision that made it obstructed, the user's, or the
    main agent's naming the user's acceptance of that claim, which the user's latest
    assessment of it still gives (LG-7, CM-13)."""
    found = decider(db, issue, "obstructed")
    if found is None or not found.in_effect:
        return _undecided("obstructed"), None
    claim = None
    if found.fields.get("assessment"):
        row = db.execute(
            "SELECT c.record_id FROM ledger_work a JOIN ledger_work c ON c.record_id=a.assesses "
            "WHERE a.record_id=? AND a.entry_id=? AND c.entry_id=? "
            "AND c.ending='obstruction_claimed' AND (SELECT l.verdict FROM ledger_work l "
            "WHERE l.assesses=c.record_id AND json_extract(l.author,'$.kind')='user' "
            "ORDER BY l.position DESC LIMIT 1)='accepted'",
            (found.fields["assessment"], issue, issue),
        ).fetchone()
        if row is None:
            return Standing(
                label="claimed",
                text=f"obstructed by {found.handle}, whose assessment the user does not accept "
                "now: not settled",
            ), None
        claim = row[0]
    elif not found.by_user:
        return Standing(
            label="claimed",
            text=f"obstructed by {found.handle}, which names no acceptance of the user's: "
            "not settled",
        ), None
    flag = _flagged(db, issue) or _flagged(db, found.entry_id, found.handle)
    if flag is not None:
        return flag, claim
    scope = found.fields.get("excluded_scope")
    return Standing(
        label="impossible",
        text=f"impossible within its excluded scope, as {found.handle} decided"
        if scope
        else f"impossible, scope not stated, as {found.handle} decided",
        scope=scope,
        assumes=_assumes(db, found.entry_id, found.revision),
        by=found.handle,
    ), claim


def of_work(
    ending: str, limit: str | None, record: str, issue: tuple[Standing, str | None] | None
) -> Standing | None:
    """The label of a work record that ends ``ending``; ``issue`` is the label of the
    obstructed issue it is on, with the claim that settled it."""
    if ending == "limit":
        return Standing(label="attempt", text=f"an attempt that did not succeed: {limit} ran out")
    if ending != "obstruction_claimed":
        return None
    if issue is not None and issue[1] == record:
        return issue[0]
    return Standing(label="claimed", text="a claimed obstruction, not settled")


def decider(db: sqlite3.Connection, entry: str, status: str) -> Decider | None:
    """The decision put in effect by the change that last moved ``entry`` into
    ``status``, affecting it, as that change wrote it."""
    moved = db.execute(
        "SELECT r.operation_id FROM ledger_revisions r WHERE r.entry_id=? AND r.status=? "
        "AND NOT EXISTS (SELECT 1 FROM ledger_revisions b WHERE b.entry_id=r.entry_id "
        "AND b.revision=r.revision-1 AND b.status=r.status) ORDER BY r.revision DESC LIMIT 1",
        (entry, status),
    ).fetchone()
    if moved is None:
        return None
    found = db.execute(
        "SELECT d.entry_id,d.number,r.fields,r.revision,json_extract(r.author,'$.kind'),"
        "(SELECT h.status FROM ledger_revisions h WHERE h.entry_id=d.entry_id "
        "AND h.revision=d.head) FROM ledger_links l JOIN ledger_revisions r "
        "ON r.entry_id=l.entry_id AND r.revision=l.revision JOIN ledger_entries d "
        "ON d.entry_id=l.entry_id WHERE l.kind='affects' AND l.target='entry' "
        "AND l.target_id=? AND d.type='decision' AND r.operation_id=? AND r.status='in_effect' "
        "ORDER BY d.position DESC LIMIT 1",
        (entry, moved[0]),
    ).fetchone()
    if found is None:
        return None
    return Decider(
        found[0],
        f"{PREFIXES['decision']}-{found[1]}",
        json.loads(found[2]),
        found[3],
        found[4] == "user",
        found[5] == "in_effect",
    )


def settled(db: sqlite3.Connection, entry: str, since: int) -> bool:
    """Whether ``entry`` was settled at its revision ``since``, when a flag was raised on
    it, or at any revision after: established, refuted, obstructed, or published and
    confirmed; or, for a decision, whether it ever settled an entry it affects, being put
    in effect in the change that moved that entry into a decided status. Clearing a flag on
    a settled entry settles it again (the owner's rule, 2026-10-15)."""
    for revision, status, kind in db.execute(
        "SELECT r.revision,r.status,e.type FROM ledger_revisions r JOIN ledger_entries e "
        "USING(entry_id) WHERE r.entry_id=? AND r.revision>=?",
        (entry, since),
    ).fetchall():
        if status in ("established", "refuted", "obstructed") or (
            kind in ("claim", "result")
            and status == "recorded"
            and frontier.confirmed(db, entry, revision) is not None
        ):
            return True
    # A reversed decision settles nothing any more (CM-10): its flags are the main
    # agent's to clear.
    return (
        db.execute(
            "SELECT 1 FROM ledger_entries e JOIN ledger_revisions h ON h.entry_id=e.entry_id "
            "AND h.revision=e.head WHERE e.entry_id=? AND h.status='in_effect'",
            (entry,),
        ).fetchone()
        is not None
        and db.execute(
            "SELECT 1 FROM ledger_revisions d JOIN ledger_links l ON l.entry_id=d.entry_id "
            "AND l.revision=d.revision AND l.kind='affects' AND l.target='entry' "
            "JOIN ledger_revisions t ON t.entry_id=l.target_id AND t.operation_id=d.operation_id "
            "WHERE d.entry_id=? AND d.status='in_effect' "
            "AND t.status IN ('established','refuted','obstructed') LIMIT 1",
            (entry,),
        ).fetchone()
        is not None
    )


def _undecided(status: str) -> Standing:
    return Standing(
        label="claimed",
        text=f"{status}, but the decision that made it so is not in effect: not settled",
    )


def _flagged(db: sqlite3.Connection, entry: str, decision: str | None = None) -> Standing | None:
    """The label of a settled entry with an open flag, or whose settling ``decision``
    has one."""
    found = db.execute(
        "SELECT cause,json_extract(source,'$.handle') FROM ledger_flags WHERE entry_id=? "
        "AND cleared IS NULL ORDER BY raised DESC,rowid DESC LIMIT 1",
        (entry,),
    ).fetchone()
    if found is None:
        return None
    cause = found[0].replace("_", " ")
    whose = "" if decision is None else f"its decision {decision} is "
    return Standing(
        label="flagged",
        text=f"settled, but {whose}flagged ({cause}, from {found[1]}): unresolved until the "
        "flag is cleared",
        by=found[1],
    )


def _assumes(db: sqlite3.Connection, entry: str, revision: int) -> list[str]:
    return [
        f"{PREFIXES['assumption']}-{number}"
        for (number,) in db.execute(
            "SELECT e.number FROM ledger_links l JOIN ledger_entries e ON e.entry_id=l.target_id "
            "WHERE l.entry_id=? AND l.revision=? AND l.kind='assumes' ORDER BY l.ordinal",
            (entry, revision),
        )
    ]
