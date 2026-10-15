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
   ``independent_check`` whose target the user produced, and which the user added or
   whose target names the entry and the revision it checks, of a revision whose
   material the change keeps;
3. ``computation``: computational evidence pinned to an artifact revision;
4. ``review``: a completed consistency review covered the entry, with no unresolved
   ``fail`` finding on it;
5. ``flags``: no open flag on the entry.

Some clauses cannot hold yet, and the limits are deliberate: the reviewer does not read
ledger entries, so clause 4 never holds and the main agent establishes nothing; checks
by delegations and verification reviews are not built, so only a check the user
produced counts in clause 2; no record says which inputs were synthetic (RA-17), so
clause 1 reads the link's type alone; execution targets are not built, so clause 3
asks for the artifact alone; and flags are not built, so clause 5 holds.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError

from . import evidence
from .models import COMPUTATIONAL, EntryLink, EvidenceLink

Json = dict[str, Any]

_NOT_COUNTING: Final = frozenset({"synthetic", "unclassified"})


_SET_ASIDE: Final = frozenset({"refuted", "withdrawn", "superseded"})
"""The statuses of an entry that no longer supports anything."""


@dataclass(frozen=True)
class Material:
    """What a check or review of an entry examined: a later revision that keeps all of it
    keeps the check (LG-17, as the coordinator decided on 2026-10-15): the statement
    (its text revision, None for one the change writes), the fields, the ``assumes``
    links and every evidence link of a kind that can count, ``supports`` as well as
    ``derivation``, so evidence added after a review is not what the review covered."""

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


def _pinned(link: EvidenceLink) -> tuple[object, ...]:
    return (link.role, link.type, link.target, link.target_id, link.revision, link.block, link.span)


def material(
    fields: Json, statement: int | None, links: list[EntryLink], held: list[EvidenceLink]
) -> Material:
    return Material(
        statement=statement,
        fields=json.dumps(fields, sort_keys=True),
        assumes=frozenset(
            (link.target_id, link.revision) for link in links if link.kind == "assumes"
        ),
        evidence=frozenset(_pinned(link) for link in held if supporting(link)),
    )


def counts(db: sqlite3.Connection, link: EvidenceLink) -> bool:
    """Whether ``link`` is evidence that counts: of a kind that can, and not to an entry
    that is now refuted, withdrawn or superseded."""
    if not supporting(link):
        return False
    if link.target != "entry":
        return True
    row = db.execute(
        "SELECT r.status FROM ledger_entries e JOIN ledger_revisions r "
        "ON r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
        (link.target_id,),
    ).fetchone()
    return row is not None and row[0] not in _SET_ASIDE


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
    examined: dict[int, Material],
    before: int,
) -> None:
    """Refuse the main agent's decision to establish ``handle`` unless the gate holds;
    ``held`` is the evidence the change leaves it, ``now`` what its new revision holds,
    ``examined`` what each earlier revision held, and ``before`` its revision before the
    change (links the change adds are the main agent's)."""
    counting = [link for link in held if counts(db, link)]
    if not counting:
        raise refused(
            entry,
            "evidence",
            f"{handle} has no evidence that counts: a supports or derivation link whose "
            "type is neither synthetic nor unclassified, to anything but a review or a "
            "refuted, withdrawn or superseded entry",
        )
    if any(link.type == "proof" for link in counting) and not any(
        link.role == "independent_check"
        and link.type not in _NOT_COUNTING
        and link.checked is not None
        and examined.get(link.checked) == now
        and by_user(db, link)
        and (added_by_user(db, entry, link, before) or names(db, link, entry))
        for link in held
    ):
        raise refused(
            entry,
            "independent_check",
            f"{handle} rests on a proof and has no independent check that counts: one whose "
            "target the user produced, which the user added or whose target names the "
            "entry and the revision it checks, of a revision whose statement, fields, "
            "assumptions and evidence the change keeps (checks by delegations are not "
            "built yet)",
        )
    if any(link.type in COMPUTATIONAL and link.target != "artifact" for link in counting):
        raise refused(
            entry,
            "computation",
            f"{handle}'s computational evidence is not pinned to the artifact revision "
            "that holds the computation",
        )
    raise refused(
        entry,
        "review",
        f"no completed consistency review has covered {handle}: the reviewer does not read "
        "ledger entries yet, so only the user's decision establishes a result",
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


def names(db: sqlite3.Connection, link: EvidenceLink, entry: str) -> bool:
    """Whether the entry revision ``link`` points to names ``entry`` at the revision the
    check examined, with a link or an evidence link pinned to it."""
    if link.target != "entry" or link.checked is None:
        return False
    found = db.execute(
        "SELECT 1 FROM ledger_links WHERE entry_id=? AND revision=? AND target='entry' "
        "AND target_id=? AND target_revision=? UNION ALL "
        "SELECT 1 FROM ledger_evidence WHERE entry_id=? AND revision=? AND target='entry' "
        "AND target_id=? AND target_revision=? LIMIT 1",
        (link.target_id, link.revision, entry, link.checked) * 2,
    ).fetchone()
    return found is not None


def by_user(db: sqlite3.Connection, link: EvidenceLink) -> bool:
    """Whether the user produced the target of ``link``: an entry revision the user
    wrote, or a document block the user last changed at the pinned revision. An artifact
    an agent run published and a review never are."""
    if link.target == "entry":
        row = db.execute(
            "SELECT json_extract(author,'$.kind') FROM ledger_revisions "
            "WHERE entry_id=? AND revision=?",
            (link.target_id, link.revision),
        ).fetchone()
        return row is not None and row[0] == "user"
    if link.target == "block":
        row = db.execute(
            "SELECT json_extract(r.author,'$.kind') FROM revisions r, "
            "json_each(r.block_map,'$.blocks') b WHERE r.document_id=? AND r.number<=? "
            "AND json_extract(b.value,'$.id')=? "
            "AND json_extract(b.value,'$.status') IN ('added','changed') "
            "ORDER BY r.number DESC LIMIT 1",
            (link.target_id, link.revision, link.block),
        ).fetchone()
        return row is not None and row[0] == "user"
    return False


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
