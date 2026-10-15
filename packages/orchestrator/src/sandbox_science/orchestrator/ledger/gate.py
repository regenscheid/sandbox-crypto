# SPDX-License-Identifier: MIT
"""The project's gate for results the main agent establishes (docs/specs/ledger.md,
LG-17).

A project claim or result becomes ``established`` only by a decision in effect. The
user's decision needs nothing more; the main agent's needs the gate to hold, and a
refusal names the first clause that does not (``gate_not_met``):

1. ``evidence``: a ``supports`` or ``derivation`` link whose type is neither
   ``synthetic`` nor ``unclassified``;
2. ``independent_check``: for an entry derived by argument (a ``derivation`` link of type
   ``proof``), an ``independent_check`` whose target the user produced, of a revision
   whose statement, fields, ``assumes`` links and derivation targets the change leaves
   as they were;
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


@dataclass(frozen=True)
class Material:
    """What a check of an entry examined: a later revision that keeps all of it keeps
    the check (LG-17). ``statement`` is the statement's text revision, None for one the
    change writes."""

    statement: int | None
    fields: str
    assumes: frozenset[tuple[str, int | None]]
    derivation: frozenset[tuple[object, ...]]


def material(
    fields: Json, statement: int | None, links: list[EntryLink], held: list[EvidenceLink]
) -> Material:
    return Material(
        statement=statement,
        fields=json.dumps(fields, sort_keys=True),
        assumes=frozenset(
            (link.target_id, link.revision) for link in links if link.kind == "assumes"
        ),
        derivation=frozenset(
            (link.target, link.target_id, link.revision, link.block, link.span)
            for link in held
            if link.role == "derivation"
        ),
    )


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
) -> None:
    """Refuse the main agent's decision to establish ``handle`` unless the gate holds;
    ``held`` is the evidence the change leaves it, ``now`` what its new revision holds,
    and ``examined`` what each earlier revision held."""
    counting = [
        link
        for link in held
        if link.role in ("supports", "derivation") and link.type not in _NOT_COUNTING
    ]
    if not counting:
        raise refused(
            entry,
            "evidence",
            f"{handle} has no evidence that counts: a supports or derivation link whose "
            "type is neither synthetic nor unclassified",
        )
    if any(link.role == "derivation" and link.type == "proof" for link in held) and not any(
        link.role == "independent_check"
        and link.type not in _NOT_COUNTING
        and link.checked is not None
        and examined.get(link.checked) == now
        and by_user(db, link)
        for link in held
    ):
        raise refused(
            entry,
            "independent_check",
            f"{handle} is derived by argument and has no independent check that counts: one "
            "whose target the user produced, of a revision whose statement, fields, "
            "assumptions and derivation the change keeps (checks by delegations are not "
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
