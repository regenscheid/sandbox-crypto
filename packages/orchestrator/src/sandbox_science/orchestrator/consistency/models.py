# SPDX-License-Identifier: MIT
"""Bounded review configuration, evidence anchors and finding responses."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.project_macros import MacroContext

ReviewType = Literal["checkpoint", "terminal", "confirmation"]
REVIEWER_MAX_RESPONSE_BYTES: Final = 6 * 1024 * 1024
"""Finite gateway envelope for a max-effort reviewer response.

Responses APIs may return large encrypted reasoning metadata alongside a small visible
answer. This matches the confined model transport ceiling and is shared by host and
confined reviewer construction so the two runtimes fail at the same boundary.
"""
REVIEWER_MAX_REPLAY_NATIVE_BYTES: Final = 12 * 1024 * 1024
"""Raw native response bytes retained in a multi-round reviewer conversation.

The confined transport base64-encodes these bytes in a 24 MiB frame. Twelve MiB
leaves roughly eight MiB after base64 expansion for the bounded visible context,
tool schemas and request envelope. The runner checks this before paid dispatch.
"""
ReviewOutcome = Literal[
    "running", "passed", "findings", "inconclusive", "capped", "failed", "dropped"
]
FindingStatus = Literal["open", "fixed", "dismissed", "superseded", "stale"]
"""A finding's status (CR-41): ``open`` until settled; ``fixed`` on a re-check's
``resolved``, a math check's pass or the user's word; ``dismissed`` by the main agent or
the user, with a reason; ``superseded`` by a later review's finding, which it links to;
``stale`` when what it reviewed was replaced and no re-check ran in its session."""
FINDING_STATUSES: Final[tuple[FindingStatus, ...]] = (
    "open",
    "fixed",
    "dismissed",
    "superseded",
    "stale",
)
RecheckAnswer = Literal["resolved", "unresolved", "inconclusive"]
"""A re-check's answer (CR-42): the reviewer's ``resolved`` or ``unresolved``, or the
platform's ``inconclusive`` when the re-check could not decide."""
RECHECK_ATTEMPTS: Final = 3
"""The dispatches a queued re-check is tried at before it waits for the user (CR-42)."""
DISMISSAL_CHARACTERS: Final = 2000
"""The longest reason the main agent may give for a dismissal (CR-43)."""
Category = Literal[
    "missing_action",
    "contradicting_value",
    "evidence_strength",
    "reference",
    "artifact_disagreement",
    "method",
    "completion",
    "handle_citation",
    # Cryptographic claims (rubric sandbox-consistency-9), checked from the claim's form.
    "cost_model",
    "preconditions",
    "unqualified_security",
    "notion",
    "parameters",
    "structure_from_naming",
    "quantum_conflation",
]
Severity = Literal["fail", "warn", "note"]
ADVISORY_CATEGORIES: Final = ("handle_citation",)
"""Categories whose findings only suggest (the owner, 2026-10-16; CR-18 as amended). They
are always severity ``note``; they are not counted among the session's unresolved
findings, never reach the author as a notice or an interruption, are never re-checked,
fixed by the author or dismissed by the author, and hold nothing."""
ReviewID = Annotated[str, Field(pattern=r"^rev-[0-9a-f]{24}$")]
EvidenceID = Annotated[str, Field(pattern=r"^ev-[0-9a-f]{24}$")]
FindingID = Annotated[str, Field(pattern=r"^finding-[0-9a-f]{24}$")]
OperationID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
DocumentID = Annotated[str, Field(pattern=r"^doc-[0-9a-f]{24}$")]
RevisionNumber = Annotated[int, Field(ge=1, le=999999999, strict=True)]


class ReviewConfig(Contract):
    """Effective project settings; alias/secrets are supplied by the deployment."""

    enabled: bool = True
    min_interval_seconds: float = Field(default=120, ge=0, le=86400, allow_inf_nan=False)
    text_characters: int = Field(default=2000, ge=1, le=1000000, strict=True)
    artifact_revisions: int = Field(default=3, ge=1, le=100, strict=True)
    kernel_cells: int = Field(default=40, ge=1, le=10000, strict=True)
    kernel_interval_seconds: float = Field(default=1800, ge=0, le=86400, allow_inf_nan=False)
    terminal_hold: bool = True
    interrupt_severity: Severity = "fail"
    response_rounds: int = Field(default=2, ge=0, le=2, strict=True)
    max_reflags: int = Field(default=2, ge=1, le=2, strict=True)
    max_findings: int = Field(default=10, ge=1, le=10, strict=True)
    max_feed_items: int = Field(default=16, ge=1, le=256, strict=True)
    """The most new evidence items in one review window, before derived context."""
    max_feed_bytes: int = Field(default=256 * 1024, ge=16 * 1024, le=8 * 1024 * 1024, strict=True)
    """The approximate new-evidence byte budget for one review window."""
    max_terminal_windows: int = Field(default=24, ge=1, le=64, strict=True)
    """Bounded catch-up of pending evidence when an author turn closes."""
    # The call limits allow a bounded evidence window to finish even when the reviewer
    # reads every item; the SNOVA audit showed that larger windows repeatedly capped.
    max_turns: int = Field(default=24, ge=1, le=30, strict=True)
    time_seconds: float = Field(default=300, ge=0.01, le=600, allow_inf_nan=False)
    max_input_chars: int = Field(default=300000, ge=4096, le=500000, strict=True)
    max_output_chars: int = Field(default=16000, ge=1024, le=64000, strict=True)
    token_budget: int | None = Field(default=None, ge=1024, le=4000000, strict=True)
    """An operator's optional cap on the tokens one review may use, input and output over
    all its calls; ``None``, as by default, for none: the reviewer's calls are accounted
    under its own role, and only the gateway's budget stops their spending (D102)."""
    calls_per_hour: int = Field(default=240, ge=1, le=10000, strict=True)
    """The reviewer calls a project may make in an hour, over all its sessions."""


class EvidenceAnchor(Contract):
    """A locator within exactly one snapshot item; never an ambient file path."""

    evidence: EvidenceID
    block: Annotated[str, Field(pattern=r"^b[0-9]{1,9}$")] | None = None
    math: Annotated[str, Field(pattern=r"^m[0-9]{1,9}$")] | None = None
    stream: Literal["stdout", "stderr"] | None = None
    first_line: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    last_line: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    field: Literal["title", "statement", "status", "fields", "links", "evidence"] | None = None
    """A field of the ledger entry revision the item is (LG-25)."""


class FindingInput(Contract):
    severity: Severity
    category: Category
    statement: str = Field(min_length=1, max_length=8192)
    anchors: tuple[EvidenceAnchor, ...] = Field(min_length=1, max_length=8)
    remedy: str | None = Field(default=None, max_length=4096)
    assessment: Literal["supported", "missing_evidence"] = "supported"
    supersedes: FindingID | None = None
    operation_id: OperationID


class FindingResponse(Contract):
    """A response to a finding (CR-22, CR-41, CR-43). ``fix`` names what addresses it and
    queues its re-check; ``dismiss`` sets it aside with a reason; ``set``, the user's
    alone, gives it any status with a reason, and for ``superseded`` optionally the
    finding that replaces it."""

    finding: FindingID
    expected: int = Field(ge=1, le=999999999, strict=True)
    action: Literal["fix", "dismiss", "set"]
    explanation: str = Field(min_length=1, max_length=8192)
    status: FindingStatus | None = None
    """The status a ``set`` gives the finding."""
    successor: FindingID | None = None
    """For a ``set`` to ``superseded``: the finding that replaces it, if any."""
    messages: tuple[Annotated[str, Field(pattern=r"^msg-[0-9a-f]{16}$")], ...] = Field(
        default=(), max_length=8
    )
    """For a ``fix`` of a finding anchored only to messages: the later archive messages
    that correct it (CR-42)."""
    evidence: tuple[EvidenceAnchor, ...] = Field(default=(), max_length=8)
    # New document revisions are validated and pinned before claiming a fix.
    revisions: tuple[tuple[DocumentID, RevisionNumber], ...] = Field(default=(), max_length=16)
    artifact_revisions: tuple[ArtifactRevisionRef, ...] = Field(default=(), max_length=16)
    entry_revisions: tuple[
        tuple[Annotated[str, Field(min_length=1, max_length=128)], RevisionNumber], ...
    ] = Field(default=(), max_length=16)
    """Changed ledger entry revisions, each an entry by ID or handle and its revision."""
    operation_id: OperationID


class ConfirmationInput(Contract):
    """A reviewer's decision on a finding its snapshot holds: a re-check's answer,
    ``resolved`` or ``unresolved`` with evidence anchors in the new revision or message
    (CR-42), or the acceptance or rejection of the author's dismissal (CR-43)."""

    finding: FindingID
    expected: int = Field(ge=1, le=999999999, strict=True)
    decision: Literal["resolved", "unresolved", "accept_dismissal", "reject_dismissal"]
    explanation: str = Field(min_length=1, max_length=8192)
    anchors: tuple[EvidenceAnchor, ...] = Field(min_length=1, max_length=8)
    operation_id: OperationID


class ReviewerFinish(Contract):
    outcome: Literal["passed", "findings", "inconclusive"]
    summary: str = Field(min_length=1, max_length=8192)
    cap_reached: bool = False


class Scope(Contract):
    project_id: Identifier
    session_id: Identifier


class FindingMacroContexts(Contract):
    """The macro contexts a finding is shown with (``mathchecks.contexts``)."""

    finding: MacroContext
    """The finding's own: its math check's pinned context, or its anchors'."""
    history: MacroContext
    """The one each entry of its history is shown with."""
    responses: dict[str, MacroContext]
    """Each response body's, by its source."""
