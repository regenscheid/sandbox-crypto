# SPDX-License-Identifier: MIT
"""The project ledger's entries and revisions in the control store
(docs/specs/ledger.md).

A mutation creates or revises one or more entries, all or nothing, under an operation
ID whose receipt replays its first result (LG-30, LG-31). Each revision names the
expected revision it follows; a stale one is a conflict naming the current revision,
and nothing is rebased. Each change commits with its event (LG-33). Who may change
what follows LG-13: the user anything, the main agent any entry, a delegated agent
only new entries in their first status; code in the sandbox nothing. Statuses that
only a decision sets (LG-4), the statuses that need links, and published entries,
which need evidence, are refused until those are built.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control import EventLog, Receipts
from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.comment_bodies import prepare
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.normalize import normalize
from sandbox_science.orchestrator.projects import ProjectError, recorded

from .models import (
    DECIDED,
    FIELDS,
    PREFIXES,
    STATUSES,
    ChangedEntry,
    EntryChange,
    EntryHistory,
    EntryPage,
    EntrySummary,
    EntryView,
    Mutation,
    MutationResult,
    ReferenceFields,
    RevisionSummary,
)

Json = dict[str, Any]

TEXTS: Final = TextRevisions("statement")
"""Each entry's statement, a text revision under the entry's ID."""

RECEIPTS: Final = Receipts("ledger", "operation ID {operation!r} names another ledger change")

_ID: Final = re.compile(r"entry-[0-9a-f]{24}")
_HANDLE: Final = re.compile(r"(I|C|R|A|D|T|REF)-([1-9][0-9]{0,8})")
_TYPES: Final = {prefix: kind for kind, prefix in PREFIXES.items()}
_REVISION: Final = (
    "SELECT r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created "
    "FROM ledger_revisions r WHERE r.entry_id=? AND r.revision=?"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _first_status(kind: str, origin: str | None) -> str:
    if kind in ("claim", "result"):
        return "recorded" if origin == "published" else "proposed"
    return STATUSES[kind][0]


def _refused(message: str) -> DocumentError:
    return DocumentError(POLICY_DENIED, f"not_permitted_for_role: {message}")


@dataclass
class _Row:
    entry_id: str
    project: str
    type: str
    number: int
    origin: str | None
    head: int

    @property
    def handle(self) -> str:
        return f"{PREFIXES[self.type]}-{self.number}"


@dataclass
class _Planned:
    """One change of a mutation, resolved: the entry it makes or revises, and what the
    new revision holds."""

    change: EntryChange
    row: _Row
    created: bool
    title: str = ""
    statement: str | None = None
    status: str = ""
    status_before: str | None = None
    fields: Json = field(default_factory=dict[str, Any])
    changed: list[str] = field(default_factory=list[str])


class Ledger:
    """A project's ledger, on the documents' control store."""

    def __init__(self, documents: DocumentStore, *, clock: Callable[[], datetime] = _now) -> None:
        self.documents, self.control, self.clock = documents, documents.control, clock
        self.events = EventLog(documents.control, "ledger_entry", documents.producer)

    # Reads.

    def entry(self, project: str, reference: str, revision: int | None = None) -> EntryView:
        """An entry at ``revision`` (its current one by default)."""
        self._project(project)
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            number = row.head if revision is None else revision
            found = db.execute(_REVISION, (row.entry_id, number)).fetchone()
            if found is None:
                raise DocumentError(NOT_FOUND, f"{row.handle} has no revision {number}")
            text = TEXTS.read(db, row.entry_id, found[3], full=False)
            assert text is not None  # noqa: S101 - written with its revision
            return EntryView(
                **self._summary(row, found),
                statement=text["body"],
                fields=json.loads(found[4]),
                head=row.head,
                author=json.loads(found[5]),
                reason=found[6],
            )

    def entries(
        self,
        project: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> EntryPage:
        """The project's entries in the order they were made, optionally of one type or
        one current status, after the entry ``after``."""
        self._project(project)
        if kind is not None and kind not in PREFIXES:
            raise DocumentError(INVALID_REQUEST, "unknown entry type")
        if not 1 <= limit <= 200:
            raise DocumentError(INVALID_REQUEST, "a page holds 1 to 200 entries")
        if self.control.missing:
            return EntryPage(entries=[], next_after=None)
        with self.control.read() as tx:
            db = tx.sql
            start = 0
            if after is not None:
                start = db.execute(
                    "SELECT rowid FROM ledger_entries WHERE entry_id=?",
                    (self._find(db, project, after).entry_id,),
                ).fetchone()[0]
            rows = db.execute(
                "SELECT e.entry_id,e.project,e.type,e.number,e.origin,e.head,"
                "r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created "
                "FROM ledger_entries e JOIN ledger_revisions r ON r.entry_id=e.entry_id "
                "AND r.revision=e.head WHERE e.project=? AND e.rowid>? AND (? IS NULL OR "
                "e.type=?) AND (? IS NULL OR r.status=?) ORDER BY e.rowid LIMIT ?",
                (project, start, kind, kind, status, status, limit + 1),
            ).fetchall()
        found = [self._summary(_Row(*r[:6]), r[6:]) for r in rows[:limit]]
        return EntryPage(
            entries=[EntrySummary(**s) for s in found],
            next_after=found[-1]["entry_id"] if len(rows) > limit else None,
        )

    def history(
        self, project: str, reference: str, *, before: int | None = None, limit: int = 50
    ) -> EntryHistory:
        """An entry's revisions, newest first, before revision ``before``."""
        self._project(project)
        if not 1 <= limit <= 200:
            raise DocumentError(INVALID_REQUEST, "a page holds 1 to 200 revisions")
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            rows = db.execute(
                "SELECT r.revision,r.title,r.status,r.statement,r.author,r.reason,"
                "r.operation_id,r.created,r.provenance,(SELECT p.statement FROM ledger_revisions p "
                "WHERE p.entry_id=r.entry_id AND p.revision=r.revision-1) "
                "FROM ledger_revisions r WHERE r.entry_id=? AND r.revision<? "
                "ORDER BY r.revision DESC LIMIT ?",
                (row.entry_id, before or row.head + 1, limit + 1),
            ).fetchall()
        revisions = [
            RevisionSummary(
                revision=r[0],
                title=r[1],
                status=r[2],
                statement_changed=r[9] != r[3],
                author=json.loads(r[4]),
                reason=r[5],
                operation_id=r[6],
                provenance=None if r[8] is None else json.loads(r[8]),
                created=r[7],
            )
            for r in rows[:limit]
        ]
        return EntryHistory(
            entry_id=row.entry_id,
            handle=row.handle,
            revisions=revisions,
            next_before=revisions[-1].revision if len(rows) > limit else None,
        )

    # Changes.

    def change(
        self, project: str, raw: object, author: Author, *, provenance: Json | None = None
    ) -> MutationResult:
        """Apply one mutation (LG-30), or replay its first result (LG-31)."""
        try:
            mutation = Mutation.model_validate(raw)
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, _first_error(error)) from None
        return self.apply(project, [(mutation, provenance)], author)[0]

    def apply(
        self, project: str, mutations: list[tuple[Mutation, Json | None]], author: Author
    ) -> list[MutationResult]:
        """Apply several mutations in one change, all or none; each keeps its own
        operation ID and receipt. ``provenance`` names where a mutation came from, such
        as the line of a results file (LG-41)."""
        role = self._authority(project, author)
        with self.control.write() as tx:
            return [
                self._apply(tx.sql, project, mutation, author, role, provenance)
                for mutation, provenance in mutations
            ]

    def _apply(
        self,
        db: sqlite3.Connection,
        project: str,
        mutation: Mutation,
        author: Author,
        role: str,
        provenance: Json | None,
    ) -> MutationResult:
        fingerprint = hashlib.sha256(
            json.dumps(mutation.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        replay = RECEIPTS.find(db, project, author.actor, mutation.operation_id, fingerprint)
        if replay is not None:
            return MutationResult.model_validate({**replay, "replayed": True})
        numbers: dict[str, int] = {}
        planned = [self._plan(db, project, change, role, numbers) for change in mutation.changes]
        ids = [p.row.entry_id for p in planned]
        if len(set(ids)) != len(ids):
            raise DocumentError(INVALID_REQUEST, "a mutation changes an entry once")
        now = self.clock().isoformat()
        changed: list[ChangedEntry] = []
        for p in planned:
            self._write(db, project, p, mutation, author, provenance, now)
            changed.append(
                ChangedEntry(
                    entry_id=p.row.entry_id,
                    handle=p.row.handle,
                    revision=p.row.head,
                    status=p.status,
                    created=p.created,
                )
            )
        result = MutationResult(operation_id=mutation.operation_id, entries=changed)
        RECEIPTS.record(
            db,
            project,
            author.actor,
            mutation.operation_id,
            fingerprint,
            result.model_dump(mode="json"),
        )
        return result

    def _plan(
        self,
        db: sqlite3.Connection,
        project: str,
        change: EntryChange,
        role: str,
        numbers: dict[str, int],
    ) -> _Planned:
        if change.entry is None:
            return self._plan_create(db, project, change, numbers)
        if change.type is not None or change.origin is not None:
            raise DocumentError(INVALID_REQUEST, "a revision keeps the entry's type and origin")
        if change.expected is None:
            raise DocumentError(INVALID_REQUEST, "a revision names the revision it expects")
        row = self._find(db, project, change.entry)
        if role == "delegated":
            raise _refused("a delegated agent creates entries but revises none")
        if change.expected != row.head:
            raise DocumentError(
                CONFLICT,
                f"{row.handle} is at revision {row.head}, not {change.expected}; read it again",
                {"entry": row.entry_id, "current": row.head},
            )
        current = db.execute(_REVISION, (row.entry_id, row.head)).fetchone()
        planned = _Planned(
            change,
            row,
            False,
            title=current[1],
            status=current[2],
            status_before=current[2],
            fields=json.loads(current[4]),
        )
        if change.title is not None and change.title != current[1]:
            planned.title, planned.changed = change.title, [*planned.changed, "title"]
        if change.statement is not None:
            text = _normalized(change.statement)
            saved = TEXTS.read(db, row.entry_id, current[3], full=False)
            if saved is None or text != saved["body"]:
                planned.statement, planned.changed = text, [*planned.changed, "statement"]
        if change.fields is not None:
            fields = _fields(row.type, change.fields)
            if fields != planned.fields:
                planned.fields, planned.changed = fields, [*planned.changed, "fields"]
        if change.status is not None and change.status != current[2]:
            planned.status = _status(row, change.status)
            planned.changed = [*planned.changed, "status"]
        if not planned.changed:
            raise DocumentError(INVALID_REQUEST, f"the change to {row.handle} changes nothing")
        return planned

    def _plan_create(
        self, db: sqlite3.Connection, project: str, change: EntryChange, numbers: dict[str, int]
    ) -> _Planned:
        if change.type is None or change.expected is not None:
            raise DocumentError(INVALID_REQUEST, "a new entry names its type and no revision")
        if change.title is None or change.statement is None:
            raise DocumentError(INVALID_REQUEST, "a new entry has a title and a statement")
        kind = change.type
        if (kind in ("claim", "result")) != (change.origin is not None):
            raise DocumentError(
                INVALID_REQUEST, "claims and results, and only they, have an origin"
            )
        if change.origin == "published":
            # LG-15: a published entry cites its reference through evidence, built next.
            raise DocumentError(
                INVALID_REQUEST, "published claims and results come with evidence links"
            )
        status = _first_status(kind, change.origin)
        if change.status is not None and change.status != status:
            raise DocumentError(INVALID_REQUEST, f"a new {kind} starts {status}")
        # Handles count per project and type; a mutation's new entries take the next ones.
        number = (
            numbers.get(kind)
            or db.execute(
                "SELECT coalesce(max(number),0) FROM ledger_entries WHERE project=? AND type=?",
                (project, kind),
            ).fetchone()[0]
        )
        number = numbers[kind] = number + 1
        row = _Row(new_id("entry"), project, kind, number, change.origin, 0)
        return _Planned(
            change,
            row,
            True,
            title=change.title,
            statement=_normalized(change.statement),
            status=status,
            fields=_fields(kind, change.fields or {}),
        )

    def _write(
        self,
        db: sqlite3.Connection,
        project: str,
        p: _Planned,
        mutation: Mutation,
        author: Author,
        provenance: Json | None,
        now: str,
    ) -> None:
        row = p.row
        if p.created:
            db.execute(
                "INSERT INTO ledger_entries(entry_id,project,type,number,origin,head) "
                "VALUES(?,?,?,?,?,1)",
                (row.entry_id, project, row.type, row.number, row.origin),
            )
        else:
            db.execute("UPDATE ledger_entries SET head=head+1 WHERE entry_id=?", (row.entry_id,))
        row.head += 1
        statement = self._statement(db, row, p.statement, author)
        db.execute(
            "INSERT INTO ledger_revisions(entry_id,revision,title,status,statement,fields,"
            "author,reason,operation_id,provenance,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.entry_id,
                row.head,
                p.title,
                p.status,
                statement,
                json.dumps(p.fields, sort_keys=True),
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                None if provenance is None else json.dumps(provenance, sort_keys=True),
                now,
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
        self.events.append(
            db,
            "ledger.entry_created" if p.created else "ledger.entry_revised",
            project,
            author.session_id,
            row.entry_id,
            payload,
            self.clock(),
        )

    def _statement(
        self, db: sqlite3.Connection, row: _Row, text: str | None, author: Author
    ) -> int:
        """The text revision of the entry's statement: a new one when it changed."""
        latest = TEXTS.read(db, row.entry_id)
        if text is None:
            assert latest is not None  # noqa: S101 - an existing entry has a statement
            return latest["revision"]
        prepared = prepare(text, author, latest)
        TEXTS.append(db, row.entry_id, prepared.revision(prepared.base + 1, author))
        return prepared.base + 1

    # Checks.

    def _authority(self, project: str, author: Author) -> str:
        """The role LG-13 gives ``author``: ``user``, ``main`` or ``delegated``. Code in
        the sandbox, imports, the platform and other agent roles change nothing."""
        self._project(project)
        try:
            sessions = recorded(self.documents.path.parent, project).sessions
        except ProjectError:
            raise DocumentError(NOT_FOUND, "no such ledger project") from None
        if author.kind == "user":
            return "user"
        if author.kind != "agent" or author.role not in ("main", "delegated"):
            raise _refused(f"a {author.role or author.kind} does not change the ledger")
        if author.session_id not in sessions:
            raise DocumentError(NOT_FOUND, "the agent's session is not in this project")
        return cast(str, author.role)

    @staticmethod
    def _project(project: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None:
            raise DocumentError(INVALID_REQUEST, "invalid ledger project")

    @staticmethod
    def _find(db: sqlite3.Connection, project: str, reference: str) -> _Row:
        if _ID.fullmatch(reference):
            row = db.execute(
                "SELECT entry_id,project,type,number,origin,head FROM ledger_entries "
                "WHERE entry_id=? AND project=?",
                (reference, project),
            ).fetchone()
        elif match := _HANDLE.fullmatch(reference):
            row = db.execute(
                "SELECT entry_id,project,type,number,origin,head FROM ledger_entries "
                "WHERE project=? AND type=? AND number=?",
                (project, _TYPES[match[1]], int(match[2])),
            ).fetchone()
        else:
            raise DocumentError(
                INVALID_REQUEST, f"{reference!r} names no entry: give an ID or handle"
            )
        if row is None:
            raise DocumentError(NOT_FOUND, f"no entry {reference} in this project")
        return _Row(*row)

    @staticmethod
    def _summary(row: _Row, revision: tuple[Any, ...]) -> Json:
        return {
            "entry_id": row.entry_id,
            "handle": row.handle,
            "type": row.type,
            "origin": row.origin,
            "revision": revision[0],
            "title": revision[1],
            "status": revision[2],
            "updated": revision[7],
        }


def _normalized(text: str) -> str:
    """The statement in the dollar form (LD-42)."""
    return normalize(parse(text, documents=False)).text


def _fields(kind: str, raw: Json) -> Json:
    model: type[BaseModel] = FIELDS[kind]
    try:
        value = model.model_validate(raw)
    except ValidationError as error:
        raise DocumentError(INVALID_REQUEST, f"{kind} fields: {_first_error(error)}") from None
    if isinstance(value, ReferenceFields):
        # LG-15: a citation has a locator, and a date: published, versioned or retrieved.
        if not (value.identifiers or value.pdf):
            raise DocumentError(INVALID_REQUEST, "a reference names a locator or its PDF")
        if not (value.published or value.version or value.retrieved):
            raise DocumentError(
                INVALID_REQUEST,
                "a reference gives its publication or version date, or when it was retrieved",
            )
    return value.model_dump(mode="json", exclude_defaults=True)


def _status(row: _Row, status: str) -> str:
    if status not in STATUSES[row.type]:
        raise DocumentError(INVALID_REQUEST, f"a {row.type} is never {status}")
    if status in DECIDED:
        raise DocumentError(
            INVALID_REQUEST, f"only a decision in effect makes an entry {status} (LG-4)"
        )
    if status == "recorded" and row.origin != "published":
        raise DocumentError(INVALID_REQUEST, "only a published entry is recorded")
    if status == "proposed" and row.origin == "published":
        raise DocumentError(INVALID_REQUEST, "a published entry is recorded, not proposed")
    if status in ("superseded", "split"):
        # LG-5, LG-30: these commit with the links that make them, built next.
        raise DocumentError(INVALID_REQUEST, f"an entry becomes {status} with its links")
    return status


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "request"
    return f"{where}: {first['msg']}"
