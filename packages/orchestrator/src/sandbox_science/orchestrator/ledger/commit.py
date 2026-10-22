# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger commit. The caller owns the transaction."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents.comment_bodies import prepare
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.pins import pin as pin_references
from sandbox_science.orchestrator.documents.pins import text_references
from sandbox_science.orchestrator.documents.project_macros import head as macro_head

from . import evidence, flags, frontier, queries, records
from .models import (
    PREFIXES,
    Mutation,
)
from .preparation import fingerprint as fingerprint
from .records import TEXTS, Json, Planned, Row


def affected_categories(
    db: sqlite3.Connection, planned: list[Planned], *, written: bool = True
) -> set[str]:
    """The frontier categories whose results a change made or revised, as they were
    and as it leaves them; ``written`` once it wrote its revisions."""
    found: set[str] = set()
    for p in planned:
        if p.row.type != "result":
            continue
        if p.fields.get("category"):
            found.add(p.fields["category"])
        if not p.created:
            before = db.execute(
                "SELECT json_extract(fields,'$.category') FROM ledger_revisions "
                "WHERE entry_id=? AND revision=?",
                (p.row.entry_id, p.row.head - 1 if written else p.row.head),
            ).fetchone()
            if before is not None and before[0]:
                found.add(before[0])
    return found


def baseline(db: sqlite3.Connection, project: str, categories: set[str], now: datetime) -> None:
    """Before a change, the best known results of each of ``categories`` whose history
    holds nothing yet, as it began: a category defined before the history was built
    (version 27) had them already (the review's L2)."""
    for name in categories:
        if (
            db.execute(
                "SELECT 1 FROM frontier_changes WHERE project=? AND category=? LIMIT 1",
                (project, name),
            ).fetchone()
            is None
        ):
            frontier.record(
                db, project, {name}, {"baseline": "frontier history begins"}, now.isoformat()
            )


def dominance_changes(db: sqlite3.Connection, planned: list[Planned]) -> list[str]:
    """The ``dominates`` links a change adds or drops, in words, the reason of each it
    adds cut short, as many as 8 (the review's M3)."""
    found: list[str] = []
    handles = {p.row.entry_id: p.row.handle for p in planned}

    def handle(entry: str) -> str:
        if entry not in handles:
            row = db.execute(
                "SELECT type,number FROM ledger_entries WHERE entry_id=?", (entry,)
            ).fetchone()
            handles[entry] = entry if row is None else f"{PREFIXES[row[0]]}-{row[1]}"
        return handles[entry]

    for p in planned:
        if p.row.type != "result" or p.links is None:
            continue
        final = {link.target_id for link in p.links if link.kind == "dominates"}
        for link in records.links_added(p, "dominates"):
            found.append(
                f"{p.row.handle} dominates {handle(link.target_id)}: "
                + records.clip(link.note or "", 100)
            )
        found += [
            f"{p.row.handle} no longer dominates {handle(link.target_id)}"
            for link in p.previous
            if link.kind == "dominates" and link.target_id not in final
        ]
    return found[:8]


def frontier_changes(
    db: sqlite3.Connection,
    project: str,
    categories: set[str],
    cause: Json,
    author: Author,
    now: datetime,
    *,
    events: EventLog,
) -> None:
    """Record what the change did to the best known results of ``categories``
    (LG-24), each category's changes under one ``ledger.frontier_changed`` event."""
    for name, changes in frontier.record(db, project, categories, cause, now.isoformat()):
        events.append(
            db,
            "ledger.frontier_changed",
            project,
            author.session_id,
            name,
            {
                "category": name,
                "operation_id": cause["operation_id"],
                "changes": [
                    {"conditions": c.conditions, "added": c.added, "removed": c.removed}
                    for c in changes
                ],
            },
            now,
        )


def write_entry(
    db: sqlite3.Connection,
    project: str,
    p: Planned,
    mutation: Mutation,
    author: Author,
    provenance: Json | None,
    now: datetime,
    *,
    events: EventLog,
) -> None:
    row = p.row
    if p.created:
        # position orders the project's entries as they were made.
        db.execute(
            "INSERT INTO ledger_entries(entry_id,project,type,number,origin,head,position) "
            "VALUES(?,?,?,?,?,1,(SELECT coalesce(max(position),0)+1 FROM ledger_entries))",
            (row.entry_id, project, row.type, row.number, row.origin),
        )
    else:
        db.execute("UPDATE ledger_entries SET head=head+1 WHERE entry_id=?", (row.entry_id,))
    before = row.head
    row.head += 1
    statement = write_statement(db, row, p.statement, author)
    pins = p.pins
    if pins is None:
        stored = (
            db.execute(
                "SELECT pins FROM ledger_revisions WHERE entry_id=? AND revision=?",
                (row.entry_id, before),
            ).fetchone()
            if before
            else None
        )
        previous: list[Json] = json.loads(stored[0]) if stored and stored[0] else []
        if p.statement is None:
            # The statement is unchanged: so are its pins, whoever revises the entry.
            pins = previous
        else:
            pins, _ = pin_references(
                db,
                project,
                author.session_id,
                text_references(p.statement, "statement"),
                previous,
            )
    db.execute(
        "INSERT INTO ledger_revisions(entry_id,revision,title,status,statement,fields,"
        "author,reason,operation_id,provenance,created,basis,anchors,pins) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row.entry_id,
            row.head,
            p.title,
            p.status,
            statement,
            records.encoded(p.fields),
            author.model_dump_json(exclude_none=True),
            mutation.reason,
            mutation.operation_id,
            None if provenance is None else json.dumps(provenance, sort_keys=True),
            now.isoformat(),
            p.basis,
            records.anchors_json(p.anchors),
            json.dumps(pins, sort_keys=True) if pins else None,
        ),
    )
    held = p.evidence
    if held is None:  # kept from the revision before
        held = evidence.stored(db, row.entry_id, before) if before else []
    evidence.write(db, row.entry_id, row.head, held)
    links = p.links
    if links is None:  # kept from the revision before
        links = queries.links(db, row.entry_id, before)
    for ordinal, link in enumerate(links, 1):
        db.execute(
            "INSERT INTO ledger_links(entry_id,revision,ordinal,kind,target,target_id,"
            "target_revision,block,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row.entry_id,
                row.head,
                ordinal,
                link.kind,
                link.target,
                link.target_id,
                link.revision,
                link.block,
                link.note,
            ),
        )
    payload: Json = {
        "entry": row.entry_id,
        "handle": row.handle,
        "type": row.type,
        "revision": row.head,
        "status": p.status,
        "operation_id": mutation.operation_id,
    }
    if not p.created:
        payload.update(status_before=p.status_before, changed=p.changed)
    events.append(
        db,
        "ledger.entry_created" if p.created else "ledger.entry_revised",
        project,
        author.session_id,
        row.entry_id,
        payload,
        now,
    )


def write_statement(db: sqlite3.Connection, row: Row, text: str | None, author: Author) -> int:
    """The text revision of the entry's statement: a new one when it changed, queued
    for its math check (LG-1, LD-53) with the project macros it was saved with."""
    latest = TEXTS.read(db, row.entry_id)
    if text is None:
        assert latest is not None  # noqa: S101 - an existing entry has a statement
        return latest["revision"]
    prepared = prepare(text, author, latest)
    revision = prepared.base + 1
    TEXTS.append(
        db,
        row.entry_id,
        prepared.revision(revision, author, macro_revision=macro_head(db, row.project)),
    )
    db.execute(
        "INSERT INTO text_math_sources(subject_type,subject_id,project,target,blocked) "
        "VALUES('statement',?,?,?,?) ON CONFLICT(subject_type,subject_id) DO UPDATE SET "
        "target=excluded.target,blocked=excluded.blocked",
        (row.entry_id, row.project, revision, prepared.held),
    )
    return revision


def flag_change(p: Planned) -> flags.Changed:
    """``p`` as the flags read it, once written: what it changed, and the links and
    evidence to other entries its revision gains."""
    before = {(link.kind, link.target_id, link.revision) for link in p.previous}
    pins = [
        (link.kind, link.target_id, link.revision)
        for link in (p.links if p.links is not None else [])
        if link.kind in ("assumes", "builds_on")
        and link.target == "entry"
        and (link.kind, link.target_id, link.revision) not in before
    ]
    pins += [
        ("evidence", link.target_id, link.revision)
        for link in (p.evidence or [])
        if link.target == "entry" and link.added == p.row.head
    ]
    return flags.Changed(
        p.row.entry_id,
        p.row.handle,
        p.row.type,
        p.row.head,
        not p.created and "statement" in p.changed,
        p.status,
        p.status_before,
        tuple(pin for pin in dict.fromkeys(pins) if pin[1] != p.row.entry_id),
    )
