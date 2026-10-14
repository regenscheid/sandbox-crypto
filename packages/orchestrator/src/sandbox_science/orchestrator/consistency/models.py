# SPDX-License-Identifier: MIT
"""Bounded review configuration, evidence anchors and finding responses."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.project_macros import MacroContext

ReviewType = Literal["checkpoint", "terminal", "confirmation"]
ReviewOutcome = Literal[
    "running", "passed", "findings", "inconclusive", "capped", "failed", "dropped"
]
FindingStatus = Literal[
    "open",
    "claimed fixed",
    "confirmed fixed",
    "unaddressed",
    "superseded",
    "inconclusive",
    "withdrawn",
]
Category = Literal[
    "missing_action",
    "contradicting_value",
    "evidence_strength",
    "reference",
    "artifact_disagreement",
    "method",
    "completion",
]
Severity = Literal["fail", "warn", "note"]
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
    max_turns: int = Field(default=12, ge=1, le=30, strict=True)
    time_seconds: float = Field(default=120, ge=0.01, le=600, allow_inf_nan=False)
    max_input_chars: int = Field(default=150000, ge=4096, le=500000, strict=True)
    max_output_chars: int = Field(default=16000, ge=1024, le=64000, strict=True)
    token_budget: int = Field(default=64000, ge=1024, le=1000000, strict=True)
    calls_per_hour: int = Field(default=60, ge=1, le=1000, strict=True)


class EvidenceAnchor(Contract):
    """A locator within exactly one snapshot item; never an ambient file path."""

    evidence: EvidenceID
    block: Annotated[str, Field(pattern=r"^b[0-9]{1,9}$")] | None = None
    math: Annotated[str, Field(pattern=r"^m[0-9]{1,9}$")] | None = None
    stream: Literal["stdout", "stderr"] | None = None
    first_line: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    last_line: int | None = Field(default=None, ge=1, le=1000000, strict=True)


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
    finding: FindingID
    expected: int = Field(ge=1, le=999999999, strict=True)
    action: Literal["fix", "rebut", "confirm", "dismiss", "reopen"]
    explanation: str = Field(min_length=1, max_length=8192)
    evidence: tuple[EvidenceAnchor, ...] = Field(default=(), max_length=8)
    # New document revisions are validated and pinned before claiming a fix.
    revisions: tuple[tuple[DocumentID, RevisionNumber], ...] = Field(default=(), max_length=16)
    artifact_revisions: tuple[ArtifactRevisionRef, ...] = Field(default=(), max_length=16)
    operation_id: OperationID


class ConfirmationInput(Contract):
    finding: FindingID
    expected: int = Field(ge=1, le=999999999, strict=True)
    decision: Literal["confirm", "reject", "inconclusive", "accept_rebuttal", "reject_rebuttal"]
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
