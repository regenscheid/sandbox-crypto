# SPDX-License-Identifier: MIT
"""The ledger's entry types and statuses, and the records a change and a read
carry (docs/specs/ledger.md, LG-1 to LG-5, LG-30)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import Field, JsonValue

from sandbox_science.contracts.model import Contract, Identifier

EntryType = Literal["issue", "claim", "result", "assumption", "decision", "task", "reference"]
Origin = Literal["published", "project"]
PREFIXES: Final[dict[str, str]] = {
    "issue": "I",
    "claim": "C",
    "result": "R",
    "assumption": "A",
    "decision": "D",
    "task": "T",
    "reference": "REF",
}
"""Each type's handle prefix: ``I-3``, ``C-12``, ``REF-2`` (LG-1, LG-2)."""

_CLAIM: Final = (
    "proposed",
    "established",
    "recorded",
    "disputed",
    "refuted",
    "superseded",
    "withdrawn",
)
STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "issue": ("open", "resolved", "obstructed", "split", "withdrawn"),
    "claim": _CLAIM,
    "result": _CLAIM,
    "assumption": ("active", "withdrawn"),
    "decision": ("proposed", "in_effect", "rejected", "reversed"),
    "task": ("todo", "in_progress", "blocked", "done", "dropped"),
    "reference": ("active", "withdrawn"),
}
"""Each type's statuses (LG-2)."""

DECIDED: Final = frozenset({"established", "obstructed", "refuted"})
"""Statuses only a decision in effect sets (LG-4, LG-7, LG-17)."""

Text = Annotated[str, Field(min_length=1, max_length=512)]
Note = Annotated[str, Field(min_length=1, max_length=4096)]
Statement = Annotated[str, Field(min_length=1, max_length=8192)]
Reference = Annotated[str, Field(min_length=1, max_length=128)]
"""An entry named by its ID or its handle."""


class ClaimFields(Contract):
    kind: (
        Literal["lemma", "theorem", "observation", "counterexample", "negative_result", "other"]
        | None
    ) = None


Measure = float | Annotated[str, Field(min_length=1, max_length=256)]


class ResultFields(ClaimFields):
    category: Identifier | None = None
    conditions: dict[Identifier, Annotated[str, Field(max_length=256)]] = Field(
        default_factory=dict[str, str], max_length=32
    )
    measures: dict[Identifier, Measure] = Field(default_factory=dict[str, Measure], max_length=32)


class PdfLink(Contract):
    artifact_id: Identifier
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)]


Scheme = Literal["doi", "eprint", "arxiv", "nist", "isbn", "url"]
"""The identifiers a reference can carry (LG-15)."""


class ReferenceFields(Contract):
    """A source's citation (LG-15); its text is the entry's statement."""

    identifiers: dict[Scheme, Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=dict[Scheme, str]
    )
    authors: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list[str], max_length=64
    )
    published: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    version: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    retrieved: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    pdf: PdfLink | None = None


class EmptyFields(Contract):
    pass


FIELDS: Final[dict[str, type[Contract]]] = {
    "issue": EmptyFields,
    "claim": ClaimFields,
    "result": ResultFields,
    "assumption": EmptyFields,
    "decision": EmptyFields,
    "task": EmptyFields,
    "reference": ReferenceFields,
}
"""Each type's own fields (LG-1); the frontier and the gate add to these later."""


class EntryChange(Contract):
    """One entry a mutation creates (``type`` given, ``entry`` and ``expected`` not) or
    revises (``entry`` and ``expected`` given). A revision replaces the fields it gives."""

    entry: Reference | None = None
    expected: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    type: EntryType | None = None
    origin: Origin | None = None
    title: Text | None = None
    statement: Statement | None = None
    status: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    fields: dict[str, JsonValue] | None = None


class Mutation(Contract):
    """A change to one or more entries, all or nothing, under an operation ID (LG-30)."""

    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    reason: Note
    changes: list[EntryChange] = Field(min_length=1, max_length=32)


class EntrySummary(Contract):
    """An entry as a list shows it."""

    entry_id: str
    handle: str
    type: EntryType
    origin: Origin | None
    revision: int
    title: str
    status: str
    updated: str


class EntryView(EntrySummary):
    """An entry at one revision, with what it says and who wrote it."""

    statement: str
    fields: dict[str, Any]
    head: int
    author: dict[str, Any]
    reason: str


class EntryPage(Contract):
    entries: list[EntrySummary]
    next_after: str | None


class RevisionSummary(Contract):
    revision: int
    title: str
    status: str
    statement_changed: bool
    author: dict[str, Any]
    reason: str
    operation_id: str
    provenance: dict[str, Any] | None
    """Where the change came from, such as a results file's line (LG-41)."""
    created: str


class EntryHistory(Contract):
    entry_id: str
    handle: str
    revisions: list[RevisionSummary]
    next_before: int | None


class ChangedEntry(Contract):
    entry_id: str
    handle: str
    revision: int
    status: str
    created: bool


class MutationResult(Contract):
    operation_id: str
    entries: list[ChangedEntry]
    replayed: bool = False
