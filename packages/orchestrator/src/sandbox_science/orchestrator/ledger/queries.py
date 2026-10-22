# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger queries. No function opens a transaction."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Final

from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)

from . import standing
from .models import (
    PREFIXES,
    EntryFinding,
    EntryFlag,
    EntryLink,
    Standing,
)
from .records import Row

Json = dict[str, Any]


def find(db: sqlite3.Connection, project: str, reference: str) -> Row:
    if ID.fullmatch(reference):
        row = db.execute(
            "SELECT entry_id,project,type,number,origin,head FROM ledger_entries "
            "WHERE entry_id=? AND project=?",
            (reference, project),
        ).fetchone()
    elif match := HANDLE.fullmatch(reference):
        row = db.execute(
            "SELECT entry_id,project,type,number,origin,head FROM ledger_entries "
            "WHERE project=? AND type=? AND number=?",
            (project, TYPES[match[1]], int(match[2])),
        ).fetchone()
    else:
        raise DocumentError(INVALID_REQUEST, f"{reference!r} names no entry: give an ID or handle")
    if row is None:
        raise DocumentError(NOT_FOUND, f"no entry {reference} in this project")
    return Row(*row)


def summary(
    row: Row, revision: tuple[Any, ...], asserted_only: bool | None, label: Standing | None
) -> Json:
    fields = json.loads(revision[4])
    return {
        "kind": fields.get("kind", "question" if row.type == "issue" else None),
        "session": fields.get("session") if row.type == "decision" else None,
        "standing": label,
        "asserted_only": asserted_only,
        "entry_id": row.entry_id,
        "handle": row.handle,
        "type": row.type,
        "origin": row.origin,
        "revision": revision[0],
        "title": revision[1],
        "status": revision[2],
        "updated": revision[7],
        "basis": revision[8],
    }


def links(db: sqlite3.Connection, entry: str, revision: int) -> list[EntryLink]:
    return [
        stored(db, row)
        for row in db.execute(
            "SELECT kind,target,target_id,target_revision,block,note FROM ledger_links "
            "WHERE entry_id=? AND revision=? ORDER BY ordinal",
            (entry, revision),
        ).fetchall()
    ]


def flags(db: sqlite3.Connection, entry: str) -> list[EntryFlag]:
    """The open flags on ``entry``, newest first, each with the reviews whose
    snapshots held the revision it was raised on (LG-28)."""
    found: list[EntryFlag] = []
    for flag, cause, source, via, revision, raised, reopens in db.execute(
        "SELECT flag_id,cause,source,via,revision,raised,reopens FROM ledger_flags "
        "WHERE entry_id=? AND cleared IS NULL ORDER BY raised DESC,rowid DESC LIMIT 50",
        (entry,),
    ):
        reviews = [
            review
            for (review,) in db.execute(
                "SELECT DISTINCT review_id FROM review_evidence WHERE kind='ledger' "
                "AND json_extract(locator,'$.entry')=? "
                "AND json_extract(locator,'$.revision')=? ORDER BY review_id",
                (entry, revision),
            )
        ]
        found.append(
            EntryFlag(
                flag_id=flag,
                cause=cause,
                source=json.loads(source),
                via=via,
                revision=revision,
                raised=raised,
                reviews=reviews,
                reopens=reopens,
            )
        )
    return found


def findings(db: sqlite3.Connection, project: str, entry: str) -> list[EntryFinding]:
    """The review findings anchored to ``entry``, newest first (LG-25)."""
    found: dict[str, EntryFinding] = {}
    for row in db.execute(
        "SELECT f.finding_id,f.session,f.status,f.severity,f.category,f.origin,"
        "coalesce(json_extract(a.value,'$.field'),json_extract(a.value,'$.locator.field')),"
        "json_extract(a.value,'$.locator.revision') "
        "FROM review_finding_entries x JOIN review_findings f USING(finding_id), "
        "json_each(f.anchors) a WHERE x.entry_id=? AND f.project=? "
        "AND json_extract(a.value,'$.locator.entry')=? ORDER BY f.created DESC,f.rowid DESC",
        (entry, project, entry),
    ):
        if row[0] not in found and len(found) < 50:
            found[row[0]] = EntryFinding(
                finding_id=row[0],
                session=row[1],
                status=row[2],
                severity=row[3],
                category=row[4],
                origin=row[5],
                field=row[6],
                revision=row[7],
            )
    return list(found.values())


def incoming(
    db: sqlite3.Connection, project: str, entry: str, after: int
) -> tuple[list[EntryLink], int]:
    """The links other entries' current revisions hold to ``entry``, from the
    ``after``-th on, and how many there are."""
    where = "WHERE l.target='entry' AND l.target_id=? AND e.project=? "
    total = db.execute(f"SELECT count(*) {CURRENT}{where}", (entry, project)).fetchone()[0]
    links = [
        EntryLink(
            kind=kind,
            target="entry",
            target_id=source,
            revision=revision,
            note=note,
            handle=f"{PREFIXES[source_type]}-{number}",
        )
        for kind, source, revision, note, source_type, number in db.execute(
            f"SELECT l.kind,l.entry_id,l.target_revision,l.note,e.type,e.number {CURRENT}"
            f"{where}ORDER BY e.position,l.ordinal LIMIT ? OFFSET ?",
            (entry, project, INCOMING, after),
        ).fetchall()
    ]
    return links, total


ID: Final = re.compile(r"entry-[0-9a-f]{24}")


HANDLE: Final = re.compile(r"(I|C|R|A|D|T|REF)-([1-9][0-9]{0,8})")


TYPES: Final = {prefix: kind for kind, prefix in PREFIXES.items()}


REVISION: Final = (
    "SELECT r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created,"
    "r.basis,r.anchors,r.pins FROM ledger_revisions r WHERE r.entry_id=? AND r.revision=?"
)


CURRENT: Final = (
    "FROM ledger_links l JOIN ledger_entries e ON e.entry_id=l.entry_id AND l.revision=e.head "
)


HOLDERS: Final = f"SELECT l.entry_id {CURRENT}WHERE l.target='entry' AND l.target_id=? AND l.kind=?"


TARGETS: Final = f"SELECT l.target_id {CURRENT}WHERE l.entry_id=? AND l.kind=? AND l.target='entry'"


INCOMING: Final = 200


def stored(db: sqlite3.Connection, row: tuple[Any, ...]) -> EntryLink:
    kind, target, target_id, revision, block, note = row
    handle = session = None
    if target == "entry":
        found = db.execute(
            "SELECT type,number FROM ledger_entries WHERE entry_id=?", (target_id,)
        ).fetchone()
        handle = f"{PREFIXES[found[0]]}-{found[1]}"
    elif target == "finding":
        # Read from the finding, whose session the workspace opens it in.
        (session,) = db.execute(
            "SELECT session FROM review_findings WHERE finding_id=?", (target_id,)
        ).fetchone()
    return EntryLink(
        kind=kind,
        target=target,
        target_id=target_id,
        revision=revision,
        block=block,
        note=note,
        handle=handle,
        session=session,
    )


def entry_id(db: sqlite3.Connection, project: str, handle: str) -> str:
    """The ID of the result ``handle`` names in ``project``."""
    number = int(handle.removeprefix(f"{PREFIXES['result']}-"))
    row = db.execute(
        "SELECT entry_id FROM ledger_entries WHERE project=? AND type='result' AND number=?",
        (project, number),
    ).fetchone()
    return "" if row is None else row[0]


def content(db: sqlite3.Connection, entry: str, revision: int) -> tuple[Any, ...]:
    """An entry's statement (its text revision) and fields at ``revision``."""
    found = db.execute(
        "SELECT statement,fields FROM ledger_revisions WHERE entry_id=? AND revision=?",
        (entry, revision),
    ).fetchone()
    return tuple(found) if found is not None else ()


def rests_on(db: sqlite3.Connection, issue: str, claim: str) -> bool:
    """Whether ``issue`` is obstructed by a decision in effect that named an assessment of
    ``claim``, as the change that obstructed it wrote that decision (the one its label
    reads, ``standing.decider``)."""
    (status,) = db.execute(
        "SELECT r.status FROM ledger_entries e JOIN ledger_revisions r "
        "ON r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
        (issue,),
    ).fetchone()
    decider = standing.decider(db, issue, "obstructed") if status == "obstructed" else None
    if decider is None or not decider.in_effect or not decider.fields.get("assessment"):
        return False
    return (
        db.execute(
            "SELECT 1 FROM ledger_work WHERE record_id=? AND assesses=?",
            (decider.fields["assessment"], claim),
        ).fetchone()
        is not None
    )
