# SPDX-License-Identifier: MIT
"""The research-state updates folds propose (docs/specs/context-management.md, CM-15).

A fold's enrichment call proposes typed operations for its span: a new issue (a
question, a hypothesis or a contradiction), a decision nobody recorded (an approach
abandoned, ``rejected`` with its reason and reopen condition, or a method or scope
chosen, ``in_effect``), a work record on an issue, claim or result that ended ``limit``,
a caveat on a claim or result, and an assumption with the entries that rely on it. Each
names its basis and its anchors, the span's messages and tool results.

``FoldProposals.propose`` checks each: its form; that the handles it names exist, of the
types the operation takes; that its anchors lie in the span and pass CM-9's checks
(``provenance.resolve`` and ``provenance.check``); that its numbers, archive IDs and
other identifiers occur in the span and its handles exist (CM-20); and that its text
holds no hidden character. It keeps the valid ones, at most 20 a fold, apart from the
ledger; a session waits on 50 at most, and a fold that would pass that keeps none and
says how many it dropped. Nothing is written to the ledger.

The main agent of the proposal's session, or the user, accepts one (``accept``) with
their own authority: the ledger applies it as a mutation of theirs, with its basis and
anchors checked again and the fold and proposal as its source, so a status that needs a
decision still needs one and the gate still applies. The mutation is kept before it is
applied, so that accepting again, with any operation ID, returns the first result and
creates nothing (LG-31). Either may dismiss one with a reason. A waiting proposal
changes no entry and settles nothing (the owner, 2026-10-15).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

from sandbox_science.contracts import Contract, new_id
from sandbox_science.orchestrator.documents.agent_text import agent_values
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author

from . import provenance
from .models import (
    MAX_ANCHORS,
    NO_CONDITION,
    AnchorInput,
    EntryChange,
    EntryLink,
    LinkInput,
    LinkKind,
    Mutation,
    Note,
    Reference,
    Statement,
    Text,
    WorkInput,
    WorkLimit,
)
from .store import Ledger

Json = dict[str, Any]

PER_FOLD: Final = 20
WAITING: Final = 50
"""The proposals a fold keeps, and a session's waiting ones, at most (CM-15, as PM-3)."""
_HANDLES: Final = 8
_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "concerns": ("issue", "claim", "result", "assumption", "decision", "task", "reference"),
    "reopen_on": ("issue", "assumption", "claim", "result"),
    "entry": ("issue", "claim", "result"),
    "relied_on_by": ("claim", "result", "decision"),
}
_SHAPES: Final[dict[str, tuple[set[str], set[str]]]] = {
    # op: (what it may give, what it must)
    "issue": ({"title", "statement", "kind", "concerns"}, {"title", "statement", "kind"}),
    "decision": (
        {"title", "statement", "status", "reason", "reopen_condition", "reopen_on"},
        {"title", "statement", "status", "reason"},
    ),
    "work": ({"entry", "tried", "limit"}, {"entry", "tried", "limit"}),
    "caveat": ({"entry", "caveat"}, {"entry", "caveat"}),
    "assumption": ({"title", "statement", "relied_on_by"}, {"title", "statement"}),
}
_IDS: Final = re.compile(
    r"msg-[0-9a-f]{16}|\b10\.\d{4,9}/[^\s\"'<>\\]*[^\s\"'<>\\.,;:)\]]"
    r"|(?<![\w/])(?:19|20)\d\d/\d{3,5}(?![\w/])"
    r"|(?<![\w-])(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8,}(?![\w-])"
)
_HANDLE: Final = re.compile(r"(?<![\w-])(?:REF|[ICRADT])-\d+(?![\w-])")
_Handles = Annotated[list[Reference], Field(max_length=_HANDLES)]


class ProposalInput(Contract):
    """One operation a fold proposes, as its enrichment call gives it (CM-15)."""

    op: Literal["issue", "decision", "work", "caveat", "assumption"]
    title: Text | None = None
    statement: Statement | None = None
    kind: Literal["question", "hypothesis", "contradiction"] | None = None
    """An issue's kind."""
    concerns: _Handles = Field(default_factory=list[Reference])
    status: Literal["rejected", "in_effect"] | None = None
    """A decision's: an approach abandoned, or a method or scope chosen."""
    reason: Note | None = None
    reopen_condition: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    reopen_on: _Handles = Field(default_factory=list[Reference])
    entry: Reference | None = None
    """The issue, claim or result a work record or a caveat is on."""
    tried: Statement | None = None
    limit: WorkLimit | None = None
    """What ran out: a proposed work record ends ``limit``."""
    caveat: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    relied_on_by: _Handles = Field(default_factory=list[Reference])
    """The claims, results and decisions that rely on a proposed assumption."""
    basis: Literal["user_quoted", "observed", "interpreted"] = "interpreted"
    anchors: list[AnchorInput] = Field(min_length=1, max_length=MAX_ANCHORS)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        given = {k for k, v in self if v not in (None, [])} - {"op", "basis", "anchors"}
        allowed, needed = _SHAPES[self.op]
        if needed - given or given - allowed:
            raise ValueError(f"a {self.op} proposal gives {', '.join(sorted(needed))}, and only "
                             f"{', '.join(sorted(allowed))}")  # fmt: skip
        if any(a.message is None and a.call is None for a in self.anchors):
            raise ValueError("a proposal anchors on the span's messages and tool results only")
        if self.kind == "contradiction" and len(self.concerns) < 2:
            raise ValueError("a contradiction concerns the two sides, at least, that disagree")
        none = (self.reopen_condition or "").strip().casefold() == NO_CONDITION
        if self.status == "rejected" and self.reopen_condition is None:
            raise ValueError(f"a rejected decision states its reopen condition, or {NO_CONDITION}")
        if self.reopen_condition is not None and none == bool(self.reopen_on):
            raise ValueError("a reopen condition names what it watches in reopen_on, unless none")
        return self

    def summary(self) -> str:
        """One line for the digest."""
        what = {
            "issue": f"new {self.kind} issue",
            "decision": f"decision ({self.status})",
            "work": f"work on {self.entry}, ended limit ({self.limit})",
            "caveat": f"caveat on {self.entry}",
            "assumption": "assumption",
        }[self.op]
        text = self.title or self.caveat or self.tried or ""
        return f"{what}: {' '.join(text.split())[:200]}"


class ProposalView(Contract):
    """A proposal as kept: what it proposes, the fold that proposed it, and its state."""

    proposal_id: str
    session: str
    fold: str
    run: str
    created: str
    proposal: ProposalInput
    anchors: list[Json]
    state: Literal["waiting", "accepted", "dismissed"]
    result: Json | None = None
    reason: str | None = None
    decided_by: Json | None = None
    decided_at: str | None = None


_COLUMNS: Final = (
    "proposal_id,session,fold,run,created,body,anchors,state,result,reason,decided_by,decided_at"
)


def _view(row: Sequence[Any]) -> ProposalView:
    loaded = [None if v is None else json.loads(v) for v in (row[8], row[10])]
    return ProposalView(
        proposal_id=row[0], session=row[1], fold=row[2], run=row[3], created=row[4],
        proposal=ProposalInput.model_validate_json(row[5]), anchors=json.loads(row[6]),
        state=row[7], result=loaded[0], reason=row[9], decided_by=loaded[1], decided_at=row[11],
    )  # fmt: skip


class FoldProposals:
    """The folds' proposals of one control store's projects, beside its ledger."""

    def __init__(self, ledger: Ledger, *, now: Callable[[], datetime] | None = None) -> None:
        self.ledger, self.control = ledger, ledger.control
        self.now = now or (lambda: datetime.now(UTC))

    def propose(
        self,
        project: str,
        session: str,
        run: str,
        fold: str,
        span: Sequence[Any],
        proposed: Sequence[object],
    ) -> Json:
        """Keep the valid ones of the ``proposed`` operations of the fold archived as
        ``fold`` over the archived messages ``span``; what was kept, dropped and refused."""
        ids = {row.message_id for row in span}
        text = "\n".join(f"{row.message_id}\n{row.entry.text}" for row in span)
        kept: list[tuple[ProposalInput, list[Json]]] = []
        refused: list[str] = []
        for n, raw in enumerate(proposed[:64], 1):
            try:
                found = ProposalInput.model_validate(agent_values(raw, f"proposal {n}"))
                kept.append((found, self._checked(project, session, found, ids, text, n)))
            except ValidationError as error:
                first = error.errors()[0]
                refused.append(f"proposal {n}: {first['msg']}")
            except DocumentError as error:
                refused.append(error.message.removeprefix(provenance.PREFIX))
        extra = max(0, len(kept) - PER_FOLD)
        kept = kept[:PER_FOLD]
        created = self.now().isoformat()
        with self.control.write() as tx:
            (waiting,) = tx.sql.execute(
                "SELECT count(*) FROM fold_proposals WHERE project=? AND session=? "
                "AND state='waiting'",
                (project, session),
            ).fetchone()
            if waiting + len(kept) > WAITING:
                return {"kept": 0, "dropped": len(kept) + extra, "refused": refused[:10]}
            tx.sql.executemany(
                "INSERT INTO fold_proposals (proposal_id,project,session,fold,run,created,body,"
                "anchors,state) VALUES (?,?,?,?,?,?,?,?,'waiting')",
                [
                    (
                        new_id("prop"),
                        project,
                        session,
                        fold,
                        run,
                        created,
                        found.model_dump_json(exclude_defaults=True),
                        json.dumps(anchors),
                    )
                    for found, anchors in kept
                ],
            )
        return {"kept": len(kept), "dropped": extra, "refused": refused[:10]}

    def _checked(
        self,
        project: str,
        session: str,
        found: ProposalInput,
        span: set[str],
        text: str,
        n: int,
    ) -> list[Json]:
        """``found``'s anchors resolved, once it is checked against the ledger and the
        span ``text``, whose archive messages are ``span``."""
        where = f"proposal {n}"
        caveat = ("claim", "result") if found.op == "caveat" else _TYPES["entry"]
        for field, types in {**_TYPES, "entry": caveat}.items():
            value: str | list[str] | None = getattr(found, field)
            for handle in value if isinstance(value, list) else [value] if value else []:
                try:
                    kind = self.ledger.entry(project, handle).type
                except ValueError:
                    raise DocumentError(INVALID_REQUEST, f"{where}: no entry {handle}") from None
                if kind not in types:
                    raise DocumentError(INVALID_REQUEST, f"{where}: {handle} is a {kind}")
        texts = [
            t
            for t in (found.title, found.statement, found.reason, found.reopen_condition,
                      found.tried, found.caveat)
            if t
        ]  # fmt: skip
        found_numbers = provenance.numbers(text)
        for value in texts:
            missing = sorted(provenance.numbers(value) - found_numbers)
            ids = [m for m in _IDS.findall(value) if m not in text]
            handles = [h for h in _HANDLE.findall(value) if not self._exists(project, h)]
            if missing or ids or handles:
                named = ", ".join([*missing, *ids, *handles][:3])
                raise DocumentError(INVALID_REQUEST, f"{where}: {named} is not in its span")

        def no_block(*_: object) -> str:
            raise DocumentError(INVALID_REQUEST, "a proposal anchors on no document")

        def no_entry(*_: object) -> tuple[str, int]:
            raise DocumentError(INVALID_REQUEST, "a proposal anchors on no entry")

        scope = provenance.Scope(session=session)
        with self.control.read() as tx:
            anchors = provenance.resolve(tx.sql, project, found.anchors, no_block, no_entry, scope)
            outside = [a["message"] for a in anchors if a["message"] not in span]
            if outside:
                raise DocumentError(
                    INVALID_REQUEST, f"{where} anchors on {outside[0]}, outside its span"
                )
            provenance.check(tx.sql, project, found.basis, anchors, [], texts, where)
        return anchors

    def _exists(self, project: str, handle: str) -> bool:
        try:
            self.ledger.entry(project, handle)
        except (DocumentError, ValueError):
            return False
        return True

    def waiting(self, project: str, session: str) -> list[ProposalView]:
        """The session's waiting proposals, oldest first."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM fold_proposals WHERE project=? AND session=? "  # noqa: S608
                "AND state='waiting' ORDER BY created, rowid",
                (project, session),
            ).fetchall()
        return [_view(row) for row in rows]

    def get(self, project: str, proposal: str) -> ProposalView:
        with self.control.read() as tx:
            row = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM fold_proposals WHERE project=? AND proposal_id=?",  # noqa: S608
                (project, proposal),
            ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, f"no fold proposal {proposal} in this project")
        return _view(row)

    def _decider(self, project: str, proposal: str, author: Author) -> ProposalView:
        found = self.get(project, proposal)
        if author.kind == "user":
            return found
        if author.kind != "agent" or author.role != "main" or author.session_id != found.session:
            raise DocumentError(
                POLICY_DENIED,
                "a fold's proposal is accepted or dismissed by the user, or by the main agent "
                "of its session",
            )
        return found

    def accept(self, project: str, proposal: str, operation_id: str, author: Author) -> Json:
        """Apply ``proposal`` with ``author``'s authority, under ``operation_id``; the
        first result again if it was accepted before."""
        found = self._decider(project, proposal, author)
        if found.state == "dismissed":
            raise DocumentError(CONFLICT, f"{proposal} was dismissed: {found.reason}")
        if found.state == "accepted":
            return {**(found.result or {}), "replayed": True}
        built = self._mutation(project, found, operation_id).model_dump_json()
        with self.control.write() as tx:  # kept before it is applied: a retry applies it
            tx.sql.execute(
                "UPDATE fold_proposals SET mutation=? WHERE proposal_id=? AND state='waiting' "
                "AND mutation IS NULL",
                (built, proposal),
            )
            (kept,) = tx.sql.execute(
                "SELECT mutation FROM fold_proposals WHERE proposal_id=?", (proposal,)
            ).fetchone()
        source = {"fold": found.fold, "proposal": proposal}
        try:
            (result,) = self.ledger.apply(
                project, [(Mutation.model_validate_json(kept), source)], author
            )
        except DocumentError:
            with self.control.write() as tx:  # nothing was written: built anew next time
                tx.sql.execute(
                    "UPDATE fold_proposals SET mutation=NULL WHERE proposal_id=? "
                    "AND state='waiting'",
                    (proposal,),
                )
            raise
        made = {
            "operation_id": result.operation_id,
            "handles": [entry.handle for entry in result.entries],
            "work": result.work,
        }
        self._decide(proposal, "accepted", author, result=made)
        return made

    def dismiss(self, project: str, proposal: str, reason: str, author: Author) -> ProposalView:
        """Dismiss ``proposal`` with ``reason``; one dismissed already stays as it was."""
        found = self._decider(project, proposal, author)
        if found.state == "accepted":
            raise DocumentError(CONFLICT, f"{proposal} was accepted")
        if found.state == "waiting":
            self._decide(proposal, "dismissed", author, reason=" ".join(reason.split())[:4096])
        return self.get(project, proposal)

    def _decide(
        self,
        proposal: str,
        state: str,
        author: Author,
        *,
        result: Json | None = None,
        reason: str | None = None,
    ) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE fold_proposals SET state=?,result=?,reason=?,decided_by=?,decided_at=? "
                "WHERE proposal_id=? AND state='waiting'",
                (
                    state,
                    None if result is None else json.dumps(result),
                    reason,
                    author.model_dump_json(exclude_none=True),
                    self.now().isoformat(),
                    proposal,
                ),
            )

    def _mutation(self, project: str, found: ProposalView, operation_id: str) -> Mutation:
        """The ledger change ``found`` proposes, against the entries as they read now; its
        first change, or its work record, carries the proposal's basis and anchors."""
        p, ledger = found.proposal, self.ledger
        known: Json = {"basis": p.basis, "anchors": p.anchors}
        changes: list[EntryChange] = []
        work = None
        if p.op == "work":
            made = WorkInput(entry=str(p.entry), tried=str(p.tried), ending="limit", limit=p.limit)
            work = [made.model_copy(update=known)]
        elif p.op == "caveat":
            view = ledger.entry(project, str(p.entry))
            fields = {**view.fields, "caveats": [*view.fields.get("caveats", ()), p.caveat]}
            changes.append(EntryChange(entry=view.entry_id, expected=view.head, fields=fields))
        else:
            named = {"issue": p.concerns, "decision": p.reopen_on, "assumption": p.relied_on_by}
            kinds: dict[str, LinkKind] = {"issue": "concerns", "decision": "reopen_on"}
            kind = kinds.get(p.op)
            fields: dict[str, Any] | None = {"kind": p.kind} if p.op == "issue" else None
            if p.reopen_condition is not None:
                fields = {"reopen_condition": p.reopen_condition}
            links = [LinkInput(kind=kind, entry=h) for h in named[p.op]] if kind else []
            new = EntryChange(
                key="proposed",
                type=p.op,
                title=p.title,
                statement=p.statement,
                status=p.status,
                fields=fields,
                links=links or None,
            )
            changes.append(new)
            for handle in named[p.op] if p.op == "assumption" else ():
                view = ledger.entry(project, handle)
                links = [*map(_input, view.links), LinkInput(kind="assumes", entry="new:proposed")]
                changes.append(EntryChange(entry=view.entry_id, expected=view.head, links=links))
        if changes:
            changes[0] = changes[0].model_copy(update=known)
        reason = p.reason or f"Proposed by fold {found.fold} for its span."
        return Mutation(
            operation_id=operation_id,
            reason=f"Accepted fold proposal {found.proposal_id}: {reason}"[:4096],
            changes=changes,
            work=work,
        )


def _input(link: EntryLink) -> LinkInput:
    """A link an entry holds, as a change gives it again."""
    if link.target == "block":
        return LinkInput(kind=link.kind, document=link.target_id, block=link.block,
                         revision=link.revision, note=link.note)  # fmt: skip
    if link.target == "finding":
        return LinkInput(kind=link.kind, finding=link.target_id, note=link.note)
    return LinkInput(kind=link.kind, entry=link.target_id, revision=link.revision, note=link.note)
