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
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.projects import ProjectError, recorded, run_id_of

from . import (
    commit,
    evidence,
    flags,
    frontier,
    gate,
    preparation,
    queries,
    records,
    rules,
    standing,
)
from . import provenance as anchoring
from .models import (
    MAX_CLEARED,
    MAX_ENTRY_WORK,
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
from .records import Planned, Row

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
        if self._authority(project, author).role == "delegated":
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
                commit.baseline(db, project, {value.category}, now)
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
            commit.frontier_changes(
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
                events=self.category_events,
            )
            RECEIPTS.record(
                db, project, _PROJECT, value.operation_id, digest, category.model_dump(mode="json")
            )
            return category

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
        authority = self._authority(project, author)
        role = authority.role
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
                        self._apply(
                            tx.sql,
                            project,
                            mutation,
                            author,
                            role,
                            pinned,
                            provenance,
                            authority.sessions,
                        )
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
        sessions: frozenset[str],
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
        rules.check_links(db, planned)
        rules.check_research(db, planned, author, sessions)
        for p in planned:
            preparation.resolve_provenance(db, project, p, author, pinned, provenance)
        rules.check_dominates(db, project, planned, role)
        rules.check_quotes(db, planned)
        rules.check_published(db, planned)
        gated = rules.check_decisions(db, planned, role)
        now = self.clock()
        cleared = self._clear(db, project, mutation, author, role, now)
        reopened = self._reopen(db, project, mutation, author, role, now)
        commit.baseline(db, project, commit.affected_categories(db, planned, written=False), now)
        changed: list[ChangedEntry] = []
        for p in planned:
            commit.write_entry(
                db, project, p, mutation, author, provenance, now, events=self.events
            )
            changed.append(
                ChangedEntry(
                    entry_id=p.row.entry_id,
                    handle=p.row.handle,
                    revision=p.row.head,
                    status=p.status,
                    created=p.created,
                )
            )
        rules.check_cycles(db, project, planned)
        work, rejected = self._record_work(
            db, project, mutation, author, keys, pinned, provenance, now.isoformat()
        )
        work += self._reopened(db, project, planned, mutation, author, provenance, now)
        raised = flags.raise_flags(
            db, project, [commit.flag_change(p) for p in planned], now.isoformat()
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
        commit.frontier_changes(
            db,
            project,
            commit.affected_categories(db, planned),
            {
                "operation_id": mutation.operation_id,
                "author": author.actor,
                "reason": records.clip(mutation.reason, 500),
                "entries": [p.row.handle for p in planned if p.row.type == "result"],
                **(
                    {"dominates": linked}
                    if (linked := commit.dominance_changes(db, planned))
                    else {}
                ),
            },
            author,
            now,
            events=self.category_events,
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

    def _authority(self, project: str, author: Author) -> records.Authority:
        """The role LG-13 gives ``author``: ``user``, ``main`` or ``delegated``. Code in
        the sandbox, imports, the platform and other agent roles change nothing."""
        self._project(project)
        try:
            sessions = recorded(self.documents.path.parent, project).sessions
        except ProjectError:
            raise DocumentError(NOT_FOUND, "no such ledger project") from None
        if author.kind == "user":
            return records.Authority("user", frozenset(sessions))
        if author.kind != "agent" or author.role not in ("main", "delegated"):
            raise records.refused(f"a {author.role or author.kind} does not change the ledger")
        if author.session_id not in sessions:
            raise DocumentError(NOT_FOUND, "the agent's session is not in this project")
        return records.Authority(cast(str, author.role), frozenset(sessions))

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
