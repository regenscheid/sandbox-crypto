# SPDX-License-Identifier: MIT
"""The project memory (docs/specs/project-memory.md): short notes on how to work in a
project, which the project's agents receive when they start.

Agents only propose (``propose``): adding an entry, changing one or removing one, with a
reason; a change or a removal names the revision it read. The user decides (``change``):
accepts or declines each proposal, and adds, edits, removes and moves entries directly,
naming the revision seen, so that concurrent changes conflict instead of overwriting.
Every change of an entry appends a version, written once, which says who proposed it and
who accepted it, or that the user made it, and the entry's place. The memory's
``version`` counts the changes of its accepted entries. Each proposal, decision and edit
is one change of the control store, with its operation receipt and its events
(``memory.proposed``, ``memory.decided``, ``memory.changed``; PM-11).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from pydantic import ConfigDict, Field, ValidationError, model_validator

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.control import EventLog, Receipts

from .documents.agent_text import agent_text
from .documents.errors import CONFLICT, INVALID_REQUEST, NOT_FOUND, POLICY_DENIED, DocumentError
from .documents.models import Author

if TYPE_CHECKING:
    from .documents.store import DocumentStore

Json = dict[str, Any]

MAX_TEXT: Final = 1000
"""An entry's text, in characters (PM-1)."""
MAX_REASON: Final = 500
"""A proposal's reason, in characters (PM-3)."""
MAX_WAITING: Final = 50
"""Proposals that may wait in a project (PM-3)."""
MAX_PER_RUN: Final = 10
"""Proposals one run may make (PM-3)."""
MAX_ENTRIES: Final = 100
"""Accepted entries a project's memory holds."""
MAX_CHANGES: Final = 50_000
MAX_PROPOSALS: Final = 10_000
"""The changes and the proposals a project's memory keeps, its history included."""
GIVEN_CHARS: Final = 16_000
"""The characters of the memory section an agent receives, as sent (PM-7)."""
SECTION_RESERVE: Final = 1_000
"""The characters of the section kept for its framing, its opening and closing lines and
the line on entries left out; the entries' blocks take the rest."""
MARKER: Final = 16
"""The length of the code that marks where each entry begins and ends in a section."""

EntryId = Annotated[str, Field(pattern=r"^mem-[1-9][0-9]{0,8}$")]
ProposalId = Annotated[str, Field(pattern=r"^prop-[1-9][0-9]{0,8}$")]
_Number = Annotated[int, Field(strict=True, ge=1, le=999999999)]
_Operation = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Action = Literal["add", "change", "remove"]
State = Literal["waiting", "accepted", "declined"]
Change = Literal["added", "edited", "removed", "moved"]

RECEIPTS: Final = Receipts("memory", "operation ID {operation!r} names another memory change")
_USER: Final = "user"
"""The actor of the user's receipts; an agent's is its run."""
_PROJECT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENTRY: Final = re.compile(r"mem-[1-9][0-9]{0,8}")


class MemoryEntry(Contract):
    """An accepted entry at its current revision, or at a version of the memory."""

    entry_id: str
    revision: int
    text: str
    changed_at: datetime


class Memory(Contract):
    """A project's memory as the Memory page shows it: the accepted entries in order."""

    project: str
    version: int
    entries: tuple[MemoryEntry, ...]
    characters: int
    """The entries' text, in characters."""
    given: int
    """How many of the entries, from the first, an agent receives (PM-7)."""
    waiting: int
    """How many proposals wait for the user."""


class MemoryProposal(Contract):
    """An agent's proposal, with the entry it would change as that entry is now."""

    proposal_id: str
    action: Action
    entry_id: str | None
    base: int | None
    """The revision of the entry the agent read."""
    text: str | None
    reason: str
    proposer: Author
    proposed_at: datetime
    state: State
    decided_at: datetime | None
    current: MemoryEntry | None
    """For a change or a removal, the entry now; ``None`` once it is removed."""


class MemoryVersion(Contract):
    """One version of an entry: what changed, who applied it, and whose proposal."""

    revision: int
    """The entry's revision after it, which counts the changes of its text; a move keeps
    it."""
    version: int
    """The memory's version this change made."""
    change: Change
    text: str | None
    """``None`` for a removal."""
    author: Author
    at: datetime
    proposal: str | None
    proposer: Author | None
    proposed_at: datetime | None
    reason: str | None


class MemoryEntryHistory(Contract):
    entry_id: str
    removed: bool
    versions: tuple[MemoryVersion, ...]
    """Newest first."""
    next_before: int | None
    """The memory's version to read on from, for older versions."""


class MemoryChange(Contract):
    """What a change did: the memory's version after it, and what it named."""

    version: int
    entry_id: str | None = None
    revision: int | None = None
    proposal_id: str | None = None
    replayed: bool = False


class _Request(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProposalRequest(_Request):
    """An agent's proposal: ``add`` with text, ``change`` with an entry, the revision
    read and the new text, or ``remove`` with an entry and the revision read (PM-3)."""

    action: Action
    entry: EntryId | None = None
    revision: _Number | None = None
    text: str | None = None
    reason: str

    @model_validator(mode="after")
    def _complete(self) -> Self:
        named = self.entry is not None and self.revision is not None
        if self.action == "add" and (self.entry is not None or self.revision is not None):
            raise ValueError("add names no entry: give text")
        if self.action != "add" and not named:
            raise ValueError(f"{self.action} names an entry and the revision you read")
        if (self.text is None) != (self.action == "remove"):
            raise ValueError("add and change give text; remove gives none")
        return self


class UserChange(_Request):
    """The user's change: ``add`` text; ``edit`` an entry to new text, ``remove`` it or
    ``move`` it up or down, naming the revision seen; ``accept`` or ``decline`` a
    proposal, an accepted change or removal naming the entry's revision seen."""

    action: Literal["add", "edit", "remove", "move", "accept", "decline"]
    entry: EntryId | None = None
    expected: _Number | None = None
    text: str | None = None
    direction: Literal["up", "down"] | None = None
    proposal: ProposalId | None = None
    operation_id: _Operation

    @model_validator(mode="after")
    def _complete(self) -> Self:
        needs = {
            "add": {"text"},
            "edit": {"entry", "expected", "text"},
            "remove": {"entry", "expected"},
            "move": {"entry", "expected", "direction"},
            "accept": {"proposal"},
            "decline": {"proposal"},
        }[self.action]
        allowed = needs | ({"expected"} if self.action == "accept" else set[str]())
        named = {
            name
            for name in ("entry", "expected", "text", "direction", "proposal")
            if getattr(self, name) is not None
        }
        if not needs <= named <= allowed:
            raise ValueError(f"{self.action} takes {', '.join(sorted(allowed))}")
        return self


def entry_block(entry: MemoryEntry, marker: str) -> str:
    """An entry as a system message gives it, between markers that carry ``marker``, a
    code no entry contains (PM-6)."""
    return (
        f'<entry-{marker} id="{entry.entry_id}" revision="{entry.revision}">\n'
        f"{entry.text}\n</entry-{marker}>"
    )


def given(entries: Sequence[MemoryEntry]) -> int:
    """How many of ``entries``, from the first, an agent receives: whole entries while
    their blocks, with the section's framing and its other lines, stay within
    ``GIVEN_CHARS`` (PM-7)."""
    total, room = 0, GIVEN_CHARS - SECTION_RESERVE
    for count, entry in enumerate(entries):
        total += len(entry_block(entry, "0" * MARKER)) + 1
        if total > room:
            return count
    return len(entries)


def _now() -> datetime:
    return datetime.now(UTC)


def _scope(project: object) -> str:
    if not isinstance(project, str) or _PROJECT.fullmatch(project) is None:
        raise DocumentError(INVALID_REQUEST, "invalid project")
    return project


def _clean(value: str, what: str, limit: int) -> str:
    """Text as kept: without surrounding white space, 1 to ``limit`` characters, and no
    character the user may not see, as it is or as a character reference (threat model,
    A3; ``check_agent_text``): every agent role receives an accepted entry, so a hidden
    instruction in one would reach them all."""
    text = agent_text(value, what).strip()  # refused as sent, never stripped first
    if not 1 <= len(text) <= limit:
        raise DocumentError(INVALID_REQUEST, f"{what} is 1 to {limit} characters")
    return text


def _parse[T: _Request](model: type[T], value: object) -> T:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        message = f"{where}: {first['msg']}" if where else first["msg"]
        raise DocumentError(INVALID_REQUEST, message[:300]) from None


def _fingerprint(value: Contract) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json", exclude_none=True), sort_keys=True).encode()
    ).hexdigest()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


_LIVE: Final = (
    "SELECT e.entry_id,e.head,v.text,v.created,e.position FROM memory_entries e "
    "JOIN memory_versions v ON v.project=e.project AND v.entry_id=e.entry_id "
    "AND v.version=(SELECT max(w.version) FROM memory_versions w "
    "WHERE w.project=e.project AND w.entry_id=e.entry_id) "
    "WHERE e.project=? AND e.removed=0"
)
"""The accepted entries, each at its latest version."""
_PROPOSAL: Final = (
    "SELECT proposal_id,action,entry_id,base,text,reason,proposer,created,state,decided "
    "FROM memory_proposals WHERE project=?"
)


class ProjectMemory:
    """The projects' memories, on the documents' control store."""

    def __init__(self, documents: DocumentStore, *, clock: Callable[[], datetime] = _now) -> None:
        self.control, self.clock = documents.control, clock
        self.events = EventLog(documents.control, "memory", documents.producer)

    # Reads.

    def memory(self, project: str) -> Memory:
        """The accepted entries in the Memory page's order, and how many proposals wait."""
        _scope(project)
        if self.control.missing:
            return Memory(project=project, version=0, entries=(), characters=0, given=0, waiting=0)
        with self.control.read() as tx:
            db = tx.sql
            entries = _live(db, project)
            waiting = db.execute(
                "SELECT count(*) FROM memory_proposals WHERE project=? AND state='waiting'",
                (project,),
            ).fetchone()[0]
            return Memory(
                project=project,
                version=_version(db, project),
                entries=tuple(entries),
                characters=sum(len(entry.text) for entry in entries),
                given=given(entries),
                waiting=waiting,
            )

    def snapshot(
        self, project: str, version: int | None = None
    ) -> tuple[int, tuple[MemoryEntry, ...]]:
        """The accepted entries at ``version`` of the memory (its current one by default),
        in the order the Memory page showed them then (PM-8), and that version."""
        _scope(project)
        if version is not None and (type(version) is not int or version < 0):
            raise DocumentError(INVALID_REQUEST, "a memory version is a count of changes")
        if self.control.missing:
            if version:
                raise DocumentError(NOT_FOUND, f"the memory has no version {version}")
            return 0, ()
        with self.control.read() as tx:
            db = tx.sql
            current = _version(db, project)
            if version is None or version == current:
                return current, tuple(_live(db, project))
            if version > current:
                raise DocumentError(NOT_FOUND, f"the memory has no version {version}")
            # Each entry at its last version up to then; a removal leaves it out.
            rows = db.execute(
                "SELECT v.entry_id,v.revision,v.text,v.created FROM memory_versions v "
                "JOIN (SELECT entry_id,max(version) AS version FROM memory_versions "
                "WHERE project=? AND version<=? GROUP BY entry_id) m "
                "ON m.entry_id=v.entry_id AND m.version=v.version "
                "WHERE v.project=? AND v.text IS NOT NULL ORDER BY v.position,v.entry_id",
                (project, version, project),
            ).fetchall()
            return version, tuple(_entry(row) for row in rows)

    def proposals(self, project: str) -> tuple[MemoryProposal, ...]:
        """The proposals that wait for the user, oldest first."""
        _scope(project)
        if self.control.missing:
            return ()
        with self.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                _PROPOSAL + " AND state='waiting' ORDER BY length(proposal_id),proposal_id",
                (project,),
            ).fetchall()
            return tuple(self._proposal(db, project, row) for row in rows)

    def history(self, project: str, entry: str, *, before: int | None = None) -> MemoryEntryHistory:
        """An entry's versions, newest first, 50 at a time, before the memory's version
        ``before``."""
        _scope(project)
        if _ENTRY.fullmatch(entry) is None:
            raise DocumentError(INVALID_REQUEST, "a memory entry is named mem-N")
        if before is not None and (type(before) is not int or before < 1):
            raise DocumentError(INVALID_REQUEST, "before is a version of the memory")
        if self.control.missing:
            raise DocumentError(NOT_FOUND, f"there is no memory entry {entry}")
        with self.control.read() as tx:
            db = tx.sql
            _, removed = _row(db, project, entry)
            rows = db.execute(
                "SELECT v.revision,v.version,v.change,v.text,v.author,v.created,v.proposal,"
                "p.proposer,p.created,p.reason FROM memory_versions v LEFT JOIN "
                "memory_proposals p ON p.project=v.project AND p.proposal_id=v.proposal "
                "WHERE v.project=? AND v.entry_id=? AND v.version<? "
                "ORDER BY v.version DESC LIMIT 51",
                (project, entry, before or 1_000_000_000),
            ).fetchall()
        versions = tuple(
            MemoryVersion(
                revision=row[0],
                version=row[1],
                change=row[2],
                text=row[3],
                author=Author.model_validate_json(row[4]),
                at=_time(row[5]),
                proposal=row[6],
                proposer=Author.model_validate_json(row[7]) if row[7] is not None else None,
                proposed_at=_time(row[8]) if row[8] is not None else None,
                reason=row[9],
            )
            for row in rows[:50]
        )
        return MemoryEntryHistory(
            entry_id=entry,
            removed=removed,
            versions=versions,
            next_before=versions[-1].version if len(rows) > 50 else None,
        )

    # Changes.

    def propose(
        self, project: str, value: object, author: Author, operation: str
    ) -> tuple[MemoryProposal, bool]:
        """An agent's proposal, which waits for the user; a retried operation returns the
        first proposal and ``True`` (PM-2, PM-3)."""
        _scope(project)
        if author.kind != "agent" or author.run_id is None or author.session_id is None:
            raise DocumentError(POLICY_DENIED, "only an agent run proposes; the user changes")
        request = _parse(ProposalRequest, value)
        text = (
            _clean(request.text, "an entry's text", MAX_TEXT) if request.text is not None else None
        )
        reason = _clean(request.reason, "the reason", MAX_REASON)
        fingerprint = _fingerprint(request)
        with self.control.write() as tx:
            db = tx.sql
            replay = RECEIPTS.find(db, project, author.run_id, operation, fingerprint)
            if replay is not None:
                row = db.execute(_PROPOSAL + " AND proposal_id=?", (project, replay["proposal"]))
                return self._proposal(db, project, row.fetchone()), True
            counters = _head(db, project)
            # Two counts the (project,state) and (project,run_id) indexes answer.
            (waiting,) = db.execute(
                "SELECT count(*) FROM memory_proposals WHERE project=? AND state='waiting'",
                (project,),
            ).fetchone()
            (made,) = db.execute(
                "SELECT count(*) FROM memory_proposals WHERE project=? AND run_id=?",
                (project, author.run_id),
            ).fetchone()
            if waiting >= MAX_WAITING:
                raise DocumentError(
                    CONFLICT,
                    f"{MAX_WAITING} proposals already wait: the user has not caught up; "
                    "do not propose more now",
                )
            if made >= MAX_PER_RUN:
                raise DocumentError(
                    CONFLICT,
                    f"this run has made {MAX_PER_RUN} proposals, as many as a run makes: the "
                    "user has not caught up",
                )
            if counters[2] >= MAX_PROPOSALS:
                raise DocumentError(CONFLICT, "the project's memory keeps no more proposals")
            if request.entry is not None and request.revision is not None:
                _current(db, project, request.entry, request.revision)
            number = counters[2] + 1
            proposal = f"prop-{number}"
            at = self.clock()
            db.execute("UPDATE memory_heads SET proposals=? WHERE project=?", (number, project))
            db.execute(
                "INSERT INTO memory_proposals(project,proposal_id,action,entry_id,base,text,"
                "reason,proposer,run_id,session,created,state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'waiting')",
                (
                    project,
                    proposal,
                    request.action,
                    request.entry,
                    request.revision,
                    text,
                    reason,
                    author.model_dump_json(exclude_none=True),
                    author.run_id,
                    author.session_id,
                    at.astimezone(UTC).isoformat(),
                ),
            )
            self.events.append(
                db,
                "memory.proposed",
                project,
                author.session_id,
                proposal,
                {"proposal": proposal, "action": request.action, "entry": request.entry},
                at,
            )
            RECEIPTS.record(
                db, project, author.run_id, operation, fingerprint, {"proposal": proposal}
            )
            row = db.execute(_PROPOSAL + " AND proposal_id=?", (project, proposal)).fetchone()
            return self._proposal(db, project, row), False

    def change(self, project: str, value: object, author: Author) -> MemoryChange:
        """The user's change; a retried operation returns the first result, ``replayed``
        (PM-2, PM-4)."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user changes the memory; agents propose")
        request = _parse(UserChange, value)
        text = (
            _clean(request.text, "an entry's text", MAX_TEXT) if request.text is not None else None
        )
        fingerprint = _fingerprint(request)
        with self.control.write() as tx:
            db = tx.sql
            replay = RECEIPTS.find(db, project, _USER, request.operation_id, fingerprint)
            if replay is not None:
                return MemoryChange.model_validate({**replay, "replayed": True})
            counters = _head(db, project)
            if counters[0] >= MAX_CHANGES and request.action != "decline":
                raise DocumentError(CONFLICT, "the project's memory keeps no more changes")
            change = _Change(self, db, project, author)
            if request.action == "accept" or request.action == "decline":
                assert request.proposal is not None  # noqa: S101 - checked by UserChange
                result = change.decide(request.proposal, request.action, request.expected)
            elif request.action == "add":
                assert text is not None  # noqa: S101 - checked by UserChange
                result = change.add(text, None)
            else:
                assert request.entry is not None  # noqa: S101 - checked by UserChange
                assert request.expected is not None  # noqa: S101 - checked by UserChange
                head = _current(db, project, request.entry, request.expected)
                if request.action == "edit":
                    if text == head.text:
                        raise DocumentError(INVALID_REQUEST, "the text is unchanged")
                    assert text is not None  # noqa: S101 - checked by UserChange
                    result = change.edit(head, text, None)
                elif request.action == "remove":
                    result = change.remove(head, None)
                else:
                    assert request.direction is not None  # noqa: S101 - checked by UserChange
                    result = change.move(head, request.direction)
            RECEIPTS.record(
                db,
                project,
                _USER,
                request.operation_id,
                fingerprint,
                result.model_dump(exclude={"replayed"}),
            )
            return result

    def _proposal(
        self, db: sqlite3.Connection, project: str, row: tuple[Any, ...]
    ) -> MemoryProposal:
        entry: MemoryEntry | None = None
        if row[2] is not None:
            found = db.execute(_LIVE + " AND e.entry_id=?", (project, row[2])).fetchone()
            entry = _entry(found) if found is not None else None
        return MemoryProposal(
            proposal_id=row[0],
            action=row[1],
            entry_id=row[2],
            base=row[3],
            text=row[4],
            reason=row[5],
            proposer=Author.model_validate_json(row[6]),
            proposed_at=_time(row[7]),
            state=row[8],
            decided_at=_time(row[9]) if row[9] is not None else None,
            current=entry,
        )


class _Change:
    """One change of a project's memory, in its write transaction: each step bumps the
    memory's version and appends the versions and events it makes."""

    def __init__(
        self, memory: ProjectMemory, db: sqlite3.Connection, project: str, author: Author
    ) -> None:
        self.memory, self.db, self.project, self.author = memory, db, project, author
        self.at = memory.clock()
        self.version = 0

    def bump(self) -> int:
        self.db.execute(
            "UPDATE memory_heads SET version=version+1 WHERE project=?", (self.project,)
        )
        self.version = _version(self.db, self.project)
        return self.version

    def append(
        self,
        entry: str,
        revision: int,
        change: Change,
        text: str | None,
        position: int,
        proposal: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO memory_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.project,
                entry,
                revision,
                self.version,
                change,
                text,
                position,
                self.author.model_dump_json(exclude_none=True),
                proposal,
                self.at.astimezone(UTC).isoformat(),
            ),
        )
        payload: Json = {"entry": entry, "revision": revision, "version": self.version}
        self.memory.events.append(
            self.db,
            "memory.changed",
            self.project,
            None,
            entry,
            {**payload, "change": change, "proposal": proposal},
            self.at,
        )

    def add(self, text: str, proposal: str | None) -> MemoryChange:
        live = self.db.execute(
            "SELECT count(*) FROM memory_entries WHERE project=? AND removed=0", (self.project,)
        ).fetchone()[0]
        if live >= MAX_ENTRIES:
            raise DocumentError(
                CONFLICT, f"the memory holds {MAX_ENTRIES} entries; remove or merge some first"
            )
        number = _head(self.db, self.project)[1] + 1
        entry = f"mem-{number}"
        position = self.bump()
        self.db.execute("UPDATE memory_heads SET entries=? WHERE project=?", (number, self.project))
        self.db.execute(
            "INSERT INTO memory_entries VALUES(?,?,1,?,0)", (self.project, entry, position)
        )
        self.append(entry, 1, "added", text, position, proposal)
        return MemoryChange(version=self.version, entry_id=entry, revision=1)

    def edit(self, head: _Head, text: str, proposal: str | None) -> MemoryChange:
        self.bump()
        self._next(head)
        self.append(head.entry, head.revision + 1, "edited", text, head.position, proposal)
        return MemoryChange(version=self.version, entry_id=head.entry, revision=head.revision + 1)

    def remove(self, head: _Head, proposal: str | None) -> MemoryChange:
        self.bump()
        self._next(head, removed=True)
        self.append(head.entry, head.revision + 1, "removed", None, head.position, proposal)
        return MemoryChange(version=self.version, entry_id=head.entry, revision=head.revision + 1)

    def move(self, head: _Head, direction: Literal["up", "down"]) -> MemoryChange:
        """Swap the entry with the one before it (``up``) or after it. A move keeps both
        entries' revisions, which count the changes of their text, so a proposal or an
        edit that names one is not stale for it; each gets a version that says so."""
        up = direction == "up"
        found = self.db.execute(
            _LIVE + f" AND e.position{'<' if up else '>'}? "
            f"ORDER BY e.position {'DESC' if up else 'ASC'} LIMIT 1",
            (self.project, head.position),
        ).fetchone()
        if found is None:
            raise DocumentError(
                INVALID_REQUEST, f"{head.entry} is {'first' if up else 'last'} already"
            )
        other = _Head(found[0], found[1], found[2], found[4])
        self.bump()
        for moved, position in ((head, other.position), (other, head.position)):
            self.db.execute(
                "UPDATE memory_entries SET position=? WHERE project=? AND entry_id=?",
                (position, self.project, moved.entry),
            )
            self.append(moved.entry, moved.revision, "moved", moved.text, position, None)
        return MemoryChange(version=self.version, entry_id=head.entry, revision=head.revision)

    def decide(
        self, proposal: str, decision: Literal["accept", "decline"], expected: int | None
    ) -> MemoryChange:
        row = self.db.execute(
            "SELECT action,entry_id,text,state,session FROM memory_proposals "
            "WHERE project=? AND proposal_id=?",
            (self.project, proposal),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, f"there is no proposal {proposal}")
        action, entry, text, state, session = row
        if state != "waiting":
            raise DocumentError(CONFLICT, f"{proposal} was {state} already")
        result = MemoryChange(version=_version(self.db, self.project), proposal_id=proposal)
        if decision == "accept":
            if action == "add":
                if expected is not None:
                    raise DocumentError(INVALID_REQUEST, "accepting an addition names no revision")
                result = self.add(text, proposal)
            else:
                if expected is None:
                    raise DocumentError(
                        INVALID_REQUEST, f"name the revision of {entry} you saw, to accept"
                    )
                head = _current(self.db, self.project, entry, expected)
                result = (
                    self.edit(head, text, proposal)
                    if action == "change"
                    else self.remove(head, proposal)
                )
            result = result.model_copy(update={"proposal_id": proposal})
        self.db.execute(
            "UPDATE memory_proposals SET state=?,decided=?,decider=? "
            "WHERE project=? AND proposal_id=?",
            (
                "accepted" if decision == "accept" else "declined",
                self.at.astimezone(UTC).isoformat(),
                self.author.model_dump_json(exclude_none=True),
                self.project,
                proposal,
            ),
        )
        self.memory.events.append(
            self.db,
            "memory.decided",
            self.project,
            session,
            proposal,
            {"proposal": proposal, "decision": decision, "version": result.version},
            self.at,
        )
        return result

    def _next(self, head: _Head, *, removed: bool = False) -> None:
        """The entry's next revision: its text changed, or it was removed."""
        self.db.execute(
            "UPDATE memory_entries SET head=?,removed=? WHERE project=? AND entry_id=?",
            (head.revision + 1, int(removed), self.project, head.entry),
        )


@dataclass(frozen=True)
class _Head:
    """An accepted entry at its current revision, as a change reads it."""

    entry: str
    revision: int
    text: str
    position: int


def _entry(row: tuple[Any, ...]) -> MemoryEntry:
    return MemoryEntry(entry_id=row[0], revision=row[1], text=row[2], changed_at=_time(row[3]))


def _live(db: sqlite3.Connection, project: str) -> list[MemoryEntry]:
    rows = db.execute(_LIVE + " ORDER BY e.position", (project,)).fetchall()
    return [_entry(row) for row in rows]


def _version(db: sqlite3.Connection, project: str) -> int:
    row = db.execute("SELECT version FROM memory_heads WHERE project=?", (project,)).fetchone()
    return 0 if row is None else row[0]


def _head(db: sqlite3.Connection, project: str) -> tuple[int, int, int]:
    """The memory's version and its entry and proposal counters, made when first needed."""
    db.execute("INSERT OR IGNORE INTO memory_heads VALUES(?,0,0,0)", (project,))
    return db.execute(
        "SELECT version,entries,proposals FROM memory_heads WHERE project=?", (project,)
    ).fetchone()


def _row(db: sqlite3.Connection, project: str, entry: str) -> tuple[int, bool]:
    found = db.execute(
        "SELECT head,removed FROM memory_entries WHERE project=? AND entry_id=?", (project, entry)
    ).fetchone()
    if found is None:
        raise DocumentError(NOT_FOUND, f"there is no memory entry {entry}")
    return found[0], bool(found[1])


def _current(db: sqlite3.Connection, project: str, entry: str, revision: int) -> _Head:
    """The accepted entry, which must be at ``revision``."""
    head, removed = _row(db, project, entry)
    if removed:
        raise DocumentError(CONFLICT, f"{entry} was removed")
    if head != revision:
        raise DocumentError(
            CONFLICT,
            f"{entry} is at revision {head}, not {revision}: read it again",
            {"revision": head},
        )
    found = db.execute(_LIVE + " AND e.entry_id=?", (project, entry)).fetchone()
    return _Head(found[0], found[1], found[2], found[4])
