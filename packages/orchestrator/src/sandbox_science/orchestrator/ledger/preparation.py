# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger preparation. The caller owns the transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from sandbox_science.orchestrator.documents.agent_text import agent_text, agent_values
from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.errors import (
    INVALID_REQUEST,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.normalize import normalize
from sandbox_science.orchestrator.projects import run_id_of

from . import evidence, frontier, records
from . import provenance as anchoring
from .models import (
    FIELDS,
    STATUSES,
    THIS_SESSION,
    EntryChange,
    EvidenceInput,
    Mutation,
    ReferenceFields,
    WorkInput,
)
from .records import Json, Row

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
