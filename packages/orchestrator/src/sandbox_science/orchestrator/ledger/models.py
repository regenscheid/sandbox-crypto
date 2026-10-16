# SPDX-License-Identifier: MIT
"""The ledger's entry types, statuses and links, and the records a change and a read
carry (docs/specs/ledger.md, LG-1 to LG-5, LG-30)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

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
    "reopen_on",
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

SET_ASIDE: Final = frozenset({"refuted", "withdrawn", "superseded"})
"""The statuses of an entry that no longer supports anything, which it leaves only by a
decision in effect that affects it."""

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
    "reopen_on": (("decision",), ("issue", "assumption", "claim", "result")),
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
Note = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]
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


MIN_QUOTE: Final = 20
"""The fewest characters, white space aside, a quote holds."""


class QuoteInput(Contract):
    """What a claim or result quotes from the reference it cites (LG-8, CR-4): a PDF's
    page and lines as ``pdf_pages`` or ``pdf_search`` numbered them, with the page's
    printed label, or an ePrint passage as the e-print tools gave it, with the date stamp
    they gave the paper; and the text, as it was read."""

    source: Literal["pdf", "eprint"]
    page: Annotated[int, Field(ge=1, le=100000, strict=True)] | None = None
    """The PDF's physical page, from 1."""
    label: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    first_line: Annotated[int, Field(ge=1, le=100000, strict=True)] | None = None
    last_line: Annotated[int, Field(ge=1, le=100000, strict=True)] | None = None
    dated: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    """For an ePrint, the date stamp the e-print tools gave the paper, such as
    ``2015-06-30T18:41:29Z; 2015-06-30T20:25:17Z``: the paper ID and this stamp are
    what names the version, as the server reports it."""
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    """At least ``MIN_QUOTE`` characters that are not white space: a few words match too
    much to vouch for anything."""

    @model_validator(mode="after")
    def _source(self) -> Self:
        if sum(not c.isspace() for c in self.text) < MIN_QUOTE:
            raise ValueError(
                f"a quote holds at least {MIN_QUOTE} characters that are not white space"
            )
        if self.source == "pdf":
            if self.page is None or self.first_line is None or self.last_line is None:
                raise ValueError("a PDF quote names its page and its first and last lines")
            if self.dated is not None:
                raise ValueError("a PDF quote names no date stamp: the PDF's hash names it")
        elif self.dated is None:
            raise ValueError("an ePrint quote names the date stamp the e-print tools gave")
        if (self.first_line is None) != (self.last_line is None) or (
            self.first_line is not None
            and self.last_line is not None
            and self.first_line > self.last_line
        ):
            raise ValueError("a quote's lines run from its first to its last")
        return self


class EvidenceInput(Contract):
    """An evidence link as a change adds it (LG-8): its role, its type, a scope note and
    one pinned target: an entry revision (``entry``, ``revision`` defaulting to the
    entry's revision as the change leaves it), an artifact revision (``artifact``,
    ``revision`` and optionally its ``sha256``), a document block or math span
    (``document``, ``revision``, ``block`` and optionally ``math``), a review
    (``review``), or, for an ``independent_check``, the outcome of a delegation that
    checked this entry (``delegation``). ``checked`` is the revision of this entry an
    ``independent_check`` examined."""

    role: EvidenceRole
    type: EvidenceType
    entry: Reference | None = None
    artifact: Identifier | None = None
    document: Identifier | None = None
    review: Identifier | None = None
    delegation: Identifier | None = None
    execution: Identifier | None = None
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    block: Identifier | None = None
    math: Identifier | None = None
    sha256: Sha256 | None = None
    checked: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    scope: Note | None = None
    """What the evidence covers, such as "$p = 17$ only"."""
    quote: QuoteInput | None = None
    """With ``entry`` naming a reference: what the claim or result quotes from it."""


ClaimKind = Literal["lemma", "theorem", "observation", "counterexample", "negative_result", "other"]
IssueKind = Literal["question", "hypothesis", "idea", "experiment", "contradiction", "reproduction"]
"""What an issue records (CM-10); ``reproduction`` is a failed reproduction."""

MAX_CAVEATS: Final = 10
Caveat = Annotated[str, Field(min_length=1, max_length=300, pattern=r"^[^\r\n]*\S[^\r\n]*$")]
"""One line that qualifies a claim or result, such as "estimated from 2^20 samples"."""


class IssueFields(Contract):
    """An issue's kind (CM-10): one that names none, as those written before kinds were,
    is a question."""

    kind: IssueKind = "question"


class ClaimFields(Contract):
    kind: ClaimKind | None = None
    caveats: list[Caveat] = Field(default_factory=list[str], max_length=MAX_CAVEATS)
    """What qualifies it beyond its evidence's scope notes and its assumptions (CM-10)."""


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


WorkID = Annotated[str, Field(pattern=r"^work-[0-9a-f]{24}$")]


class DecisionFields(Contract):
    """A decision's own fields. A decision that makes an issue ``obstructed`` names the
    scope it excludes and the user's assessment that accepted the claimed obstruction
    (LG-7, LG-F8); it cites its evidence as evidence links."""

    excluded_scope: Note | None = None
    assessment: WorkID | None = None
    reopen_condition: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    """What would justify reconsidering it (CM-10), ``none`` when nothing would; its
    ``reopen_on`` links name what it watches."""
    session: Identifier | None = None
    """The one session it is meant for, such as a constraint the user set for that
    session only; without it, it is the project's (CM-10). An agent gives ``this``."""


NO_CONDITION: Final = "none"
"""The reopen condition of a decision that nothing would reopen."""
THIS_SESSION: Final = "this"
"""The session a change names as the agent's own, which code fills in."""

FIELDS: Final[dict[str, type[Contract]]] = {
    "issue": IssueFields,
    "claim": ClaimFields,
    "result": ResultFields,
    "assumption": EmptyFields,
    "decision": DecisionFields,
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


WorkEnding = Literal["limit", "obstruction_claimed", "produced", "assessment", "reopened"]
"""How a work record ends (LG-6): the time, tools, context or ideas ran out; an obstruction
is claimed, with its evidence; it produced entries; it assesses other work; or an agent
reopened an obstructed issue, which the ledger records itself (the owner, 2026-10-15)."""
WorkLimit = Literal["time", "tools", "context", "ideas"]
Verdict = Literal["accepted", "rejected", "inconclusive"]
WORK_HOLDERS: Final = ("issue", "claim", "result")
"""The entry types work records are on (LG-6)."""
MAX_NOTES: Final = 16384
"""The most bytes of UTF-8 a work record's reference notes hold in all."""
MAX_ENTRY_WORK: Final = 5000
"""The most work records one entry holds."""


class WorkReference(Contract):
    """What a work record cites, pinned, with an optional note: a delegation's outcome
    (``delegation``), an artifact revision (``artifact`` and ``revision``, its SHA-256
    optional), a document block (``document``, ``revision`` and ``block``) or another work
    record (``work``: its ID, or ``new:<key>`` for one the same change records)."""

    delegation: Identifier | None = None
    artifact: Identifier | None = None
    document: Identifier | None = None
    revision: Annotated[int, Field(ge=1, le=999999999, strict=True)] | None = None
    block: Identifier | None = None
    sha256: Sha256 | None = None
    work: Reference | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        named = [self.delegation, self.artifact, self.document, self.work]
        if sum(value is not None for value in named) != 1:
            raise ValueError("a reference names one of delegation, artifact, document or work")
        if (self.artifact is not None or self.document is not None) != (self.revision is not None):
            raise ValueError("an artifact or document reference names its revision")
        if (self.document is not None) != (self.block is not None):
            raise ValueError("a document reference names its block")
        if self.sha256 is not None and self.artifact is None:
            raise ValueError("only an artifact reference names a SHA-256")
        return self


class WorkInput(Contract):
    """A work record a change appends (LG-6): on the issue, claim or result ``entry``
    (an ID, handle or ``new:<key>``), what was ``tried``, and how it ended. ``limit``
    names what ran out; ``produced``, the entries it created; an ``assessment`` may name
    the work record it ``assesses``, with its ``verdict``; an ``obstruction_claimed``
    cites its evidence in ``references``. ``part_of`` names the record it belongs to,
    such as a delegation outcome's; ``key`` names this one to the change's others."""

    key: Identifier | None = None
    entry: Reference
    tried: Statement
    ending: WorkEnding
    limit: WorkLimit | None = None
    produced: list[Reference] | None = Field(default=None, max_length=64)
    assesses: Reference | None = None
    verdict: Verdict | None = None
    references: list[WorkReference] = Field(default_factory=list[WorkReference], max_length=16)
    part_of: Reference | None = None

    @model_validator(mode="after")
    def _ending(self) -> Self:
        if self.ending == "reopened":
            raise ValueError("the ledger records a reopening itself, with the change's reason")
        if (self.ending == "limit") != (self.limit is not None):
            raise ValueError("a work record that ends limit names what ran out, and only it")
        if (self.ending == "produced") != bool(self.produced):
            raise ValueError("a work record that ends produced names the entries, and only it")
        if (self.assesses is not None or self.verdict is not None) and self.ending != "assessment":
            raise ValueError("only an assessment names the work it assesses and its verdict")
        if (self.assesses is None) != (self.verdict is None):
            raise ValueError("an assessment that names the work it assesses gives its verdict")
        if self.ending == "obstruction_claimed" and not self.references:
            raise ValueError("a claimed obstruction cites its evidence in references")
        if sum(len((r.note or "").encode()) for r in self.references) > MAX_NOTES:
            raise ValueError(f"the references' notes are at most {MAX_NOTES} bytes in all")
        return self


FlagID = Annotated[str, Field(pattern=r"^flag-[0-9a-f]{24}$")]

FlagCause = Literal[
    "assumption_revised",
    "assumption_withdrawn",
    "dependency_status",
    "evidence_changed",
    "spread",
    "reopen_condition",
]
"""Why an entry is flagged for reconsideration (LG-28); ``reopen_condition``: what a
decision's ``reopen_on`` links name was resolved, or changed its status or statement."""

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
    work: list[WorkInput] | None = Field(default=None, max_length=32)
    """Work records to append (LG-6), after the change's entries are written."""

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if not (
            self.changes
            or self.flags_cleared
            or self.flags_cleared_on
            or self.flags_reopened
            or self.work
        ):
            raise ValueError(
                "a change names entries to change, work to record, or flags to clear or reopen"
            )
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
    target: Literal["entry", "artifact", "block", "review", "quote", "delegation"]
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
    """An entry target's handle, as it reads now; a quote's reference's."""
    locator: dict[str, Any] | None = None
    """For a quote: its source, where it is (the PDF and its page and lines, or the
    ePrint and its date stamp) and its text. For a delegation's outcome: the run that
    checked, the model alias and the models it ran on, and how it ended, as the outcome
    says (the outcome is written once)."""


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


class Standing(Contract):
    """How the research state labels an entry or a work record (CM-11), so that a failed
    attempt never reads as an impossibility: ``attempt`` (it did not succeed, or rests
    on finite evidence only), ``claimed`` or ``disputed`` (an obstruction or negative
    result not settled), ``flagged`` (settled, but an open flag leaves it unresolved),
    or ``impossible`` (settled by the ledger), with its scope and assumptions."""

    label: Literal["attempt", "claimed", "disputed", "flagged", "impossible"]
    text: str
    """The label in words."""
    scope: str | None = None
    """What an impossibility holds within: an obstructed issue's excluded scope, or a
    negative result's evidence scope notes beside its statement."""
    assumes: list[str] = Field(default_factory=list[str])
    """The handles of the assumptions an impossibility rests on."""
    by: str | None = None
    """The decision that settled it, who confirmed a published one, or the entry a
    flag came from."""


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
    kind: str | None = None
    """An issue's kind, a question when it names none, or a claim's or result's (CM-10)."""
    session: str | None = None
    """A decision's session, when it is meant for that session only (CM-10)."""
    standing: Standing | None = None
    """At its current revision, how the research state labels it (CM-11)."""


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
    work: list[str] = Field(default_factory=list[str])
    """The IDs of the work records the change appended, in its order."""


class WorkRecord(Contract):
    """A work record as it is kept (LG-6, LG-12): append-only, on an entry, at the
    revision the entry had when it was recorded."""

    record_id: str
    position: int
    """Its place among the project's work records, oldest first."""
    entry_id: str
    handle: str
    entry_revision: int
    tried: str
    ending: WorkEnding
    limit: WorkLimit | None = None
    produced: list[str] = Field(default_factory=list[str])
    """The handles of the entries it produced."""
    assesses: str | None = None
    verdict: Verdict | None = None
    references: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    """Its references, pinned: an artifact's with its SHA-256, a document's by its ID."""
    part_of: str | None = None
    author: dict[str, Any]
    reason: str
    operation_id: str
    provenance: dict[str, Any] | None = None
    created: str
    standing: Standing | None = None
    """How the research state labels it (CM-11): an attempt that ended ``limit``, or an
    obstruction claimed and not settled, or settled by the decision that obstructs its
    issue."""
    decision: str | None = None
    """For a reopening, the handle of the decision that had made the issue obstructed."""


class WorkPage(Contract):
    """An entry's work records, newest first, with where the next page begins."""

    entry_id: str
    handle: str
    records: list[WorkRecord]
    next_before: int | None = None


SHOWN_CHECKS: Final = 20
"""The most citation checks a quote shows, newest first."""


class CitationCheckView(Contract):
    """A consistency reviewer's check of a quote (CR-4), as the entry view shows it."""

    check_id: str
    review_id: str
    session: str
    """The session of the review that checked it."""
    entry_revision: int
    verdict: Literal["supported", "unsupported", "inconclusive"]
    explanation: str
    created: AwareDatetime
    source: str
    """The review's evidence item the reviewer read: the quote's source."""
    matches: bool
    """Whether it checked the material of the revision shown: its statement, fields,
    assumptions and evidence."""


class QuoteChecks(Contract):
    """A quote of the revision shown, by its evidence ID, with its checks."""

    evidence_id: str
    checks: list[CitationCheckView]
    """Newest first, as many as ``SHOWN_CHECKS``."""
    total: int


class EntryCitations(Contract):
    """The citation checks of each quote a revision of an entry holds (CR-4)."""

    entry_id: str
    handle: str
    revision: int
    quotes: list[QuoteChecks]


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
    confirmed_by: str | None = None
    """For a published result: ``citation check`` when the consistency reviewer found
    every quote of the revision shown supported (CR-4), ``user`` when the user recorded,
    wrote or checked it; none when unverified, and for a project result."""
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
    key: str = ""
    """The conditions as the frontier folds them (as the history keys a group): the
    same whichever result's spelling ``conditions`` shows, and a cursor for groups."""
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
