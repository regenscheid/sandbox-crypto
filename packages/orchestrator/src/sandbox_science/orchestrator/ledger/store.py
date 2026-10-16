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
from typing import Any, Final, NoReturn, cast

from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control import EventLog, Receipts
from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.documents.agent_text import agent_text, agent_values
from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.comment_bodies import prepare, sha
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.normalize import normalize
from sandbox_science.orchestrator.documents.pins import newer, text_references
from sandbox_science.orchestrator.documents.pins import pin as pin_references
from sandbox_science.orchestrator.documents.project_macros import head as macro_head
from sandbox_science.orchestrator.figurechecks import results as figure_results
from sandbox_science.orchestrator.projects import ProjectError, recorded, run_id_of

from . import evidence, flags, frontier, gate, standing
from . import provenance as anchoring
from .models import (
    DECIDED,
    FIELDS,
    LINKS,
    MAX_CLEARED,
    MAX_ENTRY_WORK,
    NO_CONDITION,
    PREFIXES,
    SET_ASIDE,
    SHOWN_CHECKS,
    SHOWN_FLAGGED,
    STATUSES,
    THIS_SESSION,
    WORK_HOLDERS,
    AnchorInput,
    Category,
    CategoryDefinition,
    CategoryPage,
    ChangedEntry,
    CitationCheckView,
    ClearedFlag,
    EntryChange,
    EntryCitations,
    EntryFinding,
    EntryFlag,
    EntryHistory,
    EntryLink,
    EntryPage,
    EntryRevision,
    EntrySummary,
    EntryView,
    EvidenceInput,
    EvidenceLink,
    FrontierHistory,
    FrontierPast,
    FrontierView,
    GateClause,
    GateDecision,
    GateView,
    LinkInput,
    Mutation,
    MutationResult,
    QuoteChecks,
    ReferenceFields,
    Standing,
    WorkInput,
    WorkPage,
    WorkRecord,
    WorkReference,
    wrong_link_target,
)

Json = dict[str, Any]

TEXTS: Final = TextRevisions("statement")
_DECIDED: Final = frozenset({"established", "recorded", "in_effect", "rejected", "reversed"})
"""Statuses that settle an entry: a repin there needs an anchor (figures, FG-8)."""
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
    "SELECT r.revision,r.title,r.status,r.statement,r.fields,r.author,r.reason,r.created,"
    "r.basis,r.anchors,r.pins FROM ledger_revisions r WHERE r.entry_id=? AND r.revision=?"
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
    fields_before: Json = field(default_factory=dict[str, Any])
    """The fields of the revision before; none for an entry the change creates."""
    links: list[EntryLink] | None = None
    """The new revision's links; None keeps the revision before's."""
    previous: list[EntryLink] = field(default_factory=list[EntryLink])
    """The links of the revision before."""
    evidence: list[EvidenceLink] | None = None
    """The new revision's evidence links; None keeps the revision before's."""
    changed: list[str] = field(default_factory=list[str])
    basis: str | None = None
    """Where the new revision comes from (CM-9)."""
    anchors: list[Json] = field(default_factory=list[Json])
    pins: list[Json] | None = None
    """The statement's pins, when a ``repin`` moved them; otherwise they are made when
    the revision is written (figures spec, FG-7, FG-8)."""


class Ledger:
    """A project's ledger, on the documents' control store."""

    def __init__(self, documents: DocumentStore, *, clock: Callable[[], datetime] = _now) -> None:
        self.documents, self.control, self.clock = documents, documents.control, clock
        self.events = EventLog(documents.control, "ledger_entry", documents.producer)
        self.category_events = EventLog(documents.control, "frontier_category", documents.producer)

    # Reads.

    def reads(self, project: str, view: EntryView) -> list[dict[str, Any]]:
        """How far the project's sessions read a reference's sources, from the read
        register (LG-15 as CM-12 amends it); none for another entry."""
        if view.type != "reference":
            return []
        from sandbox_science.orchestrator.history.reads import reference_reads  # no cycle

        return reference_reads(self.control, project, view.fields)

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
            fields = json.loads(found[4])
            label = (
                standing.of_entry(db, row.entry_id, row.type, row.origin, found[2], fields, number)
                if number == row.head
                else None
            )
            return EntryView(
                **self._summary(row, found, evidence.asserted_only(row.type, held), label),
                open_flags=len(open_flags),
                statement=text["body"],
                fields=fields,
                links=self._links(db, row.entry_id, number),
                evidence=held,
                incoming=incoming,
                incoming_total=total,
                findings=self._findings(db, project, row.entry_id),
                flags=open_flags,
                head=row.head,
                author=json.loads(found[5]),
                reason=found[6],
                anchors=json.loads(found[9] or "[]"),
                pins=newer(db, project, json.loads(found[10] or "[]")),
                figure_checks=figure_results(db, "statement", row.entry_id, number),
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
                    **self._summary(
                        _Row(*r[:6]),
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

    def mark(self, project: str) -> tuple[int, int]:
        """A mark of ``project``'s ledger that any change moves: its revisions' count and
        newest row. A store without a ledger yet has (0, 0)."""
        if self.control.missing:
            return (0, 0)
        with self.control.read() as tx:
            count, newest = tx.sql.execute(
                "SELECT count(*), coalesce(max(r.rowid), 0) FROM ledger_revisions r "
                "JOIN ledger_entries e ON e.entry_id = r.entry_id WHERE e.project = ?",
                (project,),
            ).fetchone()
        return (int(count), int(newest))

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

    def citations(
        self, project: str, reference: str, revision: int | None = None
    ) -> EntryCitations:
        """The consistency reviewer's checks of each quote revision ``revision`` (the
        current one by default) of ``reference`` holds (CR-4): newest first, as many as
        ``SHOWN_CHECKS`` with their count, each saying whether it checked that revision's
        material. A read: it changes nothing."""
        self._project(project)
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            shown = row.head if revision is None else revision
            if not 1 <= shown <= row.head:
                raise DocumentError(NOT_FOUND, f"{row.handle} has no revision {shown}")
            material = gate.examined(db, row.entry_id, {shown}).get(shown)
            digest = None if material is None else gate.fingerprint(material)
            quotes: list[QuoteChecks] = []
            for link in evidence.stored(db, row.entry_id, shown):
                if link.target != "quote":
                    continue
                (total,) = db.execute(
                    "SELECT count(*) FROM citation_checks WHERE entry_id=? AND evidence_id=?",
                    (row.entry_id, link.evidence_id),
                ).fetchone()
                checks = [
                    CitationCheckView(
                        check_id=found[0],
                        review_id=found[1],
                        session=found[2],
                        entry_revision=found[3],
                        verdict=found[4],
                        explanation=found[5],
                        created=datetime.fromtimestamp(found[6], UTC),
                        source=found[7],
                        matches=found[8] == digest,
                    )
                    for found in db.execute(
                        "SELECT c.check_id,c.review_id,r.session,c.entry_revision,c.verdict,"
                        "c.explanation,c.created,c.source,c.material FROM citation_checks c "
                        "JOIN review_runs r ON r.review_id=c.review_id WHERE c.entry_id=? "
                        "AND c.evidence_id=? ORDER BY c.created DESC, c.rowid DESC LIMIT ?",
                        (row.entry_id, link.evidence_id, SHOWN_CHECKS),
                    )
                ]
                quotes.append(QuoteChecks(evidence_id=link.evidence_id, checks=checks, total=total))
        return EntryCitations(
            entry_id=row.entry_id, handle=row.handle, revision=shown, quotes=quotes
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
                written = TEXTS.read(db, row.entry_id, statement, full=False)
                now = gate.material(
                    json.loads(fields),
                    statement,
                    self._links(db, row.entry_id, row.head),
                    held,
                    None if written is None else written["sha256"],
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

    def categories(
        self, project: str, *, after: str | None = None, limit: int = 50
    ) -> CategoryPage:
        """The project's frontier categories, in the order they were defined (LG-18)."""
        self._project(project)
        if not 1 <= limit <= 100:
            raise DocumentError(INVALID_REQUEST, "a page holds 1 to 100 categories")
        if self.control.missing:
            return CategoryPage(categories=[], next_after=None)
        with self.control.read() as tx:
            return frontier.listing(tx.sql, project, after, limit)

    def category(self, project: str, name: str, revision: int | None = None) -> Category:
        """A frontier category at ``revision``, its current one by default."""
        self._project(project)
        with self.control.read() as tx:
            found = frontier.read(tx.sql, project, name, revision)
        if found is None:
            raise DocumentError(
                NOT_FOUND,
                f"no frontier category {name}"
                + ("" if revision is None else f" at revision {revision}"),
            )
        return found

    def frontier_history(
        self, project: str, name: str, *, before: int | None = None, limit: int = 50
    ) -> FrontierHistory:
        """A category's frontier changes, newest first (LG-24)."""
        self.category(project, name)
        if not 1 <= limit <= 100:
            raise DocumentError(INVALID_REQUEST, "a page holds 1 to 100 changes")
        with self.control.read() as tx:
            return frontier.history(tx.sql, project, name, before, limit)

    def frontier_past(self, project: str, name: str, at: datetime) -> FrontierPast:
        """A category's best known results as they were at ``at`` (LG-24)."""
        self.category(project, name)
        if at.tzinfo is None:
            raise DocumentError(INVALID_REQUEST, "a time names its zone")
        with self.control.read() as tx:
            return frontier.past(tx.sql, project, name, at.astimezone(UTC).isoformat())

    def frontier(
        self, project: str, name: str, *, candidates_before: int | None = None
    ) -> FrontierView:
        """A category's frontier as its results read now (LG-20, LG-21, LG-23), its
        candidates newest first, made before result ``candidates_before``."""
        self._project(project)
        with self.control.read() as tx:
            db = tx.sql
            found = frontier.read(db, project, name)
            if found is None:
                raise DocumentError(NOT_FOUND, f"no frontier category {name}")
            return frontier.view(db, project, found, candidates_before=candidates_before)

    # Changes.

    def define_category(self, project: str, raw: object, author: Author) -> Category:
        """Define a frontier category, or revise one at the revision read (LG-18): the
        user's or the main agent's, under an operation ID that replays its first result."""
        try:
            value = CategoryDefinition.model_validate(raw)
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, _first_error(error)) from None
        if self._authority(project, author) == "delegated":
            raise _refused("a delegated agent defines no frontier category")
        if author.kind != "user":
            # Agents read categories again and the user decides on them: an agent's text
            # holds no character the user may not see (threat model A3), and is kept with
            # its line ends made line feeds; the user's is kept as written.
            value = value.model_copy(
                update={
                    "target": agent_text(value.target, "the category's target"),
                    "kind": agent_text(value.kind, "the category's kind"),
                    "reason": agent_text(value.reason, "the reason"),
                    "measures": [
                        m.model_copy(
                            update={"unit": agent_text(m.unit, f"measure {m.name}'s unit")}
                        )
                        for m in value.measures
                    ],
                }
            )
        canonical = json.dumps(
            value.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.control.write() as tx:
            db = tx.sql
            replay = RECEIPTS.find(db, project, _PROJECT, value.operation_id, digest)
            if replay is not None:
                return Category.model_validate({**replay, "replayed": True})
            now = self.clock()
            if value.expected is not None:
                self._baseline(db, project, {value.category}, now)
            category = frontier.define(
                db, project, value, author.model_dump(mode="json", exclude_none=True),
                now.isoformat(),
            )  # fmt: skip
            self.category_events.append(
                db,
                "ledger.category_defined",
                project,
                author.session_id,
                value.category,
                {
                    "category": value.category,
                    "revision": category.revision,
                    "operation_id": value.operation_id,
                },
                now,
            )
            self._frontier(
                db,
                project,
                {value.category},
                {
                    "operation_id": value.operation_id,
                    "author": author.actor,
                    "category_revision": category.revision,
                },
                author,
                now,
            )
            RECEIPTS.record(
                db, project, _PROJECT, value.operation_id, digest, category.model_dump(mode="json")
            )
            return category

    @staticmethod
    def _categories(
        db: sqlite3.Connection, planned: list[_Planned], *, written: bool = True
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

    @staticmethod
    def _baseline(
        db: sqlite3.Connection, project: str, categories: set[str], now: datetime
    ) -> None:
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

    def _dominance_changes(self, db: sqlite3.Connection, planned: list[_Planned]) -> list[str]:
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
            for link in _added(p, "dominates"):
                found.append(
                    f"{p.row.handle} dominates {handle(link.target_id)}: "
                    + _clip(link.note or "", 100)
                )
            found += [
                f"{p.row.handle} no longer dominates {handle(link.target_id)}"
                for link in p.previous
                if link.kind == "dominates" and link.target_id not in final
            ]
        return found[:8]

    def _frontier(
        self,
        db: sqlite3.Connection,
        project: str,
        categories: set[str],
        cause: Json,
        author: Author,
        now: datetime,
    ) -> None:
        """Record what the change did to the best known results of ``categories``
        (LG-24), each category's changes under one ``ledger.frontier_changed`` event."""
        for name, changes in frontier.record(db, project, categories, cause, now.isoformat()):
            self.category_events.append(
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
        moved on. Only the user records one; an agent's check counts for nothing. A
        reference's check vouches for the PDF the revision names (LG-16)."""
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

    def drop_dominates(
        self,
        project: str,
        holder: str,
        target: str,
        revision: int,
        author: Author,
        *,
        reason: str,
        operation_id: str,
    ) -> MutationResult:
        """The user's dropping of ``holder``'s ``dominates`` link to ``target``, with the
        reason (LG-22; the review's M3): a change to ``holder`` at ``revision``, the one
        read, that keeps its other links. A later revision is a conflict."""
        if author.kind != "user":
            raise _refused("only the user drops a dominates link; an agent revises its links")
        self._project(project)
        with self.control.read() as tx:
            entry = self._find(tx.sql, project, holder)
            named = self._find(tx.sql, project, target).entry_id
            links = self._links(tx.sql, entry.entry_id, min(revision, entry.head))
        kept = [
            _given(link)
            for link in links
            if not (link.kind == "dominates" and link.target == "entry" and link.target_id == named)
        ]
        if len(kept) == len(links):
            raise DocumentError(NOT_FOUND, f"{entry.handle} holds no dominates link to {target}")
        return self.change(
            project,
            {
                "operation_id": operation_id,
                "reason": reason,
                "changes": [{"entry": entry.entry_id, "expected": revision, "links": kept}],
            },
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
        self,
        project: str,
        mutations: list[tuple[Mutation, Json | None]],
        author: Author,
        *,
        within: Callable[[sqlite3.Connection, list[MutationResult]], None] | None = None,
    ) -> list[MutationResult]:
        """Apply several mutations in one change, all or none; each keeps its own
        operation ID and receipt. ``provenance`` names where a mutation came from, such
        as the line of a results file (LG-41). ``within`` runs in the same change, after
        them, and refuses it all by raising: so a fold's proposal is marked accepted, or
        nothing is written (CM-15)."""
        role = self._authority(project, author)
        if role != "user":
            kept: list[tuple[Mutation, Json | None]] = []
            for mutation, provenance in mutations:
                try:
                    kept.append((_agent_text(mutation), provenance))
                except DocumentError as error:
                    raise _labelled(error, provenance) from None
            mutations = kept
        mutations = [(_this_session(m, author), provenance) for m, provenance in mutations]
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
            for work in mutation.work or ():
                for cited in work.references:
                    if cited.document and cited.revision and cited.block:
                        blocks.append((cited.document, cited.revision, cited.block, None))
            for anchor in [
                *(a for c in mutation.changes for a in c.anchors or ()),
                *(a for w in mutation.work or () for a in w.anchors or ()),
            ]:
                if anchor.document and anchor.revision and anchor.block:
                    blocks.append((anchor.document, anchor.revision, anchor.block, None))
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
            if within is not None:
                within(tx.sql, results)
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
        planned = [
            self._plan(db, project, change, role, numbers, author.session_id)
            for change in mutation.changes
        ]
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
            linker = evidence.Linker(role, author.session_id)
            self._evidence(db, project, p, keys, heads, pinned, linker)
            if not p.created and not p.changed:
                raise DocumentError(
                    INVALID_REQUEST, f"the change to {p.row.handle} changes nothing"
                )
        self._check_links(db, planned)
        self._check_research(db, project, planned, author)
        for p in planned:
            self._provenance(db, project, p, author, pinned, provenance)
        self._check_dominates(db, project, planned, role)
        self._check_quotes(db, planned)
        self._check_published(db, planned)
        gated = self._check_decisions(db, planned, role)
        now = self.clock()
        cleared = self._clear(db, project, mutation, author, role, now)
        reopened = self._reopen(db, project, mutation, author, role, now)
        self._baseline(db, project, self._categories(db, planned, written=False), now)
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
        self._check_cycles(db, project, planned)
        work, rejected = self._record_work(
            db, project, mutation, author, keys, pinned, provenance, now.isoformat()
        )
        work += self._reopened(db, project, planned, mutation, author, provenance, now)
        raised = flags.raise_flags(
            db, project, [self._changed(p) for p in planned], now.isoformat()
        )
        raised += rejected
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
        self._frontier(
            db,
            project,
            self._categories(db, planned),
            {
                "operation_id": mutation.operation_id,
                "author": author.actor,
                "reason": _clip(mutation.reason, 500),
                "entries": [p.row.handle for p in planned if p.row.type == "result"],
                **(
                    {"dominates": linked}
                    if (linked := self._dominance_changes(db, planned))
                    else {}
                ),
            },
            author,
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
            work=work,
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

    def _record_work(
        self,
        db: sqlite3.Connection,
        project: str,
        mutation: Mutation,
        author: Author,
        keys: dict[str, _Row],
        pinned: dict[evidence.Block, str],
        provenance: Json | None,
        now: str,
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
            row = self._work_entry(db, project, item.entry, keys)
            if row.type not in WORK_HOLDERS:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{where} is on {row.handle}: work is recorded on an issue, a claim or a "
                    "result",
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
                self._produced(db, project, e, keys, row, item.references)
                for e in item.produced or ()
            ]
            part_of = None if item.part_of is None else _work_id(db, project, item.part_of, named)
            detail: Json = {}
            if item.limit is not None:
                detail["limit"] = item.limit
            if produced:
                detail["produced"] = [p.entry_id for p in produced]
            assessed = None
            if item.assesses is not None:
                assessed = _work_id(db, project, item.assesses, named)
                (on,) = db.execute(
                    "SELECT entry_id FROM ledger_work WHERE record_id=?", (assessed,)
                ).fetchone()
                if on != row.entry_id:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{where} assesses work on another entry: record the assessment on "
                        "the entry the work it assesses is on",
                    )
            cited = [self._cited(db, project, ref, named, pinned) for ref in item.references]
            basis, anchors = self._sources(
                db, project, item.basis, item.anchors or [], author, pinned, provenance
            )
            if author.kind != "user" and not _platforms(provenance):
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
                    _normalized(item.tried),
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
                    provenance_json(anchors),
                ),
            )
            self.events.append(
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
                and _rests_on(db, row.entry_id, assessed)
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

    def _reopened(
        self,
        db: sqlite3.Connection,
        project: str,
        planned: list[_Planned],
        mutation: Mutation,
        author: Author,
        provenance: Json | None,
        now: datetime,
    ) -> list[str]:
        """A work record on each obstructed issue an agent's change reopens (the owner,
        2026-10-15): reopening weakens a settled state and needs no decision, and is kept
        with the change's reason, citing the decision that had made the issue obstructed."""
        made: list[str] = []
        for p in planned:
            if (
                author.kind == "user"
                or p.status_before != "obstructed"
                or p.status == p.status_before
            ):
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
                    _normalized(f"Reopened {p.row.handle}{cited}: {mutation.reason}"),
                    json.dumps({} if decider is None else {"decision": decider.entry_id}),
                    author.model_dump_json(exclude_none=True),
                    mutation.reason,
                    mutation.operation_id,
                    None if provenance is None else json.dumps(provenance, sort_keys=True),
                    now.isoformat(),
                    provenance_json(
                        []
                        if decider is None
                        else [{"entry": decider.entry_id, "revision": decider.revision}]
                    ),
                ),
            )
            self.events.append(
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

    def _produced(
        self,
        db: sqlite3.Connection,
        project: str,
        reference: str,
        keys: dict[str, _Row],
        on: _Row,
        cited: list[WorkReference],
    ) -> _Row:
        """An entry a work record says it produced (LG-6): one the same change creates,
        or one the delegation the record cites created (its child run wrote the entry's
        first revision); never the entry the record is on."""
        found = self._work_entry(db, project, reference, keys)
        if found.entry_id == on.entry_id:
            raise DocumentError(
                INVALID_REQUEST, f"a work record on {on.handle} names it as produced"
            )
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

    def _work_entry(
        self, db: sqlite3.Connection, project: str, reference: str, keys: dict[str, _Row]
    ) -> _Row:
        """The entry a work record names: one the change creates, by ``new:<key>``, or
        one of the project's."""
        if reference.startswith("new:"):
            found = keys.get(reference.removeprefix("new:"))
            if found is None:
                raise DocumentError(INVALID_REQUEST, f"no new entry is keyed {reference}")
            return found
        return self._find(db, project, reference)

    @staticmethod
    def _cited(
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
            cited["work"] = _work_id(db, project, ref.work, named)
        if ref.note is not None:
            cited["note"] = ref.note
        return cited

    def work(
        self, project: str, reference: str, *, before: int | None = None, limit: int = 50
    ) -> WorkPage:
        """The work records on an entry, newest first, as many as ``limit`` (1 to 100)
        before position ``before``, with where the next page begins (LG-6)."""
        self._project(project)
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise DocumentError(INVALID_REQUEST, "invalid work page")
        with self.control.read() as tx:
            db = tx.sql
            row = self._find(db, project, reference)
            found = db.execute(
                "SELECT record_id,position,entry_revision,tried,ending,detail,refs,part_of,"
                "author,reason,operation_id,provenance,created,assesses,verdict,basis,anchors "
                "FROM ledger_work "
                "WHERE entry_id=? AND position<? ORDER BY position DESC LIMIT ?",
                (row.entry_id, before or 2**62, limit + 1),
            ).fetchall()
            handles: dict[str, str] = {}

            def handle(entry: str) -> str:
                if entry not in handles:
                    handles[entry] = self._find(db, project, entry).handle
                return handles[entry]

            (status,) = db.execute(
                "SELECT status FROM ledger_revisions WHERE entry_id=? AND revision=?",
                (row.entry_id, row.head),
            ).fetchone()
            settled = standing.obstructed(db, row.entry_id) if status == "obstructed" else None
            records = [
                WorkRecord(
                    record_id=r[0],
                    position=r[1],
                    entry_id=row.entry_id,
                    handle=row.handle,
                    entry_revision=r[2],
                    tried=r[3],
                    ending=r[4],
                    limit=(detail := json.loads(r[5])).get("limit"),
                    produced=[handle(e) for e in detail.get("produced", [])],
                    assesses=r[13],
                    verdict=r[14],
                    references=json.loads(r[6]),
                    part_of=r[7],
                    author=json.loads(r[8]),
                    reason=r[9],
                    operation_id=r[10],
                    provenance=None if r[11] is None else json.loads(r[11]),
                    created=r[12],
                    standing=standing.of_work(r[4], detail.get("limit"), r[0], settled),
                    decision=handle(detail["decision"]) if "decision" in detail else None,
                    basis=r[15],
                    anchors=json.loads(r[16] or "[]"),
                )
                for r in found[:limit]
            ]
        more = len(found) > limit
        return WorkPage(
            entry_id=row.entry_id,
            handle=row.handle,
            records=records,
            next_before=records[-1].position if more else None,
        )

    def _plan(
        self,
        db: sqlite3.Connection,
        project: str,
        change: EntryChange,
        role: str,
        numbers: dict[str, int],
        session: str | None = None,
    ) -> _Planned:
        if change.entry is None:
            if change.repin is not None:
                raise DocumentError(INVALID_REQUEST, "a new entry's references are pinned anew")
            return self._plan_create(db, project, change, role, numbers)
        if change.type is not None or change.origin is not None or change.key is not None:
            raise DocumentError(INVALID_REQUEST, "a revision keeps the entry's type and origin")
        if change.expected is None:
            raise DocumentError(INVALID_REQUEST, "a revision names the revision it expects")
        row = self._find(db, project, change.entry)
        if role == "delegated" and (
            not change.evidence
            or change.model_dump(
                exclude_none=True, exclude={"entry", "expected", "evidence", "basis", "anchors"}
            )
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
            fields_before=json.loads(current[4]),
        )
        if change.title is not None and change.title != current[1]:
            planned.title, planned.changed = change.title, [*planned.changed, "title"]
        if change.statement is not None:
            text = _normalized(change.statement)
            saved = TEXTS.read(db, row.entry_id, current[3], full=False)
            if saved is None or text != saved["body"]:
                planned.statement, planned.changed = text, [*planned.changed, "statement"]
        if change.fields is not None:
            given = change.fields
            if (
                row.type == "decision"
                and change.status not in (None, current[2])
                and set(given) <= {"reopen_condition"}
            ):
                # A decision rejected or reversed states its reopen condition and keeps its
                # other fields, such as an obstruction's excluded scope (CM-10).
                given = {**planned.fields, **given}
            fields = _fields(row.type, given)
            _check_pdf(db, project, fields)
            frontier.check_result(db, project, fields)
            if fields != planned.fields:
                planned.fields, planned.changed = fields, [*planned.changed, "fields"]
        if change.status is not None and change.status != current[2]:
            planned.status = _status(row, current[2], change.status)
            planned.changed = [*planned.changed, "status"]
        if change.links is not None:
            planned.previous = self._links(db, row.entry_id, row.head)
        if change.repin is not None:
            body = planned.statement
            if body is None:
                saved = TEXTS.read(db, row.entry_id, current[3], full=False)
                assert saved is not None  # noqa: S101 - written with its revision
                body = saved["body"]
            moves = {("statement", item.path): item.revision for item in change.repin}
            pins, moved = pin_references(
                db,
                project,
                session,
                text_references(body, "statement"),
                json.loads(current[10] or "[]"),
                moves,
            )
            if moved:
                # A pin moved is no change of the statement's text, its evidence or what
                # it claims, so it flags nothing (LG-28 as amended for figures).
                planned.pins, planned.changed = pins, [*planned.changed, "pins"]
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
        if kind == "decision" and change.status in ("in_effect", "rejected"):
            # LG-17: the user or the main agent records a decision in effect at once, and
            # an approach abandoned as a rejected one, with its reopen condition (CM-10).
            if role == "delegated":
                raise _refused("a delegated agent's decision is proposed")
            status = change.status
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
            fields=_result(
                db, project, _check_pdf(db, project, _fields(kind, change.fields or {}))
            ),
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
        if link.kind == "dominates" and link.note is None:
            raise DocumentError(INVALID_REQUEST, "a dominates link states its reason")
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
            raise DocumentError(
                INVALID_REQUEST,
                wrong_link_target(link.kind, planned.row.type, target.handle, target.type),
            )
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
                _encoded(p.fields),
                author.model_dump_json(exclude_none=True),
                mutation.reason,
                mutation.operation_id,
                None if provenance is None else json.dumps(provenance, sort_keys=True),
                now.isoformat(),
                p.basis,
                provenance_json(p.anchors),
                json.dumps(pins, sort_keys=True) if pins else None,
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
        linker: evidence.Linker,
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
            added.append(evidence.resolve(db, project, holder, item, target, pinned, linker))
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
        accepted assessment (LG-7, ``_check_obstruction``). An entry leaves
        ``refuted``, ``withdrawn`` or ``superseded`` only by such a decision too, which
        for the main agent's on a claim or result passes the gate, whatever status it
        brings the entry back to; and a claim or result that was once established or
        recorded keeps its statement, fields, assumptions and evidence while it is
        ``disputed``, as a decided one does. Returns the entries the gate passed, which
        the flags the change raises must spare."""
        gated: set[str] = set()
        deciding = [
            p
            for p in planned
            if p.row.type == "decision"
            and p.status == "in_effect"
            and p.status_before != "in_effect"
        ]
        for p in planned:
            decided = (
                p.status in DECIDED
                or (p.row.type == "decision" and p.status == "reversed")
                # Set aside, an entry comes back only by a decision.
                or (not p.created and p.status_before in SET_ASIDE)
            )
            # An entry stays as decided: while it is established, refuted or obstructed,
            # or disputed after it was established or recorded, what a decision rested on
            # changes only with its status or a new decision.
            rewritten = (
                not p.created
                and p.status == p.status_before
                and (p.status_before in DECIDED or self._disputed_once_held(db, p))
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
                    f"{p.row.handle} leaves {p.status_before} only in a change that puts in "
                    "effect a decision that affects it"
                    if p.status_before in SET_ASIDE
                    else f"{p.row.handle} becomes {p.status} only in a change that puts in "
                    "effect a decision that affects it",
                )
            if role != "main":
                continue
            # Set aside, a claim or result the main agent brings back by any road passes
            # the gate: through disputed or proposed it could otherwise be recorded again
            # with nothing checked.
            if p.status == "established" or (
                p.status_before in SET_ASIDE and p.row.type in ("claim", "result")
            ):
                held = self._final_evidence(db, p)
                latest = TEXTS.latest(db, p.row.entry_id)
                assert latest is not None  # noqa: S101 - an existing entry has a statement
                now = gate.material(
                    p.fields,
                    None if p.statement is not None else latest[0],
                    self._final_links(db, p),
                    held,
                    latest[1] if p.statement is None else sha(p.statement),
                )
                gate.establish(db, p.row.entry_id, p.row.handle, held, now, p.row.head)
                gated.add(p.row.entry_id)
            elif p.status == "obstructed":
                self._check_obstruction(db, p, deciding)
        return gated

    def _check_obstruction(
        self, db: sqlite3.Connection, p: _Planned, deciding: list[_Planned]
    ) -> None:
        """The main agent's decision makes or keeps an issue ``obstructed`` (LG-7, LG-F8)
        only when it names the excluded scope, its evidence (an evidence link) and the
        assessment that accepted the claimed obstruction: the user's, the latest of the
        user's on that claim, both on the issue; and only for the content they were made
        on: the issue's statement and fields at the claim's and the assessment's revisions
        are those it has, and the change leaves them as they are. So a reworded issue, or
        an obstructed one rewritten, needs a fresh acceptance (as #277 and #281 hold for
        decided entries). A delegation started to assess the claim will count too once
        delegations can be briefed to assess one."""
        issue = p.row

        def refuse(why: str) -> NoReturn:
            raise gate.refused(
                issue.entry_id,
                "assessment",
                f"{issue.handle} is obstructed by the main agent's decision only on the "
                f"user's accepted assessment of an obstruction claimed on it: {why}",
            )

        naming = [
            d
            for d in deciding
            if d is not p
            and any(
                link.kind == "affects" and link.target_id == issue.entry_id
                for link in self._final_links(db, d)
            )
        ]
        named = [
            d
            for d in naming
            if d.fields.get("assessment")
            and d.fields.get("excluded_scope")
            and self._final_evidence(db, d)
        ]
        if not named:
            refuse(
                "the decision names the assessment (fields.assessment), the scope it "
                "excludes (fields.excluded_scope) and its evidence (an evidence link)"
            )
        if p.created or "statement" in p.changed or "fields" in p.changed:
            refuse("the change keeps the issue's statement and fields as they were assessed")
        current = _content(db, issue.entry_id, issue.head)
        for d in named:
            found = db.execute(
                "SELECT a.entry_id,a.verdict,json_extract(a.author,'$.kind'),"
                "a.entry_revision,c.record_id,c.entry_id,c.ending,c.entry_revision,a.created "
                "FROM ledger_work a JOIN ledger_work c ON c.record_id=a.assesses "
                "WHERE a.record_id=? AND a.project=?",
                (d.fields["assessment"], issue.project),
            ).fetchone()
            if found is None:
                refuse(f"no assessment {d.fields['assessment']} of a claim on it")
            on, verdict, kind, at, claim, claimed_on, ending, claimed_at, made = found
            # The owner's rule (2026-10-15): the acceptance is recorded after the issue was
            # last weakened, reopened among others, or obstructed.
            moved = [
                datetime.fromisoformat(created).timestamp()
                for (created,) in db.execute(
                    "SELECT r.created FROM ledger_revisions r JOIN ledger_revisions b "
                    "ON b.entry_id=r.entry_id AND b.revision=r.revision-1 WHERE r.entry_id=? "
                    "AND r.status='obstructed' AND b.status!='obstructed'",
                    (issue.entry_id,),
                )
            ]
            since = max([*moved, gate.weakened(db, issue.entry_id) or 0.0], default=0.0)
            if datetime.fromisoformat(made).timestamp() <= since:
                refuse(
                    "the acceptance it names was recorded before the issue was last obstructed, "
                    "reopened or flagged: an obstruction needs an acceptance recorded since"
                )
            if (on, claimed_on, ending) != (issue.entry_id, issue.entry_id, "obstruction_claimed"):
                refuse("the assessment it names is not of an obstruction claimed on it")
            if (verdict, kind) != ("accepted", "user"):
                refuse("the assessment it names is not the user's acceptance")
            (latest,) = db.execute(
                "SELECT verdict FROM ledger_work WHERE assesses=? "
                "AND json_extract(author,'$.kind')='user' ORDER BY position DESC LIMIT 1",
                (claim,),
            ).fetchone()
            if latest != "accepted":
                refuse("the user's latest assessment of the claim does not accept it")
            if db.execute(
                "SELECT 1 FROM ledger_revisions r JOIN ledger_entries e USING(entry_id) "
                "WHERE e.type='decision' AND r.entry_id!=? "
                "AND json_extract(r.fields,'$.assessment')=? LIMIT 1",
                (d.row.entry_id, d.fields["assessment"]),
            ).fetchone():
                refuse(
                    "the acceptance it names settled an earlier obstruction: each obstruction "
                    "needs a fresh acceptance of the user's"
                )
            if not (
                _content(db, issue.entry_id, at)
                == current
                == _content(db, issue.entry_id, claimed_at)
            ):
                refuse(
                    "its statement or fields changed since the claim or its acceptance: a "
                    "reworded issue needs a fresh acceptance"
                )

    def assess(
        self,
        project: str,
        reference: str,
        claim: str,
        verdict: str,
        revision: int,
        author: Author,
        *,
        note: str,
        operation_id: str,
    ) -> MutationResult:
        """The user's assessment of an obstruction claimed on ``reference`` (LG-7): a work
        record ending ``assessment`` that accepts or rejects ``claim``, with the note as
        what the user read, on the issue as it reads at ``revision``; a later revision is
        a conflict. Only the user records one this way; agents use their change tool."""
        if author.kind != "user":
            raise _refused("only the user records an assessment this way")
        self._project(project)
        with self.control.read() as tx:
            row = self._find(tx.sql, project, reference)
        if row.head != revision:
            raise DocumentError(
                CONFLICT,
                f"{row.handle} is at revision {row.head}, not {revision}; read it again",
                {"entry": row.entry_id, "current": row.head},
            )
        return self.change(
            project,
            {
                "operation_id": operation_id,
                "reason": note,
                "work": [
                    {
                        "entry": row.entry_id,
                        "tried": note,
                        "ending": "assessment",
                        "assesses": claim,
                        "verdict": verdict,
                    }
                ],
            },
            author,
        )

    @staticmethod
    def _disputed_once_held(db: sqlite3.Connection, p: _Planned) -> bool:
        """Whether ``p`` is a claim or result, disputed, that was once established or
        recorded: the frontier still shows it as it was then (LG-20)."""
        return (
            p.status_before == "disputed"
            and p.row.type in ("claim", "result")
            and db.execute(
                "SELECT 1 FROM ledger_revisions WHERE entry_id=? "
                "AND status IN ('established','recorded') LIMIT 1",
                (p.row.entry_id,),
            ).fetchone()
            is not None
        )

    def _rewrites(self, db: sqlite3.Connection, p: _Planned) -> bool:
        """Whether the change alters what a decision on ``p``'s entry rested on: its
        statement, fields, ``assumes`` links or evidence."""
        if {"statement", "evidence"} & set(p.changed):
            return True
        if "fields" in p.changed and _unsettles(p.fields_before, p.fields):
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
                raise _refused(
                    f"{handle} was settled when the flag was raised or since: only the user "
                    "clears a flag on a settled issue or decision"
                )
            if not gate.fresh_check(db, entry, flag):
                raise _refused(
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

    def _check_quotes(self, db: sqlite3.Connection, planned: list[_Planned]) -> None:
        """A quote names what it quotes as the reference reads at the revision it pins
        (LG-8, CR-4): a PDF quote, the reference's PDF, pinned by its SHA-256; an ePrint
        quote, the reference's ePrint identifier. The reference may be one the same
        change records or revises."""
        by_id = {p.row.entry_id: p for p in planned}
        for p in planned:
            if p.evidence is None:
                continue
            kept: list[EvidenceLink] = []
            for link in p.evidence:
                if link.target != "quote" or link.locator is None or "named" in link.locator:
                    kept.append(link)
                    continue
                kept.append(self._quoted(db, by_id, p, link))
            p.evidence = kept

    @staticmethod
    def _quoted(
        db: sqlite3.Connection, by_id: dict[str, _Planned], p: _Planned, link: EvidenceLink
    ) -> EvidenceLink:
        """``link``, a new quote, with what it quotes named from its reference's fields."""
        assert link.locator is not None  # noqa: S101 - a quote has one
        assert link.revision is not None  # noqa: S101 - and pins its reference
        reference = by_id.get(link.target_id)
        stored = db.execute(
            "SELECT head FROM ledger_entries WHERE entry_id=?", (link.target_id,)
        ).fetchone()
        if reference is not None and (stored is None or link.revision > stored[0]):
            fields: Json = reference.fields  # the revision the change makes
        else:
            row = db.execute(
                "SELECT fields FROM ledger_revisions WHERE entry_id=? AND revision=?",
                (link.target_id, link.revision),
            ).fetchone()
            fields = {} if row is None else json.loads(row[0])
        locator = dict(link.locator)
        handle = link.handle or link.target_id
        if locator["source"] == "pdf":
            pdf = fields.get("pdf")
            if not isinstance(pdf, dict):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle} quotes a PDF of {handle}, which names none: give the "
                    "reference its pdf, or quote the ePrint",
                )
            locator["named"] = {"artifact_id": pdf["artifact_id"], "revision": pdf["revision"]}
            return link.model_copy(update={"locator": locator, "sha256": pdf["sha256"]})
        identifiers = cast(Json, fields.get("identifiers") or {})
        eprint = identifiers.get("eprint")
        if not eprint:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} quotes an ePrint of {handle}, which names none: give the "
                "reference its eprint identifier, or quote its PDF",
            )
        locator["named"] = {"eprint": eprint}
        return link.model_copy(update={"locator": locator})

    def _check_published(self, db: sqlite3.Connection, planned: list[_Planned]) -> None:
        """A published claim or result cites its reference through a ``supports`` link of
        type ``source_claim`` (LG-15), which may be to a reference the same change
        records."""
        made = {p.row.entry_id for p in planned if p.row.type == "reference"}
        for p in planned:
            if p.row.origin != "published" or (not p.created and p.evidence is None):
                continue
            targets = {
                link.target_id for link in p.evidence or () if link.target in ("entry", "quote")
            }
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

    def _check_dominates(
        self, db: sqlite3.Connection, project: str, planned: list[_Planned], role: str
    ) -> None:
        """A ``dominates`` link a change adds (LG-22): the user's or the main agent's,
        between two results of one category, both eligible as the change leaves them,
        where the rule cannot compare them."""
        by_id = {p.row.entry_id: p for p in planned}
        for p in planned:
            for link in _added(p, "dominates"):
                if role == "delegated":
                    raise _refused("a delegated agent records no dominates link")
                other = by_id.get(link.target_id)
                if other is not None:
                    target = (other.row.handle, other.row.origin, other.status, other.fields)
                else:
                    row = db.execute(
                        "SELECT e.type,e.number,e.origin,r.status,r.fields FROM ledger_entries e "
                        "JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                        "WHERE e.entry_id=?",
                        (link.target_id,),
                    ).fetchone()
                    target = (f"{PREFIXES[row[0]]}-{row[1]}", row[2], row[3], json.loads(row[4]))
                handle, origin, status, fields = target
                for name, entry, of, now in (
                    (p.row.handle, p.row.entry_id, p.row.origin, p.status),
                    (handle, link.target_id, origin, status),
                ):
                    if not frontier.is_eligible(db, entry, of, now):
                        raise DocumentError(
                            INVALID_REQUEST,
                            f"{name} is not eligible for the frontier: a dominates link is "
                            "between recorded published or established project results",
                        )
                name = p.fields.get("category")
                category = None if name is None else frontier.read(db, project, name)
                if category is None or fields.get("category") != name:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{p.row.handle} and {handle} are not results of one frontier category",
                    )
                if frontier.comparable(category, p.fields, fields):
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"the frontier compares {p.row.handle} and {handle} itself: their "
                        "conditions are equal and their measures numbers",
                    )

    def _check_cycles(self, db: sqlite3.Connection, project: str, planned: list[_Planned]) -> None:
        """Once a change is written, no ``dominates`` link it added lies on a cycle of
        results that dominate one another, by links or by the rule (the review's M2), as
        the frontier reads them: a disputed result as the revision before, and only the
        results it holds eligible."""
        added = {
            (p.row.entry_id, link.target_id): p for p in planned for link in _added(p, "dominates")
        }
        if not added:
            return
        names = {p.fields.get("category") for p in added.values()} - {None}
        for name in sorted(str(n) for n in names):
            category = frontier.read(db, project, name)
            if category is None:
                continue
            for link in frontier.view(db, project, category).void_links:
                p = added.get((link.entry_id, _entry_id(db, project, link.target)))
                if p is not None and link.void == frontier.CYCLE:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{link.handle} dominating {link.target} closes a cycle: "
                        f"{link.target} already dominates {link.handle}, by dominates links "
                        "or by the rule, through the results between, as the frontier "
                        "reads them",
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

    def _sources(
        self,
        db: sqlite3.Connection,
        project: str,
        basis: str | None,
        anchors: list[AnchorInput],
        author: Author,
        pinned: dict[evidence.Block, str],
        provenance_: Json | None,
    ) -> tuple[str, list[Json]]:
        """The basis and resolved anchors of a revision or work record (CM-9): the user's
        are the user's; an import's are anchored on its results file's line, observed when
        the file is an artifact revision an execution produced and interpreted otherwise;
        a check's outcome is observed, anchored on its delegation; an agent's are what it
        names, where it may look, ``interpreted`` by default, and never the user's own."""

        def find(reference: str) -> tuple[str, int]:
            row = self._find(db, project, reference)
            return row.entry_id, row.head

        def block(name: str, revision: int, block: str) -> str:
            return pinned[(name, revision, block, None)]

        kept = anchoring.resolve(db, project, anchors, block, find, _scope(db, project, author))
        if author.kind == "user" and provenance_ is not None and "proposal" in provenance_:
            # A fold's proposal the user accepts keeps its basis: its words are the model's.
            return basis or "interpreted", kept
        if author.kind == "user":
            return "user", kept
        if provenance_ is not None and "file" in provenance_:
            artifact, revision = provenance_.get("artifact"), provenance_.get("revision")
            digest = str(provenance_.get("sha256"))
            executed = None
            if isinstance(artifact, str) and isinstance(revision, int):
                executed = anchoring.executed(db, project, artifact, revision, digest)
            if executed is None:
                line = {k: provenance_.get(k) for k in ("file", "sha256", "line")}
                return "interpreted", [line, *kept]
            line = {"artifact": artifact, "revision": revision, "sha256": digest}
            line["line"] = provenance_.get("line")
            return ("observed" if executed else "interpreted"), [line, *kept]
        if provenance_ is not None and "delegation" in provenance_:
            return "observed", [{"delegation": provenance_["delegation"]}, *kept]
        if basis == "user":
            raise anchoring.refused(
                "an agent's record is never the user's own: record what the user said as "
                "user_quoted, with a quote of the user's message"
            )
        return basis or "interpreted", kept

    def _provenance(
        self,
        db: sqlite3.Connection,
        project: str,
        p: _Planned,
        author: Author,
        pinned: dict[evidence.Block, str],
        provenance_: Json | None,
    ) -> None:
        """Set ``p``'s basis and anchors (CM-9), refusing an agent's change that needs an
        anchor and names none, whose evidence included, or whose basis its anchors do not
        bear out."""
        p.basis, p.anchors = self._sources(
            db, project, p.change.basis, p.change.anchors or [], author, pinned, provenance_
        )
        if author.kind == "user" or _platforms(provenance_):
            return
        held = self._final_evidence(db, p)
        needs = (
            p.created
            or {"statement", "fields"} & set(p.changed)
            # A pin moved on a decided entry changes what it shows, as content does.
            or ("pins" in p.changed and p.status_before in _DECIDED)
            or (
                p.row.type == "decision"
                and "status" in p.changed
                and p.status in ("in_effect", "rejected", "reversed")
            )
        )
        if needs and not p.anchors and not held:
            raise anchoring.refused(
                f"{p.row.handle} names no anchor: a message by its ID, a tool result by its "
                "call, an artifact, a document block or an entry revision, or evidence it holds"
            )
        statement = p.statement
        if statement is None and not p.created:
            latest = TEXTS.latest(db, p.row.entry_id)
            text = None if latest is None else TEXTS.read(db, p.row.entry_id, latest[0])
            statement = "" if text is None else text["body"]
        texts = [statement or "", *anchoring.values(p.fields)]
        anchoring.check(db, project, p.basis or "", p.anchors, held, texts, p.row.handle)

    def _check_research(
        self, db: sqlite3.Connection, project: str, planned: list[_Planned], author: Author
    ) -> None:
        """The research state's rules (CM-10), as the change leaves its entries: a
        contradiction concerns the two sides, at least, that disagree; a decision's reopen
        condition, its ``reopen_on`` links and its session follow ``_check_condition``,
        ``_check_kept`` and ``_check_session``; and only a decision that the change puts
        in effect to make an issue obstructed names an excluded scope and an assessment
        (LG-7), so that no later decision widens a settled impossibility."""
        by_id = {p.row.entry_id: p for p in planned}
        deciding = [
            d
            for d in planned
            if d.row.type == "decision" and d.status == "in_effect" and d.status_before != d.status
        ]
        for p in planned:
            links = self._final_links(db, p)
            if p.row.type == "issue" and p.fields.get("kind") == "contradiction":
                sides = {(k.target, k.target_id, k.block) for k in links if k.kind == "concerns"}
                if len(sides) < 2:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{p.row.handle} is a contradiction: it concerns the entries, findings "
                        "or document blocks that disagree, at least two",
                    )
            if p.row.type == "issue" and p.status == "obstructed" != p.status_before:
                deciders = [
                    d
                    for d in deciding
                    if any(
                        k.kind == "affects" and k.target_id == p.row.entry_id
                        for k in self._final_links(db, d)
                    )
                ]
                if len(deciders) > 1:
                    raise DocumentError(
                        INVALID_REQUEST, f"one decision makes {p.row.handle} obstructed"
                    )
                if (
                    author.kind == "user"
                    and deciders
                    and not deciders[0].fields.get("excluded_scope")
                ):
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"the decision that makes {p.row.handle} obstructed names the scope it "
                        "excludes (fields.excluded_scope)",
                    )
            dropped = set(p.fields_before.get("caveats", ())) - set(p.fields.get("caveats", ()))
            if author.kind != "user" and dropped:
                raise _refused(
                    f"an agent adds caveats to {p.row.handle} and removes none: the user may "
                    "remove one"
                )
            if p.row.type != "decision":
                continue
            self._check_condition(db, p, links, by_id)
            self._check_kept(db, p, author, deciding)
            self._check_session(project, p, author)
            named = [p.fields.get(k) for k in ("excluded_scope", "assessment")]
            before = [p.fields_before.get(k) for k in ("excluded_scope", "assessment")]
            if named != before and any(named):
                obstructs = any(d is p for d in deciding) and any(
                    k.kind == "affects"
                    and (target := by_id.get(k.target_id)) is not None
                    and target.row.type == "issue"
                    and target.status == "obstructed" != target.status_before
                    for k in links
                )
                if not obstructs:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{p.row.handle} names an excluded scope or an assessment only in the "
                        "change that puts it in effect and makes the issue it affects obstructed",
                    )

    def _check_condition(
        self,
        db: sqlite3.Connection,
        p: _Planned,
        links: list[EntryLink],
        by_id: dict[str, _Planned],
    ) -> None:
        """A decision's reopen condition and its ``reopen_on`` links come together, a
        condition the ledger does not hold yet being stated as an open issue of kind
        question that it links to; one that becomes rejected or reversed states its
        condition, ``none`` when nothing would reopen it (CM-10)."""
        condition = p.fields.get("reopen_condition")
        watched = [k for k in links if k.kind == "reopen_on"]
        if condition is None and watched:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle}'s reopen_on links watch for its reopen condition: give "
                "fields.reopen_condition",
            )
        if condition is not None and condition.strip().casefold() == NO_CONDITION:
            if watched:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{p.row.handle}'s reopen condition is none, so it watches nothing",
                )
        elif condition is not None and not watched:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle}'s reopen condition names what it watches with reopen_on "
                "links: an open issue of kind question that states the condition, or the "
                "assumption, claim or result whose change would matter",
            )
        moved = p.created or "status" in p.changed
        if p.status in ("rejected", "reversed") and moved and condition is None:
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} becomes {p.status}: state what would justify reopening "
                f"it (fields.reopen_condition), or {NO_CONDITION}",
            )
        for link in _added(p, "reopen_on"):
            target = by_id.get(link.target_id)
            kind, status, asked = (
                (target.row.type, target.status, target.fields.get("kind"))
                if target is not None
                else db.execute(
                    "SELECT e.type,r.status,json_extract(r.fields,'$.kind') FROM ledger_entries "
                    "e JOIN ledger_revisions r ON r.entry_id=e.entry_id AND r.revision=e.head "
                    "WHERE e.entry_id=?",
                    (link.target_id,),
                ).fetchone()
            )
            if kind == "issue" and (status != "open" or (asked or "question") != "question"):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{link.handle} is not an open question: a reopen condition watches an "
                    "open issue of kind question",
                )

    def _check_kept(
        self, db: sqlite3.Connection, p: _Planned, author: Author, deciding: list[_Planned]
    ) -> None:
        """A decision in effect, rejected or reversed keeps its statement, fields, links
        and evidence unless it changes its status in the same change or a decision put in
        effect by it affects it (LG-30). The user edits a decision of the user's freely; an
        agent never changes one's title or content, but may state the reopen condition of
        one it rejects or reverses, if it has none (the coordinator, 2026-10-15)."""
        content = {"statement", "fields", "links", "evidence"} & set(p.changed)
        if p.created or not (content or {"title", "pins"} & set(p.changed)):
            return
        users = db.execute(
            "SELECT 1 FROM ledger_revisions WHERE entry_id=? "
            "AND json_extract(author,'$.kind')='user' LIMIT 1",
            (p.row.entry_id,),
        ).fetchone()
        if users and author.kind == "user":
            return
        if users:
            others = {k: v for k, v in p.fields.items() if k != "reopen_condition"}
            final = [k for k in self._final_links(db, p) if k.kind != "reopen_on"]
            stated = (
                not {"statement", "title", "evidence", "pins"} & set(p.changed)
                and "status" in p.changed
                and p.status in ("rejected", "reversed")
                and "reopen_condition" not in p.fields_before
                and others == p.fields_before
                and ("links" not in p.changed or final == p.previous)
            )
            if not stated:
                raise _refused(
                    f"{p.row.handle} is the user's decision: an agent changes its status by a "
                    "decision, and never its title, statement (its pins included), fields, "
                    "links or evidence"
                )
        if (
            content
            and p.status_before in ("in_effect", "rejected", "reversed")
            and "status" not in p.changed
            and not any(
                d is not p
                and any(
                    k.kind == "affects" and k.target_id == p.row.entry_id
                    for k in self._final_links(db, d)
                )
                for d in deciding
            )
        ):
            raise DocumentError(
                INVALID_REQUEST,
                f"{p.row.handle} is {p.status_before}: its statement, fields, links and "
                "evidence change only with its status, or in a change that puts in effect a "
                "decision that affects it",
            )

    def _check_session(self, project: str, p: _Planned, author: Author) -> None:
        """A decision scoped to a session names one of the project's; an agent scopes a
        decision to its own session, or takes its own session's scope off, and no other."""
        session, before = p.fields.get("session"), p.fields_before.get("session")
        if session == before:
            return
        if author.kind != "user" and {session, before} - {None, author.session_id}:
            raise _refused("an agent scopes a decision to its own session, and no other")
        if (
            session is not None
            and session not in recorded(self.documents.path.parent, project).sessions
        ):
            raise DocumentError(NOT_FOUND, f"no session {session} in this project")

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
    def _summary(
        row: _Row, revision: tuple[Any, ...], asserted_only: bool | None, label: Standing | None
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


def _result(db: sqlite3.Connection, project: str, fields: Json) -> Json:
    """``fields``, once a result's category reads them (LG-19)."""
    frontier.check_result(db, project, fields)
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


def _entry_id(db: sqlite3.Connection, project: str, handle: str) -> str:
    """The ID of the result ``handle`` names in ``project``."""
    number = int(handle.removeprefix(f"{PREFIXES['result']}-"))
    row = db.execute(
        "SELECT entry_id FROM ledger_entries WHERE project=? AND type='result' AND number=?",
        (project, number),
    ).fetchone()
    return "" if row is None else row[0]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _given(link: EntryLink) -> Json:
    """A stored link as a change gives it."""
    value: Json = {"kind": link.kind}
    if link.target == "entry":
        value["entry"] = link.target_id
    elif link.target == "finding":
        value["finding"] = link.target_id
    else:
        value |= {"document": link.target_id, "block": link.block}
    if link.revision is not None:
        value["revision"] = link.revision
    if link.note is not None:
        value["note"] = link.note
    return value


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


def provenance_json(anchors: list[Json]) -> str:
    return json.dumps(anchors, sort_keys=True, ensure_ascii=False)


def _unsettles(before: Json, after: Json) -> bool:
    """Whether fields ``after`` change what a decision on the entry rested on: anything
    but caveats added. A caveat qualifies a settled entry; dropping one strengthens it."""
    kept = [{k: v for k, v in f.items() if k != "caveats"} for f in (before, after)]
    return kept[0] != kept[1] or not set(before.get("caveats", ())) <= set(after.get("caveats", ()))


def _this_session(mutation: Mutation, author: Author) -> Mutation:
    """``mutation``, a decision's session ``this`` naming the author's own (CM-10): an agent
    knows the session it runs in by no ID."""
    changes: list[EntryChange] = []
    for change in mutation.changes:
        if change.fields is None or change.fields.get("session") != THIS_SESSION:
            changes.append(change)
            continue
        if author.session_id is None:
            raise DocumentError(INVALID_REQUEST, "this session names an agent's own session")
        fields = {**change.fields, "session": author.session_id}
        changes.append(change.model_copy(update={"fields": fields}))
    return mutation.model_copy(update={"changes": changes})


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
            _ascii_conditions(change.fields, where)
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
                _agent_evidence(item, f"{where} evidence {index}'s")
                for index, item in enumerate(change.evidence, 1)
            ]
        changes.append(change.model_copy(update=update))
    reason = agent_text(mutation.reason, "the reason")
    work = None
    if mutation.work is not None:
        work = [_agent_work(item, f"work {n}'s") for n, item in enumerate(mutation.work, 1)]
    return mutation.model_copy(update={"reason": reason, "changes": changes, "work": work})


def _content(db: sqlite3.Connection, entry: str, revision: int) -> tuple[Any, ...]:
    """An entry's statement (its text revision) and fields at ``revision``."""
    found = db.execute(
        "SELECT statement,fields FROM ledger_revisions WHERE entry_id=? AND revision=?",
        (entry, revision),
    ).fetchone()
    return tuple(found) if found is not None else ()


def _rests_on(db: sqlite3.Connection, issue: str, claim: str) -> bool:
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


def _scope(db: sqlite3.Connection, project: str, author: Author) -> anchoring.Scope:
    """Where ``author``'s anchors on the archive may point (CM-37, PD-13): an agent's,
    its own session; a delegated agent's, its own run and the messages its brief names."""
    if author.kind != "agent" or author.session_id is None:
        return anchoring.Scope()
    if author.role != "delegated":
        return anchoring.Scope(session=author.session_id, run=author.run_id)
    inputs: frozenset[str] = frozenset()
    for run, brief in db.execute(
        "SELECT run,brief FROM delegations WHERE project_id=? AND session_id=?",
        (project, author.session_id),
    ):
        if author.run_id is not None and run_id_of(run) == author.run_id:
            inputs = anchoring.briefed(brief)
    return anchoring.Scope(
        session=author.session_id, run=author.run_id, delegated=True, inputs=inputs
    )


def _work_id(db: sqlite3.Connection, project: str, reference: str, named: dict[str, str]) -> str:
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


def _agent_work(item: WorkInput, where: str) -> WorkInput:
    """An agent's work record, its text and its references' notes checked."""
    return item.model_copy(
        update={
            "tried": agent_text(item.tried, f"{where} tried"),
            "references": [
                ref
                if ref.note is None
                else ref.model_copy(
                    update={"note": agent_text(ref.note, f"{where} reference {index}'s note")}
                )
                for index, ref in enumerate(item.references, 1)
            ],
        }
    )


def _ascii_conditions(fields: Json, where: str) -> None:
    """An agent writes a result's condition values in printable ASCII (the frontier
    review's M4): a letter of another script that looks Latin, a Unicode dash or slash, or
    other digits would put the result under conditions of its own. The user's are free."""
    conditions = fields.get("conditions")
    if not isinstance(conditions, dict):
        return
    for key, value in cast(Json, conditions).items():
        if isinstance(value, str) and not all(" " <= c <= "~" for c in value):
            raise DocumentError(
                INVALID_REQUEST,
                f"{where} fields.conditions.{key} holds a character outside printable ASCII: "
                "an agent writes condition values in printable ASCII, so that results compare "
                "under one spelling",
            )


def _agent_evidence(item: EvidenceInput, where: str) -> EvidenceInput:
    """An agent's evidence item, its scope and the text it quotes checked."""
    update: dict[str, object] = {}
    if item.scope is not None:
        update["scope"] = agent_text(item.scope, f"{where} scope")
    if item.quote is not None:
        text = agent_text(item.quote.text, f"{where} quote")
        update["quote"] = item.quote.model_copy(update={"text": text})
    return item.model_copy(update=update) if update else item


def _platforms(provenance: Json | None) -> bool:
    """Whether a mutation is a record the platform makes for an agent, anchored by it: an
    import's line or a delegation's outcome. A fold proposal's accepted is checked as the
    acceptor's own (CM-15)."""
    return provenance is not None and ("file" in provenance or "delegation" in provenance)


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
