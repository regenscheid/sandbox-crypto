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
from datetime import UTC, datetime
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control import EventLog, Receipts
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.documents.agent_text import agent_text
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
from sandbox_science.orchestrator.projects import ProjectError, recorded, run_id_of

from . import evidence, flags, frontier, gate, preparation, queries, records, rules, standing
from . import provenance as anchoring
from .models import (
    MAX_CLEARED,
    MAX_ENTRY_WORK,
    NO_CONDITION,
    PREFIXES,
    SHOWN_FLAGGED,
    WORK_HOLDERS,
    Category,
    CategoryDefinition,
    CategoryPage,
    ChangedEntry,
    EntryCitations,
    EntryHistory,
    EntryLink,
    EntryPage,
    EntryView,
    EvidenceLink,
    FrontierHistory,
    FrontierPast,
    FrontierView,
    GateView,
    Mutation,
    MutationResult,
    WorkPage,
    WorkReference,
)
from .preparation import fingerprint as fingerprint
from .records import TEXTS, Planned, Row

Json = dict[str, Any]

"""Statuses that settle an entry: a repin there needs an anchor (figures, FG-8)."""
"""Each entry's statement, a text revision under the entry's ID."""

RECEIPTS: Final = Receipts("ledger", "operation ID {operation!r} names another ledger change")
_PROJECT: Final = "project"
"""The one actor of the ledger's receipts: an operation ID names one change in its
project, whoever retries it, so a shared results file imported from two sessions
records its lines once (LG-31, LG-36, LG-41)."""

"""An entry's fields, as UTF-8 JSON, are at most this many bytes."""

"""The links entries' current revisions hold."""
"""A read shows as many links to an entry as this."""


def _now() -> datetime:
    return datetime.now(UTC)


LISTED_FLAGS: Final = 500
"""The flags one change's event lists; it counts the rest."""


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
        from sandbox_science.orchestrator.context.history.reads import reference_reads  # no cycle

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
            return queries.entry_view(
                tx.sql, project, reference, revision, incoming_after=incoming_after
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
            return queries.entry_page(
                tx.sql, project, kind=kind, status=status, after=after, limit=limit
            )

    def mark(self, project: str) -> tuple[int, int]:
        """A mark of ``project``'s ledger that any change moves: its revisions' count and
        newest row. A store without a ledger yet has (0, 0)."""
        if self.control.missing:
            return (0, 0)
        with self.control.read() as tx:
            return queries.mark(tx.sql, project)

    def history(
        self, project: str, reference: str, *, before: int | None = None, limit: int = 50
    ) -> EntryHistory:
        """An entry's revisions, newest first, before revision ``before``; on the first
        page, the flags cleared on it, latest first, with who cleared them and why."""
        self._project(project)
        if not 1 <= limit <= 200:
            raise DocumentError(INVALID_REQUEST, "a page holds 1 to 200 revisions")
        with self.control.read() as tx:
            return queries.entry_history(tx.sql, project, reference, before=before, limit=limit)

    def citations(
        self, project: str, reference: str, revision: int | None = None
    ) -> EntryCitations:
        """The consistency reviewer's checks of each quote revision ``revision`` (the
        current one by default) of ``reference`` holds (CR-4): newest first, as many as
        ``SHOWN_CHECKS`` with their count, each saying whether it checked that revision's
        material. A read: it changes nothing."""
        self._project(project)
        with self.control.read() as tx:
            return queries.entry_citations(tx.sql, project, reference, revision)

    def gate(self, project: str, reference: str) -> GateView:
        """The gate for ``reference`` as it reads now (LG-17): each clause as the main
        agent's decision to establish it would meet it, with why, and the decisions that
        affect it. A read: it changes nothing, and takes no write lock."""
        self._project(project)
        with self.control.read() as tx:
            return queries.gate_view(tx.sql, project, reference)

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
            return queries.category_page(tx.sql, project, after=after, limit=limit)

    def category(self, project: str, name: str, revision: int | None = None) -> Category:
        """A frontier category at ``revision``, its current one by default."""
        self._project(project)
        with self.control.read() as tx:
            return queries.category_view(tx.sql, project, name, revision)

    def frontier_history(
        self, project: str, name: str, *, before: int | None = None, limit: int = 50
    ) -> FrontierHistory:
        """A category's frontier changes, newest first (LG-24)."""
        self._project(project)
        with self.control.read() as tx:
            return queries.frontier_history(tx.sql, project, name, before=before, limit=limit)

    def frontier_past(self, project: str, name: str, at: datetime) -> FrontierPast:
        """A category's best known results as they were at ``at`` (LG-24)."""
        self._project(project)
        with self.control.read() as tx:
            return queries.frontier_past(tx.sql, project, name, at)

    def frontier(
        self, project: str, name: str, *, candidates_before: int | None = None
    ) -> FrontierView:
        """A category's frontier as its results read now (LG-20, LG-21, LG-23), its
        candidates newest first, made before result ``candidates_before``."""
        self._project(project)
        with self.control.read() as tx:
            return queries.frontier_view(tx.sql, project, name, candidates_before=candidates_before)

    # Changes.

    def define_category(self, project: str, raw: object, author: Author) -> Category:
        """Define a frontier category, or revise one at the revision read (LG-18): the
        user's or the main agent's, under an operation ID that replays its first result."""
        try:
            value = CategoryDefinition.model_validate(raw)
        except ValidationError as error:
            raise DocumentError(INVALID_REQUEST, records.validation_error(error)) from None
        if self._authority(project, author) == "delegated":
            raise records.refused("a delegated agent defines no frontier category")
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

    def _dominance_changes(self, db: sqlite3.Connection, planned: list[Planned]) -> list[str]:
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
            raise records.refused("only the user records that they checked an entry")
        self._project(project)
        with self.control.read() as tx:
            entry = queries.find(tx.sql, project, reference).entry_id
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
            raise records.refused(
                "only the user drops a dominates link; an agent revises its links"
            )
        self._project(project)
        with self.control.read() as tx:
            entry = queries.find(tx.sql, project, holder)
            named = queries.find(tx.sql, project, target).entry_id
            links = queries.links(tx.sql, entry.entry_id, min(revision, entry.head))
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
            entry = queries.find(db, project, reference)
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
            raise DocumentError(INVALID_REQUEST, records.validation_error(error)) from None
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
                    kept.append((preparation.agent_mutation(mutation), provenance))
                except DocumentError as error:
                    raise preparation.labelled(error, provenance) from None
            mutations = kept
        mutations = [
            (preparation.this_session(m, author), provenance) for m, provenance in mutations
        ]
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
                raise preparation.labelled(error, provenance) from None
        with self.control.write() as tx:
            results: list[MutationResult] = []
            for mutation, provenance in mutations:
                try:
                    results.append(
                        self._apply(tx.sql, project, mutation, author, role, pinned, provenance)
                    )
                except DocumentError as error:
                    raise preparation.labelled(error, provenance) from None
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
        digest = preparation.fingerprint(mutation)
        replay = RECEIPTS.find(db, project, _PROJECT, mutation.operation_id, digest)
        if replay is not None:
            return MutationResult.model_validate({**replay, "replayed": True})
        numbers: dict[str, int] = {}
        planned = [
            preparation.plan_entry(db, project, change, role, numbers, author.session_id)
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
                    preparation.resolve_link(db, project, p, link, keys, pinned, heads)
                    for link in p.change.links
                ]
                if not p.created and p.links != p.previous:
                    p.changed = [*p.changed, "links"]
            linker = evidence.Linker(role, author.session_id)
            preparation.resolve_evidence(db, project, p, keys, heads, pinned, linker)
            if not p.created and not p.changed:
                raise DocumentError(
                    INVALID_REQUEST, f"the change to {p.row.handle} changes nothing"
                )
        self._check_links(db, planned)
        self._check_research(db, project, planned, author)
        for p in planned:
            preparation.resolve_provenance(db, project, p, author, pinned, provenance)
        self._check_dominates(db, project, planned, role)
        self._check_quotes(db, planned)
        self._check_published(db, planned)
        gated = rules.check_decisions(db, planned, role)
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
                "reason": records.clip(mutation.reason, 500),
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
        keys: dict[str, Row],
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

    def _reopened(
        self,
        db: sqlite3.Connection,
        project: str,
        planned: list[Planned],
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
        keys: dict[str, Row],
        on: Row,
        cited: list[WorkReference],
    ) -> Row:
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
        self, db: sqlite3.Connection, project: str, reference: str, keys: dict[str, Row]
    ) -> Row:
        """The entry a work record names: one the change creates, by ``new:<key>``, or
        one of the project's."""
        if reference.startswith("new:"):
            found = keys.get(reference.removeprefix("new:"))
            if found is None:
                raise DocumentError(INVALID_REQUEST, f"no new entry is keyed {reference}")
            return found
        return queries.find(db, project, reference)

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
        with self.control.read() as tx:
            return queries.work_page(tx.sql, project, reference, before=before, limit=limit)

    def _write(
        self,
        db: sqlite3.Connection,
        project: str,
        p: Planned,
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
        self.events.append(
            db,
            "ledger.entry_created" if p.created else "ledger.entry_revised",
            project,
            author.session_id,
            row.entry_id,
            payload,
            now,
        )

    def _statement(self, db: sqlite3.Connection, row: Row, text: str | None, author: Author) -> int:
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
            raise records.refused("only the user records an assessment this way")
        self._project(project)
        with self.control.read() as tx:
            row = queries.find(tx.sql, project, reference)
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
    def _changed(p: Planned) -> flags.Changed:
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

    def _check_quotes(self, db: sqlite3.Connection, planned: list[Planned]) -> None:
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
        db: sqlite3.Connection, by_id: dict[str, Planned], p: Planned, link: EvidenceLink
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

    def _check_published(self, db: sqlite3.Connection, planned: list[Planned]) -> None:
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
        self, db: sqlite3.Connection, project: str, planned: list[Planned], role: str
    ) -> None:
        """A ``dominates`` link a change adds (LG-22): the user's or the main agent's,
        between two results of one category, both eligible as the change leaves them,
        where the rule cannot compare them."""
        by_id = {p.row.entry_id: p for p in planned}
        for p in planned:
            for link in records.links_added(p, "dominates"):
                if role == "delegated":
                    raise records.refused("a delegated agent records no dominates link")
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

    def _check_cycles(self, db: sqlite3.Connection, project: str, planned: list[Planned]) -> None:
        """Once a change is written, no ``dominates`` link it added lies on a cycle of
        results that dominate one another, by links or by the rule (the review's M2), as
        the frontier reads them: a disputed result as the revision before, and only the
        results it holds eligible."""
        added = {
            (p.row.entry_id, link.target_id): p
            for p in planned
            for link in records.links_added(p, "dominates")
        }
        if not added:
            return
        names = {p.fields.get("category") for p in added.values()} - {None}
        for name in sorted(str(n) for n in names):
            category = frontier.read(db, project, name)
            if category is None:
                continue
            for link in frontier.view(db, project, category).void_links:
                p = added.get((link.entry_id, queries.entry_id(db, project, link.target)))
                if p is not None and link.void == frontier.CYCLE:
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{link.handle} dominating {link.target} closes a cycle: "
                        f"{link.target} already dominates {link.handle}, by dominates links "
                        "or by the rule, through the results between, as the frontier "
                        "reads them",
                    )

    def _check_links(self, db: sqlite3.Connection, planned: list[Planned]) -> None:
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
            else queries.links(db, p.row.entry_id, p.row.head)
            for p in planned
        }

        def targets(entry: str, kind: str) -> list[str]:
            if entry in final:
                return [
                    link.target_id
                    for link in final[entry]
                    if link.kind == kind and link.target == "entry"
                ]
            return [row[0] for row in db.execute(queries.TARGETS, (entry, kind))]

        def holders(entry: str, kind: str) -> list[str]:
            stored = [row[0] for row in db.execute(queries.HOLDERS, (entry, kind))]
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
            for link in [
                *records.links_added(p, "supersedes"),
                *records.links_added(p, "split_into"),
            ]:
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
            for link in records.links_added(p, "supersedes"):
                if p.status == "superseded":
                    raise DocumentError(
                        INVALID_REQUEST, f"{p.row.handle} is superseded and supersedes nothing new"
                    )
                if status(link.target_id) != "superseded":
                    raise DocumentError(
                        INVALID_REQUEST,
                        f"{link.handle} becomes superseded in the change that supersedes it",
                    )
            superseding += [link.target_id for link in records.links_dropped(p, "supersedes")]
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

    def _check_research(
        self, db: sqlite3.Connection, project: str, planned: list[Planned], author: Author
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
            links = preparation.final_links(db, p)
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
                        for k in preparation.final_links(db, d)
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
                raise records.refused(
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
        p: Planned,
        links: list[EntryLink],
        by_id: dict[str, Planned],
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
        for link in records.links_added(p, "reopen_on"):
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
        self, db: sqlite3.Connection, p: Planned, author: Author, deciding: list[Planned]
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
            final = [k for k in preparation.final_links(db, p) if k.kind != "reopen_on"]
            stated = (
                not {"statement", "title", "evidence", "pins"} & set(p.changed)
                and "status" in p.changed
                and p.status in ("rejected", "reversed")
                and "reopen_condition" not in p.fields_before
                and others == p.fields_before
                and ("links" not in p.changed or final == p.previous)
            )
            if not stated:
                raise records.refused(
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
                    for k in preparation.final_links(db, d)
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

    def _check_session(self, project: str, p: Planned, author: Author) -> None:
        """A decision scoped to a session names one of the project's; an agent scopes a
        decision to its own session, or takes its own session's scope off, and no other."""
        session, before = p.fields.get("session"), p.fields_before.get("session")
        if session == before:
            return
        if author.kind != "user" and {session, before} - {None, author.session_id}:
            raise records.refused("an agent scopes a decision to its own session, and no other")
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
            raise records.refused(f"a {author.role or author.kind} does not change the ledger")
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
