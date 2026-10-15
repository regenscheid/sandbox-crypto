# SPDX-License-Identifier: MIT
"""Evidence links (docs/specs/ledger.md, LG-8 to LG-11, LG-15).

An evidence link has a role, an evidence type, a scope note and one pinned target: an
entry, artifact or document revision, a document block or math span, a review, a quote
of a reference: a PDF's page and lines, pinned by the PDF's SHA-256, or an ePrint
passage, named by the paper and the date stamp the e-print tools gave it (LG-8, CR-4),
or the outcome of a delegation that checked the entry, which is written once: an
``independent_check`` of the revision its brief named (LG-17, PD-17).
The target is the platform's record; the role, type and scope are the author's assertion
(LG-10). Links belong to the revision of the entry that holds them, which keeps them
from revision to revision, each with its ID and the revision that added it, until a
change removes it. Execution targets are not built yet and are refused.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Literal

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)

from .models import EVIDENCE_HOLDERS, PREFIXES, EvidenceInput, EvidenceLink

_COLUMNS: Final = (
    "evidence_id,role,type,target,target_id,target_revision,block,span,sha256,checked,scope,"
    "added,locator"
)

Target = Literal["entry", "artifact", "block", "review", "quote", "delegation"]

Block = tuple[str, int, str, str | None]
"""A document block or math span as a change names it: document, revision, block, span."""


@dataclass(frozen=True)
class Holder:
    """The entry that holds an evidence link, as the change leaves it."""

    entry_id: str
    handle: str
    type: str
    before: int
    """Its revision before the change; 0 for an entry the change creates."""


@dataclass(frozen=True)
class Linker:
    """Who makes the change that adds a link: the role LG-13 gives its author (``user``,
    ``main`` or ``delegated``), and an agent's session."""

    role: str
    session: str | None


@dataclass(frozen=True)
class EntryTarget:
    """An entry an evidence link points to, as the change leaves it."""

    entry_id: str
    handle: str
    type: str
    head: int


def stored(db: sqlite3.Connection, entry: str, revision: int) -> list[EvidenceLink]:
    """The evidence links revision ``revision`` of ``entry`` holds, in order."""
    links: list[EvidenceLink] = []
    for row in db.execute(
        f"SELECT {_COLUMNS} FROM ledger_evidence WHERE entry_id=? AND revision=? "  # noqa: S608 - fixed columns
        "ORDER BY ordinal",
        (entry, revision),
    ).fetchall():
        values = dict(zip(_COLUMNS.split(","), row, strict=True))
        values["revision"] = values.pop("target_revision")
        values["locator"] = None if values["locator"] is None else json.loads(values["locator"])
        if values["target"] in ("entry", "quote"):
            kind, number = db.execute(
                "SELECT type,number FROM ledger_entries WHERE entry_id=?", (values["target_id"],)
            ).fetchone()
            values["handle"] = f"{PREFIXES[kind]}-{number}"
        links.append(EvidenceLink.model_validate(values))
    return links


def write(db: sqlite3.Connection, entry: str, revision: int, links: list[EvidenceLink]) -> None:
    """Write ``links`` as revision ``revision``'s evidence, in the open change ``db``."""
    for ordinal, link in enumerate(links, 1):
        db.execute(
            f"INSERT INTO ledger_evidence(entry_id,revision,ordinal,{_COLUMNS}) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry,
                revision,
                ordinal,
                link.evidence_id,
                link.role,
                link.type,
                link.target,
                link.target_id,
                link.revision,
                link.block,
                link.span,
                link.sha256,
                link.checked,
                link.scope,
                link.added,
                None if link.locator is None else json.dumps(link.locator, sort_keys=True),
            ),
        )


MAX_SCOPES: Final = 32768
"""The scope notes of an entry revision's evidence, as UTF-8, are at most this many
bytes, so the revision, its evidence and its links fit one review item."""


def retained(
    db: sqlite3.Connection, project: str, artifact: str, revision: int, given: str | None
) -> str:
    """The SHA-256 of an artifact revision of the project whose bytes the control store
    keeps, as it captured them; a hash ``given`` must be it. A tombstone, or a revision
    that keeps only the hash the sandbox reported, pins no content (RA-31 to RA-33)."""
    found = db.execute(
        "SELECT json_extract(record,'$.reference.sha256'),"
        "coalesce(json_extract(record,'$.content_retained'),1),"
        "coalesce(json_extract(record,'$.deleted'),0),"
        "coalesce(json_extract(record,'$.hash_source'),'capture') "
        "FROM artifact_revisions WHERE artifact=? AND number=? AND project=?",
        (artifact, revision, project),
    ).fetchone()
    if found is None:
        raise DocumentError(NOT_FOUND, f"no artifact {artifact} r{revision} in this project")
    digest, kept, deleted, source = found
    if deleted or not kept or source != "capture":
        raise DocumentError(
            INVALID_REQUEST,
            f"artifact {artifact} r{revision} keeps no content the platform captured: "
            + ("it records a deletion" if deleted else "only its hash and size are recorded"),
        )
    if given is not None and given != digest:
        raise DocumentError(
            CONFLICT, f"artifact {artifact} r{revision} has another SHA-256", {"sha256": digest}
        )
    return digest


def asserted_only(kind: str, links: list[EvidenceLink]) -> bool | None:
    """For a claim or result, whether it holds no evidence links at all (LG-11)."""
    return None if kind not in ("claim", "result") else not links


def key(link: EvidenceLink) -> tuple[object, ...]:
    """What makes two evidence links the same, whoever added them."""
    return (
        link.role,
        link.type,
        link.target,
        link.target_id,
        link.revision,
        link.block,
        link.span,
        link.checked,
        None if link.locator is None else json.dumps(link.locator, sort_keys=True),
    )


def cites_reference(links: list[EvidenceLink], references: set[str]) -> bool:
    """Whether a published entry cites a reference through a ``supports`` link of type
    ``source_claim`` (LG-15), to the reference or quoting it; ``references`` are the IDs
    of reference entries."""
    return any(
        link.role == "supports"
        and link.type == "source_claim"
        and link.target in ("entry", "quote")
        and link.target_id in references
        for link in links
    )


def resolve(
    db: sqlite3.Connection,
    project: str,
    holder: Holder,
    item: EvidenceInput,
    entry: EntryTarget | None,
    blocks: dict[Block, str],
    linker: Linker,
) -> EvidenceLink:
    """``item`` as the link the new revision of ``holder`` adds, its target checked to
    exist in the project at the revision it pins."""
    if holder.type == "reference":
        # A reference holds only checks of itself: the user's record that they checked
        # a revision of it, which vouches for the PDF that revision names (LG-16).
        if item.role != "independent_check" or entry is None or entry.entry_id != holder.entry_id:
            raise DocumentError(
                INVALID_REQUEST, "a reference holds no evidence but checks of itself"
            )
    elif holder.type not in EVIDENCE_HOLDERS:
        article = "an" if holder.type[0] in "aeiou" else "a"
        raise DocumentError(INVALID_REQUEST, f"{article} {holder.type} holds no evidence")
    if item.type == "unclassified":
        raise DocumentError(INVALID_REQUEST, "unclassified evidence comes only from imports")
    named = (
        item.entry,
        item.artifact,
        item.document,
        item.review,
        item.delegation,
        item.execution,
    )
    if sum(value is not None for value in named) != 1:
        raise DocumentError(
            INVALID_REQUEST,
            "evidence names one target: an entry, an artifact, a document block, a review "
            "or a delegation",
        )
    if item.execution is not None:
        raise DocumentError(
            INVALID_REQUEST,
            "execution targets are not built yet: pin the artifact revision the execution wrote",
        )
    if item.role == "independent_check":
        if item.checked is None:
            raise DocumentError(
                INVALID_REQUEST, "an independent_check names the revision of the entry it checked"
            )
        if not item.checked <= holder.before:
            raise DocumentError(NOT_FOUND, f"{holder.handle} has no revision {item.checked}")
    elif item.checked is not None:
        raise DocumentError(INVALID_REQUEST, "only an independent_check names a checked revision")
    if item.quote is not None:
        if entry is None or entry.type != "reference":
            raise DocumentError(INVALID_REQUEST, "a quote names the reference it quotes")
        if item.type != "source_claim":
            raise DocumentError(INVALID_REQUEST, "a quote is source_claim evidence")
    if item.type == "source_claim" and (
        entry is None or entry.type != "reference" or item.role not in ("supports", "contradicts")
    ):
        raise DocumentError(
            INVALID_REQUEST, "source_claim evidence supports or contradicts with a reference"
        )

    def link(target: Target, target_id: str, **pins: object) -> EvidenceLink:
        return EvidenceLink.model_validate(
            {
                "evidence_id": new_id("evidence"),
                "role": item.role,
                "type": item.type,
                "target": target,
                "target_id": target_id,
                "checked": item.checked,
                "scope": item.scope,
                "added": holder.before + 1,
                **pins,
            }
        )

    if item.delegation is not None:
        locator = _checked_by(db, project, holder, item, linker)
        return link("delegation", item.delegation, locator=locator)
    if item.quote is not None and entry is not None:
        if item.block is not None or item.math is not None or item.sha256 is not None:
            raise DocumentError(
                INVALID_REQUEST, "a quote names its reference, its revision and what it quotes"
            )
        revision = entry.head if item.revision is None else item.revision
        if not 1 <= revision <= entry.head:
            raise DocumentError(NOT_FOUND, f"{entry.handle} has no revision {revision}")
        locator = item.quote.model_dump(mode="json", exclude_none=True)
        return link(
            "quote", entry.entry_id, revision=revision, handle=entry.handle, locator=locator
        )
    if entry is not None:
        if item.block is not None or item.math is not None or item.sha256 is not None:
            raise DocumentError(INVALID_REQUEST, "an entry target names only its revision")
        if entry.entry_id == holder.entry_id and not (
            item.role == "independent_check" and item.revision == item.checked
        ):
            # Only a check of the entry as it reads names the entry itself: the user's
            # record that they checked it (Ledger.check).
            raise DocumentError(INVALID_REQUEST, "an entry is not its own evidence")
        revision = entry.head if item.revision is None else item.revision
        if not 1 <= revision <= entry.head:
            raise DocumentError(NOT_FOUND, f"{entry.handle} has no revision {revision}")
        return link("entry", entry.entry_id, revision=revision, handle=entry.handle)
    if item.artifact is not None:
        if item.revision is None or item.block is not None or item.math is not None:
            raise DocumentError(INVALID_REQUEST, "an artifact target names its revision")
        digest = retained(db, project, item.artifact, item.revision, item.sha256)
        return link("artifact", item.artifact, revision=item.revision, sha256=digest)
    if item.document is not None:
        if item.revision is None or item.block is None or item.sha256 is not None:
            raise DocumentError(INVALID_REQUEST, "a document target names its revision and block")
        document = blocks[(item.document, item.revision, item.block, item.math)]
        return link("block", document, revision=item.revision, block=item.block, span=item.math)
    assert item.review is not None  # noqa: S101 - one target is named
    if any(v is not None for v in (item.revision, item.block, item.math, item.sha256)):
        raise DocumentError(INVALID_REQUEST, "a review target names only the review")
    if (
        db.execute(
            "SELECT 1 FROM review_runs WHERE review_id=? AND project=?", (item.review, project)
        ).fetchone()
        is None
    ):
        raise DocumentError(NOT_FOUND, f"no review {item.review} in this project")
    return link("review", item.review)


def _checked_by(
    db: sqlite3.Connection, project: str, holder: Holder, item: EvidenceInput, linker: Linker
) -> dict[str, object]:
    """The outcome of the delegation ``item`` names, as its link records it: a check of
    ``holder`` at the revision ``item`` says it examined, which has ended, linked by the
    user or by a main run of the delegation's session. The locator names the brief by
    its SHA-256 and shows the parent's note, if any (LG-17)."""
    if item.role != "independent_check":
        raise DocumentError(
            INVALID_REQUEST, "a delegation's outcome is evidence only as an independent_check"
        )
    if any(
        value is not None
        for value in (item.revision, item.block, item.math, item.sha256, item.quote)
    ):
        raise DocumentError(INVALID_REQUEST, "a delegation target names only the delegation")
    found = db.execute(
        "SELECT kind,brief,ended,run,alias,models,result,session_id,read_entry,read_proofs "
        "FROM delegations WHERE delegation_id=? AND project_id=?",
        (item.delegation, project),
    ).fetchone()
    if found is None:
        raise DocumentError(NOT_FOUND, f"no delegation {item.delegation} in this project")
    kind, brief, ended, run, alias, models, result, session, read, proofs = found
    if linker.role != "user" and (linker.role != "main" or linker.session != session):
        raise DocumentError(
            POLICY_DENIED,
            f"delegation {item.delegation}'s outcome is linked by the user or by a main run "
            "of its session",
        )
    written: dict[str, Any] = json.loads(brief)
    check: dict[str, Any] = written.get("check") or {}
    if kind != "check" or (check.get("entry"), check.get("revision")) != (
        holder.entry_id,
        item.checked,
    ):
        raise DocumentError(
            INVALID_REQUEST,
            f"delegation {item.delegation} did not check {holder.handle} at revision "
            f"{item.checked}",
        )
    if ended is None:
        raise DocumentError(CONFLICT, f"delegation {item.delegation} has not ended")
    outcome: dict[str, Any] = json.loads(result) if result is not None else {}
    note = written.get("note") or None
    return {
        "run": run,
        "alias": alias,
        "models": json.loads(models) if models is not None else [],
        "ended": ended,
        "verdict": outcome.get("verdict"),
        "status": outcome.get("status"),
        "read_entry": None if read is None else bool(read),
        "read_proofs": None if proofs is None else bool(proofs),
        "brief_sha256": hashlib.sha256(str(brief).encode()).hexdigest(),
        **({"note": note} if isinstance(note, str) else {}),
    }
