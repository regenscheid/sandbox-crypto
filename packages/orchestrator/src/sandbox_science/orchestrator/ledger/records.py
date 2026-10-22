# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger records. No function opens a transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import ValidationError

from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents.errors import (
    POLICY_DENIED,
    DocumentError,
)

from .models import (
    PREFIXES,
    EntryChange,
    EntryLink,
    EvidenceLink,
)

Json = dict[str, Any]


TEXTS: Final = TextRevisions("statement")


@dataclass
class Row:
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
class Planned:
    """One change of a mutation, resolved: the entry it makes or revises, and what the
    new revision holds."""

    change: EntryChange
    row: Row
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


def refused(message: str) -> DocumentError:
    return DocumentError(POLICY_DENIED, f"not_permitted_for_role: {message}")


def encoded(fields: Json) -> str:
    """Fields as the table keeps them: UTF-8 as is, no NaN or infinity."""
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def links_added(p: Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the change's new revision holds that the one before did not."""
    before = {(link.target, link.target_id) for link in p.previous if link.kind == kind}
    return [
        link
        for link in p.links or ()
        if link.kind == kind and (link.target, link.target_id) not in before
    ]


def links_dropped(p: Planned, kind: str) -> list[EntryLink]:
    """The links of ``kind`` the revision before held that the change's new one does not."""
    if p.links is None:
        return []
    after = {(link.target, link.target_id) for link in p.links if link.kind == kind}
    return [
        link
        for link in p.previous
        if link.kind == kind and (link.target, link.target_id) not in after
    ]


def anchors_json(anchors: list[Json]) -> str:
    return json.dumps(anchors, sort_keys=True, ensure_ascii=False)


def validation_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "request"
    return f"{where}: {first['msg']}"


@dataclass(frozen=True)
class Authority:
    """The authorized role and project sessions captured before a mutation batch."""

    role: str
    sessions: frozenset[str]
