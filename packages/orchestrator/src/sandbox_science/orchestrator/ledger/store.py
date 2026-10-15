# SPDX-License-Identifier: MIT
"""The project ledger's entries, revisions and links in the control store
(docs/specs/ledger.md).

A mutation creates or revises one or more entries, all or nothing, under an operation
ID whose receipt replays its first result (LG-30, LG-31). Each revision names the
expected revision it follows; a stale one is a conflict naming the current revision,
and nothing is rebased. Each change commits with its event (LG-33). Who may change
what follows LG-13: the user anything, the main agent any entry, a delegated agent
only new entries in their first status; code in the sandbox nothing. Statuses that
only a decision sets (LG-4) and the links and published entries that need evidence
or the frontier are refused until those are built.
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
from sandbox_science.orchestrator.documents.agent_text import agent_text, agent_values
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
from sandbox_science.orchestrator.documents.project_macros import head as macro_head
from sandbox_science.orchestrator.projects import ProjectError, recorded

from . import evidence, flags, gate
from .models import (
    DECIDED,
    FIELDS,
    LINKS,
    MAX_CLEARED,
    PREFIXES,
    SHOWN_FLAGGED,
    STATUSES,
    ChangedEntry,
    ClearedFlag,
    EntryChange,
    EntryFinding,
    EntryFlag,
    EntryHistory,
    EntryLink,
    EntryPage,
    EntryRevision,
    EntrySummary,
    EntryView,
    EvidenceLink,
    GateClause,
    GateDecision,
    GateView,
    LinkInput,
    Mutation,
    MutationResult,
    ReferenceFields,
)

Json = dict[str, Any]

TEXTS: Final = TextRevisions("statement")
"""Each entry's statement, a text revision under the entry's ID."""

RECEIPTS: Final = Receipts("ledger", "operation ID {operation!r} names another ledger change")
_PROJECT: Final = "project"
"""The one actor of the ledger's receipts: an operation ID names one change in its
project, whoever retries it, so a shared results file imported from two sessions
records its lines once (LG-31, LG-36, LG-41)."""

MAX_FIELDS: Final = 65536
"""An entry's fields, as UTF-8 JSON, are at most this many bytes."""

_ID: Final = re.compile(r"entry-[0-9a-f]{24}")
_HANDLE: Final = re.compile(r"(I|C|R|A|D|T|REF)-([1-9][0-9]{0,8})")
_TYPES: Final = {prefix: kind for kind, prefix in PREFIXES.items()}
_REVISION: Final = (
    "SELECT r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created "
    "FROM ledger_revisions r WHERE r.entry_id=? AND r.revision=?"
)
_CURRENT: Final = (
    "FROM ledger_links l JOIN ledger_entries e ON e.entry_id=l.entry_id AND l.revision=e.head "
)
"""The links entries' current revisions hold."""
_HOLDERS: Final = (
    f"SELECT l.entry_id {_CURRENT}WHERE l.target='entry' AND l.target_id=? AND l.kind=?"
)
_TARGETS: Final = (
    f"SELECT l.target_id {_CURRENT}WHERE l.entry_id=? AND l.kind=? AND l.target='entry'"
)
INCOMING: Final = 200
"""A read shows as many links to an entry as this."""


def _now() -> datetime:
    return datetime.now(UTC)


def _first_status(kind: str, origin: str | None) -> str:
    if kind in ("claim", "result"):
        return "recorded" if origin == "published" else "proposed"
    return STATUSES[kind][0]


LISTED_FLAGS: Final = 500
"""The flags one change's event lists; it counts the rest."""


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
    links: list[EntryLink] | None = None
    """The new revision's links; None keeps the revision before's."""
    previous: list[EntryLink] = field(default_factory=list[EntryLink])
    """The links of the revision before."""
    evidence: list[EvidenceLink] | None = None
    """The new revision's evidence links; None keeps the revision before's."""
    changed: list[str] = field(default_factory=list[str])


class Ledger:
    """A project's ledger, on the documents' control store."""

    def __init__(self, documents: DocumentStore, *, clock: Callable[[], datetime] = _now) -> None:
        self.documents, self.control, self.clock = documents, documents.control, clock
        self.events = EventLog(documents.control, "ledger_entry", documents.producer)

    # Reads.

    def entry(
        self,
        project: str,
        reference: str,
        revision: int | None = None,
        *,
        incoming_after: int = 0,
    ) -> EntryView:
        """An entry at ``revision`` (its current one by default); at its current revision,
        the links other entries hold to it, from the ``incoming_after``-th on."""
        self._project(project)
        if incoming_after < 0:
            raise DocumentError(INVALID_REQUEST, "incoming_after is a count of links")
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            number = row.head if revision is None else revision
            found = db.execute(_REVISION, (row.entry_id, number)).fetchone()
            if found is None:
                raise DocumentError(NOT_FOUND, f"{row.handle} has no revision {number}")
            text = TEXTS.read(db, row.entry_id, found[3], full=False)
            assert text is not None  # noqa: S101 - written with its revision
            incoming, total = (
                self._incoming(db, project, row.entry_id, incoming_after)
                if number == row.head
                else ([], 0)
            )
            held = evidence.stored(db, row.entry_id, number)
            open_flags = self._flags(db, row.entry_id)
            return EntryView(
                **self._summary(row, found, evidence.asserted_only(row.type, held)),
                open_flags=len(open_flags),
                statement=text["body"],
                fields=json.loads(found[4]),
                links=self._links(db, row.entry_id, number),
                evidence=held,
                incoming=incoming,
                incoming_total=total,
                findings=self._findings(db, project, row.entry_id),
                flags=open_flags,
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
                    "SELECT position FROM ledger_entries WHERE entry_id=?",
                    (self._find(db, project, after).entry_id,),
                ).fetchone()[0]
            rows = db.execute(
                "SELECT e.entry_id,e.project,e.type,e.number,e.origin,e.head,"
                "r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created,"
                "EXISTS(SELECT 1 FROM ledger_evidence v WHERE v.entry_id=e.entry_id "
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
                **self._summary(
                    _Row(*r[:6]), r[6:], None if r[2] not in ("claim", "result") else not r[14]
                ),
                "open_flags": r[15],
            }
            for r in rows[:limit]
        ]
        return EntryPage(
            entries=[EntrySummary(**s) for s in found],
            next_after=found[-1]["entry_id"] if len(rows) > limit else None,
        )

    def history(
        self, project: str, reference: str, *, before: int | None = None, limit: int = 50
    ) -> EntryHistory:
        """An entry's revisions, newest first, before revision ``before``; on the first
        page, the flags cleared on it, latest first, with who cleared them and why."""
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

    def gate(self, project: str, reference: str) -> GateView:
        """The gate for ``reference`` as it reads now (LG-17): each clause as the main
        agent's decision to establish it would meet it, with why, and the decisions that
        affect it. A read: it changes nothing, and takes no write lock."""
        self._project(project)
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            status, statement, fields = db.execute(
                "SELECT status,statement,fields FROM ledger_revisions "
                "WHERE entry_id=? AND revision=?",
                (row.entry_id, row.head),
            ).fetchone()
            decisions = [
                GateDecision(
                    entry_id=found[0],
                    handle=f"{PREFIXES['decision']}-{found[1]}",
                    title=found[2],
                    status=found[3],
                    revision=found[4],
                    author=json.loads(found[5]),
                )
                for found in db.execute(
                    "SELECT e.entry_id,e.number,r.title,r.status,r.revision,r.author "
                    f"{_CURRENT}JOIN ledger_revisions r ON r.entry_id=e.entry_id "
                    "AND r.revision=e.head WHERE l.kind='affects' AND l.target='entry' "
                    "AND l.target_id=? ORDER BY e.position DESC LIMIT 50",
                    (row.entry_id,),
                )
            ]
            applies = row.type in ("claim", "result") and row.origin == "project"
            found_clauses: list[GateClause] = []
            if applies:
                held = evidence.stored(db, row.entry_id, row.head)
                now = gate.material(
                    json.loads(fields), statement, self._links(db, row.entry_id, row.head), held
                )
                found_clauses = [
                    GateClause.model_validate({"clause": clause, "holds": holds, "reason": reason})
                    for clause, holds, reason in gate.clauses(
                        db, row.entry_id, row.handle, held, now, row.head
                    )
                ]
        return GateView(
            entry_id=row.entry_id,
            handle=row.handle,
            revision=row.head,
            status=status,
            applies=applies,
            holds=applies and all(clause.holds for clause in found_clauses),
            clauses=found_clauses,
            decisions=decisions,
        )

    # Changes.

    def check(
        self,
        project: str,
        reference: str,
        revision: int,
        author: Author,
        *,
        operation_id: str,
        note: str | None = None,
    ) -> MutationResult:
        """The user's record that they checked ``reference`` at ``revision`` (LG-17): an
        ``independent_check`` evidence link of the user's, on the entry as it reads at
        that revision, with the note as its scope. It is a conflict when the entry has
        moved on. Only the user records one; an agent's check counts for nothing."""
        if author.kind != "user":
            raise _refused("only the user records that they checked an entry")
        self._project(project)
        with self.control.read() as tx:
            entry = self._find(tx.sql, project, reference).entry_id
        check: Json = {
            "role": "independent_check",
            "type": "proof",
            "entry": entry,
            "revision": revision,
            "checked": revision,
        }
        if note is not None:
            check["scope"] = note
        return self.change(
            project,
            {
                "operation_id": operation_id,
                "reason": note or f"The user checked revision {revision}.",
                "changes": [{"entry": entry, "expected": revision, "evidence": [check]}],
            },
            author,
        )

    def clear_flags(
        self,
        project: str,
        reference: str,
        flags: list[str] | None,
        author: Author,
        *,
        reason: str,
        operation_id: str,
        cause: str | None = None,
    ) -> MutationResult:
        """Clear open flags on ``reference`` with ``reason`` (LG-29): those ``flags``
        names, or else all its open flags, or those of ``cause``, in a change that
        revises nothing. A flag on another entry is not found."""
        if flags is None:
            scope = {"entry": reference, "cause": cause}
            return self.change(
                project,
                {"operation_id": operation_id, "reason": reason, "flags_cleared_on": [scope]},
                author,
            )
        self._on(project, reference, flags)
        return self.change(
            project,
            {"operation_id": operation_id, "reason": reason, "flags_cleared": flags},
            author,
        )

    def reopen_flags(
        self,
        project: str,
        reference: str,
        flags: list[str],
        author: Author,
        *,
        reason: str,
        operation_id: str,
    ) -> MutationResult:
        """Open cleared flags on ``reference`` again, with ``reason``: the user's reversal
        of a clearing (LG-29), each a new flag that names the one it reopens."""
        self._on(project, reference, flags)
        return self.change(
            project,
            {"operation_id": operation_id, "reason": reason, "flags_reopened": flags},
            author,
        )

    def _on(self, project: str, reference: str, flags: list[str]) -> None:
        """Refuse a flag that is not on ``reference``."""
        self._project(project)
        with self.control.read() as tx:
            db = tx.sql
            entry = self._find(db, project, reference)
            for flag in flags:
                if (
                    db.execute(
                        "SELECT 1 FROM ledger_flags WHERE flag_id=? AND entry_id=?",
                        (flag, entry.entry_id),
                    ).fetchone()
                    is None
                ):
                    raise DocumentError(NOT_FOUND, f"{entry.handle} has no flag {flag}")

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
        if role != "user":
            kept: list[tuple[Mutation, Json | None]] = []
            for mutation, provenance in mutations:
                try:
                    kept.append((_agent_text(mutation), provenance))
                except DocumentError as error:
                    raise _labelled(error, provenance) from None
            mutations = kept
        pinned: dict[evidence.Block, str] = {}
        for mutation, provenance in mutations:
            blocks: list[evidence.Block] = []
            for change in mutation.changes:
                for link in change.links or ():
                    if link.document and link.revision and link.block:
                        blocks.append((link.document, link.revision, link.block, None))
                for item in change.evidence or ():
                    if item.document and item.revision and item.block:
                        blocks.append((item.document, item.revision, item.block, item.math))
            try:
                pinned.update(self._blocks(project, blocks))
            except DocumentError as error:
                raise _labelled(error, provenance) from None
        with self.control.write() as tx:
            results: list[MutationResult] = []
            for mutation, provenance in mutations:
                try:
                    results.append(
                        self._apply(tx.sql, project, mutation, author, role, pinned, provenance)
                    )
                except DocumentError as error:
                    raise _labelled(error, provenance) from None
            return results

    def _apply(
        self,
        db: sqlite3.Connection,
        project: str,
        mutation: Mutation,
        author: Author,
        role: str,
        pinned: dict[evidence.Block, str],
        provenance: Json | None,
    ) -> MutationResult:
        digest = fingerprint(mutation)
        replay = RECEIPTS.find(db, project, _PROJECT, mutation.operation_id, digest)
        if replay is not None:
            return MutationResult.model_validate({**replay, "replayed": True})
        numbers: dict[str, int] = {}
        planned = [self._plan(db, project, change, role, numbers) for change in mutation.changes]
        keys = {p.change.key: p.row for p in planned if p.created and p.change.key}
        if len(keys) != sum(1 for p in planned if p.created and p.change.key):
            raise DocumentError(INVALID_REQUEST, "two created entries share a key")
        ids = [p.row.entry_id for p in planned]
        if len(set(ids)) != len(ids):
            raise DocumentError(INVALID_REQUEST, "a mutation changes an entry once")
        # An entry the mutation also revises is linked as the mutation leaves it.
        heads = {p.row.entry_id: p.row.head + 1 for p in planned}
        for p in planned:
            if p.change.links is not None:
                p.links = [
                    self._link(db, project, p, link, keys, pinned, heads) for link in p.change.links
                ]
                if not p.created and p.links != p.previous:
                    p.changed = [*p.changed, "links"]
            self._evidence(db, project, p, keys, heads, pinned)
            if not p.created and not p.changed:
                raise DocumentError(
                    INVALID_REQUEST, f"the change to {p.row.handle} changes nothing"
                )
        self._check_links(db, planned)
        self._check_published(db, planned)
        gated = self._check_decisions(db, planned, role)
        now = self.clock()
        cleared = self._clear(db, project, mutation, author, role, now)
        reopened = self._reopen(db, project, mutation, author, role, now)
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
        raised = flags.raise_flags(
            db, project, [self._changed(p) for p in planned], now.isoformat()
        )
        if role == "main":
            for flag in raised:
                if flag.entry_id in gated:
                    raise gate.refused(
                        flag.entry_id,
                        "flags",
                        f"the change that establishes {flag.handle} also flags it "
                        f"({flag.cause}, from {flag.source['handle']}): make the change, "
                        "reconsider the entry, and establish it after",
                    )
        self._announce(
            db,
            project,
            author,
            "ledger.flags_raised",
            mutation,
            [(f.flag_id, f.entry_id, f.handle, f.cause, f.source["handle"]) for f in raised],
            now,
        )
        flagged = list(dict.fromkeys(flag.handle for flag in raised))
        result = MutationResult(
            operation_id=mutation.operation_id,
            entries=changed,
            flagged=flagged[:SHOWN_FLAGGED],
            flagged_total=len(flagged),
            flags_cleared=cleared[:SHOWN_FLAGGED],
            cleared_total=len(cleared),
            flags_reopened=reopened,
        )
        RECEIPTS.record(
            db,
            project,
            _PROJECT,
            mutation.operation_id,
            digest,
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
            return self._plan_create(db, project, change, role, numbers)
        if change.type is not None or change.origin is not None or change.key is not None:
            raise DocumentError(INVALID_REQUEST, "a revision keeps the entry's type and origin")
        if change.expected is None:
            raise DocumentError(INVALID_REQUEST, "a revision names the revision it expects")
        row = self._find(db, project, change.entry)
        if role == "delegated" and (
            not change.evidence
            or change.model_dump(exclude_none=True, exclude={"entry", "expected", "evidence"})
        ):
            # LG-13: a delegated agent adds evidence, and changes nothing else.
            raise _refused("a delegated agent creates entries and adds evidence, nothing else")
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
            _check_pdf(db, project, fields)
            if fields != planned.fields:
                planned.fields, planned.changed = fields, [*planned.changed, "fields"]
        if change.status is not None and change.status != current[2]:
            planned.status = _status(row, current[2], change.status)
            planned.changed = [*planned.changed, "status"]
        if change.links is not None:
            planned.previous = self._links(db, row.entry_id, row.head)
        return planned

    def _plan_create(
        self,
        db: sqlite3.Connection,
        project: str,
        change: EntryChange,
        role: str,
        numbers: dict[str, int],
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
        status = _first_status(kind, change.origin)
        if kind == "decision" and change.status == "in_effect":
            # LG-17: the user or the main agent records a decision in effect at once.
            if role == "delegated":
                raise _refused("a delegated agent's decision is proposed")
            status = "in_effect"
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
            fields=_check_pdf(db, project, _fields(kind, change.fields or {})),
            links=[] if change.links is None else None,
        )

    def _link(
        self,
        db: sqlite3.Connection,
        project: str,
        planned: _Planned,
        link: LinkInput,
        keys: dict[str, _Row],
        pinned: dict[evidence.Block, str],
        heads: dict[str, int],
    ) -> EntryLink:
        holders, targets = LINKS[link.kind]
        if planned.row.type not in holders:
            raise DocumentError(INVALID_REQUEST, f"a {planned.row.type} holds no {link.kind} link")
        if link.kind == "dominates":
            raise DocumentError(INVALID_REQUEST, "dominates links come with the frontier")
        if link.kind == "compares_to" and link.note is None:
            raise DocumentError(INVALID_REQUEST, "a compares_to link states the difference")
        named = [link.entry, link.finding, link.document]
        if sum(value is not None for value in named) != 1:
            raise DocumentError(INVALID_REQUEST, "a link names one entry, finding or block")
        if link.finding is not None:
            if "finding" not in targets or link.revision is not None or link.block is not None:
                raise DocumentError(INVALID_REQUEST, f"a {link.kind} link names no finding")
            found = db.execute(
                "SELECT session FROM review_findings WHERE finding_id=? AND project=?",
                (link.finding, project),
            ).fetchone()
            if found is None:
                raise DocumentError(NOT_FOUND, f"no finding {link.finding} in this project")
            return EntryLink(
                kind=link.kind,
                target="finding",
                target_id=link.finding,
                note=link.note,
                session=found[0],
            )
        if link.document is not None:
            if "block" not in targets or link.block is None or link.revision is None:
                raise DocumentError(
                    INVALID_REQUEST, f"a {link.kind} link names no document block, or no revision"
                )
            # Stored by the document's ID, which a name given here may later not name.
            return EntryLink(
                kind=link.kind,
                target="block",
                target_id=pinned[(link.document, link.revision, link.block, None)],
                revision=link.revision,
                block=link.block,
                note=link.note,
            )
        assert link.entry is not None  # noqa: S101 - one of the three is named
        if link.block is not None:
            raise DocumentError(INVALID_REQUEST, "an entry link names no block")
        if link.entry.startswith("new:"):
            target = keys.get(link.entry.removeprefix("new:"))
            if target is None:
                raise DocumentError(INVALID_REQUEST, f"no new entry is keyed {link.entry}")
        else:
            target = self._find(db, project, link.entry)
        head = heads.get(target.entry_id, target.head)
        if target.entry_id == planned.row.entry_id:
            raise DocumentError(INVALID_REQUEST, "an entry does not link to itself")
        if target.type not in targets:
            raise DocumentError(INVALID_REQUEST, f"a {link.kind} link points to no {target.type}")
        revision = link.revision
        if revision is not None and not 1 <= revision <= head:
            raise DocumentError(NOT_FOUND, f"{target.handle} has no revision {revision}")
        if link.kind == "assumes" and revision is None:
            revision = head  # pinned to the assumption as it reads now (LG-27)
        return EntryLink(
            kind=link.kind,
            target="entry",
            target_id=target.entry_id,
            revision=revision,
            note=link.note,
            handle=target.handle,
        )

    def _write(
        self,
        db: sqlite3.Connection,
        project: str,
        p: _Planned,
        mutation: Mutation,
        author: Author,
        provenance: Json | None,
        now: datetime,
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
                _encoded(p.fields),
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                None if provenance is None else json.dumps(provenance, sort_keys=True),
                now.isoformat(),
            ),
        )
        held = p.evidence
        if held is None:  # kept from the revision before
            held = evidence.stored(db, row.entry_id, before) if before else []
        evidence.write(db, row.entry_id, row.head, held)
        links = p.links
        if links is None:  # kept from the revision before
            links = self._links(db, row.entry_id, before)
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
        self.events.append(
            db,
            "ledger.entry_created" if p.created else "ledger.entry_revised",
            project,
            author.session_id,
            row.entry_id,
            payload,
            now,
        )

    def _statement(
        self, db: sqlite3.Connection, row: _Row, text: str | None, author: Author
    ) -> int:
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

    # Checks.

    def _evidence(
        self,
        db: sqlite3.Connection,
        project: str,
        p: _Planned,
        keys: dict[str, _Row],
        heads: dict[str, int],
        pinned: dict[evidence.Block, str],
    ) -> None:
        """The evidence the change leaves the entry: what it held, without the links the
        change removes, with those it adds (LG-8)."""
        if p.change.evidence is None and p.change.evidence_removed is None:
            return
        held = evidence.stored(db, p.row.entry_id, p.row.head) if not p.created else []
        removed = set(p.change.evidence_removed or ())
        unknown = removed - {link.evidence_id for link in held}
        if unknown:
            raise DocumentError(
                NOT_FOUND, f"{p.row.handle} holds no evidence {', '.join(sorted(unknown))}"
            )
        holder = evidence.Holder(p.row.entry_id, p.row.handle, p.row.type, p.row.head)
        added: list[EvidenceLink] = []
        for item in p.change.evidence or ():
            target = None
            if item.entry is not None:
                if item.entry.startswith("new:"):
                    row = keys.get(item.entry.removeprefix("new:"))
                    if row is None:
                        raise DocumentError(INVALID_REQUEST, f"no new entry is keyed {item.entry}")
                else:
                    row = self._find(db, project, item.entry)
                target = evidence.EntryTarget(
                    row.entry_id, row.handle, row.type, heads.get(row.entry_id, row.head)
                )
            added.append(evidence.resolve(db, project, holder, item, target, pinned))
        result = [link for link in held if link.evidence_id not in removed] + added
        if len({evidence.key(link) for link in result}) != len(result):
            raise DocumentError(INVALID_REQUEST, f"{p.row.handle} holds that evidence already")
        if len(result) > 128:
            raise DocumentError(INVALID_REQUEST, "an entry holds at most 128 evidence links")
        if sum(len((link.scope or "").encode()) for link in result) > evidence.MAX_SCOPES:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle}'s evidence scope notes exceed {evidence.MAX_SCOPES} bytes",
            )
        p.evidence = result
        if not p.created:
            p.changed = [*p.changed, "evidence"]

    def _check_decisions(
        self, db: sqlite3.Connection, planned: list[_Planned], role: str
    ) -> set[str]:
        """Statuses only a decision sets (LG-4): an entry becomes ``established``,
        ``obstructed`` or ``refuted``, and a decision ``reversed``, only in a change that
        puts in effect a decision that affects it. The main agent's decision to establish
        a result needs the gate (LG-17), and one that makes an issue obstructed needs an
        accepted assessment (LG-7), which work records will carry. Returns the entries
        the gate passed, which the flags the change raises must spare."""
        gated: set[str] = set()
        deciding = [
            p
            for p in planned
            if p.row.type == "decision"
            and p.status == "in_effect"
            and p.status_before != "in_effect"
        ]
        for p in planned:
            decided = p.status in DECIDED or (p.row.type == "decision" and p.status == "reversed")
            # An entry stays as decided: while it is established, refuted or obstructed,
            # what a decision rested on changes only with its status or a new decision.
            rewritten = (
                not p.created
                and p.status_before in DECIDED
                and p.status == p.status_before
                and self._rewrites(db, p)
            )
            if not (decided and "status" in p.changed) and not rewritten:
                continue
            if rewritten and not any(
                d is not p
                and any(
                    link.kind == "affects" and link.target_id == p.row.entry_id
                    for link in self._final_links(db, d)
                )
                for d in deciding
            ):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} is {p.status}: its statement, fields, assumptions and "
                    "evidence change only with its status, or in a change that puts in "
                    "effect a decision that affects it",
                )
            if not any(
                d is not p
                and any(
                    link.kind == "affects" and link.target_id == p.row.entry_id
                    for link in self._final_links(db, d)
                )
                for d in deciding
            ):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} becomes {p.status} only in a change that puts in effect "
                    "a decision that affects it",
                )
            if role != "main":
                continue
            if p.status == "established":
                held = self._final_evidence(db, p)
                latest = TEXTS.latest(db, p.row.entry_id)
                assert latest is not None  # noqa: S101 - an existing entry has a statement
                now = gate.material(
                    p.fields,
                    None if p.statement is not None else latest[0],
                    self._final_links(db, p),
                    held,
                )
                gate.establish(db, p.row.entry_id, p.row.handle, held, now, p.row.head)
                gated.add(p.row.entry_id)
            elif p.status == "obstructed":
                raise gate.refused(
                    p.row.entry_id,
                    "assessment",
                    f"{p.row.handle} becomes obstructed with an assessment that accepts the "
                    "claimed obstruction; work records are not built yet, so only the user's "
                    "decision makes an issue obstructed",
                )
        return gated

    def _rewrites(self, db: sqlite3.Connection, p: _Planned) -> bool:
        """Whether the change alters what a decision on ``p``'s entry rested on: its
        statement, fields, ``assumes`` links or evidence."""
        if {"statement", "fields", "evidence"} & set(p.changed):
            return True
        if "links" not in p.changed:
            return False

        def assumed(links: list[EntryLink]) -> set[tuple[str, int | None]]:
            return {(link.target_id, link.revision) for link in links if link.kind == "assumes"}

        return assumed(p.previous) != assumed(self._final_links(db, p))

    def _final_links(self, db: sqlite3.Connection, p: _Planned) -> list[EntryLink]:
        """The links the change leaves ``p``'s entry."""
        return p.links if p.links is not None else self._links(db, p.row.entry_id, p.row.head)

    def _final_evidence(self, db: sqlite3.Connection, p: _Planned) -> list[EvidenceLink]:
        """The evidence the change leaves ``p``'s entry."""
        if p.evidence is not None:
            return p.evidence
        return evidence.stored(db, p.row.entry_id, p.row.head) if not p.created else []

    @staticmethod
    def _changed(p: _Planned) -> flags.Changed:
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

    def _clear(
        self,
        db: sqlite3.Connection,
        project: str,
        mutation: Mutation,
        author: Author,
        role: str,
        now: datetime,
    ) -> list[str]:
        """Clear the open flags the mutation names, by ID or by entry and cause, with its
        reason (LG-29): the user's or the main agent's to clear, never a delegated
        agent's. One event records them all."""
        if not mutation.flags_cleared and not mutation.flags_cleared_on:
            return []
        if role == "delegated":
            raise _refused("a delegated agent clears no flag")
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
            entry = self._find(db, project, scope.entry)
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
        self._announce(
            db,
            project,
            author,
            "ledger.flags_cleared",
            mutation,
            [(flag, *rest) for flag, rest in found.items()],
            now,
        )
        return list(found)

    def _reopen(
        self,
        db: sqlite3.Connection,
        project: str,
        mutation: Mutation,
        author: Author,
        role: str,
        now: datetime,
    ) -> list[str]:
        """Open cleared flags again, as new flags that name them (LG-29; settled question
        2: the user can reverse what the main agent did). The user's only."""
        if not mutation.flags_reopened:
            return []
        if role != "user":
            raise _refused("only the user reopens a flag")
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
        self._announce(db, project, author, "ledger.flags_reopened", mutation, reopened, now)
        return opened

    def _announce(
        self,
        db: sqlite3.Connection,
        project: str,
        author: Author,
        kind: str,
        mutation: Mutation,
        found: list[tuple[str, str, str, str, str]],
        now: datetime,
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
        self.events.append(
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

    def _check_published(self, db: sqlite3.Connection, planned: list[_Planned]) -> None:
        """A published claim or result cites its reference through a ``supports`` link of
        type ``source_claim`` (LG-15), which may be to a reference the same change
        records."""
        made = {p.row.entry_id for p in planned if p.row.type == "reference"}
        for p in planned:
            if p.row.origin != "published" or (not p.created and p.evidence is None):
                continue
            targets = {link.target_id for link in p.evidence or () if link.target == "entry"}
            references = made | {
                entry
                for (entry,) in db.execute(
                    "SELECT entry_id FROM ledger_entries WHERE type='reference' AND entry_id IN "  # noqa: S608 - placeholders only
                    f"({','.join('?' * len(targets))})",
                    tuple(targets),
                )
            }
            if not evidence.cites_reference(p.evidence or [], references):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} is published: it cites its reference with a supports "
                    "link of type source_claim",
                )

    def _check_links(self, db: sqlite3.Connection, planned: list[_Planned]) -> None:
        """What holds once the change commits (LG-5, LG-30). ``supersedes`` and
        ``split_into`` links never lead back to their holder. An entry is superseded
        exactly while a current revision supersedes it, so a supersedes link and its
        target becoming superseded commit together, and so do the link's removal and the
        target leaving superseded; an entry that is superseded supersedes nothing new. An
        issue is split exactly while it holds split_into links."""
        by_id = {p.row.entry_id: p for p in planned}
        final = {
            p.row.entry_id: p.links
            if p.links is not None
            else self._links(db, p.row.entry_id, p.row.head)
            for p in planned
        }

        def targets(entry: str, kind: str) -> list[str]:
            if entry in final:
                return [
                    link.target_id
                    for link in final[entry]
                    if link.kind == kind and link.target == "entry"
                ]
            return [row[0] for row in db.execute(_TARGETS, (entry, kind))]

        def holders(entry: str, kind: str) -> list[str]:
            stored = [row[0] for row in db.execute(_HOLDERS, (entry, kind))]
            return [holder for holder in stored if holder not in final] + [
                holder for holder in final if entry in targets(holder, kind)
            ]

        def status(entry: str) -> str:
            if entry in by_id:
                return by_id[entry].status
            return db.execute(
                "SELECT r.status FROM ledger_entries e JOIN ledger_revisions r ON "
                "r.entry_id=e.entry_id AND r.revision=e.head WHERE e.entry_id=?",
                (entry,),
            ).fetchone()[0]

        def handle(entry: str) -> str:
            if entry in by_id:
                return by_id[entry].row.handle
            kind, number = db.execute(
                "SELECT type,number FROM ledger_entries WHERE entry_id=?", (entry,)
            ).fetchone()
            return f"{PREFIXES[kind]}-{number}"

        superseding: list[str] = []
        for p in planned:
            for link in [*_added(p, "supersedes"), *_added(p, "split_into")]:
                seen: set[str] = set()
                reached = [link.target_id]
                while reached:
                    entry = reached.pop()
                    if entry == p.row.entry_id:
                        raise DocumentError(
                            INVALID_REQUEST,
                            f"{p.row.handle} {link.kind} {link.handle}, "
                            f"which already leads back to {p.row.handle}",
                        )
                    if entry not in seen:
                        seen.add(entry)
                        reached.extend(targets(entry, link.kind))
            for link in _added(p, "supersedes"):
                if p.status == "superseded":
                    raise DocumentError(
                        INVALID_REQUEST, f"{p.row.handle} is superseded and supersedes nothing new"
                    )
                if status(link.target_id) != "superseded":
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{link.handle} becomes superseded in the change that supersedes it",
                    )
            superseding += [link.target_id for link in _dropped(p, "supersedes")]
        moved = [p.row.entry_id for p in planned if "status" in p.changed]
        for entry in dict.fromkeys([*moved, *superseding]):
            if by_id.get(entry) is not None and by_id[entry].row.type not in ("claim", "result"):
                continue
            held = holders(entry, "supersedes")
            if status(entry) == "superseded" and not held:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{handle(entry)} is superseded only while an entry supersedes it",
                )
            if status(entry) != "superseded" and held:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{handle(entry)} stays superseded while {handle(held[0])} supersedes it",
                )
        for p in planned:
            if p.row.type != "issue" or ("status" not in p.changed and p.change.links is None):
                continue
            split = bool(targets(p.row.entry_id, "split_into"))
            if p.status == "split" and not split:
                raise DocumentError(
                    INVALID_REQUEST, f"{p.row.handle} names the issues it is split into"
                )
            if p.status != "split" and split:
                raise DocumentError(
                    INVALID_REQUEST, f"{p.row.handle} holds split_into links only while split"
                )

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

    def _blocks(self, project: str, blocks: list[evidence.Block]) -> dict[evidence.Block, str]:
        """The documents of the blocks and math spans links and evidence pin, by ID, each
        checked to exist at its revision: a change names a document by ID or name, and a
        name can later name another document. Read before the change, since document
        revisions do not change."""
        found: dict[evidence.Block, str] = {}
        for key in blocks:
            name, revision, block, span = key
            if key in found:
                continue
            with self.control.read() as tx:
                document = self.documents.find(tx.sql, project, name).document_id
            mapped = self.documents.mapped(project, document, revision)
            index = mapped.index(block)
            if index is None:
                raise DocumentError(NOT_FOUND, f"{name} has no block {block} at r{revision}")
            if span is not None:
                at = mapped.span_index(span)
                if at is None or mapped.parsed.spans[at].block != index:
                    raise DocumentError(
                        NOT_FOUND, f"{name} has no math span {span} in block {block} at r{revision}"
                    )
            found[key] = document
        return found

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
    def _summary(row: _Row, revision: tuple[Any, ...], asserted_only: bool | None) -> Json:
        return {
            "asserted_only": asserted_only,
            "entry_id": row.entry_id,
            "handle": row.handle,
            "type": row.type,
            "origin": row.origin,
            "revision": revision[0],
            "title": revision[1],
            "status": revision[2],
            "updated": revision[7],
        }

    @staticmethod
    def _links(db: sqlite3.Connection, entry: str, revision: int) -> list[EntryLink]:
        return [
            _stored(db, row)
            for row in db.execute(
                "SELECT kind,target,target_id,target_revision,block,note FROM ledger_links "
                "WHERE entry_id=? AND revision=? ORDER BY ordinal",
                (entry, revision),
            ).fetchall()
        ]

    @staticmethod
    def _flags(db: sqlite3.Connection, entry: str) -> list[EntryFlag]:
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

    @staticmethod
    def _findings(db: sqlite3.Connection, project: str, entry: str) -> list[EntryFinding]:
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

    @staticmethod
    def _incoming(
        db: sqlite3.Connection, project: str, entry: str, after: int
    ) -> tuple[list[EntryLink], int]:
        """The links other entries' current revisions hold to ``entry``, from the
        ``after``-th on, and how many there are."""
        where = "WHERE l.target='entry' AND l.target_id=? AND e.project=? "
        total = db.execute(f"SELECT count(*) {_CURRENT}{where}", (entry, project)).fetchone()[0]
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
                f"SELECT l.kind,l.entry_id,l.target_revision,l.note,e.type,e.number {_CURRENT}"
                f"{where}ORDER BY e.position,l.ordinal LIMIT ? OFFSET ?",
                (entry, project, INCOMING, after),
            ).fetchall()
        ]
        return links, total


def _stored(db: sqlite3.Connection, row: tuple[Any, ...]) -> EntryLink:
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
    fields = value.model_dump(mode="json", exclude_defaults=True)
    if len(_encoded(fields).encode()) > MAX_FIELDS:
        raise DocumentError(INVALID_REQUEST, f"{kind} fields exceed {MAX_FIELDS} bytes")
    return fields


def _encoded(fields: Json) -> str:
    """Fields as the table keeps them: UTF-8 as is, no NaN or infinity."""
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _check_pdf(db: sqlite3.Connection, project: str, fields: Json) -> Json:
    """A reference's PDF is a revision of an artifact of the project (LG-15, RA-19),
    pinned by its SHA-256: a hash the change gives must be the revision's, and the
    revision's is recorded when it gives none."""
    pdf = fields.get("pdf")
    if pdf is None:
        return fields
    pdf["sha256"] = evidence.retained(
        db, project, pdf["artifact_id"], pdf["revision"], pdf.get("sha256")
    )
    if len(_encoded(fields).encode()) > MAX_FIELDS:
        raise DocumentError(INVALID_REQUEST, f"reference fields exceed {MAX_FIELDS} bytes")
    return fields


def fingerprint(mutation: Mutation) -> str:
    """What a receipt compares a retry with: the mutation as JSON without the fields it
    leaves unset, so that a field a later version adds does not change it."""
    canonical = json.dumps(
        mutation.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _status(row: _Row, before: str, status: str) -> str:
    """``status`` for ``row``, which is ``before``, if its type allows the change; that a
    decision makes it is checked once the whole change is planned (LG-4)."""
    if status not in STATUSES[row.type]:
        raise DocumentError(INVALID_REQUEST, f"a {row.type} is never {status}")
    if row.type == "decision":
        # LG-30: a decision in effect is reversed by another decision, which states its
        # own changes; nothing returns to a status by itself.
        if before in ("rejected", "reversed"):
            raise DocumentError(
                INVALID_REQUEST, f"a {before} decision stays {before}: make a new decision"
            )
        if before == "in_effect" and status != "reversed":
            raise DocumentError(
                INVALID_REQUEST, "a decision in effect is reversed by another decision"
            )
        if before == "proposed" and status == "reversed":
            raise DocumentError(INVALID_REQUEST, "a proposed decision is rejected, not reversed")
    if status == "established" and row.origin != "project":
        raise DocumentError(INVALID_REQUEST, "only a project claim or result is established")
    if status == "recorded" and row.origin != "published":
        raise DocumentError(INVALID_REQUEST, "only a published entry is recorded")
    if status == "proposed" and row.origin == "published":
        raise DocumentError(INVALID_REQUEST, "a published entry is recorded, not proposed")
    return status


def _added(p: _Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the change's new revision holds that the one before did not."""
    before = {(link.target, link.target_id) for link in p.previous if link.kind == kind}
    return [
        link
        for link in p.links or ()
        if link.kind == kind and (link.target, link.target_id) not in before
    ]


def _dropped(p: _Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the revision before held that the change's new one does not."""
    if p.links is None:
        return []
    after = {(link.target, link.target_id) for link in p.links if link.kind == kind}
    return [
        link
        for link in p.previous
        if link.kind == kind and (link.target, link.target_id) not in after
    ]


def _agent_text(mutation: Mutation) -> Mutation:
    """An agent's ledger text as it is kept: its line ends line feeds, and no character the
    user may not see, as it is or as a character reference (``check_agent_text``, threat
    model A3): the user reads and decides on entries, and agents read them again. Checked
    when written, so stored text still reads; the user's own text is kept as written."""
    changes: list[EntryChange] = []
    for number, change in enumerate(mutation.changes, 1):
        where = f"change {number}'s"
        update: dict[str, object] = {}
        if change.title is not None:
            update["title"] = agent_text(change.title, f"{where} title")
        if change.statement is not None:
            update["statement"] = agent_text(change.statement, f"{where} statement")
        if change.fields is not None:
            update["fields"] = agent_values(change.fields, f"{where} fields")
        if change.links is not None:
            update["links"] = [
                link
                if link.note is None
                else link.model_copy(
                    update={"note": agent_text(link.note, f"{where} link {index}'s note")}
                )
                for index, link in enumerate(change.links, 1)
            ]
        if change.evidence is not None:
            update["evidence"] = [
                item
                if item.scope is None
                else item.model_copy(
                    update={"scope": agent_text(item.scope, f"{where} evidence {index}'s scope")}
                )
                for index, item in enumerate(change.evidence, 1)
            ]
        changes.append(change.model_copy(update=update))
    reason = agent_text(mutation.reason, "the reason")
    return mutation.model_copy(update={"reason": reason, "changes": changes})


def _labelled(error: DocumentError, provenance: Json | None) -> DocumentError:
    """``error``, naming the results file's line its mutation came from."""
    line = (provenance or {}).get("line")
    if line is None:
        return error
    return DocumentError(
        error.kind, f"line {line}: {error.message}", {**error.details, "line": line}
    )


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "request"
    return f"{where}: {first['msg']}"
