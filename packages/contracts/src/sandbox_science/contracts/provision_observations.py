# SPDX-License-Identifier: MIT
"""Bounded, read-only CP-26 observations; records are evidence, never permission grants."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from .artifacts import Sha256
from .environments import PolicyRevision
from .model import Contract, Identifier
from .provisioning import (
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
    MAX_LOCKFILE_BYTES,
    MAX_LOCKFILE_ENTRIES,
    Fingerprint,
    LockfileRef,
    ProbeResult,
)
from .worker import ErrorKind

MAX_OBSERVATION_BYTES = 64 * 1024
MAX_OBSERVATION_PAGE = 8
Counter = Annotated[int, Field(strict=True, ge=0, le=2**62)]
Position = Annotated[int, Field(strict=True, ge=-1, le=2**62)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_OBSERVATION_PAGE)]


class Observation(Contract):
    """Exact authenticated scope and worker control receipt time, not guest freshness."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    project_id: Identifier
    environment_id: Identifier
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observation time needs a timezone")
        return value


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
    permission_request: Annotated[int, Field(strict=True, ge=1, le=2**62)] | None
    submitted_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    files_total: Annotated[int, Field(strict=True, ge=0, le=2000)]
    changes_total: Annotated[int, Field(strict=True, ge=0, le=4000)]
    probes_total: Annotated[int, Field(strict=True, ge=0, le=64)]
    executions_total: Annotated[int, Field(strict=True, ge=0, le=128)]
    notes_total: Annotated[int, Field(strict=True, ge=0, le=32)]

    @field_validator("submitted_at", "started_at", "ended_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("operation time needs a timezone")
        return value


class ProvisionObservation(Observation):
    gate: AdmissionSummary
    current_revision: Counter | None
    queue_total: Annotated[int, Field(strict=True, ge=0, le=32)]
    operations: tuple[ProvisionSummary, ...] = Field(max_length=32)

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
    total: Annotated[int, Field(strict=True, ge=0, le=2000)]
    offset: Counter
    limit: PageLimit
    next_offset: Counter | None
    files: tuple[ResolvedFile, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        page_end = self.offset + len(self.files)
        if (
            self.offset > self.total
            or len(self.files) > self.limit
            or page_end > self.total
            or self.next_offset != (page_end if page_end < self.total else None)
        ):
            raise ValueError("file page does not bind its exact range")
        if page_end < self.total and not self.files:
            raise ValueError("nonterminal page must make progress")
        return self


class RevisionSummary(Contract):
    revision: Counter
    record_sha256: Sha256
    fingerprint: Fingerprint
    managed: bool | None = Field(strict=True)
    changed_by: Identifier | None
    operation: Identifier | None
    recorded_at: datetime
    lockfile: LockfileRef | None
    commands_total: Annotated[int, Field(strict=True, ge=0, le=8)]
    omitted_commands: Counter
    probes_total: Annotated[int, Field(strict=True, ge=0, le=64)]

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

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("revision time needs a timezone")
        return value


class RevisionSummaryPage(Observation):
    retained_total: Annotated[int, Field(strict=True, ge=0, le=512)]
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
    total: Annotated[int, Field(strict=True, ge=0, le=64)]
    offset: Counter
    limit: PageLimit
    next_offset: Counter | None
    probes: tuple[ProbeResult, ...] = Field(max_length=MAX_OBSERVATION_PAGE)

    @model_validator(mode="after")
    def _page(self) -> Self:
        page_end = self.offset + len(self.probes)
        if (
            self.offset > self.total
            or len(self.probes) > self.limit
            or page_end > self.total
            or self.next_offset != (page_end if page_end < self.total else None)
        ):
            raise ValueError("probe page does not bind its exact range")
        if page_end < self.total and not self.probes:
            raise ValueError("nonterminal page must make progress")
        if any(
            probe.revision is not None and probe.revision != self.revision for probe in self.probes
        ):
            raise ValueError("probe belongs to another revision")
        return self
