# SPDX-License-Identifier: MIT
"""Worker-local observations, distinct from artifact revisions and writer proof."""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .model import Contract, Identifier
from .worker import TERMINAL_STATES, ExecutionState

MAX_REPORT_BYTES = 16 * 1024
MAX_REPORT_ENTRIES = 64
MAX_MARKER_BYTES = 12 * 1024
MAX_PAGE_BYTES = 64 * 1024
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
Short = Annotated[str, Field(max_length=256)]
Flag = Annotated[bool, Field(strict=True)]
Scope = Literal["session", "shared"]


class OutputPath(Contract):
    """An exact relative byte path; presentation strings never determine identity."""

    area: Scope
    path_b64: Annotated[str, Field(min_length=1, max_length=5464)]

    @field_validator("path_b64")
    @classmethod
    def _path(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64 path") from exc
        if (
            base64.b64encode(raw).decode("ascii") != value
            or len(raw) > 4096
            or b"\0" in raw
            or raw.startswith(b"/")
            or any(part in (b"", b".", b"..") for part in raw.split(b"/"))
        ):
            raise ValueError("invalid relative byte path")
        return value

    def raw(self) -> bytes:
        """Return validated source bytes, including names that are not UTF-8."""
        return base64.b64decode(self.path_b64, validate=True)

    def identity(self) -> str:
        """A domain-separated path identity; project/session scope belongs to its binding."""
        raw = self.raw()
        return hashlib.sha256(
            b"output-path-v1\0" + self.area.encode() + b"\0" + len(raw).to_bytes(4) + raw
        ).hexdigest()


class OutputStat(Contract):
    """Untrusted stat metadata, never a content hash or producer attribution."""

    mtime_ns: Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    size: Counter
    inode: Counter


class OutputObservation(OutputPath):
    """A regular stat difference or omission from a particular after report."""

    kind: Literal["regular_stat_difference", "regular_observed", "not_seen_after"]
    before: OutputStat | None = None
    after: OutputStat | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind == "not_seen_after":
            if self.before is None or self.after is not None:
                raise ValueError("not_seen_after requires only a before observation")
        elif self.after is None:
            raise ValueError("regular observations require after metadata")
        elif self.kind == "regular_observed" and self.before is not None:
            raise ValueError("regular_observed has no known before metadata")
        elif self.before == self.after:
            raise ValueError("unchanged metadata is not a stat difference")
        return self


class OutputCoverage(Contract):
    mutation_coverage: Literal["incomplete"] = "incomplete"
    links: Literal["unavailable"] = "unavailable"
    special_files: Literal["unavailable"] = "unavailable"
    deletions: Literal["unproven"] = "unproven"
    before_limited: Flag = False
    after_limited: Flag = False
    before_error: Flag = False
    after_error: Flag = False
    path_omitted: Flag = False
    report_truncated: Flag = False


class OutputReport(Contract):
    """A bounded source report; empty and non-truncated never mean complete mutation coverage."""

    protocol: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    source: Literal["regular_stat_report_v1"] = "regular_stat_report_v1"
    clock: Literal["executor_monotonic"] = "executor_monotonic"
    before_started_ns: Counter | None = None
    before_finished_ns: Counter | None = None
    after_started_ns: Counter | None = None
    after_finished_ns: Counter | None = None
    entries: Annotated[tuple[OutputObservation, ...], Field(max_length=MAX_REPORT_ENTRIES)] = ()
    coverage: OutputCoverage = OutputCoverage()

    @model_validator(mode="after")
    def _bound(self) -> Self:
        for started, finished in (
            (self.before_started_ns, self.before_finished_ns),
            (self.after_started_ns, self.after_finished_ns),
        ):
            if finished is not None and (started is None or finished < started):
                raise ValueError("invalid source observation interval")
        keys = [(entry.area, entry.path_b64) for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate output path")
        if len(self.model_dump_json().encode()) > MAX_REPORT_BYTES:
            raise ValueError("output report exceeds byte limit")
        return self


class OutputScope(Contract):
    project_id: Identifier
    session_id: Identifier
    environment_id: Identifier


class OutputBinding(OutputScope):
    """Accepted request and original dispatch facts; actor is informational."""

    execution_id: Identifier
    attempt_id: Identifier
    operation_id: Identifier
    request_sha256: Digest | None
    kind: Literal["shell", "cell", "job", "pdf"]
    actor: Identifier
    policy_revision: Short | None = None
    dispatched: Flag = False
    incarnation: Annotated[str, Field(max_length=128)] | None = None
    sandbox: Annotated[str, Field(max_length=2048)] | None = None
    sandbox_id: Annotated[str, Field(max_length=2048)] | None = None
    binding_unavailable: Flag = False
    producer: Literal["unknown"] = "unknown"
    observed_inputs: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def _pair(self) -> Self:
        if (self.sandbox is None) != (self.sandbox_id is None):
            raise ValueError("managed identity must be paired")
        if self.incarnation is not None and (
            not self.incarnation.startswith("inc-")
            or len(self.incarnation) != 28
            or any(char not in "0123456789abcdef" for char in self.incarnation[4:])
        ):
            raise ValueError("invalid original incarnation")
        if not self.dispatched and any(
            value is not None for value in (self.incarnation, self.sandbox, self.sandbox_id)
        ):
            raise ValueError("undispatched attempt has no dispatch facts")
        return self


Gap = Literal["missing", "invalid", "oversized", "quota", "evicted", "binding_unavailable"]


class OutputMarker(Contract):
    """Fixed terminal coverage record retained even when detailed payload is absent."""

    binding: OutputBinding
    seq: Counter | None
    state: ExecutionState
    received_at: datetime
    report_sha256: Digest | None = None
    gap: Gap | None = None
    mutation_coverage: Literal["incomplete", "unavailable"]
    potentially_partial: Flag

    @field_validator("received_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observation time must include timezone")
        return value

    @model_validator(mode="after")
    def _terminal(self) -> Self:
        if self.state not in TERMINAL_STATES:
            raise ValueError("observation marker requires a terminal state")
        if self.seq is None and self.gap != "invalid":
            raise ValueError("unsequenced marker requires an invalid-history gap")
        if len(self.model_dump_json().encode()) > MAX_MARKER_BYTES:
            raise ValueError("observation marker exceeds byte limit")
        return self


class OutputRecord(Contract):
    marker: OutputMarker
    report: OutputReport | None


class OutputPage(OutputScope):
    """Internal scoped page; sequence numbers never reveal another scope's activity."""

    stream_id: Digest
    baseline: datetime
    history: Literal["feature_baseline_only"] = "feature_baseline_only"
    target: Counter
    next_after: Counter
    evicted_through: Counter
    records: Annotated[tuple[OutputRecord, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if self.baseline.tzinfo is None or self.next_after > self.target:
            raise ValueError("invalid observation page metadata")
        if len(self.model_dump_json().encode()) > MAX_PAGE_BYTES:
            raise ValueError("observation page exceeds byte limit")
        return self
