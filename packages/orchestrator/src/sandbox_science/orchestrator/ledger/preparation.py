# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger  The caller owns the transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.documents.agent_text import agent_text, agent_values
from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.normalize import normalize
from sandbox_science.orchestrator.documents.pins import pin as pin_references
from sandbox_science.orchestrator.documents.pins import text_references
from sandbox_science.orchestrator.projects import run_id_of

from . import evidence, frontier, queries, records
from . import provenance as anchoring
from .models import (
    FIELDS,
    LINKS,
    STATUSES,
    THIS_SESSION,
    AnchorInput,
    EntryChange,
    EntryLink,
    EvidenceInput,
    EvidenceLink,
    LinkInput,
    Mutation,
    ReferenceFields,
    WorkInput,
    wrong_link_target,
)
from .records import TEXTS, Json, MutationInput, Planned, PreparedMutation, Row

MAX_FIELDS: Final = 65536


def first_status(kind: str, origin: str | None) -> str:
    if kind in ("claim", "result"):
        return "recorded" if origin == "published" else "proposed"
    return STATUSES[kind][0]


def normalized(text: str) -> str:
    """The statement in the dollar form (LD-42)."""
    return normalize(parse(text, documents=False)).text


def validated_fields(kind: str, raw: Json) -> Json:
    model: type[BaseModel] = FIELDS[kind]
    try:
        value = model.model_validate(raw)
    except ValidationError as error:
        raise DocumentError(
            INVALID_REQUEST, f"{kind} fields: {records.validation_error(error)}"
        ) from None
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
    if len(records.encoded(fields).encode()) > MAX_FIELDS:
        raise DocumentError(INVALID_REQUEST, f"{kind} fields exceed {MAX_FIELDS} bytes")
    return fields


def result_fields(db: sqlite3.Connection, project: str, fields: Json) -> Json:
    """``fields``, once a result's category reads them (LG-19)."""
    frontier.check_result(db, project, fields)
    return fields


def check_pdf(db: sqlite3.Connection, project: str, fields: Json) -> Json:
    """A reference's PDF is a revision of an artifact of the project (LG-15, RA-19),
    pinned by its SHA-256: a hash the change gives must be the revision's, and the
    revision's is recorded when it gives none."""
    pdf = fields.get("pdf")
    if pdf is None:
        return fields
    pdf["sha256"] = evidence.retained(
        db, project, pdf["artifact_id"], pdf["revision"], pdf.get("sha256")
    )
    if len(records.encoded(fields).encode()) > MAX_FIELDS:
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


def revision_status(row: Row, before: str, status: str) -> str:
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


def this_session(mutation: Mutation, author: Author) -> Mutation:
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


def agent_mutation(mutation: Mutation) -> Mutation:
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
            ascii_conditions(change.fields, where)
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
                agent_evidence(item, f"{where} evidence {index}'s")
                for index, item in enumerate(change.evidence, 1)
            ]
        changes.append(change.model_copy(update=update))
    reason = agent_text(mutation.reason, "the reason")
    work = None
    if mutation.work is not None:
        work = [agent_work(item, f"work {n}'s") for n, item in enumerate(mutation.work, 1)]
    return mutation.model_copy(update={"reason": reason, "changes": changes, "work": work})


def anchor_scope(db: sqlite3.Connection, project: str, author: Author) -> anchoring.Scope:
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


def agent_work(item: WorkInput, where: str) -> WorkInput:
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


def ascii_conditions(fields: Json, where: str) -> None:
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


def agent_evidence(item: EvidenceInput, where: str) -> EvidenceInput:
    """An agent's evidence item, its scope and the text it quotes checked."""
    update: dict[str, object] = {}
    if item.scope is not None:
        update["scope"] = agent_text(item.scope, f"{where} scope")
    if item.quote is not None:
        text = agent_text(item.quote.text, f"{where} quote")
        update["quote"] = item.quote.model_copy(update={"text": text})
    return item.model_copy(update=update) if update else item


def platform_provenance(provenance: Json | None) -> bool:
    """Whether a mutation is a record the platform makes for an agent, anchored by it: an
    import's line or a delegation's outcome. A fold proposal's accepted is checked as the
    acceptor's own (CM-15)."""
    return provenance is not None and ("file" in provenance or "delegation" in provenance)


def labelled(error: DocumentError, provenance: Json | None) -> DocumentError:
    """``error``, naming the results file's line its mutation came from."""
    line = (provenance or {}).get("line")
    if line is None:
        return error
    return DocumentError(
        error.kind, f"line {line}: {error.message}", {**error.details, "line": line}
    )


def plan_entry(
    db: sqlite3.Connection,
    project: str,
    change: EntryChange,
    role: str,
    numbers: dict[str, int],
    session: str | None = None,
) -> Planned:
    if change.entry is None:
        if change.repin is not None:
            raise DocumentError(INVALID_REQUEST, "a new entry's references are pinned anew")
        return create_entry(db, project, change, role, numbers)
    if change.type is not None or change.origin is not None or change.key is not None:
        raise DocumentError(INVALID_REQUEST, "a revision keeps the entry's type and origin")
    if change.expected is None:
        raise DocumentError(INVALID_REQUEST, "a revision names the revision it expects")
    row = queries.find(db, project, change.entry)
    if role == "delegated" and (
        not change.evidence
        or change.model_dump(
            exclude_none=True, exclude={"entry", "expected", "evidence", "basis", "anchors"}
        )
    ):
        # LG-13: a delegated agent adds evidence, and changes nothing else.
        raise records.refused("a delegated agent creates entries and adds evidence, nothing else")
    if change.expected != row.head:
        raise DocumentError(
            CONFLICT,
            f"{row.handle} is at revision {row.head}, not {change.expected}; read it again",
            {"entry": row.entry_id, "current": row.head},
        )
    current = db.execute(queries.REVISION, (row.entry_id, row.head)).fetchone()
    planned = Planned(
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
        text = normalized(change.statement)
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
        fields = validated_fields(row.type, given)
        check_pdf(db, project, fields)
        frontier.check_result(db, project, fields)
        if fields != planned.fields:
            planned.fields, planned.changed = fields, [*planned.changed, "fields"]
    if change.status is not None and change.status != current[2]:
        planned.status = revision_status(row, current[2], change.status)
        planned.changed = [*planned.changed, "status"]
    if change.links is not None:
        planned.previous = queries.links(db, row.entry_id, row.head)
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


def create_entry(
    db: sqlite3.Connection,
    project: str,
    change: EntryChange,
    role: str,
    numbers: dict[str, int],
) -> Planned:
    if change.type is None or change.expected is not None:
        raise DocumentError(INVALID_REQUEST, "a new entry names its type and no revision")
    if change.title is None or change.statement is None:
        raise DocumentError(INVALID_REQUEST, "a new entry has a title and a statement")
    kind = change.type
    if (kind in ("claim", "result")) != (change.origin is not None):
        raise DocumentError(INVALID_REQUEST, "claims and results, and only they, have an origin")
    status = first_status(kind, change.origin)
    if kind == "decision" and change.status in ("in_effect", "rejected"):
        # LG-17: the user or the main agent records a decision in effect at once, and
        # an approach abandoned as a rejected one, with its reopen condition (CM-10).
        if role == "delegated":
            raise records.refused("a delegated agent's decision is proposed")
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
    row = Row(new_id("entry"), project, kind, number, change.origin, 0)
    return Planned(
        change,
        row,
        True,
        title=change.title,
        statement=normalized(change.statement),
        status=status,
        fields=result_fields(
            db,
            project,
            check_pdf(db, project, validated_fields(kind, change.fields or {})),
        ),
        links=[] if change.links is None else None,
    )


def resolve_link(
    db: sqlite3.Connection,
    project: str,
    planned: Planned,
    link: LinkInput,
    keys: dict[str, Row],
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
        target = queries.find(db, project, link.entry)
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


def resolve_evidence(
    db: sqlite3.Connection,
    project: str,
    p: Planned,
    keys: dict[str, Row],
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
                row = queries.find(db, project, item.entry)
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


def final_links(db: sqlite3.Connection, p: Planned) -> list[EntryLink]:
    """The links the change leaves ``p``'s entry."""
    return p.links if p.links is not None else queries.links(db, p.row.entry_id, p.row.head)


def final_evidence(db: sqlite3.Connection, p: Planned) -> list[EvidenceLink]:
    """The evidence the change leaves ``p``'s entry."""
    if p.evidence is not None:
        return p.evidence
    return evidence.stored(db, p.row.entry_id, p.row.head) if not p.created else []


def resolve_sources(
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
        row = queries.find(db, project, reference)
        return row.entry_id, row.head

    def block(name: str, revision: int, block: str) -> str:
        return pinned[(name, revision, block, None)]

    kept = anchoring.resolve(db, project, anchors, block, find, anchor_scope(db, project, author))
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


def resolve_provenance(
    db: sqlite3.Connection,
    project: str,
    p: Planned,
    author: Author,
    pinned: dict[evidence.Block, str],
    provenance_: Json | None,
) -> None:
    """Set ``p``'s basis and anchors (CM-9), refusing an agent's change that needs an
    anchor and names none, whose evidence included, or whose basis its anchors do not
    bear out."""
    p.basis, p.anchors = resolve_sources(
        db, project, p.change.basis, p.change.anchors or [], author, pinned, provenance_
    )
    if author.kind == "user" or platform_provenance(provenance_):
        return
    held = final_evidence(db, p)
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


_DECIDED: Final = frozenset({"established", "recorded", "in_effect", "rejected", "reversed"})


def prepare_mutation(inputs: MutationInput) -> PreparedMutation:
    """Resolve the mutation against its active transaction without writing entries."""
    db, project, mutation, author = inputs.db, inputs.project, inputs.mutation, inputs.author
    role, pinned = inputs.authority.role, inputs.pinned
    numbers: dict[str, int] = {}
    planned = [
        plan_entry(db, project, change, role, numbers, author.session_id)
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
                resolve_link(db, project, p, link, keys, pinned, heads) for link in p.change.links
            ]
            if not p.created and p.links != p.previous:
                p.changed = [*p.changed, "links"]
        linker = evidence.Linker(role, author.session_id)
        resolve_evidence(db, project, p, keys, heads, pinned, linker)
        if not p.created and not p.changed:
            raise DocumentError(INVALID_REQUEST, f"the change to {p.row.handle} changes nothing")
    return PreparedMutation(inputs, planned, keys)
