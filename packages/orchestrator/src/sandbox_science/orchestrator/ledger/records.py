# SPDX-License-Identifier: MIT
"""Transaction-scoped ledger records. No function opens a transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from sandbox_science.orchestrator.control.texts import TextRevisions

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
