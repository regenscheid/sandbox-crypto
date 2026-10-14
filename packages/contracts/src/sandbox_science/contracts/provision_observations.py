# SPDX-License-Identifier: MIT
"""Bounded, read-only CP-26 observations; records are evidence, never permission grants."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .environments import PolicyRevision
from .model import (
    MAX_COUNTER,
    Contract,
    Counter,
    Identifier,
    Sha256,
    StrictContract,
    check_page,
)
from .provisioning import (
    MAX_CHANGES,
    MAX_EXECUTIONS,
    MAX_FILES,
    MAX_NOTES,
    MAX_QUEUED,
    CatalogRef,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    Reason,
    ResolvedFile,
    Stage,
    Text,
)
from .revisions import (
    MAX_COMMANDS,
    MAX_LOCKFILE_BYTES,
    MAX_LOCKFILE_ENTRIES,
    MAX_PROBES,
    Fingerprint,
    LockfileRef,
    ProbeResult,
)
from .worker import ErrorKind

MAX_OBSERVATION_BYTES = 64 * 1024
MAX_OBSERVATION_PAGE = 8
MAX_RETAINED_REVISIONS = 512
Position = Annotated[int, Field(strict=True, ge=-1, le=MAX_COUNTER)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_OBSERVATION_PAGE)]


class Observation(StrictContract):
    """Exact authenticated scope and worker control receipt time, not guest freshness."""

    project_id: Identifier
    environment_id: Identifier
    observed_at: AwareDatetime


class AdmissionSummary(Contract):
    generation: Counter
    mode: Literal["ready", "hold", "needs_recovery"]
    operation: Identifier | None
    revision: Counter | None
    recorded: bool = Field(strict=True)

    @model_validator(mode="after")
    def _gate(self) -> Self:
        if (self.mode == "ready" and self.operation is not None) or (
            self.mode == "needs_recovery" and self.operation is None
        ):
            raise ValueError("admission mode does not bind its operation")
        if not self.recorded and (self.generation, self.mode, self.operation, self.revision) != (
            0,
            "ready",
            None,
            None,
        ):
            raise ValueError("unrecorded admission must describe only the existing default")
        return self


class ProvisionSummary(Contract):
    """No request prose, package file sets, probe outputs or execution records."""

    provision_id: Identifier
    operation_id: Identifier
    session_id: Identifier
    seq: Counter
    state: ProvisionState
    outcome: ProvisionOutcome | None
    reason: Reason | None
    stage: Stage | None
    planned_revision: Counter | None
    revision_before: Counter | None
    revision_after: Counter | None
    permission_request: Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)] | None
    submitted_at: AwareDatetime
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    files_total: Annotated[int, Field(strict=True, ge=0, le=MAX_FILES)]
    changes_total: Annotated[int, Field(strict=True, ge=0, le=MAX_CHANGES)]
    probes_total: Annotated[int, Field(strict=True, ge=0, le=MAX_PROBES)]
    executions_total: Annotated[int, Field(strict=True, ge=0, le=MAX_EXECUTIONS)]
    notes_total: Annotated[int, Field(strict=True, ge=0, le=MAX_NOTES)]


class ProvisionObservation(Observation):
    gate: AdmissionSummary
    current_revision: Counter | None
    queue_total: Annotated[int, Field(strict=True, ge=0, le=MAX_QUEUED)]
    operations: tuple[ProvisionSummary, ...] = Field(max_length=MAX_QUEUED)

    @model_validator(mode="after")
    def _queue(self) -> Self:
        if len(self.operations) != self.queue_total or len(
            {o.provision_id for o in self.operations}
        ) != len(self.operations):
            raise ValueError("queue must expose its complete bounded summary set")
        if any(o.state == "finished" for o in self.operations):
            raise ValueError("queue must be unfinished")
        return self


class ProvisionDetail(Observation):
    summary: ProvisionSummary
    request: ProvisionRequest
    policy: PolicyRevision | None
    catalog: CatalogRef | None
    next_step: Text | None
    error_present: bool = Field(strict=True)
    error_kind: ErrorKind | None

    @model_validator(mode="after")
    def _scope(self) -> Self:
        context = self.request.context
        if (
            context.project_id,
            context.environment_id,
            context.session_id,
            self.request.operation_id,
        ) != (
            self.project_id,
            self.environment_id,
            self.summary.session_id,
            self.summary.operation_id,
        ):
            raise ValueError("operation detail does not bind its observation scope")
        if self.error_present != (self.error_kind is not None):
            raise ValueError("error presence must agree with its recorded kind")
        return self


class ProvisionFilePage(Observation):
    provision_id: Identifier
    seq: Counter
    total: Annotated[int, Field(strict=True, ge=0, le=MAX_FILES)]
    offset: Counter
    limit: PageLimit
    next_offset: Counter | None
    files: tuple[ResolvedFile, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        check_page(self.offset, len(self.files), self.limit, self.total, self.next_offset)
        return self


class RevisionSummary(Contract):
    revision: Counter
    record_sha256: Sha256
    fingerprint: Fingerprint
    managed: bool | None = Field(strict=True)
    changed_by: Identifier | None
    operation: Identifier | None
    recorded_at: AwareDatetime
    lockfile: LockfileRef | None
    commands_total: Annotated[int, Field(strict=True, ge=0, le=MAX_COMMANDS)]
    omitted_commands: Counter
    probes_total: Annotated[int, Field(strict=True, ge=0, le=MAX_PROBES)]

    @field_validator("lockfile")
    @classmethod
    def _lock_bounds(cls, value: LockfileRef | None) -> LockfileRef | None:
        if value is not None and (
            value.size > MAX_LOCKFILE_BYTES
            or value.conda_packages > MAX_LOCKFILE_ENTRIES
            or value.pip_distributions > MAX_LOCKFILE_ENTRIES
        ):
            raise ValueError("lockfile reference exceeds retained bounds")
        return value


class RevisionSummaryPage(Observation):
    retained_total: Annotated[int, Field(strict=True, ge=0, le=MAX_RETAINED_REVISIONS)]
    first_retained: Counter | None
    target: Counter | None
    after: Position
    limit: PageLimit
    next_after: Counter | None
    revisions: tuple[RevisionSummary, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        numbers = [entry.revision for entry in self.revisions]
        if self.retained_total == 0 and (
            self.first_retained is not None
            or self.target is not None
            or self.next_after is not None
            or numbers
        ):
            raise ValueError("empty retained history must remain explicitly empty")
        if self.retained_total and (
            self.first_retained is None
            or self.target is None
            or self.first_retained > self.target
            or len(numbers) > self.retained_total
            or any(n < self.first_retained for n in numbers)
        ):
            raise ValueError("retained history does not bind its visible window")
        if (
            len(numbers) > self.limit
            or numbers != sorted(set(numbers))
            or any(n <= self.after or self.target is None or n > self.target for n in numbers)
        ):
            raise ValueError("revision page does not bind its selected window")
        if self.next_after is not None and (
            not numbers
            or self.next_after != numbers[-1]
            or self.target is None
            or self.next_after >= self.target
        ):
            raise ValueError("revision continuation must name its last actual row")
        return self


class RevisionProbePage(Observation):
    revision: Counter
    record_sha256: Sha256
    total: Annotated[int, Field(strict=True, ge=0, le=MAX_PROBES)]
    offset: Counter
    limit: PageLimit
    next_offset: Counter | None
    probes: tuple[ProbeResult, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        check_page(self.offset, len(self.probes), self.limit, self.total, self.next_offset)
        if any(
            probe.revision is not None and probe.revision != self.revision for probe in self.probes
        ):
            raise ValueError("probe belongs to another revision")
        return self
