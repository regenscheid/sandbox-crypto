# SPDX-License-Identifier: MIT
"""The ledger's entry types, statuses and links, and the records a change and a read
carry (docs/specs/ledger.md, LG-1 to LG-5, LG-30)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import Field, JsonValue

from sandbox_science.contracts.model import Contract, Identifier, Sha256

EntryType = Literal["issue", "claim", "result", "assumption", "decision", "task", "reference"]
Origin = Literal["published", "project"]
LinkKind = Literal[
    "answers",
    "assumes",
    "builds_on",
    "restates",
    "compares_to",
    "supersedes",
    "dominates",
    "split_into",
    "affects",
    "concerns",
]

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

LINKS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    # kind: (the types that may hold it, the types it may point to; "finding" and
    # "block" are a review finding and a document block)
    "answers": (("claim", "result"), ("issue",)),
    "assumes": (("claim", "result", "decision"), ("assumption",)),
    "builds_on": (("claim", "result", "issue"), ("claim", "result")),
    "restates": (("claim", "result"), ("claim", "result")),
    "compares_to": (("claim", "result"), ("claim", "result")),
    "supersedes": (("claim", "result"), ("claim", "result")),
    "dominates": (("result",), ("result",)),
    "split_into": (("issue",), ("issue",)),
    "affects": (("decision",), tuple(PREFIXES)),
    "concerns": (("issue",), (*PREFIXES, "finding", "block")),
}
"""Which entries hold each kind of link, and what it points to (LG-5)."""

EvidenceRole = Literal["supports", "contradicts", "derivation", "independent_check"]
EvidenceType = Literal[
    "proof",
    "exact_computation",
    "finite_test",
    "numerical_experiment",
    "source_claim",
    "synthetic",
    "unclassified",
]
"""Evidence types (LG-9); ``unclassified`` is for imported links whose type the source
does not settle (LG-37)."""

EVIDENCE_HOLDERS: Final = ("claim", "result", "decision")
"""The entry types that hold evidence links (LG-8)."""

COMPUTATIONAL: Final = frozenset({"exact_computation", "finite_test", "numerical_experiment"})
"""Evidence types a computation produces."""

Text = Annotated[str, Field(min_length=1, max_length=512)]
Note = Annotated[str, Field(min_length=1, max_length=4096)]
Statement = Annotated[str, Field(min_length=1, max_length=8192)]
Reference = Annotated[str, Field(min_length=1, max_length=128)]
"""An entry named by its ID, its handle, or ``new:<key>`` for an entry the same
change creates."""


class LinkInput(Contract):
    """A link as a change gives it. An entry target is named by ``entry``; a review
    finding by ``finding``; a document block by ``document``, ``revision`` and
    ``block``. ``revision`` pins an entry target to a revision (``assumes`` needs one)."""

    kind: LinkKind
    entry: Reference | None = None
    finding: Identifier | None = None
    document: Identifier | None = None
    block: Identifier | None = None
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    note: Note | None = None


class EvidenceInput(Contract):
    """An evidence link as a change adds it (LG-8): its role, its type, a scope note and
    one pinned target: an entry revision (``entry``, ``revision`` defaulting to the
    entry's revision as the change leaves it), an artifact revision (``artifact``,
    ``revision`` and optionally its ``sha256``), a document block or math span
    (``document``, ``revision``, ``block`` and optionally ``math``) or a review
    (``review``). ``checked`` is the revision of this entry an ``independent_check``
    examined."""

    role: EvidenceRole
    type: EvidenceType
    entry: Reference | None = None
    artifact: Identifier | None = None
    document: Identifier | None = None
    review: Identifier | None = None
    execution: Identifier | None = None
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    block: Identifier | None = None
    math: Identifier | None = None
    sha256: Sha256 | None = None
    checked: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    scope: Note | None = None
    """What the evidence covers, such as "$p = 17$ only"."""


class ClaimFields(Contract):
    kind: (
        Literal["lemma", "theorem", "observation", "counterexample", "negative_result", "other"]
        | None
    ) = None


Measure = (
    Annotated[float, Field(allow_inf_nan=False)]
    | Annotated[str, Field(min_length=1, max_length=256)]
)


class ResultFields(ClaimFields):
    category: Identifier | None = None
    conditions: dict[Identifier, Annotated[str, Field(max_length=256)]] = Field(
        default_factory=dict[str, str], max_length=32
    )
    measures: dict[Identifier, Measure] = Field(default_factory=dict[str, Measure], max_length=32)


class PdfLink(Contract):
    """A reference's PDF: an artifact revision of the project, pinned by its SHA-256.
    A change may leave the hash out; the ledger records the revision's own."""

    artifact_id: Identifier
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)]
    sha256: Sha256 | None = None


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
    revises (``entry`` and ``expected`` given). A revision replaces the fields it gives:
    ``links`` replaces the entry's outgoing links."""

    entry: Reference | None = None
    expected: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    key: Identifier | None = None
    """A created entry's key, which other changes of the mutation name as ``new:<key>``."""
    type: EntryType | None = None
    origin: Origin | None = None
    title: Text | None = None
    statement: Statement | None = None
    status: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    fields: dict[str, JsonValue] | None = None
    links: list[LinkInput] | None = Field(default=None, max_length=64)
    evidence: list[EvidenceInput] | None = Field(default=None, max_length=32)
    """Evidence links to add; the entry keeps those it holds."""
    evidence_removed: list[Identifier] | None = Field(default=None, max_length=32)
    """The IDs of evidence links to remove."""


class Mutation(Contract):
    """A change to one or more entries, all or nothing, under an operation ID (LG-30)."""

    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    reason: Note
    changes: list[EntryChange] = Field(min_length=1, max_length=32)


class EntryLink(Contract):
    """A stored link: its kind, its target and the target's pinned revision."""

    kind: LinkKind
    target: Literal["entry", "finding", "block"]
    target_id: str
    revision: int | None = None
    block: str | None = None
    note: str | None = None
    handle: str | None = None
    """An entry target's handle, as it reads now."""
    session: str | None = None
    """A finding target's session, as it is now: the workspace reads a finding in its
    session, and a math finding can move to another."""


class EvidenceLink(Contract):
    """A stored evidence link: its role and type, its pinned target, its scope, and the
    revision of the entry that added it, whose author added it (LG-8)."""

    evidence_id: str
    role: EvidenceRole
    type: EvidenceType
    target: Literal["entry", "artifact", "block", "review"]
    target_id: str
    revision: int | None = None
    """The target's pinned revision: an entry's, an artifact's or a document's."""
    block: str | None = None
    span: str | None = None
    sha256: str | None = None
    """An artifact target's SHA-256."""
    checked: int | None = None
    """The revision of the holding entry an ``independent_check`` examined."""
    scope: str | None = None
    added: int
    handle: str | None = None
    """An entry target's handle, as it reads now."""


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
    asserted_only: bool | None = None
    """For a claim or result: whether it holds no evidence links at all (LG-11)."""


class EntryView(EntrySummary):
    """An entry at one revision, with what it says, its outgoing links and who wrote it,
    and (at its current revision) the links other entries hold to it."""

    statement: str
    fields: dict[str, Any]
    links: list[EntryLink]
    evidence: list[EvidenceLink]
    incoming: list[EntryLink]
    """Links other entries' current revisions hold to it, in the order those entries were
    made: as many as 200 from the one a read starts at."""
    incoming_total: int
    """How many there are in all."""
    head: int
    author: dict[str, Any]
    reason: str


class EntryPage(Contract):
    entries: list[EntrySummary]
    next_after: str | None


class EntryRevision(Contract):
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
    revisions: list[EntryRevision]
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
