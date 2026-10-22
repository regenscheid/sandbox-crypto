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
from sandbox_science.orchestrator.documents.pins import newer
from sandbox_science.orchestrator.figurechecks import results as figure_results

from . import evidence, standing
from .models import (
    PREFIXES,
    ClearedFlag,
    EntryFinding,
    EntryFlag,
    EntryHistory,
    EntryLink,
    EntryPage,
    EntryRevision,
    EntrySummary,
    EntryView,
    Standing,
)
from .records import TEXTS, Row

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


def read_flags(db: sqlite3.Connection, entry: str) -> list[EntryFlag]:
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


def incoming_links(
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


def entry_history(
    db: sqlite3.Connection,
    project: str,
    reference: str,
    *,
    before: int | None = None,
    limit: int = 50,
) -> EntryHistory:
    """An entry's revisions, newest first, before revision ``before``; on the first
    page, the flags cleared on it, latest first, with who cleared them and why."""
    if not 1 <= limit <= 200:
        raise DocumentError(INVALID_REQUEST, "a page holds 1 to 200 revisions")
    row = find(db, project, reference)
    rows = db.execute(
        "SELECT r.revision,r.title,r.status,r.statement,r.author,r.reason,"
        "r.operation_id,r.created,r.provenance,(SELECT p.statement FROM ledger_revisions p "
        "WHERE p.entry_id=r.entry_id AND p.revision=r.revision-1),r.basis,r.anchors "
        "FROM ledger_revisions r WHERE r.entry_id=? AND r.revision<? "
        "ORDER BY r.revision DESC LIMIT ?",
        (row.entry_id, before or row.head + 1, limit + 1),
    ).fetchall()
    cleared = [
        ClearedFlag(
            flag_id=f[0],
            cause=f[1],
            source=json.loads(f[2]),
            via=f[3],
            revision=f[4],
            raised=f[5],
            cleared=f[6],
            cleared_by=json.loads(f[7]),
            reason=f[8],
            operation_id=f[9],
        )
        for f in db.execute(
            "SELECT flag_id,cause,source,via,revision,raised,cleared,cleared_by,reason,"
            "operation_id FROM ledger_flags WHERE entry_id=? AND cleared IS NOT NULL "
            "ORDER BY cleared DESC,rowid DESC LIMIT 50",
            (row.entry_id,),
        )
    ]
    revisions = [
        EntryRevision(
            revision=r[0],
            title=r[1],
            status=r[2],
            statement_changed=r[9] != r[3],
            author=json.loads(r[4]),
            reason=r[5],
            operation_id=r[6],
            provenance=None if r[8] is None else json.loads(r[8]),
            created=r[7],
            basis=r[10],
            anchors=json.loads(r[11] or "[]"),
        )
        for r in rows[:limit]
    ]
    return EntryHistory(
        entry_id=row.entry_id,
        handle=row.handle,
        revisions=revisions,
        next_before=revisions[-1].revision if len(rows) > limit else None,
        flags_cleared=[] if before is not None else cleared,
    )


def entry_page(
    db: sqlite3.Connection,
    project: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> EntryPage:
    """The project's entries in the order they were made, optionally of one type or
    one current status, after the entry ``after``."""
    if kind is not None and kind not in PREFIXES:
        raise DocumentError(INVALID_REQUEST, "unknown entry type")
    if not 1 <= limit <= 200:
        raise DocumentError(INVALID_REQUEST, "a page holds 1 to 200 entries")
    start = 0
    if after is not None:
        start = db.execute(
            "SELECT position FROM ledger_entries WHERE entry_id=?",
            (find(db, project, after).entry_id,),
        ).fetchone()[0]
    rows = db.execute(
        "SELECT e.entry_id,e.project,e.type,e.number,e.origin,e.head,"
        "r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created,"
        "r.basis,EXISTS(SELECT 1 FROM ledger_evidence v WHERE v.entry_id=e.entry_id "
        "AND v.revision=e.head),"
        "(SELECT count(*) FROM ledger_flags f WHERE f.entry_id=e.entry_id "
        "AND f.cleared IS NULL) "
        "FROM ledger_entries e JOIN ledger_revisions r ON r.entry_id=e.entry_id "
        "AND r.revision=e.head WHERE e.project=? AND e.position>? AND (? IS NULL OR "
        "e.type=?) AND (? IS NULL OR r.status=?) ORDER BY e.position LIMIT ?",
        (project, start, kind, kind, status, status, limit + 1),
    ).fetchall()
    found = [
        {
            **summary(
                Row(*r[:6]),
                r[6:],
                None if r[2] not in ("claim", "result") else not r[15],
                standing.of_entry(db, r[0], r[2], r[4], r[8], json.loads(r[10]), r[6]),
            ),
            "open_flags": r[16],
        }
        for r in rows[:limit]
    ]
    return EntryPage(
        entries=[EntrySummary(**s) for s in found],
        next_after=found[-1]["entry_id"] if len(rows) > limit else None,
    )


def entry_view(
    db: sqlite3.Connection,
    project: str,
    reference: str,
    revision: int | None = None,
    *,
    incoming_after: int = 0,
) -> EntryView:
    """An entry at ``revision`` (its current one by default); at its current revision,
    the links other entries hold to it, from the ``incoming_after``-th on."""
    if incoming_after < 0:
        raise DocumentError(INVALID_REQUEST, "incoming_after is a count of links")
    row = find(db, project, reference)
    number = row.head if revision is None else revision
    found = db.execute(REVISION, (row.entry_id, number)).fetchone()
    if found is None:
        raise DocumentError(NOT_FOUND, f"{row.handle} has no revision {number}")
    text = TEXTS.read(db, row.entry_id, found[3], full=False)
    assert text is not None  # noqa: S101 - written with its revision
    incoming, total = (
        incoming_links(db, project, row.entry_id, incoming_after) if number == row.head else ([], 0)
    )
    held = evidence.stored(db, row.entry_id, number)
    open_flags = read_flags(db, row.entry_id)
    fields = json.loads(found[4])
    label = (
        standing.of_entry(db, row.entry_id, row.type, row.origin, found[2], fields, number)
        if number == row.head
        else None
    )
    return EntryView(
        **summary(row, found, evidence.asserted_only(row.type, held), label),
        open_flags=len(open_flags),
        statement=text["body"],
        fields=fields,
        links=links(db, row.entry_id, number),
        evidence=held,
        incoming=incoming,
        incoming_total=total,
        findings=findings(db, project, row.entry_id),
        flags=open_flags,
        head=row.head,
        author=json.loads(found[5]),
        reason=found[6],
        anchors=json.loads(found[9] or "[]"),
        pins=newer(db, project, json.loads(found[10] or "[]")),
        figure_checks=figure_results(db, "statement", row.entry_id, number),
    )
