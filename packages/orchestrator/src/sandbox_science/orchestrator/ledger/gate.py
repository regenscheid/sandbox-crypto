# SPDX-License-Identifier: MIT
"""The project's gate for results the main agent establishes (docs/specs/ledger.md,
LG-17).

A project claim or result becomes ``established`` only by a decision in effect. The
user's decision needs nothing more; the main agent's needs the gate to hold, and a
refusal names the first clause that does not (``gate_not_met``):

1. ``evidence``: evidence that counts: a ``supports`` or ``derivation`` link whose type
   is neither ``synthetic`` nor ``unclassified``, and whose target is not a review nor an
   entry that is ``refuted``, ``withdrawn`` or ``superseded``;
2. ``independent_check``: when evidence that counts is of type ``proof``, an
   ``independent_check`` the user added themselves, at the revision they checked, whose
   material the change keeps;
3. ``computation``: computational evidence pinned to an artifact revision;
4. ``review``: a completed checkpoint or terminal review (``passed`` or ``findings``)
   whose coverage was complete had in its snapshot, whole, a revision of the entry whose
   material the change keeps; when a fix was confirmed of a ``fail`` finding raised on
   that material, the review started after the confirmation, so the review that raised
   it does not count;
5. ``finding``: no ``fail`` finding anchored to the entry is unresolved: open, claimed
   fixed or unaddressed, or superseded until a completed review that started after the
   supersession covered the entry's material as the change leaves it. A confirmed fix or
   a withdrawal resolves it, but only for the material it was decided for: while a
   ``fail`` finding of any status is anchored to a revision whose material the change
   keeps, the entry must have kept that material at every revision since, or the
   finding holds;
6. ``flags``: no open flag on the entry (LG-28).

Some clauses cannot hold in full yet, and the limits are deliberate: checks by
delegations and verification reviews are not built, so only a check the user produced
counts in clause 2; no record says which inputs were synthetic (RA-17), so clause 1
reads the link's type alone; execution targets are not built, so clause 3 asks for the
artifact alone; and no record says which revision of the entry a finding's decision
saw, so clause 5 reads that the entry kept the material since the finding, over at
most ``KEPT_REVISIONS`` revisions.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError

from . import evidence
from .models import COMPUTATIONAL, SET_ASIDE, EntryLink, EvidenceLink

Json = dict[str, Any]

_NOT_COUNTING: Final = frozenset({"synthetic", "unclassified"})


_RESOLVED: Final = frozenset({"confirmed fixed", "withdrawn"})

KEPT_REVISIONS: Final = 256
"""The most revisions since a ``fail`` finding the gate reads to see that the entry kept
the material the finding was raised on; past it, the entry is taken to have left it."""


@dataclass(frozen=True)
class Material:
    """What a check or review of an entry examined: a later revision that keeps all of it
    keeps the check (LG-17, as the coordinator decided on 2026-10-15): the statement
    (its text revision, None for one the change writes), the fields, the ``assumes``
    links and every evidence link of a kind that bears on it, ``supports``,
    ``derivation`` and ``contradicts``, so evidence of either sign added after a review
    is not what the review covered."""

    statement: int | None
    fields: str
    assumes: frozenset[tuple[str, int | None]]
    evidence: frozenset[tuple[object, ...]]


def supporting(link: EvidenceLink) -> bool:
    """Whether ``link`` is of a kind that can count: a ``supports`` or ``derivation`` link
    whose type is neither ``synthetic`` nor ``unclassified``, to anything but a review."""
    return (
        link.role in ("supports", "derivation")
        and link.type not in _NOT_COUNTING
        and link.target != "review"
    )


def bearing(link: EvidenceLink) -> bool:
    """Whether ``link`` bears on the entry: a link that can count, or one that
    contradicts it with evidence of a type that could."""
    return supporting(link) or (
        link.role == "contradicts" and link.type not in _NOT_COUNTING and link.target != "review"
    )


def _pinned(link: EvidenceLink) -> tuple[object, ...]:
    where = None if link.locator is None else json.dumps(link.locator, sort_keys=True)
    return (
        link.role,
        link.type,
        link.target,
        link.target_id,
        link.revision,
        link.block,
        link.span,
        where,
    )


def material(
    fields: Json, statement: int | None, links: list[EntryLink], held: list[EvidenceLink]
) -> Material:
    return Material(
        statement=statement,
        fields=json.dumps(fields, sort_keys=True),
        assumes=frozenset(
            (link.target_id, link.revision) for link in links if link.kind == "assumes"
        ),
        evidence=frozenset(_pinned(link) for link in held if bearing(link)),
    )


def counts(db: sqlite3.Connection, link: EvidenceLink) -> bool:
    """Whether ``link`` is evidence that counts: of a kind that can, and not to an entry
    that is now refuted, withdrawn or superseded."""
    if not supporting(link):
        return False
    if link.target not in ("entry", "quote"):
        return True
    row = db.execute(
        "SELECT r.status FROM ledger_entries e JOIN ledger_revisions r "
        "ON r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
        (link.target_id,),
    ).fetchone()
    return row is not None and row[0] not in SET_ASIDE


def refused(entry: str, clause: str, message: str) -> DocumentError:
    return DocumentError(
        POLICY_DENIED, f"gate_not_met: {message}", {"entry": entry, "clause": clause}
    )


def establish(
    db: sqlite3.Connection,
    entry: str,
    handle: str,
    held: list[EvidenceLink],
    now: Material,
    before: int,
) -> None:
    """Refuse the main agent's decision to establish ``handle`` unless the gate holds;
    ``held`` is the evidence the change leaves it, ``now`` what its new revision holds,
    and ``before`` its revision before the change (links the change adds are the main
    agent's). The clauses are read in order, and the first that does not hold refuses."""
    for clause, holds, reason in clauses(db, entry, handle, held, now, before):
        if not holds:
            raise refused(entry, clause, reason)


def clauses(
    db: sqlite3.Connection,
    entry: str,
    handle: str,
    held: list[EvidenceLink],
    now: Material,
    before: int,
) -> Iterator[tuple[str, bool, str]]:
    """Each clause of the gate in order, read when asked for: its name, whether it holds,
    and why."""
    counting = [link for link in held if counts(db, link)]
    if not counting:
        yield (
            "evidence",
            False,
            f"{handle} has no evidence that counts: a supports or derivation link whose "
            "type is neither synthetic nor unclassified, to anything but a review or a "
            "refuted, withdrawn or superseded entry",
        )
    else:
        types = ", ".join(dict.fromkeys(link.type for link in counting))
        yield "evidence", True, f"{handle} has {len(counting)} evidence links that count ({types})"
    checked = {link.checked for link in held if link.checked is not None}
    if not any(link.type == "proof" for link in counting):
        yield "independent_check", True, "no evidence that counts is a proof, so no check is needed"
    elif any(
        link.role == "independent_check"
        and link.type not in _NOT_COUNTING
        and link.checked is not None
        and examined(db, entry, checked).get(link.checked) == now
        and link.added == link.checked + 1
        and added_by_user(db, entry, link, before)
        for link in held
    ):
        yield (
            "independent_check",
            True,
            "the user checked a revision with the statement, fields, assumptions and "
            "evidence it holds",
        )
    else:
        yield (
            "independent_check",
            False,
            f"{handle} rests on a proof and has no independent check that counts: one the "
            "user added themselves, at the revision they checked (the workspace's check, "
            "or the ledger check command), of a revision whose statement, fields, "
            "assumptions and evidence the change keeps (checks by delegations are not "
            "built yet)",
        )
    if any(link.type in COMPUTATIONAL and link.target != "artifact" for link in counting):
        yield (
            "computation",
            False,
            f"{handle}'s computational evidence is not pinned to the artifact revision "
            "that holds the computation",
        )
    else:
        yield (
            "computation",
            True,
            "its computational evidence is pinned to artifact revisions"
            if any(link.type in COMPUTATIONAL for link in counting)
            else "it holds no computational evidence",
        )
    fails = failing(db, entry, now, before)
    confirmed = max(
        (
            f.confirmed
            for f in fails
            if f.on_now and f.status == "confirmed fixed" and f.confirmed is not None
        ),
        default=None,
    )
    covering = examined(db, entry, set(reviewed(db, entry, after=confirmed)))
    covered = sorted(revision for revision, material in covering.items() if material == now)
    if not covered:
        yield (
            "review",
            False,
            f"no completed consistency review has covered {handle} as the change leaves "
            "it: a review whose coverage was complete, whose snapshot held, whole, a "
            "revision with the same statement, fields, assumptions and evidence, and "
            "which started after any fix confirmed of a fail finding raised on them",
        )
    else:
        yield (
            "review",
            True,
            f"a completed review covered {handle} at revision {covered[-1]}, which holds the "
            "same statement, fields, assumptions and evidence",
        )
    yield "finding", *_findings(db, entry, handle, now, fails)
    flagged = db.execute(
        "SELECT flag_id FROM ledger_flags WHERE entry_id=? AND cleared IS NULL "
        "ORDER BY raised LIMIT 1",
        (entry,),
    ).fetchone()
    if flagged is not None:
        yield (
            "flags",
            False,
            f"{handle} has an open flag, {flagged[0]}: clear it with a reason once the entry "
            "still holds, or revise it",
        )
    else:
        yield "flags", True, f"{handle} has no open flag"


def _findings(
    db: sqlite3.Connection, entry: str, handle: str, now: Material, fails: list[Failing]
) -> tuple[bool, str]:
    """The finding clause: whether it holds, and why."""
    for f in fails:
        if f.status in _RESOLVED and (not f.on_now or f.kept):
            continue
        if f.status in _RESOLVED or (f.status == "superseded" and f.on_now):
            return (
                False,
                f"{handle} has a fail finding, {f.finding}, raised on the statement, "
                "fields, assumptions and evidence the change leaves it, and the entry "
                "has left that material since: "
                + (
                    "the finding was superseded when it left, not resolved"
                    if f.status == "superseded"
                    else f"the finding is {f.status}, which was not decided for this material"
                ),
            )
        if (
            f.status == "superseded"
            and now in examined(db, entry, set(reviewed(db, entry, after=f.superseded))).values()
        ):
            continue
        return (
            False,
            f"{handle} has an unresolved fail finding, {f.finding}: fix or rebut it first"
            + (
                "; it was superseded, and no review since has covered the entry as it is"
                if f.status == "superseded"
                else ""
            ),
        )
    return True, f"no fail finding on {handle} is unresolved"


@dataclass(frozen=True)
class Failing:
    """A ``fail`` finding anchored to an entry, as the gate reads it for a change."""

    finding: str
    status: str
    on_now: bool
    """An anchor names a revision of the entry whose material the change keeps."""
    kept: bool
    """The entry has kept that material at every revision since that one."""
    confirmed: float | None
    """When a fix of it was last confirmed."""
    superseded: float | None
    """When it was last superseded."""


def failing(db: sqlite3.Connection, entry: str, now: Material, before: int) -> list[Failing]:
    """The ``fail`` findings anchored to ``entry``, in any status, oldest first: whether
    each was raised on ``now``, the material a change leaves it, and whether the entry
    has kept that material since, through ``before``, its revision before the change."""
    rows = db.execute(
        "SELECT f.finding_id,f.status,"
        "(SELECT max(t.created) FROM review_transitions t "
        "WHERE t.finding_id=f.finding_id AND t.status='confirmed fixed'),"
        "(SELECT max(t.created) FROM review_transitions t "
        "WHERE t.finding_id=f.finding_id AND t.status='superseded') "
        "FROM review_finding_entries x JOIN review_findings f USING(finding_id) "
        "WHERE x.entry_id=? AND f.severity='fail' ORDER BY f.created",
        (entry,),
    ).fetchall()
    anchored: dict[str, set[int]] = {row[0]: set() for row in rows}
    for finding, revision in db.execute(
        "SELECT x.finding_id,json_extract(a.value,'$.locator.revision') "
        "FROM review_finding_entries x JOIN review_findings f USING(finding_id), "
        "json_each(CASE WHEN json_valid(f.anchors) THEN f.anchors ELSE '[]' END) a "
        "WHERE x.entry_id=? AND f.severity='fail' "
        "AND json_extract(a.value,'$.locator.entry')=?",
        (entry, entry),
    ):
        if finding in anchored and type(revision) is int:
            anchored[finding].add(revision)
    held = examined(db, entry, {r for revisions in anchored.values() for r in revisions})
    found: list[Failing] = []
    for finding, status, confirmed, superseded in rows:
        on = sorted(r for r in anchored[finding] if held.get(r) == now)
        found.append(
            Failing(
                finding=finding,
                status=status,
                on_now=bool(on),
                kept=bool(on) and _kept(db, entry, on[0], before, now, held),
                confirmed=confirmed,
                superseded=superseded,
            )
        )
    return found


def _kept(
    db: sqlite3.Connection,
    entry: str,
    since: int,
    through: int,
    now: Material,
    held: dict[int, Material],
) -> bool:
    """Whether every revision of ``entry`` after ``since``, through ``through``, holds
    ``now``; at most ``KEPT_REVISIONS`` of them are read."""
    if through - since > KEPT_REVISIONS:
        return False
    for revision in range(since + 1, through + 1):
        if revision not in held:
            held.update(examined(db, entry, {revision}))
        if held.get(revision) != now:
            return False
    return True


def reviewed(db: sqlite3.Connection, entry: str, *, after: float | None = None) -> list[int]:
    """The revisions of ``entry`` a completed checkpoint or terminal review whose coverage
    was complete held, whole, in its snapshot; only reviews started after ``after``, when
    given."""
    return [
        revision
        for (revision,) in db.execute(
            "SELECT DISTINCT json_extract(e.locator,'$.revision') FROM review_evidence e "
            "JOIN review_runs r USING(review_id) WHERE e.kind='ledger' "
            "AND json_extract(e.locator,'$.entry')=? AND e.complete=1 "
            "AND r.state IN ('passed','findings') AND r.kind IN ('checkpoint','terminal') "
            "AND r.coverage_complete=1 AND (? IS NULL OR r.created>?)",
            (entry, after, after),
        )
        if isinstance(revision, int)
    ]


def added_by_user(db: sqlite3.Connection, entry: str, link: EvidenceLink, before: int) -> bool:
    """Whether the user added ``link``: the revision of ``entry`` that added it is the
    user's. A link the change adds is not (only the main agent's decisions are gated)."""
    if link.added > before:
        return False
    row = db.execute(
        "SELECT json_extract(author,'$.kind') FROM ledger_revisions "
        "WHERE entry_id=? AND revision=?",
        (entry, link.added),
    ).fetchone()
    return row is not None and row[0] == "user"


def examined(db: sqlite3.Connection, entry: str, revisions: set[int]) -> dict[int, Material]:
    """What each of ``revisions`` of ``entry`` held, for the checks that name them."""
    found: dict[int, Material] = {}
    for revision in revisions:
        row = db.execute(
            "SELECT statement,fields FROM ledger_revisions WHERE entry_id=? AND revision=?",
            (entry, revision),
        ).fetchone()
        if row is None:
            continue
        links = [
            EntryLink(kind=kind, target="entry", target_id=target, revision=pinned)
            for kind, target, pinned in db.execute(
                "SELECT kind,target_id,target_revision FROM ledger_links WHERE entry_id=? "
                "AND revision=? AND kind='assumes'",
                (entry, revision),
            )
        ]
        found[revision] = material(
            json.loads(row[1]), row[0], links, evidence.stored(db, entry, revision)
        )
    return found
