# SPDX-License-Identifier: MIT
"""Exact retained-lockfile comparisons, never package or permission authority."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .artifacts import Sha256
from .model import Contract
from .provision_observations import Counter, Observation, PageLimit
from .revisions import CondaPackage, LockfileRef, PipDistribution, Version

Name = Annotated[str, StringConstraints(max_length=256)]
HoldReason = Literal[
    "lockfile_unrecorded", "blob_missing", "unsupported_format", "skipped_entries", "input_limit"
]


class Bounded(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LockfileSelection(Bounded):
    """A selected revision's original byte pin and recorded blob reference."""

    revision: Counter
    record_sha256: Sha256
    lockfile: LockfileRef | None


class LockfileHeader(Bounded):
    """Verified source bytes/header; not an independent inventory of the guest."""

    sha256: Sha256
    size: Annotated[int, Field(strict=True, ge=1, le=8 * 1024 * 1024)]
    python: Version | None
    platform: (
        Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z0-9]+(?:-[a-z0-9_]+)?$")]
        | None
    )
    conda_packages: Annotated[int, Field(strict=True, ge=0, le=20_000)]
    pip_distributions: Annotated[int, Field(strict=True, ge=0, le=20_000)]
    skipped: Counter


class LockfileHold(Bounded):
    side: Literal["before", "after"]
    reason: HoldReason


class LicenseObservation(Bounded):
    declared: Annotated[str, StringConstraints(max_length=256)] | None
    agpl: bool | None = Field(strict=True)

    @model_validator(mode="after")
    def _known(self) -> Self:
        if (self.declared is None) != (self.agpl is None):
            raise ValueError("unknown license must retain unknown AGPL status")
        return self


class LockfileDifference(Bounded):
    ecosystem: Literal["conda", "pip"]
    name: Name
    location: Literal["prefix", "user"] | None
    change: Literal["added", "removed", "changed"]
    before: CondaPackage | PipDistribution | None
    after: CondaPackage | PipDistribution | None
    changed_fields: tuple[Name, ...] = Field(max_length=10)
    before_license: LicenseObservation | None
    after_license: LicenseObservation | None

    @model_validator(mode="after")
    def _entry(self) -> Self:
        if (self.ecosystem == "conda") != (self.location is None):
            raise ValueError("package location does not bind its ecosystem")
        for entry, license_info in (
            (self.before, self.before_license),
            (self.after, self.after_license),
        ):
            if (entry is None) != (license_info is None):
                raise ValueError("license side does not bind its package")
            if entry is not None and (
                (self.ecosystem == "conda") != isinstance(entry, CondaPackage)
            ):
                raise ValueError("package does not bind its ecosystem")
            if isinstance(entry, CondaPackage) and (
                entry.name != self.name
                or license_info is None
                or license_info.declared != entry.license
            ):
                raise ValueError("Conda identity or declared license mismatch")
            if isinstance(entry, PipDistribution) and (
                entry.location != self.location
                or re.sub(r"[-_.]+", "-", entry.name).lower() != self.name
                or license_info is None
                or license_info.declared is not None
            ):
                raise ValueError("pip identity or unknown license mismatch")
        if (
            (self.change == "added" and (self.before is not None or self.after is None))
            or (self.change == "removed" and (self.before is None or self.after is not None))
            or (self.change == "changed" and (self.before is None or self.after is None))
        ):
            raise ValueError("difference sides do not bind change")
        if not self.changed_fields or len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("difference needs unique changed fields")
        a = self.before.model_dump() if self.before else {}
        b = self.after.model_dump() if self.after else {}
        if self.changed_fields != tuple(
            sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        ):
            raise ValueError("changed fields do not match selected entries")
        return self


class LockfileDiffPage(Observation):
    before: LockfileSelection
    after: LockfileSelection
    status: Literal["complete", "held"]
    holds: tuple[LockfileHold, ...] = Field(max_length=2)
    before_header: LockfileHeader | None
    after_header: LockfileHeader | None
    header_changes: tuple[Literal["python", "platform"], ...] = Field(max_length=2)
    total: Annotated[int, Field(strict=True, ge=0, le=80_000)] | None
    added: Counter | None
    removed: Counter | None
    changed: Counter | None
    offset: Annotated[int, Field(strict=True, ge=0, le=80_000)]
    limit: PageLimit
    next_offset: Counter | None
    differences: tuple[LockfileDifference, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def _page(self) -> Self:
        for selection, header in (
            (self.before, self.before_header),
            (self.after, self.after_header),
        ):
            if header is not None:
                ref = selection.lockfile
                if ref is None or (
                    header.sha256,
                    header.size,
                    header.conda_packages,
                    header.pip_distributions,
                ) != (ref.sha256, ref.size, ref.conda_packages, ref.pip_distributions):
                    raise ValueError("header does not bind selected input")
        if self.status == "held":
            if len({hold.side for hold in self.holds}) != len(self.holds):
                raise ValueError("held sources need unique sides")
            if (
                not self.holds
                or any(
                    v is not None
                    for v in (self.total, self.added, self.removed, self.changed, self.next_offset)
                )
                or self.differences
                or self.header_changes
            ):
                raise ValueError("held inputs cannot claim a comparison")
        else:
            if self.holds or self.before_header is None or self.after_header is None:
                raise ValueError("complete comparison needs both proved headers")
            for selection, header in (
                (self.before, self.before_header),
                (self.after, self.after_header),
            ):
                ref = selection.lockfile
                if ref is None or header.skipped:
                    raise ValueError("header does not bind complete selected input")
            if self.header_changes != tuple(
                k
                for k in ("python", "platform")
                if getattr(self.before_header, k) != getattr(self.after_header, k)
            ):
                raise ValueError("header changes do not match selected input")
            if (
                self.total is None
                or self.added is None
                or self.removed is None
                or self.changed is None
            ):
                raise ValueError("complete comparison needs totals")
            if self.total != self.added + self.removed + self.changed or self.offset > self.total:
                raise ValueError("comparison totals mismatch")
            end = self.offset + len(self.differences)
            if (
                len(self.differences) > self.limit
                or end > self.total
                or (self.next_offset != (end if end < self.total else None))
                or (self.next_offset is not None and not self.differences)
            ):
                raise ValueError("comparison continuation mismatch")
        return self
