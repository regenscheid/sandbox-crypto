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
   ``independent_check`` of a revision whose material the change keeps, by the user or
   by a delegation: one the user added themselves, at the revision they checked, or one
   whose target is the outcome of a delegation of the project that checked that
   revision and returned ``confirmed`` and ``complete``, with no plan fence refusal, a
   whole read of the entry at that revision and of each proof's target at the revision
   its link pins, run by a run that added none of the entry's proofs (a fresh context,
   PD-13; the same model counts, coordinator 2026-10-15). A delegation's check counts
   only while no delegation that checked a revision with the same statement text,
   fields, assumptions and supporting evidence returned ``refuted``, or
   ``inconclusive`` from a complete check, or is still running, linked or not; the
   user's check still counts;
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

Some clauses cannot hold in full yet, and the limits are deliberate: verification
reviews are not built, so they do not count in clause 2, whose delegations' runs are
told apart from a proof's author by the run that added the proof to the entry, not by
who wrote the block it cites; no record says which inputs were synthetic (RA-17), so clause 1
reads the link's type alone; execution targets are not built, so clause 3 asks for the
artifact alone; and no record says which revision of the entry a finding's decision
saw, so clause 5 reads that the entry kept the material since the finding, over at
most ``KEPT_REVISIONS`` revisions.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError
from sandbox_science.orchestrator.projects import run_id_of

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
    text: str | None = None
    """The SHA-256 of the statement's body, which a revision that restores an earlier
    text shares with it, under a new text revision."""

    def claimed(self) -> Material:
        """What the entry claims and rests on: the material with its statement by its
        body, not its text revision, and without its ``contradicts`` links. A check that
        disagreed with it holds while the entry keeps this, whatever evidence against it
        was added or removed since, and when its statement was changed and restored
        (LG-17)."""
        kept = frozenset(pinned for pinned in self.evidence if pinned[0] != "contradicts")
        statement = None if self.text is not None else self.statement
        return dataclasses.replace(self, statement=statement, evidence=kept)


def fingerprint(material: Material) -> str:
    """A digest of ``material``: two revisions that hold the same material have the same
    one, so a check of one covers the other."""
    return hashlib.sha256(
        json.dumps(
            [
                material.statement,
                material.fields,
                sorted(json.dumps(list(a)) for a in material.assumes),
                sorted(json.dumps(list(e), default=str) for e in material.evidence),
            ]
        ).encode()
    ).hexdigest()


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
    fields: Json,
    statement: int | None,
    links: list[EntryLink],
    held: list[EvidenceLink],
    text: str | None = None,
) -> Material:
    """The material of a revision: ``statement`` is its statement's text revision, and
    ``text`` the SHA-256 of its body. Caveats qualify what a check or review covered and
    are not part of it (the coordinator, 2026-10-15, which the owner can reverse): adding
    one unsettles nothing, and removing one from a settled entry takes a decision."""
    return Material(
        text=text,
        statement=statement,
        fields=json.dumps({k: v for k, v in fields.items() if k != "caveats"}, sort_keys=True),
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
    since = weakened(db, entry)
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
    else:
        kept = examined(db, entry, checked)
        valid = [
            link
            for link in held
            if link.role == "independent_check"
            and link.type not in _NOT_COUNTING
            and link.checked is not None
            and kept.get(link.checked) == now
            and after(db, entry, link, since)
        ]
        proofs = [link for link in counting if link.type == "proof"]
        project = _project(db, entry)
        found = next(
            (c for d in valid if (c := delegated(db, project, entry, d, proofs)) is not None),
            None,
        )
        against = disputed(db, project, entry, now) if found is not None else None
        if any(checked_by_user(db, entry, link, before) for link in valid):
            yield (
                "independent_check",
                True,
                "the user checked a revision with the statement, fields, assumptions and "
                "evidence it holds",
            )
        elif found is not None and against is not None and against.running:
            yield (
                "independent_check",
                False,
                f"delegation {against.delegation} is still checking a revision with the "
                "statement, fields, assumptions and supporting evidence it holds: wait for "
                "it, or cancel it",
            )
        elif found is not None and against is not None:
            yield (
                "independent_check",
                False,
                f"delegation {against.delegation} checked a revision with the statement, "
                "fields, assumptions and supporting evidence it holds and did not confirm "
                "it (refuted, or a complete check that was inconclusive): while it keeps "
                "that material, no delegation's check counts, and only the user's check does",
            )
        elif found is not None:
            note = "" if not found.note else f"; the requester's note: {found.note}"
            yield (
                "independent_check",
                True,
                f"delegation {found.delegation} confirmed a revision with the statement, "
                f"fields, assumptions and evidence it holds, in a fresh context{note}",
            )
        else:
            yield (
                "independent_check",
                False,
                f"{handle} rests on a proof and has no independent check that counts, of a "
                "revision whose statement, fields, assumptions and evidence the change "
                "keeps: one the user added themselves, at the revision they checked (the "
                "workspace's check, or the ledger check command), or the outcome of a "
                "delegation that checked that revision and returned confirmed and "
                "complete, read the entry and its proofs' targets at the revisions they "
                "pin, met no plan fence, and added none of its proofs, while no check of "
                "the same material disagreed, each recorded since the entry was last "
                "weakened (a dispute, a flag, a reopening)",
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
    since = max((t for t in (confirmed, since) if t is not None), default=None)
    covering = examined(db, entry, set(reviewed(db, entry, after=since)))
    covered = sorted(revision for revision, material in covering.items() if material == now)
    if not covered:
        yield (
            "review",
            False,
            f"no completed consistency review has covered {handle} as the change leaves "
            "it: a review whose coverage was complete, whose snapshot held, whole, a "
            "revision with the same statement, fields, assumptions and evidence, and "
            "which started after any fix confirmed of a fail finding raised on them, "
            "and after the entry was last weakened (a dispute, a flag, a reopening)",
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


@dataclass(frozen=True)
class Checked:
    """A delegation's check that counts, and the note its parent gave the checker."""

    delegation: str
    note: str


def delegated(
    db: sqlite3.Connection,
    project: str,
    entry: str,
    link: EvidenceLink,
    proofs: list[EvidenceLink],
) -> Checked | None:
    """``link``'s delegation, when it is a check that counts (LG-17): its outcome is of a
    check of ``entry`` at the revision the link names, in ``project``, which returned
    ``confirmed`` and ``complete``, met no plan fence, read the entry at that revision
    and the target of each of its proofs at the revision its link pins, and whose run
    added none of ``proofs`` to the entry. The outcome is written once, so it is read as
    it is."""
    if link.target != "delegation":
        return None
    found = db.execute(
        "SELECT kind,brief,ended,result,refused,read_entry,read_proofs,run FROM delegations "
        "WHERE delegation_id=? AND project_id=?",
        (link.target_id, project),
    ).fetchone()
    if found is None:
        return None
    kind, brief, ended, result, refused, read, read_proofs, run = found
    written: dict[str, Any] = json.loads(brief)
    check: dict[str, Any] = written.get("check") or {}
    outcome: dict[str, Any] = json.loads(result) if result is not None else {}
    if not (
        kind == "check"
        and (check.get("entry"), check.get("revision")) == (entry, link.checked)
        and ended == "returned"
        and outcome.get("verdict") == "confirmed"
        and outcome.get("status") == "complete"
        and refused is None
        and read == 1
        and read_proofs == 1
    ):
        return None
    checker = run_id_of(str(run))
    if checker is None or any(_author_run(db, entry, proof.added) == checker for proof in proofs):
        return None
    note = written.get("note")
    return Checked(link.target_id, note if isinstance(note, str) else "")


@dataclass(frozen=True)
class Disputed:
    """A check of the same material that did not confirm it, or that is still running."""

    delegation: str
    running: bool


def disputed(db: sqlite3.Connection, project: str, entry: str, now: Material) -> Disputed | None:
    """The first delegation of ``project`` that checked ``entry``, linked to it or not,
    at a revision that claims and rests on what ``now`` does (``Material.claimed``), and
    did not confirm it: it returned ``refuted``, or ``inconclusive`` from a complete
    check; or one that is still running, which may yet. Retrying a check until one
    confirms gains nothing (LG-17)."""
    rows = [
        (delegation, revision, ended is None)
        for delegation, revision, ended in db.execute(
            "SELECT delegation_id,json_extract(brief,'$.check.revision'),ended FROM delegations "
            "WHERE project_id=? AND kind='check' AND json_extract(brief,'$.check.entry')=? "
            "AND (ended IS NULL OR (result IS NOT NULL "
            "AND (json_extract(result,'$.verdict')='refuted' "
            "OR (json_extract(result,'$.verdict')='inconclusive' "
            "AND json_extract(result,'$.status')='complete')))) "
            "ORDER BY started_at, delegation_id",
            (project, entry),
        )
        if isinstance(revision, int)
    ]
    held = examined(db, entry, {revision for _, revision, _ in rows})
    claimed = now.claimed()
    return next(
        (
            Disputed(d, running)
            for d, revision, running in rows
            if revision in held and held[revision].claimed() == claimed
        ),
        None,
    )


SETTLED: Final = frozenset({"established", "obstructed", "refuted", "recorded"})
"""The statuses an entry leaves only by being weakened."""


def weakened(db: sqlite3.Connection, entry: str, until: str | None = None) -> float | None:
    """When ``entry`` was last weakened, as a POSIX time, at or before ``until`` when
    given: a flag raised on it, or a revision that moved it down, into ``disputed`` or out
    of a settled status, such as a dispute, a reopening or a refutation reversed. The
    owner's rule (2026-10-15): a strengthening change by an agent counts only checks,
    reviews and acceptances recorded after it."""
    times = [raised for (raised,) in db.execute(
        "SELECT raised FROM ledger_flags WHERE entry_id=?", (entry,)
    )]  # fmt: skip
    times += [
        created
        for created, status, before in db.execute(
            "SELECT r.created,r.status,b.status FROM ledger_revisions r JOIN ledger_revisions b "
            "ON b.entry_id=r.entry_id AND b.revision=r.revision-1 WHERE r.entry_id=? "
            "AND r.status!=b.status",
            (entry,),
        )
        if status == "disputed" or before in SETTLED
    ]
    bound = None if until is None else datetime.fromisoformat(until)
    found = [
        moment
        for moment in (datetime.fromisoformat(t) for t in times)
        if bound is None or moment <= bound
    ]
    return max(found).timestamp() if found else None


def after(db: sqlite3.Connection, entry: str, link: EvidenceLink, since: float | None) -> bool:
    """Whether the check ``link`` names was recorded after ``since``: a delegation's when
    it started, another's when the revision that added it was written."""
    if since is None:
        return True
    if link.target == "delegation":
        row = db.execute(
            "SELECT started_at FROM delegations WHERE delegation_id=?", (link.target_id,)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT created FROM ledger_revisions WHERE entry_id=? AND revision=?",
            (entry, link.added),
        ).fetchone()
    return row is not None and datetime.fromisoformat(row[0]).timestamp() > since


def fresh_check(db: sqlite3.Connection, entry: str, flag: str) -> bool:
    """Whether a delegation started since ``entry`` was last weakened checked it at its
    current revision from a brief that named ``flag``, in a way that counts
    (``delegated``), while no check of the same material disagrees: the gate the main
    agent's clearing of a flag on a settled claim or result needs (the owner's rule,
    2026-10-15). The check need not be linked as evidence."""
    project = _project(db, entry)
    (head,) = db.execute("SELECT head FROM ledger_entries WHERE entry_id=?", (entry,)).fetchone()
    now = examined(db, entry, {head}).get(head)
    if now is None:
        return False
    held = evidence.stored(db, entry, head)
    proofs = [link for link in held if counts(db, link) and link.type == "proof"]
    since = weakened(db, entry)
    for (delegation,) in db.execute(
        "SELECT delegation_id FROM delegations WHERE project_id=? AND kind='check' "
        "AND json_extract(brief,'$.check.entry')=? AND json_extract(brief,'$.check.revision')=? "
        "AND EXISTS (SELECT 1 FROM json_each(brief,'$.check.flags') WHERE value=?)",
        (project, entry, head, flag),
    ).fetchall():
        check = EvidenceLink(
            evidence_id=delegation,
            role="independent_check",
            type="proof",
            target="delegation",
            target_id=delegation,
            checked=head,
            added=head,
        )
        if after(db, entry, check, since) and delegated(db, project, entry, check, proofs):
            return disputed(db, project, entry, now) is None
    return False


def _project(db: sqlite3.Connection, entry: str) -> str:
    row = db.execute("SELECT project FROM ledger_entries WHERE entry_id=?", (entry,)).fetchone()
    return "" if row is None else str(row[0])


def _author_run(db: sqlite3.Connection, entry: str, revision: int) -> str | None:
    row = db.execute(
        "SELECT json_extract(author,'$.run_id') FROM ledger_revisions "
        "WHERE entry_id=? AND revision=?",
        (entry, revision),
    ).fetchone()
    return None if row is None else row[0]


def checked_by_user(db: sqlite3.Connection, entry: str, link: EvidenceLink, before: int) -> bool:
    """Whether ``link`` is the user's own check: an ``independent_check`` the user added
    themselves at the revision after the one it names (the workspace's check, or
    ``ledger check``). A delegation's outcome the user linked is a delegation's check,
    not theirs."""
    return (
        link.role == "independent_check"
        and link.target != "delegation"
        and link.checked is not None
        and link.added == link.checked + 1
        and added_by_user(db, entry, link, before)
    )


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
            "SELECT r.statement,r.fields,t.sha256 FROM ledger_revisions r "
            "LEFT JOIN text_revisions t ON t.subject_type='statement' "
            "AND t.subject_id=r.entry_id AND t.revision=r.statement "
            "WHERE r.entry_id=? AND r.revision=?",
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
            json.loads(row[1]), row[0], links, evidence.stored(db, entry, revision), row[2]
        )
    return found
