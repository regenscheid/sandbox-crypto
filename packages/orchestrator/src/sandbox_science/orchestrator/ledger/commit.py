# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger commit. The caller owns the transaction."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Final

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents.comment_bodies import prepare
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.pins import pin as pin_references
from sandbox_science.orchestrator.documents.pins import text_references
from sandbox_science.orchestrator.documents.project_macros import head as macro_head
from sandbox_science.orchestrator.projects import run_id_of

from . import evidence, flags, frontier, gate, preparation, queries, records, standing
from . import provenance as anchoring
from .models import (
    MAX_CLEARED,
    MAX_ENTRY_WORK,
    PREFIXES,
    WORK_HOLDERS,
    Mutation,
    WorkReference,
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


def record_work(
    db: sqlite3.Connection,
    project: str,
    mutation: Mutation,
    author: Author,
    keys: dict[str, Row],
    pinned: dict[evidence.Block, str],
    provenance: Json | None,
    now: str,
    *,
    events: EventLog,
) -> tuple[list[str], list[flags.Raised]]:
    """Append the change's work records (LG-6), after its entries are written, so
    that one may be on an entry the change creates: each on an issue, a claim or a
    result, at the revision the change leaves it, with its references pinned. Any
    role that changes the ledger records work (LG-13); a record changes no status."""
    recorded: list[str] = []
    rejections: list[flags.Raised] = []
    named: dict[str, str] = {}
    for number, item in enumerate(mutation.work or (), 1):
        where = f"work {number}"
        row = work_entry(db, project, item.entry, keys)
        if row.type not in WORK_HOLDERS:
            raise DocumentError(
                INVALID_REQUEST,
                f"{where} is on {row.handle}: work is recorded on an issue, a claim or a result",
            )
        (held,) = db.execute(
            "SELECT count(*) FROM ledger_work WHERE entry_id=?", (row.entry_id,)
        ).fetchone()
        if held >= MAX_ENTRY_WORK:
            raise DocumentError(
                POLICY_DENIED,
                f"{row.handle} holds {MAX_ENTRY_WORK} work records, the most an entry "
                "holds: record further work on the entries it produced, or a new issue",
            )
        produced = [
            resolve_produced(db, project, e, keys, row, item.references)
            for e in item.produced or ()
        ]
        part_of = None if item.part_of is None else work_id(db, project, item.part_of, named)
        detail: Json = {}
        if item.limit is not None:
            detail["limit"] = item.limit
        if produced:
            detail["produced"] = [p.entry_id for p in produced]
        assessed = None
        if item.assesses is not None:
            assessed = work_id(db, project, item.assesses, named)
            (on,) = db.execute(
                "SELECT entry_id FROM ledger_work WHERE record_id=?", (assessed,)
            ).fetchone()
            if on != row.entry_id:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{where} assesses work on another entry: record the assessment on "
                    "the entry the work it assesses is on",
                )
        cited = [
            resolve_work_references(db, project, ref, named, pinned) for ref in item.references
        ]
        basis, anchors = preparation.resolve_sources(
            db, project, item.basis, item.anchors or [], author, pinned, provenance
        )
        if author.kind != "user" and not preparation.platform_provenance(provenance):
            rests = [c for c in cited if "work" not in c]
            if not anchors and not rests:
                raise anchoring.refused(
                    f"{where} names no anchor: a message by its ID, a tool result by its "
                    "call, an artifact, a document block or an entry revision, or a "
                    "reference to a delegation, an artifact or a document"
                )
            observed = [*anchors, *(c for c in cited if "artifact" in c)]
            anchoring.check(db, project, basis, observed, [], [item.tried], where)
        record = new_id("work")
        if item.key is not None:
            if item.key in named:
                raise DocumentError(INVALID_REQUEST, f"two work records are keyed {item.key}")
            named[item.key] = record
        (head,) = db.execute(
            "SELECT head FROM ledger_entries WHERE entry_id=?", (row.entry_id,)
        ).fetchone()
        db.execute(
            "INSERT INTO ledger_work(record_id,position,project,entry_id,entry_revision,"
            "tried,ending,detail,refs,part_of,assesses,verdict,author,reason,"
            "operation_id,provenance,created,basis,anchors) VALUES(?,"
            "(SELECT coalesce(max(position),0)+1 FROM ledger_work),"
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record,
                project,
                row.entry_id,
                head,
                preparation.normalized(item.tried),
                item.ending,
                json.dumps(detail, sort_keys=True, ensure_ascii=False),
                json.dumps(cited, sort_keys=True, ensure_ascii=False),
                part_of,
                assessed,
                item.verdict,
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                None if provenance is None else json.dumps(provenance, sort_keys=True),
                now,
                basis,
                records.anchors_json(anchors),
            ),
        )
        events.append(
            db,
            "ledger.work_recorded",
            project,
            author.session_id,
            row.entry_id,
            {
                "record": record,
                "entry": row.entry_id,
                "handle": row.handle,
                "ending": item.ending,
                "operation_id": mutation.operation_id,
            },
            datetime.fromisoformat(now),
        )
        recorded.append(record)
        if (
            assessed is not None
            and item.verdict != "accepted"
            and author.kind == "user"
            and queries.rests_on(db, row.entry_id, assessed)
        ):
            # The user no longer accepts the claim an obstruction rests on (LG-7).
            found = flags.raise_one(
                db,
                project,
                row.entry_id,
                "evidence_changed",
                {
                    "entry": row.entry_id,
                    "handle": row.handle,
                    "work": record,
                    "verdict": item.verdict,
                },
                now,
            )
            if found is not None:
                rejections.append(found)
    return recorded, rejections


def record_reopened(
    db: sqlite3.Connection,
    project: str,
    planned: list[Planned],
    mutation: Mutation,
    author: Author,
    provenance: Json | None,
    now: datetime,
    *,
    events: EventLog,
) -> list[str]:
    """A work record on each obstructed issue an agent's change reopens (the owner,
    2026-10-15): reopening weakens a settled state and needs no decision, and is kept
    with the change's reason, citing the decision that had made the issue obstructed."""
    made: list[str] = []
    for p in planned:
        if author.kind == "user" or p.status_before != "obstructed" or p.status == p.status_before:
            continue
        decider = standing.decider(db, p.row.entry_id, "obstructed")
        cited = "" if decider is None else f", which {decider.handle} had made obstructed"
        record = new_id("work")
        db.execute(
            "INSERT INTO ledger_work(record_id,position,project,entry_id,entry_revision,"
            "tried,ending,detail,refs,author,reason,operation_id,provenance,created,"
            "basis,anchors) VALUES(?,(SELECT coalesce(max(position),0)+1 FROM ledger_work),"
            "?,?,?,?,'reopened',?,'[]',?,?,?,?,?,'interpreted',?)",
            (
                record,
                project,
                p.row.entry_id,
                p.row.head,
                preparation.normalized(f"Reopened {p.row.handle}{cited}: {mutation.reason}"),
                json.dumps({} if decider is None else {"decision": decider.entry_id}),
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                None if provenance is None else json.dumps(provenance, sort_keys=True),
                now.isoformat(),
                records.anchors_json(
                    []
                    if decider is None
                    else [{"entry": decider.entry_id, "revision": decider.revision}]
                ),
            ),
        )
        events.append(
            db,
            "ledger.work_recorded",
            project,
            author.session_id,
            p.row.entry_id,
            {
                "record": record,
                "entry": p.row.entry_id,
                "handle": p.row.handle,
                "ending": "reopened",
                "operation_id": mutation.operation_id,
            },
            now,
        )
        made.append(record)
    return made


def resolve_produced(
    db: sqlite3.Connection,
    project: str,
    reference: str,
    keys: dict[str, Row],
    on: Row,
    cited: list[WorkReference],
) -> Row:
    """An entry a work record says it produced (LG-6): one the same change creates,
    or one the delegation the record cites created (its child run wrote the entry's
    first revision); never the entry the record is on."""
    found = work_entry(db, project, reference, keys)
    if found.entry_id == on.entry_id:
        raise DocumentError(INVALID_REQUEST, f"a work record on {on.handle} names it as produced")
    if reference.startswith("new:"):
        return found
    # A delegation names its child by the run's record name, its start time and its
    # ID; an entry's author names the run by its ID.
    runs = {
        found
        for ref in cited
        if ref.delegation is not None
        for (run,) in db.execute(
            "SELECT run FROM delegations WHERE delegation_id=? AND project_id=?",
            (ref.delegation, project),
        )
        for found in (run, run_id_of(run))
        if found is not None
    }
    first = db.execute(
        "SELECT json_extract(author,'$.run_id') FROM ledger_revisions "
        "WHERE entry_id=? AND revision=1",
        (found.entry_id,),
    ).fetchone()
    if first is None or first[0] not in runs:
        raise DocumentError(
            INVALID_REQUEST,
            f"{found.handle} is named as produced: a work record names the entries its "
            "change creates (new:<key>), or those the delegation it cites created",
        )
    return found


def work_entry(db: sqlite3.Connection, project: str, reference: str, keys: dict[str, Row]) -> Row:
    """The entry a work record names: one the change creates, by ``new:<key>``, or
    one of the project's."""
    if reference.startswith("new:"):
        found = keys.get(reference.removeprefix("new:"))
        if found is None:
            raise DocumentError(INVALID_REQUEST, f"no new entry is keyed {reference}")
        return found
    return queries.find(db, project, reference)


def resolve_work_references(
    db: sqlite3.Connection,
    project: str,
    ref: WorkReference,
    named: dict[str, str],
    pinned: dict[evidence.Block, str],
) -> Json:
    """A work record's reference, pinned: a delegation of the project that has
    ended, an artifact revision whose bytes the platform keeps, with its SHA-256, a
    document block at its revision, by the document's ID, or a work record."""
    cited: Json = {}
    if ref.delegation is not None:
        found = db.execute(
            "SELECT ended FROM delegations WHERE delegation_id=? AND project_id=?",
            (ref.delegation, project),
        ).fetchone()
        if found is None:
            raise DocumentError(NOT_FOUND, f"no delegation {ref.delegation} in this project")
        if found[0] is None:
            raise DocumentError(
                CONFLICT, f"delegation {ref.delegation} has not ended: cite its outcome after"
            )
        cited["delegation"] = ref.delegation
    elif ref.artifact is not None:
        assert ref.revision is not None  # noqa: S101 - the model requires it
        digest = evidence.retained(db, project, ref.artifact, ref.revision, ref.sha256)
        cited.update(artifact=ref.artifact, revision=ref.revision, sha256=digest)
    elif ref.document is not None:
        assert ref.revision is not None  # noqa: S101 - the model requires it
        assert ref.block is not None  # noqa: S101 - the model requires it
        document = pinned[(ref.document, ref.revision, ref.block, None)]
        cited.update(document=document, revision=ref.revision, block=ref.block)
    else:
        assert ref.work is not None  # noqa: S101 - one target is named
        cited["work"] = work_id(db, project, ref.work, named)
    if ref.note is not None:
        cited["note"] = ref.note
    return cited


def work_id(db: sqlite3.Connection, project: str, reference: str, named: dict[str, str]) -> str:
    """The work record ``reference`` names: one the change records before it, by
    ``new:<key>``, or one of the project's, by its ID."""
    if reference.startswith("new:"):
        found = named.get(reference.removeprefix("new:"))
        if found is None:
            raise DocumentError(
                INVALID_REQUEST, f"no work record the change records before it is keyed {reference}"
            )
        return found
    if (
        db.execute(
            "SELECT 1 FROM ledger_work WHERE record_id=? AND project=?", (reference, project)
        ).fetchone()
        is None
    ):
        raise DocumentError(NOT_FOUND, f"no work record {reference} in this project")
    return reference


def clear_flags(
    db: sqlite3.Connection,
    project: str,
    mutation: Mutation,
    author: Author,
    role: str,
    now: datetime,
    *,
    events: EventLog,
) -> list[str]:
    """Clear the open flags the mutation names, by ID or by entry and cause, with its
    reason (LG-29): the user's or the main agent's to clear, never a delegated
    agent's. One event records them all."""
    if not mutation.flags_cleared and not mutation.flags_cleared_on:
        return []
    if role == "delegated":
        raise records.refused("a delegated agent clears no flag")
    found: dict[str, tuple[str, str, str, str]] = {}
    for flag in mutation.flags_cleared or ():
        row = db.execute(
            "SELECT f.entry_id,f.cleared,f.cause,e.type,e.number,f.source "
            "FROM ledger_flags f JOIN ledger_entries e USING(entry_id) "
            "WHERE f.flag_id=? AND f.project=?",
            (flag, project),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, f"no flag {flag} in this project")
        if row[1] is not None:
            raise DocumentError(CONFLICT, f"flag {flag} is cleared already")
        found[flag] = (
            row[0],
            f"{PREFIXES[row[3]]}-{row[4]}",
            row[2],
            json.loads(row[5])["handle"],
        )
    for scope in mutation.flags_cleared_on or ():
        entry = queries.find(db, project, scope.entry)
        rows = db.execute(
            "SELECT flag_id,cause,source FROM ledger_flags WHERE entry_id=? "
            "AND cleared IS NULL AND (? IS NULL OR cause=?) ORDER BY raised,rowid LIMIT ?",
            (entry.entry_id, scope.cause, scope.cause, MAX_CLEARED + 1),
        ).fetchall()
        if not rows:
            raise DocumentError(
                CONFLICT,
                f"{entry.handle} has no open flag"
                + (f" of cause {scope.cause}" if scope.cause else ""),
            )
        for flag, cause, source in rows:
            found[flag] = (entry.entry_id, entry.handle, cause, json.loads(source)["handle"])
    if len(found) > MAX_CLEARED:
        raise DocumentError(INVALID_REQUEST, f"a change clears at most {MAX_CLEARED} flags")
    for flag, (entry, handle, _, _) in found.items() if role == "main" else ():
        # The owner's rule (2026-10-15): clearing a flag on a settled entry settles it
        # again, which needs the user or a passing gate.
        (revision, kind) = db.execute(
            "SELECT f.revision,e.type FROM ledger_flags f JOIN ledger_entries e "
            "USING(entry_id) WHERE f.flag_id=?",
            (flag,),
        ).fetchone()
        if not standing.settled(db, entry, revision):
            continue
        if kind not in ("claim", "result"):
            raise records.refused(
                f"{handle} was settled when the flag was raised or since: only the user "
                "clears a flag on a settled issue or decision"
            )
        if not gate.fresh_check(db, entry, flag):
            raise records.refused(
                f"{handle} is settled, and clearing its flag settles it again: that needs "
                "the user, or a delegation's check of it at its current revision, from a "
                "brief that named the flag, started since it was last weakened, that "
                "counts; ask for one, or leave the flag for the user"
            )
    for flag in found:
        db.execute(
            "UPDATE ledger_flags SET cleared=?,cleared_by=?,reason=?,operation_id=? "
            "WHERE flag_id=?",
            (
                now.isoformat(),
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                flag,
            ),
        )
    announce_flags(
        db,
        project,
        author,
        "ledger.flags_cleared",
        mutation,
        [(flag, *rest) for flag, rest in found.items()],
        now,
        events=events,
    )
    return list(found)


def reopen_flags(
    db: sqlite3.Connection,
    project: str,
    mutation: Mutation,
    author: Author,
    role: str,
    now: datetime,
    *,
    events: EventLog,
) -> list[str]:
    """Open cleared flags again, as new flags that name them (LG-29; settled question
    2: the user can reverse what the main agent did). The user's only."""
    if not mutation.flags_reopened:
        return []
    if role != "user":
        raise records.refused("only the user reopens a flag")
    opened: list[str] = []
    reopened: list[tuple[str, str, str, str, str]] = []
    for old in dict.fromkeys(mutation.flags_reopened):
        row = db.execute(
            "SELECT f.entry_id,f.cleared,f.cause,f.source,f.via,f.key,e.head,e.type,"
            "e.number FROM ledger_flags f JOIN ledger_entries e USING(entry_id) "
            "WHERE f.flag_id=? AND f.project=?",
            (old, project),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, f"no flag {old} in this project")
        entry, cleared, cause, source, via, same, head, kind, number = row
        if cleared is None:
            raise DocumentError(CONFLICT, f"flag {old} is open")
        flag = new_id("flag")
        inserted = db.execute(
            "INSERT INTO ledger_flags(flag_id,project,entry_id,revision,cause,source,via,"
            "reopens,key,raised) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (flag, project, entry, head, cause, source, via, old, same, now.isoformat()),
        ).rowcount
        if not inserted:
            raise DocumentError(CONFLICT, f"a flag like {old} is open on its entry again")
        opened.append(flag)
        handle = f"{PREFIXES[kind]}-{number}"
        reopened.append((flag, entry, handle, cause, json.loads(source)["handle"]))
    announce_flags(
        db, project, author, "ledger.flags_reopened", mutation, reopened, now, events=events
    )
    return opened


def announce_flags(
    db: sqlite3.Connection,
    project: str,
    author: Author,
    kind: str,
    mutation: Mutation,
    found: list[tuple[str, str, str, str, str]],
    now: datetime,
    *,
    events: EventLog,
) -> None:
    """One event for the flags a change raised, cleared or reopened: as many as
    ``LISTED_FLAGS`` of them, each its ID, entry, handle, cause and source, with
    their count, the change's reason and the run (LG-29, LG-33)."""
    if not found:
        return
    listed = [
        {"flag": flag, "entry": entry, "handle": handle, "cause": cause, "from": source}
        for flag, entry, handle, cause, source in found[:LISTED_FLAGS]
    ]
    events.append(
        db,
        kind,
        project,
        author.session_id,
        found[0][1],
        {
            "operation_id": mutation.operation_id,
            "reason": mutation.reason,
            "run": author.run_id,
            "total": len(found),
            "flags": listed,
        },
        now,
    )


LISTED_FLAGS: Final = 500
