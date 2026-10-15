# SPDX-License-Identifier: MIT
"""The ledger's entry types, statuses and links, and the records a change and a read
carry (docs/specs/ledger.md, LG-1 to LG-5, LG-30)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, JsonValue, model_validator

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


ClaimKind = Literal["lemma", "theorem", "observation", "counterexample", "negative_result", "other"]


class ClaimFields(Contract):
    kind: ClaimKind | None = None


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


FlagID = Annotated[str, Field(pattern=r"^flag-[0-9a-f]{24}$")]

FlagCause = Literal[
    "assumption_revised", "assumption_withdrawn", "dependency_status", "evidence_changed", "spread"
]
"""Why an entry is flagged for reconsideration (LG-28)."""

MAX_CLEARED: Final = 256
"""The most flags one change clears, by ID or by entry."""


class FlagScope(Contract):
    """All the open flags on an entry, or those of one cause."""

    entry: Reference
    cause: FlagCause | None = None


class Mutation(Contract):
    """A change to one or more entries, all or nothing, under an operation ID (LG-30);
    it may also clear flags, with its reason, and the user's may reopen cleared ones
    (LG-29)."""

    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    reason: Note
    changes: list[EntryChange] = Field(default_factory=list[EntryChange], max_length=32)
    flags_cleared: list[FlagID] | None = Field(default=None, max_length=MAX_CLEARED)
    """Open flags the change clears, with its reason."""
    flags_cleared_on: list[FlagScope] | None = Field(default=None, max_length=32)
    """Entries whose open flags, or those of one cause, the change clears."""
    flags_reopened: list[FlagID] | None = Field(default=None, max_length=64)
    """Cleared flags the user opens again."""

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if not (self.changes or self.flags_cleared or self.flags_cleared_on or self.flags_reopened):
            raise ValueError("a change names entries to change or flags to clear or reopen")
        return self


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


class EntryFinding(Contract):
    """A review finding anchored to the entry, with its own status (LG-25): the field
    and revision it is anchored to (a math finding's revision is its statement's text
    revision, and it names no entry revision)."""

    finding_id: str
    session: str
    status: str
    severity: str
    category: str
    origin: str
    field: str | None = None
    revision: int | None = None


class EntryFlag(Contract):
    """An open flag for reconsideration (LG-28): its cause, what it came from, the flag it
    spread from, the revision it was raised on, the reviews whose snapshots held that
    revision, and the cleared flag it opens again, when the user reopened one."""

    flag_id: str
    cause: FlagCause
    source: dict[str, Any]
    via: str | None
    revision: int
    raised: str
    reviews: list[str]
    reopens: str | None = None


class ClearedFlag(Contract):
    """A flag that was cleared (LG-29): who cleared it, when, and why."""

    flag_id: str
    cause: FlagCause
    source: dict[str, Any]
    via: str | None
    revision: int
    raised: str
    cleared: str
    cleared_by: dict[str, Any]
    reason: str
    operation_id: str


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
    open_flags: int = 0
    """How many flags for reconsideration are open on it (LG-28)."""


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
    findings: list[EntryFinding]
    """The review findings anchored to the entry, newest first: as many as 50."""
    flags: list[EntryFlag]
    """Its open flags, newest first: as many as 50."""
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
    flags_cleared: list[ClearedFlag] = Field(default_factory=list[ClearedFlag])
    """On the first page: the flags cleared on the entry, latest first, as many as 50."""


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
    flagged: list[str] = Field(default_factory=list[str])
    """The handles of the entries the change flagged for reconsideration (LG-28), as many
    as ``SHOWN_FLAGGED``."""
    flagged_total: int = 0
    """How many entries it flagged in all."""
    flags_cleared: list[str] = Field(default_factory=list[str])
    """The flags it cleared, as many as ``SHOWN_FLAGGED``."""
    cleared_total: int = 0
    """How many flags it cleared in all."""
    flags_reopened: list[str] = Field(default_factory=list[str])
    """The flags the user's change opened again, each a new flag."""


SHOWN_FLAGGED: Final = 50
"""The flagged entries and cleared flags a change's result lists; it counts the rest."""


class GateClause(Contract):
    """One clause of the gate (LG-17): whether it holds for the entry as it reads, and why."""

    clause: Literal["evidence", "independent_check", "computation", "review", "finding", "flags"]
    holds: bool
    reason: str


class GateDecision(Contract):
    """A decision whose current revision ``affects`` the entry (LG-4)."""

    entry_id: str
    handle: str
    title: str
    status: str
    revision: int
    author: dict[str, Any]


class GateView(Contract):
    """The gate for an entry as it reads now, as the main agent's decision to establish it
    would meet it, read without changing anything (LG-17)."""

    entry_id: str
    handle: str
    revision: int
    status: str
    applies: bool
    """Whether the gate applies: only a project claim or result is established."""
    holds: bool
    """Whether every clause holds, so that the main agent could establish it now."""
    clauses: list[GateClause]
    """Every clause, in the order the gate reads them; none when it does not apply."""
    decisions: list[GateDecision]
    """The decisions that affect the entry, newest first, as many as 50."""


# The frontier (LG-18 to LG-24).


class CategoryMeasure(Contract):
    """A measure results in a category are compared on: its unit, and whether lower or
    higher is better."""

    name: Identifier
    unit: Text
    direction: Literal["lower", "higher"]


Operation = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class CategoryDefinition(Contract):
    """A frontier category as the user or the main agent defines it (LG-18): new, or a
    revision of the one at ``expected``."""

    category: Identifier
    expected: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    """The revision read, to revise it; none to define a new category."""
    target: Note
    """What the results are about, such as a scheme and parameter set."""
    kind: Note
    """The kind of result, such as key recovery cost or a bound."""
    measures: list[CategoryMeasure] = Field(min_length=1, max_length=16)
    conditions: list[Identifier] = Field(default_factory=list[str], max_length=16)
    """The condition keys results are compared under, such as attack or cost model."""
    reason: Note
    operation_id: Operation

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        names = [measure.name for measure in self.measures]
        # The frontier reads condition keys case folded (frontier.normal; they are ASCII).
        keys = [key.casefold() for key in self.conditions]
        if len(set(names)) != len(names) or len(set(keys)) != len(keys):
            raise ValueError("a category names each measure and condition key once")
        return self


class Category(Contract):
    """A frontier category at one revision."""

    category: str
    revision: int
    head: int
    target: str
    kind: str
    measures: list[CategoryMeasure]
    conditions: list[str]
    author: dict[str, Any]
    reason: str
    operation_id: str
    created: str
    replayed: bool = False


class CategoryPage(Contract):
    """A project's frontier categories, in the order they were defined."""

    categories: list[Category]
    next_after: str | None


class DominatesLink(Contract):
    """A ``dominates`` link that takes a result from the best known or from those not
    compared (LG-22): the result that holds it, its reason, and who recorded it in which
    revision of the holder."""

    entry_id: str
    handle: str
    reason: str
    author: dict[str, Any]
    revision: int
    target: str = ""
    """The handle of the result it names."""
    void: str | None = None
    """Why it counts for nothing, when it does: the rule compares the two results now, or
    it lies on a cycle of results that dominate one another."""


class FrontierResult(Contract):
    """A result as the frontier shows it (LG-23): what it states, under which conditions,
    its measures, origin, status, who wrote it, the types of its evidence, the references
    it cites and the artifacts it is pinned to, and its open findings and flags."""

    entry_id: str
    handle: str
    revision: int
    """The revision shown: the current one, or for a disputed result the one before it
    was disputed, which was eligible (LG-20)."""
    title: str
    statement: str
    status: str
    origin: str
    disputed: bool
    """Eligible, and now ``disputed`` (LG-20)."""
    unverified: bool = False
    """A published result an agent recorded that the user has not confirmed as the
    revision shown reads: a candidate, not eligible (LG-16, LG-20 as amended)."""
    author: dict[str, Any] = Field(default_factory=dict[str, Any])
    """Who wrote the revision shown: its kind, and an agent's role."""
    conditions: dict[str, str]
    measures: dict[str, float | str]
    evidence_types: list[str]
    sources: list[str]
    """The handles of the references it cites."""
    artifacts: list[dict[str, Any]]
    """The artifact revisions its evidence is pinned to."""
    open_findings: int
    open_flags: int
    dominated_by: list[str] = Field(default_factory=list[str])
    """For a dominated result, the results that dominate it."""
    linked_by: list[DominatesLink] = Field(default_factory=list[DominatesLink])
    """Of those, the ones that hold a ``dominates`` link to it, with the link's reason
    and author: the user can drop one."""
    would_dominate: list[str] = Field(default_factory=list[str])
    """For a candidate, the best known results it would dominate if established."""


class FrontierGroup(Contract):
    """The results of a category under one set of conditions (LG-21)."""

    conditions: dict[str, str]
    best: list[FrontierResult]
    """The eligible results no other eligible result dominates."""
    dominated: list[FrontierResult]
    not_compared: list[FrontierResult]
    """Eligible results with a measure missing or not a number."""
    candidates: list[FrontierResult]
    """Project results that are not eligible yet, and unverified published ones (LG-23)."""
    more: dict[str, int] = Field(default_factory=dict[str, int])
    """For an answer that lists only the first of a kind of result (``best``,
    ``dominated``, ``not_compared``, ``candidates``), how many it leaves out."""


class CategoryRevised(Contract):
    """The main agent's revision of a category that the user has not revised since,
    which the frontier shows first: what it compares changed without the user."""

    revision: int
    created: str
    author: dict[str, Any]
    reason: str
    since: int | None
    """The user's latest revision before it, which ``changed`` compares it with; none
    when the user wrote none."""
    changed: list[str]
    """What changed since, in words, such as ``measure time's direction: higher is
    better``."""


class FrontierView(Contract):
    """A category's frontier (LG-21, LG-23): its results grouped by their conditions."""

    category: Category
    groups: list[FrontierGroup]
    unplaced: list[FrontierResult]
    """Eligible results that do not give a value for each of the category's condition
    keys, which it gained after they were recorded."""
    truncated: bool
    """Whether the category has more proposed results than the frontier reads as
    candidates, newest first; it reads every eligible one."""
    next_candidates_before: int | None = None
    """When it has, the number of the oldest candidate read: the next read begins there."""
    lookalike: list[list[dict[str, str]]] = Field(default_factory=list[list[dict[str, str]]])
    """Sets of groups whose conditions differ only in characters that look alike (a
    partial table: Unicode dashes and slashes, Cyrillic and Greek letters that look Latin,
    other scripts' digits): the user's spellings, kept apart, and shown as a warning."""
    void_links: list[DominatesLink] = Field(default_factory=list[DominatesLink])
    """The dominates links between eligible results that count for nothing, with why: the
    user drops one, or one of its cycle, to settle it (LG-22)."""
    agent_revised: CategoryRevised | None = None
    """The main agent's revision of the category, when the user has not revised it since."""
    unplaced_more: int = 0
    """For an answer that lists only the first unplaced results, how many it leaves out."""


class FrontierMember(Contract):
    """A best known result as a frontier change recorded it."""

    entry_id: str
    handle: str
    revision: int


class FrontierChange(Contract):
    """A change to a category's best known results under one set of conditions (LG-24):
    the set it left, what it added and removed, and what caused it."""

    seq: int
    conditions: dict[str, str]
    best: list[FrontierMember]
    added: list[str]
    """The handles of the results it made best known."""
    removed: list[str]
    """The handles of the results it took from the best known: they stay in history."""
    cause: dict[str, Any]
    """The change that caused it: its operation ID, author, reason, the entries it
    changed and the dominates links it added or dropped, or the category's revision;
    ``{"baseline": "frontier history begins"}`` for the best known a category already
    had when its history began."""
    created: str
    more: dict[str, int] = Field(default_factory=dict[str, int])
    """For an answer that lists only the first of ``best``, ``added`` or ``removed``,
    how many it leaves out."""


class FrontierHistory(Contract):
    """A category's frontier changes, newest first."""

    category: str
    changes: list[FrontierChange]
    next_before: int | None


class FrontierPastGroup(Contract):
    conditions: dict[str, str]
    best: list[FrontierMember]
    more: int = 0
    """For an answer that lists only the first best known, how many it leaves out."""


class FrontierPast(Contract):
    """A category's best known results as they were at ``at`` (LG-24)."""

    category: str
    at: str
    groups: list[FrontierPastGroup]
